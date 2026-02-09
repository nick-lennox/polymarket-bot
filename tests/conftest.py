"""
Pytest configuration and fixtures.
"""
import pytest
import sys
from pathlib import Path

# Add src to path for imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path.parent))

# Configure pytest-asyncio
pytest_plugins = ["pytest_asyncio"]


@pytest.fixture
def mock_settings():
    from unittest.mock import MagicMock
    from src.config import Settings

    settings = MagicMock(spec=Settings)
    settings.max_trade_size_usd = 50.0
    settings.max_buy_price = 0.95
    settings.min_edge = 0.05
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


@pytest.fixture
def mock_outcome():
    from unittest.mock import MagicMock

    def _create(name="Test", token_id="token1", price=0.10):
        outcome = MagicMock()
        outcome.outcome = name
        outcome.token_id = token_id
        outcome.no_token_id = f"no_{token_id}"
        outcome.order_book = MagicMock()
        outcome.order_book.asks = [MagicMock(price=price)]
        return outcome

    return _create
