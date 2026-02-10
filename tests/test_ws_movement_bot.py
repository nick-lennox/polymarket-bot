"""
Tests for WebSocket Movement Bot.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

from src.ws_movement_bot import WebSocketMovementBot
from src.config import Settings


def create_mock_settings():
    settings = MagicMock(spec=Settings)
    settings.max_trade_size_usd = 100.0
    settings.max_buy_price = 0.95
    settings.dry_run = True
    settings.zscore_threshold = 2.5
    settings.scale_in_pcts = "50,30,20"
    settings.min_price_change = 0.05
    settings.monitor_window_start_hour = 7
    settings.monitor_window_end_hour = 10
    settings.poll_interval_seconds = 30
    settings.target_market_slug = ""
    settings.log_level = "INFO"
    polymarket_config = MagicMock()
    polymarket_config.private_key = ""
    settings.get_polymarket_config.return_value = polymarket_config
    return settings


class TestWebSocketBotInit:
    def test_initialization(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        assert bot._budget_remaining == 100.0
        assert bot._running is False
        assert bot._ws is None
        assert bot._update_count == 0

    @pytest.mark.asyncio
    async def test_initialize_without_credentials(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        await bot.initialize()
        assert bot.polymarket is None
        assert bot.detector is not None


class TestProcessBookUpdate:
    def test_processes_valid_update(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)

        # Create a real-ish detector mock that won't trigger
        bot.detector = MagicMock()
        state_mock = MagicMock()
        state_mock.get_zscore.return_value = 0.5  # Below threshold, no signal
        bot.detector.outcomes = {"Test Outcome": state_mock}
        bot.detector._check_trigger.return_value = None

        bot._token_id_to_outcome = {"token123": "Test Outcome"}

        data = {
            "event_type": "book",
            "asset_id": "token123",
            "asks": [{"price": "0.25", "size": "100"}],
            "bids": [{"price": "0.24", "size": "50"}],
        }

        bot._process_book_update(data)
        assert bot._update_count == 1
        state_mock.update_price.assert_called_once()

    def test_ignores_unknown_token(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.detector = MagicMock()
        bot._token_id_to_outcome = {}

        data = {
            "event_type": "book",
            "asset_id": "unknown_token",
            "asks": [{"price": "0.25", "size": "100"}],
        }

        bot._process_book_update(data)
        assert bot._update_count == 0

    def test_handles_empty_asks(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.detector = MagicMock()
        bot._token_id_to_outcome = {"token123": "Test"}

        data = {
            "event_type": "book",
            "asset_id": "token123",
            "asks": [],
        }

        bot._process_book_update(data)
        assert bot._update_count == 0


class TestMessageParsing:
    """Tests for WebSocket message parsing - the actual format from Polymarket."""

    @pytest.mark.asyncio
    async def test_handles_array_of_updates(self):
        """Polymarket WS sends arrays of updates, not single objects."""
        import json

        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.detector = MagicMock()
        state_mock = MagicMock()
        bot.detector.outcomes = {"Test Outcome": state_mock}
        bot.detector._check_trigger.return_value = None
        bot._token_id_to_outcome = {"token123": "Test Outcome"}

        # This is what Polymarket actually sends - an ARRAY
        ws_message = json.dumps([
            {
                "event_type": "book",
                "asset_id": "token123",
                "asks": [{"price": "0.25", "size": "100"}],
            },
            {
                "event_type": "book",
                "asset_id": "token123",
                "asks": [{"price": "0.26", "size": "50"}],
            },
        ])

        # Parse like the real handler does
        data = json.loads(ws_message)
        updates = data if isinstance(data, list) else [data]

        for update in updates:
            if isinstance(update, dict) and update.get("event_type") == "book":
                bot._process_book_update(update)

        # Should have processed both updates
        assert bot._update_count == 2

    @pytest.mark.asyncio
    async def test_handles_single_object(self):
        """Should still work if WS sends a single object."""
        import json

        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.detector = MagicMock()
        state_mock = MagicMock()
        bot.detector.outcomes = {"Test Outcome": state_mock}
        bot.detector._check_trigger.return_value = None
        bot._token_id_to_outcome = {"token123": "Test Outcome"}

        # Single object (not array)
        ws_message = json.dumps({
            "event_type": "book",
            "asset_id": "token123",
            "asks": [{"price": "0.25", "size": "100"}],
        })

        data = json.loads(ws_message)
        updates = data if isinstance(data, list) else [data]

        for update in updates:
            if isinstance(update, dict) and update.get("event_type") == "book":
                bot._process_book_update(update)

        assert bot._update_count == 1

    @pytest.mark.asyncio
    async def test_ignores_non_dict_items_in_array(self):
        """Should skip non-dict items in the array."""
        import json

        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.detector = MagicMock()
        bot._token_id_to_outcome = {}

        # Array with mixed types
        ws_message = json.dumps([
            "string_item",
            123,
            None,
            {"event_type": "book", "asset_id": "unknown"},
        ])

        data = json.loads(ws_message)
        updates = data if isinstance(data, list) else [data]

        # Should not raise
        for update in updates:
            if isinstance(update, dict) and update.get("event_type") == "book":
                bot._process_book_update(update)

        assert bot._update_count == 0  # No valid tokens


class TestBotStop:
    def test_stop(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot._running = True
        bot.stop()
        assert bot._running is False


class TestExecuteSignal:
    def test_dry_run_logs_only(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.polymarket = None

        signal = MagicMock()
        signal.outcome_name = "Test"
        signal.current_price = 0.25
        signal.budget_pct = 50.0

        # Should not raise
        bot._execute_signal(signal)

    def test_tracks_latency(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.polymarket = None

        signal = MagicMock()
        signal.outcome_name = "Test"
        signal.current_price = 0.25
        signal.budget_pct = 50.0

        bot._execute_signal(signal)
        assert bot._last_signal_time is not None

        # Second signal should have latency calculated
        bot._execute_signal(signal)
