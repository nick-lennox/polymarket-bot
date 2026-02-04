"""
Polymarket CLOB API Wrapper

Handles connection to Polymarket, reading order books, and executing trades.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

from .config import PolymarketConfig

logger = logging.getLogger(__name__)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderBookLevel:
    """Single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Order book for a market outcome."""
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
    """Represents a single outcome in a market."""
    token_id: str
    outcome: str
    order_book: Optional[OrderBook] = None


@dataclass
class Market:
    """Represents a Polymarket market with multiple outcomes."""
    condition_id: str
    question: str
    outcomes: list[MarketOutcome]


@dataclass
class TradeResult:
    """Result of a trade execution."""
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
        """Initialize connection to Polymarket CLOB."""
        if not self.config.private_key:
            raise ValueError("Private key is required for Polymarket connection")

        logger.info(f"Connecting to Polymarket CLOB at {self.config.api_url}")

        self._client = ClobClient(
            host=self.config.api_url,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            funder=self.config.funder_address,
        )

        self._api_creds = self._client.derive_api_creds()
        self._client.set_api_creds(self._api_creds)

        logger.info("Successfully connected to Polymarket")

    @property
    def client(self) -> ClobClient:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    def get_market(self, condition_id: str) -> Optional[Market]:
        """Fetch market details by condition ID."""
        try:
            market_data = self.client.get_market(condition_id)
            if not market_data:
                return None

            outcomes = []
            tokens = market_data.get("tokens", [])

            for token in tokens:
                outcomes.append(MarketOutcome(
                    token_id=token.get("token_id", ""),
                    outcome=token.get("outcome", ""),
                ))

            return Market(
                condition_id=condition_id,
                question=market_data.get("question", ""),
                outcomes=outcomes,
            )
        except Exception as e:
            logger.error(f"Failed to get market {condition_id}: {e}")
            return None

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch order book for a specific outcome token."""
        try:
            book_data = self.client.get_order_book(token_id)

            bids = [
                OrderBookLevel(
                    price=float(level.get("price", 0)),
                    size=float(level.get("size", 0)),
                )
                for level in book_data.get("bids", [])
            ]

            asks = [
                OrderBookLevel(
                    price=float(level.get("price", 0)),
                    size=float(level.get("size", 0)),
                )
                for level in book_data.get("asks", [])
            ]

            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)

            return OrderBook(token_id=token_id, bids=bids, asks=asks)
        except Exception as e:
            logger.error(f"Failed to get order book for {token_id}: {e}")
            return None

    def get_market_with_books(self, condition_id: str) -> Optional[Market]:
        """Fetch market with order books for all outcomes."""
        market = self.get_market(condition_id)
        if not market:
            return None

        for outcome in market.outcomes:
            outcome.order_book = self.get_order_book(outcome.token_id)

        return market

    def buy_market_order(
        self,
        token_id: str,
        amount_usd: float,
        dry_run: bool = True,
    ) -> TradeResult:
        """Execute a market buy order."""
        if dry_run:
            logger.info(f"[DRY RUN] Would BUY ${amount_usd} of token {token_id}")
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

    def get_balance(self) -> float:
        """Get USDC balance."""
        try:
            balance = self.client.get_balance()
            return float(balance)
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0
