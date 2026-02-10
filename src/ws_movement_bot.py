"""
WebSocket-Based Movement Trading Bot

Real-time order book monitoring using Polymarket's WebSocket API.
Detection latency: ~50-100ms vs 750ms with polling.

Strategy:
- Subscribe to order book updates for all market outcomes
- Detect z-score spikes in real-time as orders flow in
- Scale into position as conviction grows
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, time, date
from typing import Optional, Dict
import pytz
import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import load_settings, print_config, Settings
from .polymarket import PolymarketClient, Market
from .movement_detector import MovementDetector, MovementSignal, parse_scale_in_pcts

logger = logging.getLogger(__name__)
ET_TIMEZONE = pytz.timezone("America/New_York")

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10  # seconds
RECONNECT_DELAY = 1  # initial delay, will exponential backoff
MAX_RECONNECT_DELAY = 60


class WebSocketMovementBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.polymarket: Optional[PolymarketClient] = None
        self.detector: Optional[MovementDetector] = None
        self._running = False
        self._current_market: Optional[Market] = None
        self._budget_remaining = settings.max_trade_size_usd
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._token_id_to_outcome: Dict[str, str] = {}  # token_id -> outcome_name
        self._update_count = 0
        self._last_signal_time: Optional[datetime] = None

    async def initialize(self):
        logger.info("Initializing WebSocket Movement Trading Bot...")
        polymarket_config = self.settings.get_polymarket_config()
        if polymarket_config.private_key:
            self.polymarket = PolymarketClient(polymarket_config)
            try:
                self.polymarket.connect()
                logger.info("Connected to Polymarket API")
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
        logger.info(f"Detector configured: z-threshold={self.settings.zscore_threshold}, scale={scale_pcts}")

    def _in_monitor_window(self) -> bool:
        now_et = datetime.now(ET_TIMEZONE)
        is_weekday = now_et.weekday() < 5
        start = time(self.settings.monitor_window_start_hour, 0)
        end = time(self.settings.monitor_window_end_hour, 0)
        in_window = start <= now_et.time() <= end
        return is_weekday and in_window

    async def _discover_market(self) -> Optional[Market]:
        if not self.polymarket:
            return None
        slug = self.settings.target_market_slug
        if not slug:
            slug = self.polymarket.discover_tsa_market(date.today())
            if not slug:
                logger.error("Could not auto-discover market")
                return None
            logger.info(f"Auto-discovered: {slug}")
        return self.polymarket.get_market_with_books(slug)

    def _execute_signal(self, signal: MovementSignal):
        latency_ms = None
        if self._last_signal_time:
            latency_ms = (datetime.now() - self._last_signal_time).total_seconds() * 1000
        self._last_signal_time = datetime.now()

        if not self.polymarket:
            logger.info(f"[DRY] Would buy {signal.outcome_name} @ ${signal.current_price:.4f}" +
                       (f" (latency: {latency_ms:.0f}ms)" if latency_ms else ""))
            return

        alloc_pct = signal.budget_pct / 100
        amount = min(self._budget_remaining * alloc_pct, self._budget_remaining)
        if amount < 1.0:
            logger.info("Budget exhausted")
            return

        logger.info(f"EXECUTE: BUY ${amount:.2f} of {signal.outcome_name} @ ${signal.current_price:.4f}")
        result = self.polymarket.buy_market_order(
            token_id=signal.token_id,
            amount_usd=amount,
            dry_run=self.settings.dry_run
        )
        if result.success:
            self._budget_remaining -= amount
            logger.info(f"SUCCESS: {result.order_id} - budget remaining: ${self._budget_remaining:.2f}")
        else:
            logger.error(f"FAILED: {result.error}")

    def _process_book_update(self, data: dict):
        """Process a WebSocket order book update."""
        asset_id = data.get("asset_id")
        if not asset_id or asset_id not in self._token_id_to_outcome:
            return

        outcome_name = self._token_id_to_outcome[asset_id]
        asks = data.get("asks", [])

        if not asks:
            return

        # Get best ask price
        try:
            best_ask = float(asks[0]["price"])
        except (IndexError, KeyError, ValueError):
            return

        self._update_count += 1

        # Update detector with new price
        if outcome_name in self.detector.outcomes:
            state = self.detector.outcomes[outcome_name]
            state.update_price(best_ask, datetime.now())

            # Check for trigger
            signal = self.detector._check_trigger(state)
            if signal:
                self.detector.total_signals += 1
                self._execute_signal(signal)

        # Log periodic status
        if self._update_count % 100 == 0:
            logger.debug(f"Processed {self._update_count} order book updates")

    async def _ping_loop(self):
        """Send periodic pings to keep connection alive."""
        while self._running and self._ws:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws:
                    await self._ws.ping()
            except Exception as e:
                logger.debug(f"Ping failed: {e}")
                break

    async def _subscribe_to_market(self, ws, market: Market):
        """Subscribe to order book updates for all market outcomes."""
        token_ids = []
        self._token_id_to_outcome.clear()

        for outcome in market.outcomes:
            token_ids.append(outcome.token_id)
            self._token_id_to_outcome[outcome.token_id] = outcome.outcome
            logger.info(f"  Subscribing: {outcome.outcome} ({outcome.token_id[:16]}...)")

        subscription = {
            "type": "market",
            "assets_ids": token_ids
        }
        await ws.send(json.dumps(subscription))
        logger.info(f"Subscribed to {len(token_ids)} token order books")

    async def _run_websocket(self):
        """Main WebSocket connection and message loop."""
        reconnect_delay = RECONNECT_DELAY

        while self._running:
            try:
                logger.info(f"Connecting to WebSocket: {WS_MARKET_URL}")
                async with ws_connect(
                    WS_MARKET_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    reconnect_delay = RECONNECT_DELAY  # Reset on successful connect
                    logger.info("WebSocket connected!")

                    # Subscribe to market
                    if self._current_market:
                        await self._subscribe_to_market(ws, self._current_market)

                    # Start ping loop
                    ping_task = asyncio.create_task(self._ping_loop())

                    try:
                        async for message in ws:
                            if not self._running:
                                break

                            try:
                                data = json.loads(message)

                                # Handle both single objects and arrays of updates
                                updates = data if isinstance(data, list) else [data]

                                for update in updates:
                                    if not isinstance(update, dict):
                                        continue
                                    event_type = update.get("event_type")

                                    if event_type == "book":
                                        self._process_book_update(update)
                                    elif event_type == "price_change":
                                        # Could also use these for faster detection
                                        pass

                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON: {message[:100]}")
                            except Exception as e:
                                logger.error(f"Error processing message: {e}", exc_info=True)

                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
            except WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)

            self._ws = None

            if self._running:
                logger.info(f"Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def run(self):
        self._running = True
        logger.info("Starting WebSocket Movement Bot")
        logger.info(f"  Monitor window: {self.settings.monitor_window_start_hour}:00 - {self.settings.monitor_window_end_hour}:00 ET")
        logger.info(f"  Z-score threshold: {self.settings.zscore_threshold}")
        logger.info(f"  Budget: ${self.settings.max_trade_size_usd}")
        logger.info(f"  Dry run: {self.settings.dry_run}")
        logger.info(f"  Mode: WEBSOCKET (real-time)")

        was_in_window = False

        while self._running:
            try:
                in_window = self._in_monitor_window()

                # Window just started
                if in_window and not was_in_window:
                    logger.info("=== MONITOR WINDOW STARTED ===")
                    self._budget_remaining = self.settings.max_trade_size_usd
                    self.detector.reset()
                    self._update_count = 0

                    self._current_market = await self._discover_market()
                    if self._current_market:
                        logger.info(f"Market: {self._current_market.question}")
                        self.detector.set_baseline(self._current_market.outcomes)

                        # Run WebSocket until window ends
                        ws_task = asyncio.create_task(self._run_websocket())

                        # Wait for window to end
                        while self._running and self._in_monitor_window():
                            await asyncio.sleep(1)

                        # Stop WebSocket
                        if self._ws:
                            await self._ws.close()
                        ws_task.cancel()
                        try:
                            await ws_task
                        except asyncio.CancelledError:
                            pass

                # Window just ended
                if not in_window and was_in_window:
                    logger.info("=== MONITOR WINDOW ENDED ===")
                    status = self.detector.get_status()
                    logger.info(f"Session summary: {status['total_signals']} signals, "
                               f"{status['budget_spent_pct']}% budget used, "
                               f"{self._update_count} WS updates processed")

                was_in_window = in_window

                # Outside window, just wait
                if not in_window:
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    def stop(self):
        logger.info("Stopping bot...")
        self._running = False


def setup_logging(level: str = "INFO"):
    """Configure logging for the bot."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def main():
    """Main entry point for the WebSocket movement bot."""
    settings = load_settings()
    setup_logging(settings.log_level)

    logger.info("=" * 60)
    logger.info("  WebSocket Movement-Based Trading Bot")
    logger.info("=" * 60)
    print_config(settings)

    bot = WebSocketMovementBot(settings)

    # Signal handlers (Unix only)
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
