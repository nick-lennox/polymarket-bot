"""
WebSocket-Based Movement Trading Bot

Real-time order book monitoring using Polymarket's WebSocket API.
Detection latency: ~50-100ms vs 750ms with polling.

Strategy:
- Subscribe to order book updates for all market outcomes
- Detect z-score spikes in real-time as orders flow in
- Scale into position as conviction grows
- On Monday, trade Fri + Sat + Sun markets with shared budget
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, time, date, timedelta
from typing import Optional, Dict

import pytz
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import load_settings, print_config, Settings
from .polymarket import PolymarketClient, Market
from .movement_detector import MovementDetector, MovementSignal, parse_scale_in_pcts

logger = logging.getLogger(__name__)

ET_TIMEZONE = pytz.timezone("America/New_York")
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10
RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60


class WebSocketMovementBot:
    """Real-time movement bot using Polymarket WebSocket feed."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.polymarket: Optional[PolymarketClient] = None
        self.detector: Optional[MovementDetector] = None
        self._running = False
        self._current_markets: list[Market] = []
        self._market_detectors: Dict[str, MovementDetector] = {}
        self._budget_remaining = settings.max_trade_size_usd
        self._ws = None
        self._ws_should_run = False
        self._token_id_to_outcome: Dict[str, str] = {}
        self._token_id_to_slug: Dict[str, str] = {}
        self._update_count = 0
        self._dropped_count = 0
        self._last_signal_time: Optional[datetime] = None

    async def initialize(self):
        """Connect to Polymarket and create reference detector."""
        logger.info("Initializing WebSocket Movement Trading Bot...")
        polymarket_config = self.settings.get_polymarket_config()

        if polymarket_config.private_key:
            self.polymarket = PolymarketClient(polymarket_config)
            try:
                self.polymarket.connect()
                logger.info("Connected to Polymarket")
            except Exception as e:
                logger.error(f"Failed to connect: {e}")
                self.polymarket = None
        else:
            logger.warning("No credentials - monitor mode only")

        scale_pcts = parse_scale_in_pcts(self.settings.scale_in_pcts)
        self.detector = MovementDetector(
            zscore_threshold=self.settings.zscore_threshold,
            scale_in_pcts=scale_pcts,
            max_buy_price=self.settings.max_buy_price,
            min_price_change=self.settings.min_price_change,
        )
        logger.info(
            f"Detector configured: z-threshold={self.settings.zscore_threshold}, "
            f"scale={scale_pcts}"
        )

    def _in_monitor_window(self) -> bool:
        """Check if current time is within the ET monitoring window."""
        now_et = datetime.now(ET_TIMEZONE)
        is_weekday = now_et.weekday() < 5
        start = time(self.settings.monitor_window_start_hour, 0)
        end = time(self.settings.monitor_window_end_hour, 0)
        in_window = start <= now_et.time() <= end
        return is_weekday and in_window

    def _discover_tsa_slugs(self, target_date: date) -> list[str]:
        """Build TSA market slugs for the given date.

        On Monday (weekday 0), returns 3 slugs for Fri + Sat + Sun
        because weekend data is released on Monday morning.
        On other weekdays, returns only the previous day slug.
        """
        slugs: list[str] = []
        weekday = target_date.weekday()

        if weekday == 0:
            # Monday: include Friday, Saturday, Sunday
            for days_back in [3, 2, 1]:
                d = target_date - timedelta(days=days_back)
                month_name = d.strftime("%B").lower()
                slugs.append(f"number-of-tsa-passengers-{month_name}-{d.day}")
        else:
            # Tuesday-Friday: previous day only
            d = target_date - timedelta(days=1)
            month_name = d.strftime("%B").lower()
            slugs.append(f"number-of-tsa-passengers-{month_name}-{d.day}")

        return slugs

    async def _discover_markets(self) -> list[Market]:
        """Discover and fetch markets with order books.

        If target_market_slug is set, use it directly.
        Otherwise, auto-discover TSA markets (multiple on Monday).
        """
        if not self.polymarket:
            return []

        slug = self.settings.target_market_slug
        if slug:
            market = self.polymarket.get_market_with_books(slug)
            if market:
                market.event_slug = slug
                return [market]
            logger.error(f"Could not fetch target market: {slug}")
            return []

        # Auto-discover based on date
        today = date.today()
        slugs = self._discover_tsa_slugs(today)
        logger.info(f"Auto-discovered {len(slugs)} market slug(s): {slugs}")

        markets: list[Market] = []
        for s in slugs:
            # Parse date from slug to verify via discover_tsa_market
            date_part = s.replace("number-of-tsa-passengers-", "")
            try:
                parsed = datetime.strptime(date_part, "%B-%d")
                check_date = parsed.replace(year=today.year).date()
            except ValueError:
                logger.warning(f"Could not parse date from slug: {s}")
                continue

            verified = self.polymarket.discover_tsa_market(check_date)
            if not verified:
                logger.warning(f"Market not found for slug: {s}")
                continue

            market = self.polymarket.get_market_with_books(verified)
            if market:
                market.event_slug = verified
                markets.append(market)
                logger.info(
                    f"Market: {market.question} ({len(market.outcomes)} outcomes)"
                )

        if not markets:
            logger.error("Could not discover any TSA markets")

        return markets

    def _execute_signal(self, sig: MovementSignal):
        """Execute a trading signal (or log in dry-run mode)."""
        latency_ms = (
            (datetime.now() - sig.timestamp).total_seconds() * 1000
            if sig.timestamp
            else 0.0
        )
        logger.info(
            f"SIGNAL: {sig.outcome_name} z={sig.zscore:.2f} "
            f"price={sig.baseline_price:.4f}->{sig.current_price:.4f} "
            f"(+{sig.price_change:.4f}, +{sig.price_change_pct:.1f}%) "
            f"trigger #{sig.trigger_number} -> {sig.budget_pct}% budget "
            f"[latency={latency_ms:.0f}ms]"
        )

        if self.settings.dry_run or not self.polymarket:
            logger.info(
                f"[DRY] Would buy {sig.outcome_name} @ ${sig.current_price:.4f}"
            )
            return

        alloc_pct = sig.budget_pct / 100
        amount = min(self._budget_remaining * alloc_pct, self._budget_remaining)
        if amount < 1.0:
            logger.info("Budget exhausted - skipping trade")
            return

        logger.info(
            f"EXECUTE: BUY ${amount:.2f} of {sig.outcome_name} "
            f"@ ${sig.current_price:.4f}"
        )
        result = self.polymarket.buy_market_order(
            token_id=sig.token_id,
            amount_usd=amount,
            dry_run=self.settings.dry_run,
        )
        if result.success:
            self._budget_remaining -= amount
            self._last_signal_time = datetime.now(ET_TIMEZONE)
            logger.info(
                f"SUCCESS: {result.order_id} - "
                f"budget remaining: ${self._budget_remaining:.2f}"
            )
        else:
            logger.error(f"FAILED: {result.error}")

    def _process_message(self, data: dict):
        """Process a WebSocket message (book or price_change event)."""
        event_type = data.get("event_type", "")

        if event_type == "book":
            self._process_book_event(data)
        elif event_type == "price_change":
            self._process_price_change(data)
        elif event_type == "last_trade_price":
            pass  # Not used for detection
        else:
            self._dropped_count += 1
            if self._dropped_count <= 5:
                logger.debug(
                    f"Unknown event_type={event_type!r}, "
                    f"keys={list(data.keys())[:8]}"
                )

    def _process_book_event(self, data: dict):
        """Process a full order book snapshot (event_type=book)."""
        asset_id = data.get("asset_id") or data.get("market")
        if not asset_id:
            return

        outcome_name = self._token_id_to_outcome.get(asset_id)
        slug = self._token_id_to_slug.get(asset_id)
        if not outcome_name or not slug:
            return

        asks = data.get("asks", [])
        if not asks:
            return

        try:
            first_ask = asks[0]
            if isinstance(first_ask, dict):
                best_ask = float(first_ask.get("price", first_ask.get("p", 0)))
            else:
                best_ask = float(first_ask)
        except (IndexError, TypeError, ValueError):
            return

        if best_ask <= 0:
            return

        self._update_count += 1
        self._feed_price_to_detector(asset_id, outcome_name, slug, best_ask)

    def _process_price_change(self, data: dict):
        """Process an incremental price change (event_type=price_change)."""
        # price_change can have asset_id at top level with price+side
        asset_id = data.get("asset_id") or data.get("market")
        side = data.get("side", "")
        price_str = data.get("price")

        if asset_id and price_str and side.upper() == "ASK":
            outcome_name = self._token_id_to_outcome.get(asset_id)
            slug = self._token_id_to_slug.get(asset_id)
            if outcome_name and slug:
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    return
                if price > 0:
                    self._update_count += 1
                    self._feed_price_to_detector(
                        asset_id, outcome_name, slug, price
                    )
                    return

        # Also handle nested price_changes array format
        changes = data.get("price_changes", [])
        for change in changes:
            if not isinstance(change, dict):
                continue
            aid = change.get("asset_id", "")
            best_ask_str = change.get("best_ask")
            if not aid or not best_ask_str:
                continue
            outcome_name = self._token_id_to_outcome.get(aid)
            slug = self._token_id_to_slug.get(aid)
            if not outcome_name or not slug:
                continue
            try:
                best_ask = float(best_ask_str)
            except (ValueError, TypeError):
                continue
            if best_ask > 0:
                self._update_count += 1
                self._feed_price_to_detector(aid, outcome_name, slug, best_ask)

    def _feed_price_to_detector(
        self, asset_id: str, outcome_name: str, slug: str, price: float
    ):
        """Feed a price update to the appropriate market detector."""
        detector = self._market_detectors.get(slug)
        if not detector:
            return

        state = detector.outcomes.get(outcome_name)
        if not state:
            return

        state.update_price(price, datetime.now())
        sig = detector._check_trigger(state)
        if sig:
            detector.total_signals += 1
            self._execute_signal(sig)

    async def _ping_loop(self):
        """Send periodic pings to keep the WebSocket alive."""
        while self._ws_should_run and self._ws:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._ws_should_run:
                    pong = await self._ws.ping()
                    await asyncio.wait_for(pong, timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                logger.warning("Ping pong timeout - connection may be stale")
                break
            except Exception as e:
                logger.debug(f"Ping loop error: {e}")
                break

    async def _subscribe_to_markets(self, ws, markets: list[Market]):
        """Subscribe to order book updates for all market outcomes."""
        self._token_id_to_outcome.clear()
        self._token_id_to_slug.clear()
        all_token_ids: list[str] = []

        for market in markets:
            slug = getattr(market, "event_slug", market.condition_id)
            for outcome in market.outcomes:
                if outcome.token_id:
                    self._token_id_to_outcome[outcome.token_id] = outcome.outcome
                    self._token_id_to_slug[outcome.token_id] = slug
                    all_token_ids.append(outcome.token_id)

        if not all_token_ids:
            logger.warning("No token IDs to subscribe to")
            return

        subscription = {
            "auth": {},
            "type": "market",
            "assets_ids": all_token_ids,
        }
        await ws.send(json.dumps(subscription))
        logger.info(
            f"Subscribed to {len(all_token_ids)} token(s) across "
            f"{len(markets)} market(s)"
        )
        # Log token mappings for diagnostics
        for tid, name in self._token_id_to_outcome.items():
            slug = self._token_id_to_slug.get(tid, "?")
            logger.debug(f"  Token {tid[:20]}... -> {name} [{slug}]")

    async def _run_websocket(self):
        """Main WebSocket connection loop with reconnection logic."""
        delay = RECONNECT_DELAY

        while self._ws_should_run:
            try:
                logger.info(f"Connecting to WebSocket: {WS_MARKET_URL}")
                async with ws_connect(
                    WS_MARKET_URL,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    delay = RECONNECT_DELAY
                    logger.info("WebSocket connected")

                    await self._subscribe_to_markets(ws, self._current_markets)

                    ping_task = asyncio.create_task(self._ping_loop())
                    try:
                        async for raw_message in ws:
                            if not self._ws_should_run:
                                break
                            try:
                                message = json.loads(raw_message)
                            except json.JSONDecodeError:
                                continue

                            # Log first few messages for diagnostics
                            if self._update_count == 0 and self._dropped_count < 3:
                                raw_preview = str(raw_message)[:200]
                                logger.info(f"WS msg sample: {raw_preview}")

                            if isinstance(message, list):
                                for item in message:
                                    if isinstance(item, dict):
                                        self._process_message(item)
                            elif isinstance(message, dict):
                                self._process_message(message)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled")
                break
            except Exception as e:
                logger.error(f"Unexpected WebSocket error: {e}", exc_info=True)
            finally:
                self._ws = None

            if self._ws_should_run:
                logger.info(f"Reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def run(self):
        """Main bot loop: discover markets, connect WS, monitor movements."""
        self._running = True
        logger.info("Starting WebSocket Movement Bot")
        logger.info(
            f"  Monitor window: {self.settings.monitor_window_start_hour}:00 - "
            f"{self.settings.monitor_window_end_hour}:00 ET"
        )
        logger.info(f"  Z-score threshold: {self.settings.zscore_threshold}")
        logger.info(f"  Budget: ${self.settings.max_trade_size_usd}")
        logger.info(f"  Dry run: {self.settings.dry_run}")

        was_in_window = False
        ws_task: Optional[asyncio.Task] = None

        while self._running:
            try:
                in_window = self._in_monitor_window()

                # === Window just started ===
                if in_window and not was_in_window:
                    logger.info("=== MONITOR WINDOW STARTED ===")
                    self._budget_remaining = self.settings.max_trade_size_usd
                    self._update_count = 0
                    self._dropped_count = 0
                    self._last_signal_time = None
                    self._market_detectors.clear()

                    self._current_markets = await self._discover_markets()
                    if self._current_markets:
                        scale_pcts = parse_scale_in_pcts(
                            self.settings.scale_in_pcts
                        )
                        for market in self._current_markets:
                            slug = getattr(
                                market, "event_slug", market.condition_id
                            )
                            det = MovementDetector(
                                zscore_threshold=self.settings.zscore_threshold,
                                scale_in_pcts=scale_pcts,
                                max_buy_price=self.settings.max_buy_price,
                                min_price_change=self.settings.min_price_change,
                            )
                            det.set_baseline(market.outcomes)
                            self._market_detectors[slug] = det
                            logger.info(
                                f"Detector for [{slug}]: "
                                f"{len(market.outcomes)} outcomes baselined"
                            )

                        # Launch WebSocket
                        self._ws_should_run = True
                        ws_task = asyncio.create_task(self._run_websocket())
                        logger.info("WebSocket feed started")

                # === Window just ended ===
                if not in_window and was_in_window:
                    logger.info("=== MONITOR WINDOW ENDED ===")
                    self._ws_should_run = False
                    if self._ws:
                        await self._ws.close()
                    if ws_task and not ws_task.done():
                        ws_task.cancel()
                        try:
                            await ws_task
                        except asyncio.CancelledError:
                            pass
                    ws_task = None
                    self._ws = None

                    total_signals = sum(
                        d.total_signals
                        for d in self._market_detectors.values()
                    )
                    budget_used = (
                        self.settings.max_trade_size_usd
                        - self._budget_remaining
                    )
                    budget_pct = (
                        budget_used / self.settings.max_trade_size_usd * 100
                        if self.settings.max_trade_size_usd > 0
                        else 0.0
                    )
                    logger.info(
                        f"Session summary: {total_signals} signals, "
                        f"{budget_pct:.1f}% budget used, "
                        f"{self._update_count} WS updates processed, "
                        f"{self._dropped_count} unknown events dropped"
                    )
                    self._current_markets.clear()
                    self._market_detectors.clear()

                was_in_window = in_window

                # Budget exhaustion check
                if in_window and self._budget_remaining < 1.0:
                    if self._ws_should_run:
                        logger.info(
                            "Budget exhausted - stopping WebSocket early"
                        )
                        self._ws_should_run = False
                        if self._ws:
                            await self._ws.close()
                        if ws_task and not ws_task.done():
                            ws_task.cancel()
                            try:
                                await ws_task
                            except asyncio.CancelledError:
                                pass
                            ws_task = None

                # Sleep between checks
                if in_window:
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    def stop(self):
        """Signal the bot to stop gracefully."""
        logger.info("Stopping bot...")
        self._running = False
        self._ws_should_run = False


class ETFormatter(logging.Formatter):
    """Log formatter that displays timestamps in Eastern Time."""

    def formatTime(self, record, datefmt=None):
        utc_dt = datetime.fromtimestamp(record.created, tz=pytz.utc)
        et_dt = utc_dt.astimezone(ET_TIMEZONE)
        if datefmt:
            return et_dt.strftime(datefmt)
        return et_dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(level: str = "INFO"):
    """Configure logging with ET timestamps."""
    formatter = ETFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Quiet noisy libraries
    for lib in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def main():
    """Main entry point for the WebSocket movement bot."""
    settings = load_settings()
    setup_logging(settings.log_level)

    logger.info("=" * 60)
    logger.info("  WebSocket Movement-Based Trading Bot")
    logger.info("=" * 60)
    print_config(settings)

    bot = WebSocketMovementBot(settings)

    # Signal handlers (Unix only - works in Docker/Linux)
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
    except NotImplementedError:
        pass

    await bot.initialize()
    await bot.run()


def run():
    """Synchronous wrapper for main()."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(0)


if __name__ == "__main__":
    run()
