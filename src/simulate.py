"""
End-to-end simulation test.

Verifies the full pipeline without executing real trades:
1. Fetches live TSA data
2. Connects to Polymarket and discovers the market
3. Fetches order books for all outcomes
4. Runs trading logic against current data
5. Reports what WOULD happen if this were a live run

Usage:
    python -m src.simulate
"""

import asyncio
import json
import logging
import sys

# Setup path for direct execution
if __name__ == "__main__":
    sys.path.insert(0, ".")

from src.config import load_settings, print_config
from src.tsa_scraper import TSAScraper
from src.trading import TradingEngine, get_polymarket_bracket


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def print_header(text):
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def print_section(text):
    print()
    print(f"--- {text} ---")


async def run_simulation():
    settings = load_settings()
    errors = []
    warnings = []

    print_header("TSA POLYMARKET BOT - SIMULATION TEST")

    # STEP 1: Config
    print_section("STEP 1: Configuration")
    print_config(settings)

    if not settings.polymarket_private_key:
        errors.append("POLYMARKET_PRIVATE_KEY is not set")
    if not settings.target_market_slug:
        print("[INFO] TARGET_MARKET_SLUG not set - will use auto-discovery")
    if settings.dry_run:
        print("[OK] Dry run mode is ON (safe)")
    else:
        warnings.append("DRY_RUN is OFF - real trades will execute!")

    # STEP 2: TSA Scraper
    print_section("STEP 2: TSA Data Fetch")
    tsa_data = None
    async with TSAScraper() as scraper:
        all_data = await scraper.get_all_data()
        if all_data:
            tsa_data = all_data[0]
            print(f"[OK] Fetched {len(all_data)} data points")
            print(f"     Latest: {tsa_data.date} - {tsa_data.formatted_count} passengers")
            bracket = get_polymarket_bracket(tsa_data.passenger_count)
            print(f"     Bracket: {bracket}")
        else:
            errors.append("Failed to fetch TSA data")

    # STEP 3: Polymarket Connection
    print_section("STEP 3: Polymarket Connection")
    poly_client = None
    if settings.polymarket_private_key:
        try:
            from src.polymarket import PolymarketClient
            config = settings.get_polymarket_config()
            poly_client = PolymarketClient(config)
            poly_client.connect()
            print("[OK] Connected to Polymarket CLOB")

            try:
                balance_info = poly_client.get_balance_info()
                print(f"[OK] Balance/Allowance: {balance_info}")
            except Exception as e:
                warnings.append(f"Could not fetch balance: {e}")
        except Exception as e:
            errors.append(f"Failed to connect to Polymarket: {e}")
    else:
        errors.append("No private key - cannot test Polymarket connection")

    # STEP 4: Market Discovery
    print_section("STEP 4: Market Discovery")
    market = None
    market_slug = settings.target_market_slug
    if poly_client:
        try:
            if not market_slug:
                print("[INFO] Attempting auto-discovery of TSA market...")
                if tsa_data:
                    market_slug = poly_client.discover_tsa_market(tsa_data.date)
                else:
                    market_slug = poly_client.discover_tsa_market()
                if market_slug:
                    print(f"[OK] Auto-discovered market slug: {market_slug}")
                else:
                    errors.append("Auto-discovery failed - no TSA market found for today")
            if market_slug:
                market = poly_client.get_market_with_books(market_slug)
            if market:
                print(f"[OK] Found market: {market.question}")
                print(f"     Outcomes: {len(market.outcomes)}")
                print()
                for outcome in market.outcomes:
                    book = outcome.order_book
                    if book:
                        bid_str = f"bid={book.best_bid:.4f}" if book.best_bid else "bid=none"
                        ask_str = f"ask={book.best_ask:.4f}" if book.best_ask else "ask=none"
                        spread_str = f"spread={book.spread:.4f}" if book.spread else ""
                        bid_depth = sum(l.size for l in book.bids)
                        ask_depth = sum(l.size for l in book.asks)
                        print(f"     {outcome.outcome:12s}  {bid_str}  {ask_str}  {spread_str}")
                        print(f"                   bid_depth={bid_depth:.0f}  ask_depth={ask_depth:.0f}")
                    else:
                        print(f"     {outcome.outcome:12s}  [no order book]")
            else:
                errors.append(f"Market not found: {market_slug}")
        except Exception as e:
            errors.append(f"Failed to discover market: {e}")

    # STEP 5: Trading Simulation
    print_section("STEP 5: Trading Simulation")
    if tsa_data and market and poly_client:
        trading_config = settings.get_trading_config()
        trading_config.dry_run = True  # Force dry run

        engine = TradingEngine(poly_client, trading_config)
        decision = engine.analyze_market(tsa_data, market)

        print(f"     TSA Count: {tsa_data.formatted_count}")
        print(f"     Correct bracket: {decision.correct_bracket}")
        print(f"     Signals generated: {len(decision.signals)}")
        print()

        if decision.signals:
            for signal in decision.signals:
                if signal.action == "BUY_YES":
                    print(f"     >>> WOULD BUY YES on '{signal.outcome.outcome}'")
                    print(f"         Price: {signal.target_price:.4f}")
                    print(f"         Size: ${signal.size_usd:.2f}")
                    print(f"         Edge: {signal.edge:.1%}")
                    print(f"         Reason: {signal.reason}")
                elif signal.action == "HOLD":
                    print(f"     --- HOLD on '{signal.outcome.outcome}'")
                    print(f"         Reason: {signal.reason}")
        else:
            print("     No trading opportunities found")
            warnings.append("No signals generated")
    else:
        missing = []
        if not tsa_data: missing.append("TSA data")
        if not market: missing.append("market")
        if not poly_client: missing.append("Polymarket connection")
        print(f"     Cannot simulate - missing: {', '.join(missing)}")

    # STEP 6: Summary
    print_header("SIMULATION RESULTS")

    if errors:
        print()
        print("ERRORS (must fix before live run):")
        for e in errors:
            print(f"  [X] {e}")

    if warnings:
        print()
        print("WARNINGS:")
        for w in warnings:
            print(f"  [!] {w}")

    if not errors and not warnings:
        print()
        print("  ALL CHECKS PASSED")
        print("  The bot is ready for live trading.")
        print("  Set DRY_RUN=false when you want to go live.")

    if not errors and warnings:
        print()
        print("  No critical errors. Review warnings above.")

    print()


if __name__ == "__main__":
    asyncio.run(run_simulation())
