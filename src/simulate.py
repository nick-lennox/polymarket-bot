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
                        print(f"     {outcome.outcome:12s}  YES: {bid_str}  {ask_str}  {spread_str}")
                        print(f"                   bid_depth={bid_depth:.0f}  ask_depth={ask_depth:.0f}")
                    else:
                        print(f"     {outcome.outcome:12s}  YES: [no order book]")

                    no_book = outcome.no_order_book
                    if no_book:
                        no_bid = f"bid={no_book.best_bid:.4f}" if no_book.best_bid else "bid=none"
                        no_ask = f"ask={no_book.best_ask:.4f}" if no_book.best_ask else "ask=none"
                        print(f"                   NO:  {no_bid}  {no_ask}")
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

        # Separate HOLDs from actionable signals, rank by edge
        holds = [s for s in decision.signals if s.action == "HOLD"]
        actionable = [s for s in decision.signals if s.action != "HOLD"]
        actionable.sort(key=lambda s: s.edge, reverse=True)

        if actionable:
            budget = trading_config.max_trade_size_usd
            sim_spent = 0.0
            sim_profit = 0.0
            print(f"     Budget: ${budget:.2f}")
            print()
            for signal in actionable:
                remaining = budget - sim_spent
                if remaining < 1.0:
                    print(f"     --- SKIP '{signal.outcome.outcome}' (budget exhausted)")
                    continue
                alloc = min(signal.size_usd, remaining)
                sim_spent += alloc
                # Profit = payout - cost. Each token pays $1, cost is target_price per token.
                # Shares bought = alloc / target_price. Payout = shares * $1.
                shares = alloc / signal.target_price
                payout = shares * 1.0
                profit = payout - alloc
                sim_profit += profit
                print(f"     >>> WOULD {signal.action} on '{signal.outcome.outcome}'")
                print(f"         Price: {signal.target_price:.4f}")
                print(f"         Spend: ${alloc:.2f}  (liquidity: ${signal.size_usd:.2f})")
                print(f"         Edge: {signal.edge:.1%}")
                print(f"         Profit: ${profit:.2f}  (${alloc:.2f} -> ${payout:.2f})")
            print()
            print(f"     Total would spend: ${sim_spent:.2f} / ${budget:.2f}")
            print(f"     Projected profit:  ${sim_profit:.2f}  ({sim_profit/sim_spent*100:.1f}% return)" if sim_spent > 0 else "")

        for signal in holds:
            print(f"     --- HOLD on '{signal.outcome.outcome}'")
            print(f"         Reason: {signal.reason}")

        if not actionable and not holds:
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
