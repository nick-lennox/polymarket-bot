"""
Order Book Movement Detector

Monitors Polymarket order books during the TSA data release window
and detects statistically significant price movements using z-scores.

Strategy:
1. At window start (7:00 AM ET), snapshot baseline prices
2. Poll order books every 1-2 seconds
3. Calculate z-score of price movement for each outcome
4. When z-score exceeds threshold, trigger buy signal
5. Scale in: allocate budget portions on successive triggers
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class OutcomeState:
    """Tracks price history and statistics for one outcome."""
    outcome_name: str
    token_id: str
    no_token_id: Optional[str] = None
    price_history: deque = field(default_factory=lambda: deque(maxlen=60))
    baseline_price: Optional[float] = None
    current_price: Optional[float] = None
    last_update: Optional[datetime] = None
    triggered: bool = False
    trigger_count: int = 0
    
    def update_price(self, price: float, timestamp=None):
        self.current_price = price
        self.last_update = timestamp or datetime.now()
        self.price_history.append(price)
    
    def set_baseline(self, price: float):
        self.baseline_price = price
        self.price_history.clear()
        self.price_history.append(price)
        self.current_price = price
        self.triggered = False
        self.trigger_count = 0
        logger.info(f"  {self.outcome_name}: baseline={price:.4f}")
    
    def get_zscore(self):
        if self.baseline_price is None or self.current_price is None:
            return None
        if len(self.price_history) < 5:
            return None
        try:
            std_dev = statistics.stdev(self.price_history)
            if std_dev < 0.001:
                change = self.current_price - self.baseline_price
                if abs(change) > 0.05:
                    return 10.0 if change > 0 else -10.0
                return 0.0
            return (self.current_price - self.baseline_price) / std_dev
        except statistics.StatisticsError:
            return None
    
    def get_price_change(self):
        if self.baseline_price is None or self.current_price is None:
            return None
        return self.current_price - self.baseline_price

@dataclass 
class MovementSignal:
    outcome_name: str
    token_id: str
    no_token_id: Optional[str]
    current_price: float
    baseline_price: float
    zscore: float
    price_change: float
    price_change_pct: float
    trigger_number: int
    budget_pct: float
    timestamp: datetime


class MovementDetector:
    def __init__(self, zscore_threshold=2.5, scale_in_pcts=None, max_buy_price=0.95, min_price_change=0.05):
        self.zscore_threshold = zscore_threshold
        self.scale_in_pcts = scale_in_pcts or [50.0, 30.0, 20.0]
        self.max_buy_price = max_buy_price
        self.min_price_change = min_price_change
        self.outcomes = {}
        self.baseline_set = False
        self.window_start = None
        self.total_signals = 0
        self.budget_spent_pct = 0.0
        self._locked_outcome: Optional[str] = None  # Once one outcome triggers, lock to it
        
    def reset(self):
        self.outcomes.clear()
        self.baseline_set = False
        self.window_start = None
        self.total_signals = 0
        self.budget_spent_pct = 0.0
        self._locked_outcome = None
        logger.info("MovementDetector reset")
    
    def set_baseline(self, market_outcomes):
        self.window_start = datetime.now()
        self.baseline_set = True
        logger.info(f"Setting baseline prices at {self.window_start}")
        for outcome in market_outcomes:
            best_ask = self._get_best_ask(outcome.order_book)
            if best_ask is None:
                logger.warning(f"  {outcome.outcome}: no asks")
                continue
            state = OutcomeState(outcome_name=outcome.outcome, token_id=outcome.token_id,
                                 no_token_id=getattr(outcome, "no_token_id", None))
            state.set_baseline(best_ask)
            self.outcomes[outcome.outcome] = state
    
    def update_prices(self, market_outcomes):
        """Update prices for all outcomes but only signal on the top mover.

        TSA markets have multiple brackets (e.g. 2.0M-2.2M, 2.2M-2.4M, etc).
        When data drops, the winning bracket surges while others fall.
        We only want to buy the single outcome with the highest z-score,
        not scatter budget across multiple brackets.
        """
        if not self.baseline_set:
            return []

        now = datetime.now()
        # First pass: update all prices
        for outcome in market_outcomes:
            if outcome.outcome not in self.outcomes:
                continue
            state = self.outcomes[outcome.outcome]
            best_ask = self._get_best_ask(outcome.order_book)
            if best_ask is None:
                continue
            state.update_price(best_ask, now)

        # Second pass: find the outcome with the highest z-score
        best_state = None
        best_zscore = 0.0
        for name, state in self.outcomes.items():
            zscore = state.get_zscore()
            if zscore is not None and zscore > best_zscore:
                best_zscore = zscore
                best_state = state

        # Only trigger on the single best outcome
        signals = []
        if best_state:
            signal = self._check_trigger(best_state)
            if signal:
                signals.append(signal)
                self.total_signals += 1
        return signals

    def _check_trigger(self, state):
        zscore = state.get_zscore()
        if zscore is None or zscore < self.zscore_threshold:
            return None
        price_change = state.get_price_change()
        if price_change is not None and price_change < self.min_price_change:
            return None
        if state.current_price > self.max_buy_price:
            logger.info(f"  {state.outcome_name}: z={zscore:.2f} but price {state.current_price:.4f} > max")
            return None
        # Once one outcome triggers, only allow that same outcome
        if self._locked_outcome is not None and self._locked_outcome != state.outcome_name:
            return None
        trigger_num = state.trigger_count + 1
        if trigger_num > len(self.scale_in_pcts):
            return None
        budget_pct = self.scale_in_pcts[trigger_num - 1]
        state.triggered = True
        state.trigger_count = trigger_num
        self.budget_spent_pct += budget_pct
        if self._locked_outcome is None:
            self._locked_outcome = state.outcome_name
            logger.info(f"  Locked to outcome: {state.outcome_name}")
        pct = (price_change / state.baseline_price * 100) if state.baseline_price > 0 else 0
        logger.info(f"SIGNAL: BUY YES {state.outcome_name} z={zscore:.2f} "
                    f"price={state.baseline_price:.4f}->{state.current_price:.4f} "
                    f"(+{price_change:.4f}, +{pct:.1f}%) trigger #{trigger_num} -> {budget_pct}% budget")
        return MovementSignal(
            outcome_name=state.outcome_name, token_id=state.token_id, no_token_id=state.no_token_id,
            current_price=state.current_price, baseline_price=state.baseline_price, zscore=zscore,
            price_change=price_change, price_change_pct=pct, trigger_number=trigger_num,
            budget_pct=budget_pct, timestamp=state.last_update or datetime.now())
    
    def _get_best_ask(self, order_book):
        if order_book is None:
            return None
        asks = getattr(order_book, "asks", None)
        if not asks:
            return None
        try:
            if isinstance(asks[0], dict):
                return float(asks[0].get("price", asks[0].get("p", 0)))
            elif hasattr(asks[0], "price"):
                return float(asks[0].price)
            return float(asks[0][0])
        except:
            return None
    
    def get_status(self):
        return {
            "baseline_set": self.baseline_set,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "outcomes_tracked": len(self.outcomes),
            "total_signals": self.total_signals,
            "budget_spent_pct": self.budget_spent_pct,
        }


def parse_scale_in_pcts(value):
    if not value:
        return [50.0, 30.0, 20.0]
    try:
        pcts = [float(x.strip()) for x in value.split(",")]
        if sum(pcts) > 100:
            total = sum(pcts)
            pcts = [p * 100 / total for p in pcts]
        return pcts
    except ValueError:
        return [50.0, 30.0, 20.0]
