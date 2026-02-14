"""
Tests for MovementBot.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.movement_bot import MovementBot, setup_logging
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
    polymarket_config = MagicMock()
    polymarket_config.private_key = ""
    settings.get_polymarket_config.return_value = polymarket_config
    return settings


class TestMovementBotInit:
    def test_initialization(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        assert bot._budget_remaining == 100.0
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_initialize_without_credentials(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        await bot.initialize()
        assert bot.polymarket is None
        assert bot.detector is not None


class TestPollInterval:
    def test_in_window(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        with patch.object(bot, "_in_monitor_window", return_value=True):
            assert bot._get_poll_interval() == 1.5

    def test_outside_window(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        with patch.object(bot, "_in_monitor_window", return_value=False):
            assert bot._get_poll_interval() == 30


class TestExecuteSignal:
    def test_without_polymarket(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        bot.polymarket = None
        signal = MagicMock()
        signal.outcome_name = "Test"
        signal.current_price = 0.25
        bot._execute_signal(signal)

    def test_budget_exhausted(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        bot._budget_remaining = 0.50
        bot.polymarket = MagicMock()
        signal = MagicMock()
        signal.budget_pct = 50.0
        bot._execute_signal(signal)
        bot.polymarket.buy_market_order.assert_not_called()


class TestDiscoverMarkets:
    @pytest.mark.asyncio
    async def test_without_polymarket(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        bot.polymarket = None
        result = await bot._discover_markets()
        assert result == []


class TestBotStop:
    def test_stop(self):
        settings = create_mock_settings()
        bot = MovementBot(settings)
        bot._running = True
        bot.stop()
        assert bot._running is False
