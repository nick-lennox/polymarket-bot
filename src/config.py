"""
Configuration management for the TSA Polymarket Trading Bot.
"""

import os
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class TradingConfig(BaseModel):
    """Trading risk parameters."""
    max_trade_size_usd: float = Field(default=50.0, ge=0.0)
    max_buy_price: float = Field(default=0.95, ge=0.0, le=1.0)
    min_edge: float = Field(default=0.05, ge=0.0, le=1.0)
    dry_run: bool = Field(default=True)


class PolymarketConfig(BaseModel):
    """Polymarket API configuration."""
    private_key: str = Field(default="")
    funder_address: Optional[str] = Field(default=None)
    api_url: str = Field(default="https://clob.polymarket.com")
    chain_id: int = Field(default=137)
    target_market_slug: str = Field(default="")

    @field_validator("private_key")
    @classmethod
    def validate_private_key(cls, v: str) -> str:
        if v and not v.startswith("0x"):
            return f"0x{v}"
        return v


class ScraperConfig(BaseModel):
    """TSA scraper configuration."""
    poll_interval_seconds: int = Field(default=30, ge=5)
    timeout_seconds: float = Field(default=30.0, ge=1.0)


class Settings(BaseSettings):
    """Main application settings."""
    polymarket_private_key: str = Field(default="", alias="POLYMARKET_PRIVATE_KEY")
    polymarket_funder: Optional[str] = Field(default=None, alias="POLYMARKET_FUNDER")
    polymarket_api_url: str = Field(default="https://clob.polymarket.com", alias="POLYMARKET_API_URL")
    target_market_slug: str = Field(default="", alias="TARGET_MARKET_SLUG")
    max_trade_size_usd: float = Field(default=50.0, alias="MAX_TRADE_SIZE_USD")
    max_buy_price: float = Field(default=0.95, alias="MAX_BUY_PRICE")
    min_edge: float = Field(default=0.05, alias="MIN_EDGE")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    poll_interval_seconds: int = Field(default=30, alias="POLL_INTERVAL_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    
    # Movement detection settings
    zscore_threshold: float = Field(default=2.5, alias="ZSCORE_THRESHOLD")
    scale_in_pcts: str = Field(default="50,30,20", alias="SCALE_IN_PCTS")
    min_price_change: float = Field(default=0.05, alias="MIN_PRICE_CHANGE")
    monitor_window_start_hour: int = Field(default=7, alias="MONITOR_WINDOW_START_HOUR")
    monitor_window_end_hour: int = Field(default=10, alias="MONITOR_WINDOW_END_HOUR")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_trading_config(self) -> TradingConfig:
        return TradingConfig(
            max_trade_size_usd=self.max_trade_size_usd,
            max_buy_price=self.max_buy_price,
            min_edge=self.min_edge,
            dry_run=self.dry_run,
        )

    def get_polymarket_config(self) -> PolymarketConfig:
        return PolymarketConfig(
            private_key=self.polymarket_private_key,
            funder_address=self.polymarket_funder,
            api_url=self.polymarket_api_url,
            target_market_slug=self.target_market_slug,
        )

    def get_scraper_config(self) -> ScraperConfig:
        return ScraperConfig(poll_interval_seconds=self.poll_interval_seconds)


def load_settings() -> Settings:
    """Load settings from environment and .env file."""
    return Settings()


def print_config(settings: Settings, hide_secrets: bool = True):
    """Print current configuration."""
    print("Current Configuration:")
    print("=" * 50)
    pk = settings.polymarket_private_key
    if hide_secrets and pk:
        pk = f"{pk[:6]}...{pk[-4:]}" if len(pk) > 10 else "****"
    print(f"Polymarket API URL: {settings.polymarket_api_url}")
    print(f"Private Key: {pk or '(not set)'}")
    print(f"Funder Address: {settings.polymarket_funder or '(not set)'}")
    print(f"Target Market: {settings.target_market_slug or '(not set)'}")
    print()
    print(f"Max Trade Size: ${settings.max_trade_size_usd}")
    print(f"Max Buy Price: {settings.max_buy_price}")
    print(f"Min Edge: {settings.min_edge}")
    print(f"Dry Run: {settings.dry_run}")
    print()
    print(f"Poll Interval: {settings.poll_interval_seconds}s")
    print(f"Log Level: {settings.log_level}")
    print()
    print(f"Z-Score Threshold: {settings.zscore_threshold}")
    print(f"Scale-In Percentages: {settings.scale_in_pcts}")
    print(f"Min Price Change: {settings.min_price_change}")
    print(f"Monitor Window: {settings.monitor_window_start_hour}:00 - {settings.monitor_window_end_hour}:00 ET")
    print("=" * 50)


if __name__ == "__main__":
    settings = load_settings()
    print_config(settings)
