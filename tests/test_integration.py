"""
Integration tests for end-to-end scenarios.
"""

import pytest
from unittest.mock import MagicMock

from src.movement_detector import MovementDetector
from src.config import load_settings


class TestEndToEndScenarios:
    def create_mock_outcome(self, name, token_id, price):
        outcome = MagicMock()
        outcome.outcome = name
        outcome.token_id = token_id
        outcome.no_token_id = f"no_{token_id}"
        outcome.order_book = MagicMock()
        outcome.order_book.asks = [MagicMock(price=price)]
        return outcome

    def test_smart_money_spike(self):
        detector = MovementDetector(
            zscore_threshold=2.5,
            scale_in_pcts=[50.0, 30.0, 20.0],
            min_price_change=0.05,
        )
        brackets = [
            ("< 1.5M", "t1", 0.08),
            ("1.5M - 1.7M", "t2", 0.10),
            ("1.7M - 1.9M", "t3", 0.10),
            ("1.9M - 2.1M", "t4", 0.10),
        ]
        baseline = [self.create_mock_outcome(n, t, p) for n, t, p in brackets]
        detector.set_baseline(baseline)
        assert detector.baseline_set is True

        # Simulate price spike on winning bracket
        for price in [0.11, 0.13, 0.18, 0.25, 0.35]:
            updates = [self.create_mock_outcome(n, t, p) for n, t, p in brackets]
            updates[2].order_book.asks = [MagicMock(price=price)]
            signals = detector.update_prices(updates)
            if signals:
                assert signals[0].outcome_name == "1.7M - 1.9M"

    def test_false_positive_prevention(self):
        detector = MovementDetector(
            zscore_threshold=2.5,
            min_price_change=0.05,
        )
        outcome = self.create_mock_outcome("Test", "t1", 0.10)
        detector.set_baseline([outcome])

        # Small oscillations should not trigger
        for price in [0.10, 0.11, 0.10, 0.11, 0.10, 0.11]:
            outcome.order_book.asks = [MagicMock(price=price)]
            signals = detector.update_prices([outcome])
            assert signals == []

    def test_scale_in_behavior(self):
        detector = MovementDetector(
            zscore_threshold=1.5,
            scale_in_pcts=[50.0, 30.0, 20.0],
            min_price_change=0.03,
        )
        outcome = self.create_mock_outcome("Winner", "t1", 0.10)
        detector.set_baseline([outcome])

        signals_received = []
        for price in [0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
            outcome.order_book.asks = [MagicMock(price=price)]
            signals = detector.update_prices([outcome])
            signals_received.extend(signals)

        assert len(signals_received) == 3
        assert signals_received[0].budget_pct == 50.0
        assert signals_received[1].budget_pct == 30.0
        assert signals_received[2].budget_pct == 20.0


class TestEdgeCases:
    def test_empty_order_book(self):
        detector = MovementDetector()
        outcome = MagicMock()
        outcome.outcome = "Test"
        outcome.token_id = "t1"
        outcome.order_book = MagicMock()
        outcome.order_book.asks = []
        detector.set_baseline([outcome])
        assert len(detector.outcomes) == 0

    def test_reset_clears_state(self):
        detector = MovementDetector()
        outcome = MagicMock()
        outcome.outcome = "Test"
        outcome.token_id = "t1"
        outcome.order_book = MagicMock()
        outcome.order_book.asks = [MagicMock(price=0.10)]
        detector.set_baseline([outcome])
        detector.total_signals = 5
        detector.reset()
        assert detector.baseline_set is False
        assert detector.total_signals == 0
