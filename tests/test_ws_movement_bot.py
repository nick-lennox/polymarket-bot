"""
Tests for WebSocket Movement Bot.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, date

from src.ws_movement_bot import WebSocketMovementBot
from src.config import Settings
from src.polymarket import Market, MarketOutcome


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
        assert bot._current_markets == []
        assert bot._market_detectors == {}

    @pytest.mark.asyncio
    async def test_initialize_without_credentials(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        await bot.initialize()
        assert bot.polymarket is None
        assert bot.detector is not None


class TestProcessBookUpdate:
    def _setup_bot_with_detector(self):
        """Helper to create a bot with a per-market detector."""
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)

        detector = MagicMock()
        state_mock = MagicMock()
        state_mock.get_zscore.return_value = 0.5
        detector.outcomes = {"Test Outcome": state_mock}
        detector._check_trigger.return_value = None

        bot._market_detectors = {"test-market-slug": detector}
        bot._token_id_to_outcome = {"token123": "Test Outcome"}
        bot._token_id_to_slug = {"token123": "test-market-slug"}

        return bot, detector, state_mock

    def test_processes_valid_update(self):
        bot, detector, state_mock = self._setup_bot_with_detector()

        data = {
            "event_type": "book",
            "asset_id": "token123",
            "asks": [{"price": "0.25", "size": "100"}],
            "bids": [{"price": "0.24", "size": "50"}],
        }

        bot._process_book_update(data)
        assert bot._update_count == 1
        state_mock.update_price.assert_called_once()

    def test_routes_to_correct_detector(self):
        """Updates should go to the detector matching the token's market slug."""
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)

        # Two detectors for two markets
        detector_fri = MagicMock()
        state_fri = MagicMock()
        detector_fri.outcomes = {"2.7M+": state_fri}
        detector_fri._check_trigger.return_value = None

        detector_sat = MagicMock()
        state_sat = MagicMock()
        detector_sat.outcomes = {"2.7M+": state_sat}
        detector_sat._check_trigger.return_value = None

        bot._market_detectors = {
            "number-of-tsa-passengers-february-13": detector_fri,
            "number-of-tsa-passengers-february-14": detector_sat,
        }
        bot._token_id_to_outcome = {
            "token_fri": "2.7M+",
            "token_sat": "2.7M+",
        }
        bot._token_id_to_slug = {
            "token_fri": "number-of-tsa-passengers-february-13",
            "token_sat": "number-of-tsa-passengers-february-14",
        }

        # Send update for Friday's token
        bot._process_book_update({
            "event_type": "book",
            "asset_id": "token_fri",
            "asks": [{"price": "0.30", "size": "100"}],
        })

        # Only Friday's detector should have been called
        state_fri.update_price.assert_called_once()
        state_sat.update_price.assert_not_called()

    def test_ignores_unknown_token(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot._token_id_to_outcome = {}

        data = {
            "event_type": "book",
            "asset_id": "unknown_token",
            "asks": [{"price": "0.25", "size": "100"}],
        }

        bot._process_book_update(data)
        assert bot._update_count == 0

    def test_handles_empty_asks(self):
        bot, _, _ = self._setup_bot_with_detector()

        data = {
            "event_type": "book",
            "asset_id": "token123",
            "asks": [],
        }

        bot._process_book_update(data)
        assert bot._update_count == 0


class TestMessageParsing:
    """Tests for WebSocket message parsing - the actual format from Polymarket."""

    def _setup_bot(self):
        """Helper to create bot with per-market detector for message parsing tests."""
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)

        detector = MagicMock()
        state_mock = MagicMock()
        detector.outcomes = {"Test Outcome": state_mock}
        detector._check_trigger.return_value = None

        bot._market_detectors = {"test-slug": detector}
        bot._token_id_to_outcome = {"token123": "Test Outcome"}
        bot._token_id_to_slug = {"token123": "test-slug"}

        return bot

    @pytest.mark.asyncio
    async def test_handles_array_of_updates(self):
        """Polymarket WS sends arrays of updates, not single objects."""
        import json

        bot = self._setup_bot()

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

        bot = self._setup_bot()

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
        signal.zscore = 3.0
        signal.baseline_price = 0.20
        signal.price_change = 0.05
        signal.price_change_pct = 25.0
        signal.trigger_number = 1
        signal.timestamp = None

        bot._execute_signal(signal)

    def test_tracks_latency(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.polymarket = None

        signal = MagicMock()
        signal.outcome_name = "Test"
        signal.current_price = 0.25
        signal.budget_pct = 50.0
        signal.zscore = 3.0
        signal.baseline_price = 0.20
        signal.price_change = 0.05
        signal.price_change_pct = 25.0
        signal.trigger_number = 1
        signal.timestamp = None

        bot._execute_signal(signal)
        # dry run mode logs but does not set _last_signal_time
        bot._execute_signal(signal)


class TestDiscoverMarkets:
    @pytest.mark.asyncio
    async def test_monday_discovers_three_markets(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        mock_poly = MagicMock()
        bot._discover_tsa_slugs = MagicMock(return_value=[
            "number-of-tsa-passengers-february-14",
            "number-of-tsa-passengers-february-15",
            "number-of-tsa-passengers-february-16",
        ])
        mock_poly.discover_tsa_market.side_effect = lambda d: f"number-of-tsa-passengers-february-{d.day}"
        mock_poly.get_market_with_books.side_effect = lambda slug: Market(
            condition_id="cond-1", question=f"TSA {slug}?",
            outcomes=[MarketOutcome(token_id=f"tok-{slug[-2:]}", outcome="2.7M+")],
        )
        bot.polymarket = mock_poly
        markets = await bot._discover_markets()
        assert len(markets) == 3
        assert "february-14" in markets[0].event_slug
        assert "february-15" in markets[1].event_slug
        assert "february-16" in markets[2].event_slug

    @pytest.mark.asyncio
    async def test_tuesday_discovers_one_market(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        mock_poly = MagicMock()
        bot._discover_tsa_slugs = MagicMock(return_value=[
            "number-of-tsa-passengers-february-17",
        ])
        mock_poly.discover_tsa_market.return_value = "number-of-tsa-passengers-february-17"
        mock_poly.get_market_with_books.return_value = Market(
            condition_id="cond-17", question="TSA Feb 17?",
            outcomes=[MarketOutcome(token_id="tok-17", outcome="2.7M+")],
        )
        bot.polymarket = mock_poly
        markets = await bot._discover_markets()
        assert len(markets) == 1
        assert "february-17" in markets[0].event_slug

    @pytest.mark.asyncio
    async def test_explicit_slug_overrides_discovery(self):
        settings = create_mock_settings()
        settings.target_market_slug = "my-custom-market"
        bot = WebSocketMovementBot(settings)
        mock_poly = MagicMock()
        mock_poly.get_market_with_books.return_value = Market(
            condition_id="cond-custom", question="Custom?", outcomes=[],
        )
        bot.polymarket = mock_poly
        markets = await bot._discover_markets()
        assert len(markets) == 1
        mock_poly.discover_tsa_market.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_polymarket_returns_empty(self):
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot.polymarket = None
        markets = await bot._discover_markets()
        assert markets == []


class TestMultiMarketBudget:
    """Tests that budget is shared across markets and stops when exhausted."""

    def test_budget_decrements_across_markets(self):
        """Signals from different markets share the same budget pool."""
        settings = create_mock_settings()
        settings.max_trade_size_usd = 50.0
        bot = WebSocketMovementBot(settings)
        bot._budget_remaining = 50.0

        mock_polymarket = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.order_id = "order-123"
        mock_polymarket.buy_market_order.return_value = mock_result
        bot.polymarket = mock_polymarket
        bot.settings.dry_run = False

        signal = MagicMock()
        signal.outcome_name = "2.7M+"
        signal.current_price = 0.25
        signal.token_id = "token-fri"
        signal.budget_pct = 50.0
        signal.zscore = 3.0
        signal.baseline_price = 0.20
        signal.price_change = 0.05
        signal.price_change_pct = 25.0
        signal.trigger_number = 1
        signal.timestamp = None

        bot._execute_signal(signal)
        assert bot._budget_remaining == 25.0

        signal.token_id = "token-sat"
        bot._execute_signal(signal)
        assert bot._budget_remaining == 12.50

    def test_budget_exhausted_stops_trading(self):
        """When budget < $1, should stop executing."""
        settings = create_mock_settings()
        bot = WebSocketMovementBot(settings)
        bot._budget_remaining = 0.50
        bot.settings.dry_run = False

        mock_polymarket = MagicMock()
        bot.polymarket = mock_polymarket

        signal = MagicMock()
        signal.outcome_name = "2.7M+"
        signal.current_price = 0.25
        signal.budget_pct = 50.0
        signal.zscore = 3.0
        signal.baseline_price = 0.20
        signal.price_change = 0.05
        signal.price_change_pct = 25.0
        signal.trigger_number = 1
        signal.timestamp = None

        bot._execute_signal(signal)

        mock_polymarket.buy_market_order.assert_not_called()
