"""
Polymarket CLOB API Wrapper

Handles connection to Polymarket, reading order books, and executing trades.
Uses Gamma API for market discovery and CLOB API for order execution.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from enum import Enum

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

from .config import PolymarketConfig

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


@dataclass
class MarketOutcome:
    token_id: str
    outcome: str
    condition_id: str = ""
    group_item_title: str = ""
    no_token_id: str = ""
    order_book: Optional[OrderBook] = None
    no_order_book: Optional[OrderBook] = None


@dataclass
class Market:
    condition_id: str
    question: str
    outcomes: list[MarketOutcome]
    neg_risk_market_id: str = ""
    event_slug: str = ""


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    filled_price: float = 0.0
    error: Optional[str] = None


class PolymarketClient:
    """Client for interacting with Polymarket CLOB API."""

    def __init__(self, config: PolymarketConfig):
        self.config = config
        self._client: Optional[ClobClient] = None
        self._api_creds = None

    def connect(self):
        if not self.config.private_key:
            raise ValueError("Private key is required")

        logger.info(f"Connecting to Polymarket CLOB at {self.config.api_url}")

        self._client = ClobClient(
            host=self.config.api_url,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            funder=self.config.funder_address,
        )

        self._api_creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(self._api_creds)

        logger.info("Successfully connected to Polymarket")

    @property
    def client(self) -> ClobClient:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    def get_market_by_slug(self, event_slug: str) -> Optional[Market]:
        """Fetch market details from Gamma API by event slug."""
        try:
            resp = httpx.get(
                f"{GAMMA_API_URL}/events",
                params={"slug": event_slug},
                timeout=15.0,
            )
            resp.raise_for_status()
            events = resp.json()

            if not events:
                logger.error(f"No event found for slug: {event_slug}")
                return None

            event = events[0]
            sub_markets = event.get("markets", [])
            neg_risk_market_id = event.get("negRiskMarketID", "")

            logger.info(f"Found event: {event.get('title', '')}")
            logger.info(f"Neg-risk market ID: {neg_risk_market_id}")
            logger.info(f"Sub-markets: {len(sub_markets)}")

            outcomes = []
            for sm in sub_markets:
                if not sm.get("active", False) and sm.get("groupItemTitle") != "Other":
                    continue

                clob_token_ids_raw = sm.get("clobTokenIds", "[]")
                try:
                    clob_token_ids = json.loads(clob_token_ids_raw)
                except (json.JSONDecodeError, TypeError):
                    clob_token_ids = []

                yes_token_id = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
                no_token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else ""
                group_title = sm.get("groupItemTitle", "")
                question = sm.get("question", "")

                outcomes.append(MarketOutcome(
                    token_id=yes_token_id,
                    outcome=group_title or question,
                    condition_id=sm.get("conditionId", ""),
                    group_item_title=group_title,
                    no_token_id=no_token_id,
                ))

                price = sm.get("outcomePrices", "")
                logger.info(f"  {group_title}: YES_token={yes_token_id[:15]}... price={price}")

            return Market(
                condition_id=neg_risk_market_id or event.get("id", ""),
                question=event.get("title", ""),
                outcomes=outcomes,
                neg_risk_market_id=neg_risk_market_id,
                event_slug=event_slug,
            )
        except Exception as e:
            logger.error(f"Failed to get market by slug {event_slug}: {e}")
            return None

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        try:
            book_data = self.client.get_order_book(token_id)

            bids = [
                OrderBookLevel(
                    price=float(level.price),
                    size=float(level.size),
                )
                for level in book_data.bids
            ]

            asks = [
                OrderBookLevel(
                    price=float(level.price),
                    size=float(level.size),
                )
                for level in book_data.asks
            ]

            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)

            return OrderBook(token_id=token_id, bids=bids, asks=asks)
        except Exception as e:
            error_str = str(e)
            # Don't spam logs for 404s (market resolved/no orderbook)
            if "404" in error_str or "No orderbook exists" in error_str:
                logger.debug(f"No order book for {token_id[:15]}... (likely resolved)")
            else:
                logger.error(f"Failed to get order book for {token_id[:15]}...: {e}")
            return None

    def get_market_with_books(self, event_slug: str) -> Optional[Market]:
        """Fetch market via Gamma API with order books for all outcomes."""
        market = self.get_market_by_slug(event_slug)
        if not market:
            return None

        for outcome in market.outcomes:
            if outcome.token_id:
                outcome.order_book = self.get_order_book(outcome.token_id)
            if outcome.no_token_id:
                outcome.no_order_book = self.get_order_book(outcome.no_token_id)

        return market

    def buy_market_order(self, token_id, amount_usd, dry_run=True):
        if dry_run:
            logger.info(f"[DRY RUN] Would BUY ${amount_usd} of token {token_id[:15]}...")
            return TradeResult(success=True, order_id="dry-run")

        try:
            order = self.client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount_usd)
            )
            response = self.client.post_order(order, OrderType.FOK)
            order_id = response.get("orderID", "")
            logger.info(f"Market buy order submitted: {order_id}")
            return TradeResult(
                success=True,
                order_id=order_id,
                filled_size=float(response.get("matchedAmount", 0)),
                filled_price=float(response.get("matchedPrice", 0)),
            )
        except Exception as e:
            logger.error(f"Market buy failed: {e}")
            return TradeResult(success=False, error=str(e))

    def discover_tsa_market(self, target_date=None):
        """Build the TSA passenger count market slug for a given date.

        Slug pattern is: number-of-tsa-passengers-{month}-{day}
        Verifies the market exists on Gamma API before returning.

        NOTE: TSA releases YESTERDAY's data each morning. So at 7 AM on Feb 12,
        we want to trade the Feb 11 market, not Feb 12.
        """
        from datetime import date as _date, timedelta
        import pytz

        if target_date is None:
            # Use ET date, not UTC - TSA markets are based on ET
            et_tz = pytz.timezone("America/New_York")
            today_et = datetime.now(et_tz).date()

            # TSA releases data Mon-Fri by 9 AM ET
            # On Monday, they release Fri + Sat + Sun data
            # Use discover_tsa_markets() (plural) for Monday to get all 3
            target_date = today_et - timedelta(days=1)
            logger.info(f"Trading yesterday's market: {target_date.strftime('%A %B %d')}")

        month_name = target_date.strftime('%B').lower()
        day = target_date.day
        slug = f"number-of-tsa-passengers-{month_name}-{day}"
        expected_title_part = f"{target_date.strftime('%B')} {day}"

        try:
            resp = httpx.get(
                f'{GAMMA_API_URL}/events',
                params={'slug': slug},
                timeout=15.0,
            )
            resp.raise_for_status()
            events = resp.json()

            if events:
                event_title = events[0].get("title", "")
                # Verify the returned market matches expected date
                if expected_title_part not in event_title:
                    logger.error(f"Market date mismatch! Expected '{expected_title_part}' but got '{event_title}'")
                    logger.error(f"This may indicate the market for today doesn't exist yet")
                    return None
                logger.info(f"Verified TSA market exists: {slug} ({event_title})")
                return slug

            logger.warning(f"TSA market not found for slug: {slug}")
            return None

        except Exception as e:
            logger.error(f"Failed to verify TSA market {slug}: {e}")
            return None

    def discover_tsa_markets(self) -> list[str]:
        """Discover all TSA markets to trade today.

        TSA releases data Mon-Fri by 9 AM ET:
        - Tue-Fri: releases yesterday's data → 1 market
        - Monday: releases Fri + Sat + Sun data → 3 markets
        - Sat/Sun: no new data released → 0 markets (but could still trade if markets exist)

        Returns list of market slugs.
        """
        from datetime import timedelta
        import pytz

        et_tz = pytz.timezone("America/New_York")
        today_et = datetime.now(et_tz).date()
        weekday = today_et.weekday()  # Monday=0, Sunday=6

        markets = []

        if weekday == 0:  # Monday - TSA releases Fri, Sat, Sun
            logger.info("Monday: TSA releases Friday, Saturday, and Sunday data")
            for days_back in [3, 2, 1]:  # Fri, Sat, Sun
                target_date = today_et - timedelta(days=days_back)
                slug = self.discover_tsa_market(target_date=target_date)
                if slug:
                    markets.append(slug)
        elif weekday in [5, 6]:  # Sat/Sun - no TSA release, but try yesterday's market
            logger.info(f"Weekend: no TSA release, trying yesterday's market")
            slug = self.discover_tsa_market()  # Uses yesterday by default
            if slug:
                markets.append(slug)
        else:  # Tue-Fri - normal case, yesterday's data
            slug = self.discover_tsa_market()  # Uses yesterday by default
            if slug:
                markets.append(slug)

        logger.info(f"Found {len(markets)} market(s) to trade: {markets}")
        return markets

    def get_balance_info(self) -> dict:
        try:
            return self.client.get_balance_allowance()
        except Exception as e:
            logger.error(f"Failed to get balance info: {e}")
            return {}
