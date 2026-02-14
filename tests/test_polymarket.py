"""
Tests for the Polymarket module - market discovery and API interactions.
"""

import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
import pytz

from src.polymarket import PolymarketClient


ET_TIMEZONE = pytz.timezone("America/New_York")


class TestDiscoverTsaMarket:
    """Tests for TSA market discovery logic.

    CRITICAL: TSA releases YESTERDAY's passenger data each morning.
    At 7 AM on Feb 12, we trade the Feb 11 market, not Feb 12.
    """

    def test_discovers_yesterdays_market_not_todays(self):
        """
        This is the key test that would have caught the wrong-day bug.

        On Feb 12 at 7 AM ET, we should be looking for:
        - number-of-tsa-passengers-february-11 (CORRECT)
        - NOT number-of-tsa-passengers-february-12 (WRONG)
        """
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Mock the current time as Feb 12, 7:00 AM ET
        feb_12_7am_et = datetime(2026, 2, 12, 7, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = feb_12_7am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 11?",
                    "slug": "number-of-tsa-passengers-february-11"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                # Should request YESTERDAY's market (Feb 11), not today's (Feb 12)
                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                assert params['slug'] == 'number-of-tsa-passengers-february-11', \
                    f"Expected february-11 but got {params['slug']}"
                assert slug == 'number-of-tsa-passengers-february-11'

    def test_uses_et_timezone_not_utc(self):
        """
        At 11 PM UTC on Feb 11 = 6 PM ET on Feb 11.
        Should still be looking at Feb 10's market (yesterday in ET).
        """
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # 11 PM UTC on Feb 11 = 6 PM ET on Feb 11
        # Yesterday in ET is Feb 10
        feb_11_11pm_utc = datetime(2026, 2, 11, 23, 0, 0, tzinfo=pytz.UTC)
        feb_11_6pm_et = feb_11_11pm_utc.astimezone(ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = feb_11_6pm_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 10?",
                    "slug": "number-of-tsa-passengers-february-10"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                # Should be Feb 10 (yesterday in ET), not Feb 11
                assert 'february-10' in params['slug'], \
                    f"Expected february-10 but got {params['slug']}"

    def test_explicit_date_overrides_auto_discovery(self):
        """When a specific date is passed, use that date directly."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        with patch('src.polymarket.httpx.get') as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = [{
                "title": "Number of TSA Passengers February 15?",
                "slug": "number-of-tsa-passengers-february-15"
            }]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            # Explicitly pass Feb 15
            slug = client.discover_tsa_market(target_date=date(2026, 2, 15))

            call_args = mock_get.call_args
            params = call_args.kwargs.get('params') or call_args[1].get('params')

            assert params['slug'] == 'number-of-tsa-passengers-february-15'

    def test_rejects_market_with_wrong_date_in_title(self):
        """
        If the API returns a market with the wrong date in the title,
        we should reject it (prevents trading resolved markets).
        """
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        with patch('src.polymarket.httpx.get') as mock_get:
            mock_response = MagicMock()
            # API returns Feb 4 market when we asked for Feb 11
            mock_response.json.return_value = [{
                "title": "Number of TSA Passengers February 4?",
                "slug": "number-of-tsa-passengers-february-4"
            }]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            # Ask for Feb 11
            slug = client.discover_tsa_market(target_date=date(2026, 2, 11))

            # Should return None because title doesn't match
            assert slug is None, "Should reject market with wrong date in title"

    def test_handles_month_boundary(self):
        """On March 1, yesterday is Feb 28."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # March 1, 2026 at 8 AM ET - yesterday is Feb 28
        march_1_8am_et = datetime(2026, 3, 1, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = march_1_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 28?",
                    "slug": "number-of-tsa-passengers-february-28"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                # March 1 - 1 day = Feb 28
                assert params['slug'] == 'number-of-tsa-passengers-february-28'

    def test_monday_trades_sundays_market(self):
        """On Monday, trade yesterday's (Sunday's) market."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Monday Feb 16, 2026 at 8 AM ET
        monday_8am_et = datetime(2026, 2, 16, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = monday_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 15?",
                    "slug": "number-of-tsa-passengers-february-15"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                # Monday Feb 16 - 1 day = Sunday Feb 15
                assert params['slug'] == 'number-of-tsa-passengers-february-15', \
                    f"On Monday, expected Sunday's market (february-15) but got {params['slug']}"

    def test_saturday_trades_fridays_market(self):
        """On Saturday, trade yesterday's (Friday's) market."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Saturday Feb 14, 2026 at 8 AM ET
        saturday_8am_et = datetime(2026, 2, 14, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = saturday_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 13?",
                    "slug": "number-of-tsa-passengers-february-13"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                # Saturday Feb 14 - 1 day = Friday Feb 13
                assert params['slug'] == 'number-of-tsa-passengers-february-13'

    def test_sunday_trades_saturdays_market(self):
        """On Sunday, trade yesterday's (Saturday's) market."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Sunday Feb 15, 2026 at 8 AM ET
        sunday_8am_et = datetime(2026, 2, 15, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = sunday_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 14?",
                    "slug": "number-of-tsa-passengers-february-14"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                slug = client.discover_tsa_market()

                call_args = mock_get.call_args
                params = call_args.kwargs.get('params') or call_args[1].get('params')

                # Sunday Feb 15 - 1 day = Saturday Feb 14
                assert params['slug'] == 'number-of-tsa-passengers-february-14'


class TestDiscoverTsaMarkets:
    """Tests for discover_tsa_markets() - plural, returns multiple markets on Monday."""

    def test_monday_returns_three_markets(self):
        """On Monday, TSA releases Fri + Sat + Sun data → 3 markets."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Monday Feb 16, 2026 at 8 AM ET
        monday_8am_et = datetime(2026, 2, 16, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = monday_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                def mock_response_for_slug(url, params, timeout):
                    slug = params['slug']
                    day = slug.split('-')[-1]
                    response = MagicMock()
                    response.json.return_value = [{
                        "title": f"Number of TSA Passengers February {day}?",
                        "slug": slug
                    }]
                    response.raise_for_status = MagicMock()
                    return response

                mock_get.side_effect = mock_response_for_slug

                markets = client.discover_tsa_markets()

                # Monday should return Fri (13), Sat (14), Sun (15)
                assert len(markets) == 3
                assert 'february-13' in markets[0]  # Friday
                assert 'february-14' in markets[1]  # Saturday
                assert 'february-15' in markets[2]  # Sunday

    def test_tuesday_returns_one_market(self):
        """On Tuesday, TSA releases Monday's data → 1 market."""
        config = MagicMock()
        config.api_url = "https://clob.polymarket.com"
        config.private_key = None

        client = PolymarketClient(config)

        # Tuesday Feb 17, 2026 at 8 AM ET
        tuesday_8am_et = datetime(2026, 2, 17, 8, 0, 0, tzinfo=ET_TIMEZONE)

        with patch('src.polymarket.datetime') as mock_datetime:
            mock_datetime.now.return_value = tuesday_8am_et
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with patch('src.polymarket.httpx.get') as mock_get:
                mock_response = MagicMock()
                mock_response.json.return_value = [{
                    "title": "Number of TSA Passengers February 16?",
                    "slug": "number-of-tsa-passengers-february-16"
                }]
                mock_response.raise_for_status = MagicMock()
                mock_get.return_value = mock_response

                markets = client.discover_tsa_markets()

                assert len(markets) == 1
                assert markets[0] == 'number-of-tsa-passengers-february-16'
