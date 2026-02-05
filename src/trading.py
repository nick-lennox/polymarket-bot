"""
Trading Logic Engine

Determines when and how to trade based on TSA data and market conditions.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from .tsa_scraper import TSADataPoint
from .polymarket import PolymarketClient, Market, MarketOutcome, TradeResult
from .config import TradingConfig

logger = logging.getLogger(__name__)


# Polymarket TSA brackets (200K increments)
TSA_BRACKETS = [
    (0, 1.5, "<1.5M"),
    (1.5, 1.7, "1.5M-1.7M"),
    (1.7, 1.9, "1.7M-1.9M"),
    (1.9, 2.1, "1.9M-2.1M"),
    (2.1, 2.3, "2.1M-2.3M"),
    (2.3, 99.0, ">2.3M"),
]


@dataclass
class TradeSignal:
    action: str  # "BUY_YES", "BUY_NO", "HOLD"
    outcome: MarketOutcome
    reason: str
    target_price: Optional[float] = None
    size_usd: float = 0.0
    edge: float = 0.0


@dataclass
class TradingDecision:
    tsa_data: TSADataPoint
    correct_bracket: str
    signals: list[TradeSignal]
    timestamp: datetime


def get_polymarket_bracket(passenger_count: int) -> str:
    """Map a passenger count to the Polymarket bracket name."""
    millions = passenger_count / 1_000_000
    for lower, upper, name in TSA_BRACKETS:
        if lower <= millions < upper:
            return name
    return ">2.3M"


class TradingEngine:
    """Trading decision engine."""

    def __init__(self, polymarket_client: PolymarketClient, config: TradingConfig):
        self.client = polymarket_client
        self.config = config
        self._trade_history: list[TradeResult] = []

    def analyze_market(self, tsa_data: TSADataPoint, market: Market) -> TradingDecision:
        """Analyze market given new TSA data."""
        signals = []
        correct_bracket = get_polymarket_bracket(tsa_data.passenger_count)

        logger.info(f"Analyzing market for {tsa_data.date}")
        logger.info(f"Actual count: {tsa_data.formatted_count} ({tsa_data.millions:.3f}M)")
        logger.info(f"Correct bracket: {correct_bracket}")

        # Find the matching outcome
        correct_outcome: Optional[MarketOutcome] = None
        for outcome in market.outcomes:
            if self._brackets_match(outcome.outcome, correct_bracket):
                correct_outcome = outcome
                break

        if not correct_outcome:
            logger.warning(f"Could not find outcome matching bracket: {correct_bracket}")
            logger.warning(f"Available outcomes: {[o.outcome for o in market.outcomes]}")
            return TradingDecision(
                tsa_data=tsa_data,
                correct_bracket=correct_bracket,
                signals=[],
                timestamp=datetime.now(),
            )

        logger.info(f"Matched outcome: '{correct_outcome.outcome}' (token: {correct_outcome.token_id[:15]}...)")

        # Analyze the correct outcome - BUY YES if cheap
        if correct_outcome.order_book:
            signal = self._analyze_correct_outcome(correct_outcome)
            if signal:
                signals.append(signal)
        else:
            logger.warning("No order book available for correct outcome")

        # Analyze wrong outcomes - BUY NO where stale YES bids create edge
        for outcome in market.outcomes:
            if outcome == correct_outcome:
                continue
            if not outcome.no_token_id:
                continue
            if outcome.no_order_book:
                signal = self._analyze_wrong_outcome(outcome)
                if signal:
                    signals.append(signal)

        return TradingDecision(
            tsa_data=tsa_data,
            correct_bracket=correct_bracket,
            signals=signals,
            timestamp=datetime.now(),
        )

    def _brackets_match(self, outcome_name: str, bracket: str) -> bool:
        """Check if an outcome name matches a bracket.

        Strict matching to prevent buying the wrong bracket.
        """
        # Normalize: remove spaces, lowercase
        o = outcome_name.lower().replace(" ", "").replace(",", "")
        b = bracket.lower().replace(" ", "").replace(",", "")

        # Direct match
        if b == o:
            return True

        # Extract numbers and compare - must have same count and values
        o_nums = re.findall(r"[\d.]+", o)
        b_nums = re.findall(r"[\d.]+", b)

        if not o_nums or not b_nums:
            return False

        if o_nums != b_nums:
            return False

        # Numbers match - verify same bracket type (both ranges, or both < or >)
        o_has_range = "-" in o or "to" in o
        b_has_range = "-" in b or "to" in b
        o_has_lt = "<" in o or "under" in o or "less" in o or "below" in o
        b_has_lt = "<" in b
        o_has_gt = ">" in o or "over" in o or "more" in o or "above" in o
        b_has_gt = ">" in b

        # Both ranges with same numbers
        if o_has_range and b_has_range:
            return True
        # Both less-than with same number
        if o_has_lt and b_has_lt:
            return True
        # Both greater-than with same number
        if o_has_gt and b_has_gt:
            return True
        # Bracket is range, outcome uses same numbers (flexible name match)
        if len(o_nums) == len(b_nums) == 2 and not o_has_lt and not o_has_gt and not b_has_lt and not b_has_gt:
            return True

        return False

    def _analyze_correct_outcome(self, outcome: MarketOutcome) -> Optional[TradeSignal]:
        """Analyze the correct outcome for buying opportunity."""
        book = outcome.order_book
        if not book or not book.best_ask:
            return None

        ask_price = book.best_ask
        fair_value = 1.0
        edge = fair_value - ask_price

        logger.info(f"Correct outcome '{outcome.outcome}': best_ask={ask_price:.4f}, edge={edge:.4f}")

        if edge < self.config.min_edge:
            return TradeSignal(
                action="HOLD",
                outcome=outcome,
                reason=f"Insufficient edge: {edge:.3f} < {self.config.min_edge}",
                edge=edge,
            )

        if ask_price > self.config.max_buy_price:
            return TradeSignal(
                action="HOLD",
                outcome=outcome,
                reason=f"Price too high: {ask_price:.3f} > {self.config.max_buy_price}",
                edge=edge,
            )

        available_liquidity = sum(level.size * level.price for level in book.asks)

        if available_liquidity < 1.0:
            return TradeSignal(
                action="HOLD",
                outcome=outcome,
                reason="Insufficient liquidity",
                edge=edge,
            )

        return TradeSignal(
            action="BUY_YES",
            outcome=outcome,
            reason=f"Buy correct outcome with {edge:.1%} edge",
            target_price=ask_price,
            size_usd=available_liquidity,
            edge=edge,
        )

    def _analyze_wrong_outcome(self, outcome: MarketOutcome) -> Optional[TradeSignal]:
        """Analyze a wrong outcome for NO-buying opportunity.

        If someone has stale YES asks (or equivalently, cheap NO asks),
        we can buy NO since we know this outcome will resolve to NO.
        """
        book = outcome.no_order_book
        if not book or not book.best_ask:
            return None

        no_ask_price = book.best_ask
        fair_value = 1.0  # NO is worth  since this outcome is wrong
        edge = fair_value - no_ask_price

        logger.info(f"Wrong outcome '{outcome.outcome}': NO best_ask={no_ask_price:.4f}, edge={edge:.4f}")

        if edge < self.config.min_edge:
            return None  # Silent skip - most wrong brackets won't have edge

        if no_ask_price > self.config.max_buy_price:
            return None

        available_liquidity = sum(level.size * level.price for level in book.asks)

        if available_liquidity < 1.0:
            return None

        return TradeSignal(
            action="BUY_NO",
            outcome=outcome,
            reason=f"Buy NO on wrong outcome with {edge:.1%} edge",
            target_price=no_ask_price,
            size_usd=available_liquidity,
            edge=edge,
        )

    def execute_signals(self, signals: list[TradeSignal]) -> list[TradeResult]:
        """Execute trading signals, ranked by edge, from a single budget.

        Sorts all actionable signals by edge (best first), then allocates
        from MAX_TRADE_SIZE_USD until the budget is exhausted.
        """
        results = []
        budget = self.config.max_trade_size_usd
        spent = 0.0

        # Log HOLDs, collect actionable signals
        actionable = []
        for signal in signals:
            if signal.action == "HOLD":
                logger.info(f"HOLD: {signal.outcome.outcome} - {signal.reason}")
            else:
                actionable.append(signal)

        # Sort by edge descending - best opportunities first
        actionable.sort(key=lambda s: s.edge, reverse=True)

        if actionable:
            logger.info(f"Ranked {len(actionable)} opportunities by edge (budget: ${budget:.2f}):")
            for i, s in enumerate(actionable):
                logger.info(f"  {i+1}. {s.action} '{s.outcome.outcome}' edge={s.edge:.1%} liquidity=${s.size_usd:.2f}")

        for signal in actionable:
            remaining = budget - spent
            if remaining < 1.0:
                logger.info(f"Budget exhausted (${spent:.2f}/${budget:.2f}) - skipping remaining")
                break

            trade_amount = min(signal.size_usd, remaining)
            token_id = signal.outcome.token_id if signal.action == "BUY_YES" else signal.outcome.no_token_id

            logger.info(
                f"EXECUTING: {signal.action} on '{signal.outcome.outcome}' "
                f"for ${trade_amount:.2f} @ {signal.target_price:.3f} (edge: {signal.edge:.1%})"
            )

            result = self.client.buy_market_order(
                token_id=token_id,
                amount_usd=trade_amount,
                dry_run=self.config.dry_run,
            )

            results.append(result)
            self._trade_history.append(result)

            if result.success:
                spent += trade_amount
                logger.info(f"Trade executed: {result.order_id} (spent: ${spent:.2f}/${budget:.2f})")
            else:
                logger.error(f"Trade failed: {result.error}")

        return results

    def get_trade_history(self) -> list[TradeResult]:
        return self._trade_history.copy()
