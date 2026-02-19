"""
Movement-Based Trading Bot

Monitors order book movements and trades when significant
price changes are detected (z-score based).

Strategy:
- During monitor window (7-10 AM ET), poll order books every 1-2s
- At window start, snapshot baseline prices
- Detect z-score spikes (smart money moving)
- Scale into position as conviction grows
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, date
from typing import Optional
import pytz

from .config import load_settings, print_config, Settings
from .polymarket import PolymarketClient, Market
from .movement_detector import MovementDetector, MovementSignal, parse_scale_in_pcts

logger = logging.getLogger(__name__)
ET_TIMEZONE = pytz.timezone("America/New_York")


class MovementBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.polymarket: Optional[PolymarketClient] = None
        self.detector: Optional[MovementDetector] = None
        self._running = False
        self._current_market: Optional[Market] = None
        self._budget_remaining = settings.max_trade_size_usd
        
    async def initialize(self):
        logger.info("Initializing Movement Trading Bot...")
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
        logger.info(f"Detector configured: z-threshold={self.settings.zscore_threshold}, scale={scale_pcts}")

    def _in_monitor_window(self) -> bool:
        now_et = datetime.now(ET_TIMEZONE)
        is_weekday = now_et.weekday() < 5
        start = time(self.settings.monitor_window_start_hour, 0)
        end = time(self.settings.monitor_window_end_hour, 0)
        in_window = start <= now_et.time() <= end
        return is_weekday and in_window
    
    def _get_poll_interval(self) -> float:
        if self._in_monitor_window():
            return 1.5
        return self.settings.poll_interval_seconds
    
    async def _discover_markets(self) -> list[Market]:
        """Discover all markets to trade today. Returns list of Markets."""
        if not self.polymarket:
            return []

        # If explicit slug set, use that
        if self.settings.target_market_slug:
            market = self.polymarket.get_market_with_books(self.settings.target_market_slug)
            return [market] if market else []

        # Auto-discover (handles Monday = 3 markets, etc.)
        slugs = self.polymarket.discover_tsa_markets()
        markets = []
        for slug in slugs:
            market = self.polymarket.get_market_with_books(slug)
            if market:
                markets.append(market)
                logger.info(f"Loaded market: {market.question} (slug: {slug})")

        return markets
    
    async def _refresh_order_books_for_market(self, market: Market) -> Optional[Market]:
        """Refresh order books for a specific market."""
        if not self.polymarket:
            return None
        try:
            valid_books = 0
            for outcome in market.outcomes:
                ob = self.polymarket.get_order_book(outcome.token_id)
                outcome.order_book = ob
                if ob and (ob.asks or ob.bids):
                    valid_books += 1

            if valid_books == 0:
                logger.debug(f"No valid order books for {market.event_slug} - may be resolved")
                return None

            return market
        except Exception as e:
            logger.error(f"Failed to refresh order books: {e}")
            return None
    
    def _execute_signal(self, signal: MovementSignal):
        if not self.polymarket:
            logger.info(f"[DRY] Would BUY YES {signal.outcome_name} @ ${signal.current_price:.4f}")
            return
        
        alloc_pct = signal.budget_pct / 100
        amount = min(self._budget_remaining * alloc_pct, self._budget_remaining)
        if amount < 1.0:
            logger.info("Budget exhausted")
            return
        
        logger.info(f"EXECUTE: BUY YES ${amount:.2f} on {signal.outcome_name} @ ${signal.current_price:.4f}")
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

    async def run(self):
        self._running = True
        logger.info("Starting Movement Bot")
        logger.info(f"  Monitor window: {self.settings.monitor_window_start_hour}:00 - {self.settings.monitor_window_end_hour}:00 ET")
        logger.info(f"  Z-score threshold: {self.settings.zscore_threshold}")
        logger.info(f"  Budget: ${self.settings.max_trade_size_usd}")
        logger.info(f"  Dry run: {self.settings.dry_run}")

        was_in_window = False
        last_interval = None
        self._current_markets: list[Market] = []
        self._market_detectors: dict[str, MovementDetector] = {}  # slug -> detector

        while self._running:
            try:
                in_window = self._in_monitor_window()

                # Window just started
                if in_window and not was_in_window:
                    logger.info("=== MONITOR WINDOW STARTED ===")
                    self._budget_remaining = self.settings.max_trade_size_usd

                    # Discover all markets for today (1 on Tue-Fri, 3 on Monday)
                    self._current_markets = await self._discover_markets()
                    num_markets = len(self._current_markets)

                    if num_markets > 0:
                        # Split budget evenly across markets
                        budget_per_market = self.settings.max_trade_size_usd / num_markets
                        logger.info(f"Trading {num_markets} market(s), ${budget_per_market:.2f} budget each")

                        # Create a detector for each market
                        self._market_detectors = {}
                        for market in self._current_markets:
                            scale_pcts = parse_scale_in_pcts(self.settings.scale_in_pcts)
                            detector = MovementDetector(
                                zscore_threshold=self.settings.zscore_threshold,
                                scale_in_pcts=scale_pcts,
                                max_buy_price=self.settings.max_buy_price,
                                min_price_change=self.settings.min_price_change,
                            )
                            detector.set_baseline(market.outcomes)
                            self._market_detectors[market.event_slug] = detector
                            logger.info(f"  Market: {market.question}")
                    else:
                        logger.warning("No markets found to trade")

                # Window just ended
                if not in_window and was_in_window:
                    logger.info("=== MONITOR WINDOW ENDED ===")
                    total_signals = sum(d.total_signals for d in self._market_detectors.values())
                    logger.info(f"Session summary: {total_signals} signals across {len(self._current_markets)} market(s)")
                    # Clear state so next window starts fresh
                    self._current_markets = []
                    self._market_detectors = {}

                was_in_window = in_window

                # Active monitoring - poll all markets
                if in_window and self._current_markets:
                    for market in self._current_markets:
                        detector = self._market_detectors.get(market.event_slug)
                        if not detector or not detector.baseline_set:
                            continue

                        # Refresh order books for this market
                        refreshed = await self._refresh_order_books_for_market(market)
                        if refreshed:
                            signals = detector.update_prices(market.outcomes)
                            for signal in signals:
                                self._execute_signal(signal)

                interval = self._get_poll_interval()
                if interval != last_interval:
                    logger.info(f"Poll interval: {interval}s")
                    last_interval = interval
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)
    
    def stop(self):
        logger.info("Stopping bot...")
        self._running = False


class ETFormatter(logging.Formatter):
    """Formatter that converts timestamps to Eastern Time."""
    def formatTime(self, record, datefmt=None):
        from datetime import datetime
        dt = datetime.fromtimestamp(record.created, tz=ET_TIMEZONE)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(level: str = "INFO"):
    """Configure logging for the bot with ET timestamps."""
    handler = logging.StreamHandler()
    handler.setFormatter(ETFormatter(
        fmt="%(asctime)s ET | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.root.handlers = []
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main():
    """Main entry point for the movement bot."""
    settings = load_settings()
    setup_logging(settings.log_level)
    
    logger.info("=" * 60)
    logger.info("  Movement-Based Trading Bot")
    logger.info("=" * 60)
    print_config(settings)
    
    bot = MovementBot(settings)
    
    # Signal handlers (Unix only - works in Docker/Linux)
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
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
