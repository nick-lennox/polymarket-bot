"""
Tests for the Movement Detector module.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock

from src.movement_detector import (
    OutcomeState,
    MovementSignal,
    MovementDetector,
    parse_scale_in_pcts,
)


class TestParseScaleInPcts:
    def test_default_when_empty(self):
        assert parse_scale_in_pcts("") == [50.0, 30.0, 20.0]
        assert parse_scale_in_pcts(None) == [50.0, 30.0, 20.0]

    def test_parses_valid_string(self):
        assert parse_scale_in_pcts("50,30,20") == [50.0, 30.0, 20.0]
        assert parse_scale_in_pcts("60,40") == [60.0, 40.0]

    def test_handles_whitespace(self):
        assert parse_scale_in_pcts(" 50 , 30 , 20 ") == [50.0, 30.0, 20.0]

    def test_invalid_returns_default(self):
        assert parse_scale_in_pcts("abc,def") == [50.0, 30.0, 20.0]


class TestOutcomeState:
    def test_initialization(self):
        state = OutcomeState(outcome_name="1.7M-1.9M", token_id="abc123")
        assert state.outcome_name == "1.7M-1.9M"
        assert state.baseline_price is None
        assert state.triggered is False

    def test_set_baseline(self):
        state = OutcomeState(outcome_name="Test", token_id="abc")
        state.set_baseline(0.15)
        assert state.baseline_price == 0.15
        assert state.current_price == 0.15
        assert len(state.price_history) == 1

    def test_update_price(self):
        state = OutcomeState(outcome_name="Test", token_id="abc")
        state.set_baseline(0.10)
        state.update_price(0.12)
        assert state.current_price == 0.12
        assert len(state.price_history) == 2

    def test_get_price_change(self):
        state = OutcomeState(outcome_name="Test", token_id="abc")
        state.set_baseline(0.10)
        state.update_price(0.15)
        change = state.get_price_change()
        assert abs(change - 0.05) < 0.0001

    def test_get_zscore_requires_min_data(self):
        state = OutcomeState(outcome_name="Test", token_id="abc")
        state.set_baseline(0.10)
        assert state.get_zscore() is None  # Only 1 point
        for _ in range(4):
            state.update_price(0.10)
        assert state.get_zscore() is not None  # Now 5 points


class TestMovementDetector:
    def test_initialization(self):
        detector = MovementDetector()
        assert detector.zscore_threshold == 2.5
        assert detector.baseline_set is False

    def test_reset(self):
        detector = MovementDetector()
        detector.baseline_set = True
        detector.total_signals = 5
        detector.reset()
        assert detector.baseline_set is False
        assert detector.total_signals == 0

    def test_set_baseline_with_mock(self):
        detector = MovementDetector()
        outcome = MagicMock()
        outcome.outcome = "Test"
        outcome.token_id = "token1"
        outcome.no_token_id = None
        outcome.order_book = MagicMock()
        outcome.order_book.asks = [MagicMock(price=0.15)]
        detector.set_baseline([outcome])
        assert detector.baseline_set is True
        assert "Test" in detector.outcomes

    def test_get_status(self):
        detector = MovementDetector()
        status = detector.get_status()
        assert "baseline_set" in status
        assert "total_signals" in status

    def test_trigger_respects_max_price(self):
        detector = MovementDetector(max_buy_price=0.50)
        state = OutcomeState("Test", "token1")
        state.set_baseline(0.40)
        for _ in range(5):
            state.update_price(0.60)  # Above max
        detector.outcomes["Test"] = state
        detector.baseline_set = True
        signal = detector._check_trigger(state)
        assert signal is None  # Price too high


class TestGetBestAsk:
    def test_handles_none(self):
        detector = MovementDetector()
        assert detector._get_best_ask(None) is None

    def test_handles_empty_asks(self):
        detector = MovementDetector()
        ob = MagicMock()
        ob.asks = []
        assert detector._get_best_ask(ob) is None

    def test_handles_object_price(self):
        detector = MovementDetector()
        ob = MagicMock()
        ask = MagicMock()
        ask.price = 0.25
        ob.asks = [ask]
        assert detector._get_best_ask(ob) == 0.25
