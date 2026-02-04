"""
Trading Logic Engine

Determines when and how to trade based on TSA data and market conditions.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from .tsa_scraper import TSADataPoint
from .polymarket import PolymarketClient, Market, MarketOutcome, TradeResult
from .config import TradingConfig

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """A signal to execute a trade."""
    action: str  # "BUY_YES", "SELL_NO", "HOLD"
    outcome: MarketOutcome
    reason: str
    target_price: Optional[float] = None
    size_usd: float = 0.0
    edge: float = 0.0


@dataclass
class TradingDecision:
    """Result of analyzing a market given new TSA data."""
    tsa_data: TSADataPoint
    correct_bracket: str
    signals: list[TradeSignal]
    timestamp: datetime


class TradingEngine:
    """Trading decision engine."""

    def __init__(self, polymarket_client: PolymarketClient, config: TradingConfig):
        self.client = polymarket_client
        self.config = config
        self._trade_history: list[TradeResult] = []

    def analyze_market(self, tsa_data: TSADataPoint, market: Market) -> TradingDecision:
        """Analyze market given new TSA data."""
        signals = []
        correct_bracket = tsa_data.get_bracket()

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

        # Analyze the correct outcome - BUY YES if cheap
        if correct_outcome.order_book:
            signal = self._analyze_correct_outcome(correct_outcome)
            if signal:
                signals.append(signal)

        return TradingDecision(
            tsa_data=tsa_data,
            correct_bracket=correct_bracket,
            signals=signals,
            timestamp=datetime.now(),
        )

    def _brackets_match(self, outcome_name: str, bracket: str) -> bool:
        """Check if an outcome name matches a bracket."""
        import re
        outcome_lower = outcome_name.lower().replace(",", "").replace(" ", "")
        bracket_lower = bracket.lower().replace(",", "").replace(" ", "")

        if bracket_lower in outcome_lower:
            return True

        bracket_nums = re.findall(r"(\d+\.?\d*)m?", bracket_lower)
        if len(bracket_nums) >= 2:
            lower_bound = bracket_nums[0]
            upper_bound = bracket_nums[1]
            if lower_bound in outcome_lower and upper_bound in outcome_lower:
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

        logger.info(f"Correct outcome '{outcome.outcome}': ask={ask_price:.3f}, edge={edge:.3f}")

        if edge < self.config.min_edge:
            logger.debug(f"Insufficient edge ({edge:.3f} < {self.config.min_edge})")
            return TradeSignal(
                action="HOLD",
                outcome=outcome,
                reason=f"Insufficient edge: {edge:.3f} < {self.config.min_edge}",
                edge=edge,
            )

        if ask_price > self.config.max_buy_price:
            logger.debug(f"Price too high ({ask_price:.3f} > {self.config.max_buy_price})")
            return TradeSignal(
                action="HOLD",
                outcome=outcome,
                reason=f"Price too high: {ask_price:.3f} > {self.config.max_buy_price}",
                edge=edge,
            )

        available_liquidity = sum(level.size * level.price for level in book.asks)
        trade_size = min(self.config.max_trade_size_usd, available_liquidity)

        if trade_size < 1.0:
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
            size_usd=trade_size,
            edge=edge,
        )

    def execute_signals(self, signals: list[TradeSignal]) -> list[TradeResult]:
        """Execute trading signals."""
        results = []

        for signal in signals:
            if signal.action == "HOLD":
                logger.info(f"HOLD: {signal.outcome.outcome} - {signal.reason}")
                continue

            if signal.action == "BUY_YES":
                logger.info(
                    f"EXECUTING: BUY YES on '{signal.outcome.outcome}' "
                    f"for ${signal.size_usd:.2f} @ {signal.target_price:.3f}"
                )

                result = self.client.buy_market_order(
                    token_id=signal.outcome.token_id,
                    amount_usd=signal.size_usd,
                    dry_run=self.config.dry_run,
                )

                results.append(result)
                self._trade_history.append(result)

                if result.success:
                    logger.info(f"Trade executed: {result.order_id}")
                else:
                    logger.error(f"Trade failed: {result.error}")

        return results

    def get_trade_history(self) -> list[TradeResult]:
        return self._trade_history.copy()


class BracketMapper:
    """Maps passenger counts to market outcome brackets."""

    def __init__(self, bracket_size_millions: float = 0.1, brackets=None):
        self.bracket_size = bracket_size_millions
        self.explicit_brackets = brackets

    def get_bracket_name(self, passenger_count: int) -> str:
        millions = passenger_count / 1_000_000

        if self.explicit_brackets:
            for lower, upper, name in self.explicit_brackets:
                if lower <= millions < upper:
                    return name

        lower = int(millions / self.bracket_size) * self.bracket_size
        upper = lower + self.bracket_size
        return f"{lower:.1f}M - {upper:.1f}M"
