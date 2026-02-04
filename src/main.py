"""
TSA Polymarket Trading Bot - Main Entry Point

Orchestrates the TSA data monitoring and trading execution loop.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time
from typing import Optional
import pytz

from .config import load_settings, print_config, Settings
from .tsa_scraper import TSAScraper, TSADataPoint
from .polymarket import PolymarketClient
from .trading import TradingEngine, TradingDecision

logger = logging.getLogger(__name__)

ET_TIMEZONE = pytz.timezone("America/New_York")
TSA_UPDATE_TIME = time(9, 0)


class TradingBot:
    """Main trading bot orchestrator."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.scraper: Optional[TSAScraper] = None
        self.polymarket: Optional[PolymarketClient] = None
        self.engine: Optional[TradingEngine] = None
        self._running = False
        self._last_trade_decision: Optional[TradingDecision] = None

    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing TSA Polymarket Trading Bot...")

        self.scraper = TSAScraper(
            timeout=self.settings.get_scraper_config().timeout_seconds
        )

        polymarket_config = self.settings.get_polymarket_config()
        if polymarket_config.private_key:
            self.polymarket = PolymarketClient(polymarket_config)
            try:
                self.polymarket.connect()
                logger.info("Connected to Polymarket")

                self.engine = TradingEngine(
                    polymarket_client=self.polymarket,
                    config=self.settings.get_trading_config(),
                )
            except Exception as e:
                logger.error(f"Failed to connect to Polymarket: {e}")
                logger.info("Running in monitor-only mode")
                self.polymarket = None
                self.engine = None
        else:
            logger.warning("No Polymarket credentials - running in monitor-only mode")

    async def run(self):
        """Main bot loop."""
        self._running = True
        poll_interval = self.settings.poll_interval_seconds

        logger.info(f"Starting monitoring loop (poll interval: {poll_interval}s)")
        logger.info(f"Target market: {self.settings.target_market_slug or '(not set)'}")
        logger.info(f"Dry run mode: {self.settings.dry_run}")

        async with self.scraper:
            logger.info("Fetching initial TSA data...")
            initial_data = await self.scraper.get_latest_data()
            if initial_data:
                self.scraper.last_known_date = initial_data.date
                logger.info(
                    f"Baseline established: {initial_data.date} - "
                    f"{initial_data.formatted_count} ({initial_data.get_bracket()})"
                )
            else:
                logger.warning("Could not fetch initial TSA data")

            while self._running:
                try:
                    await self._check_and_trade()
                except Exception as e:
                    logger.error(f"Error in main loop: {e}", exc_info=True)

                await asyncio.sleep(poll_interval)

    async def _check_and_trade(self):
        """Check for new data and execute trades if appropriate."""
        now_et = datetime.now(ET_TIMEZONE)
        is_weekday = now_et.weekday() < 5
        is_update_window = time(8, 30) <= now_et.time() <= time(10, 0)

        if is_update_window and is_weekday:
            logger.debug("In TSA update window - checking more carefully")

        new_data = await self.scraper.check_for_new_data()

        if not new_data:
            return

        logger.info("=" * 60)
        logger.info("NEW TSA DATA DETECTED!")
        logger.info(f"Date: {new_data.date}")
        logger.info(f"Passenger Count: {new_data.formatted_count}")
        logger.info(f"Bracket: {new_data.get_bracket()}")
        logger.info("=" * 60)

        await self._execute_trading(new_data)

    async def _execute_trading(self, tsa_data: TSADataPoint):
        """Execute trading logic for new TSA data."""
        if not self.engine or not self.polymarket:
            logger.info("No trading engine - skipping trade execution")
            return

        market_slug = self.settings.target_market_slug
        if not market_slug:
            logger.warning("No target market configured - skipping trade")
            return

        logger.info(f"Fetching market: {market_slug}")
        market = self.polymarket.get_market_with_books(market_slug)

        if not market:
            logger.error(f"Could not fetch market: {market_slug}")
            return

        logger.info(f"Market: {market.question}")
        logger.info(f"Outcomes: {[o.outcome for o in market.outcomes]}")

        decision = self.engine.analyze_market(tsa_data, market)
        self._last_trade_decision = decision

        if not decision.signals:
            logger.info("No trade signals generated")
            return

        for signal in decision.signals:
            logger.info(
                f"Signal: {signal.action} on '{signal.outcome.outcome}' - "
                f"${signal.size_usd:.2f} @ {signal.target_price or 0:.3f} "
                f"(edge: {signal.edge:.1%}) - {signal.reason}"
            )

        results = self.engine.execute_signals(decision.signals)

        for result in results:
            if result.success:
                logger.info(f"Trade successful: {result.order_id}")
            else:
                logger.error(f"Trade failed: {result.error}")

    def stop(self):
        """Stop the bot gracefully."""
        logger.info("Stopping bot...")
        self._running = False

    @property
    def status(self) -> dict:
        """Get current bot status."""
        return {
            "running": self._running,
            "last_known_date": str(self.scraper.last_known_date) if self.scraper else None,
            "polymarket_connected": self.polymarket is not None,
            "dry_run": self.settings.dry_run,
            "last_decision": self._last_trade_decision,
        }


def setup_logging(level: str = "INFO"):
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main():
    """Main entry point."""
    settings = load_settings()
    setup_logging(settings.log_level)
    print_config(settings)
    print()

    bot = TradingBot(settings)
    await bot.initialize()

    loop = asyncio.get_event_loop()

    def signal_handler():
        bot.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        bot.stop()
        logger.info("Bot stopped")


def run():
    """Synchronous entry point for CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
