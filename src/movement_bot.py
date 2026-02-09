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
    
    async def _refresh_order_books(self) -> Optional[Market]:
        if not self.polymarket or not self._current_market:
            return None
        try:
            for outcome in self._current_market.outcomes:
                ob = self.polymarket.get_order_book(outcome.token_id)
                outcome.order_book = ob
            return self._current_market
        except Exception as e:
            logger.error(f"Failed to refresh order books: {e}")
            return None
    
    def _execute_signal(self, signal: MovementSignal):
        if not self.polymarket:
            logger.info(f"[DRY] Would buy {signal.outcome_name} @ ${signal.current_price:.4f}")
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

    async def run(self):
        self._running = True
        logger.info("Starting Movement Bot")
        logger.info(f"  Monitor window: {self.settings.monitor_window_start_hour}:00 - {self.settings.monitor_window_end_hour}:00 ET")
        logger.info(f"  Z-score threshold: {self.settings.zscore_threshold}")
        logger.info(f"  Budget: ${self.settings.max_trade_size_usd}")
        logger.info(f"  Dry run: {self.settings.dry_run}")
        
        was_in_window = False
        last_interval = None
        
        while self._running:
            try:
                in_window = self._in_monitor_window()
                
                # Window just started
                if in_window and not was_in_window:
                    logger.info("=== MONITOR WINDOW STARTED ===")
                    self._budget_remaining = self.settings.max_trade_size_usd
                    self.detector.reset()
                    
                    self._current_market = await self._discover_market()
                    if self._current_market:
                        logger.info(f"Market: {self._current_market.question}")
                        self.detector.set_baseline(self._current_market.outcomes)
                
                # Window just ended
                if not in_window and was_in_window:
                    logger.info("=== MONITOR WINDOW ENDED ===")
                    status = self.detector.get_status()
                    logger.info(f"Session summary: {status['total_signals']} signals, {status['budget_spent_pct']}% budget used")
                
                was_in_window = in_window
                
                # Active monitoring
                if in_window and self._current_market and self.detector.baseline_set:
                    market = await self._refresh_order_books()
                    if market:
                        signals = self.detector.update_prices(market.outcomes)
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


def setup_logging(level: str = "INFO"):
    """Configure logging for the bot."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
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
