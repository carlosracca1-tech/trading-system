"""
Central configuration for the trading system.
All settings are loaded from environment variables / .env file.
Validation happens at startup — invalid config fails fast.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PAPER = "paper"
    LIVE = "live"


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Trading System API"
    app_version: str = "0.1.0"
    trading_mode: TradingMode = TradingMode.DEV
    debug: bool = False
    dry_run: bool = True  # DEFAULT TRUE — safety guard

    # ── API ──────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = Field(default="dev-key-change-before-paper")

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql://trading:trading_dev_pass@localhost:5433/trading_dev"
    )
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout: int = 30

    # ── Broker — Alpaca ──────────────────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"

    # ── Data — Polygon.io ────────────────────────────────────────────────────
    polygon_api_key: str = ""
    polygon_paid_tier: bool = False  # True = no rate-limit sleep between requests

    # ── Alerting — Telegram ──────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE  # console for dev readability

    # ── Sentry ───────────────────────────────────────────────────────────────
    sentry_dsn: str = ""

    # ── Derived properties ───────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def is_paper_or_live(self) -> bool:
        return self.trading_mode in (TradingMode.PAPER, TradingMode.LIVE)

    @property
    def is_dev(self) -> bool:
        return self.trading_mode == TradingMode.DEV

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got '{v}'")
        return upper

    @model_validator(mode="after")
    def validate_live_requirements(self) -> "Settings":
        """
        Critical safety check: if TRADING_MODE=live, the broker URL must
        point to the real Alpaca API, not the paper endpoint.
        This prevents accidentally trading real money on the paper account.
        """
        if self.trading_mode == TradingMode.LIVE:
            if "paper-api" in self.alpaca_base_url:
                raise ValueError(
                    "CRITICAL: TRADING_MODE=live but ALPACA_BASE_URL points to the "
                    "paper endpoint. Set ALPACA_BASE_URL=https://api.alpaca.markets/v2"
                )
            if not self.alpaca_api_key or not self.alpaca_secret_key:
                raise ValueError(
                    "TRADING_MODE=live requires ALPACA_API_KEY and ALPACA_SECRET_KEY"
                )
            if self.dry_run:
                raise ValueError(
                    "TRADING_MODE=live requires DRY_RUN=false. "
                    "Explicitly set DRY_RUN=false to confirm live trading."
                )
        return self

    @model_validator(mode="after")
    def validate_paper_requirements(self) -> "Settings":
        if self.trading_mode == TradingMode.PAPER:
            if not self.alpaca_api_key or not self.alpaca_secret_key:
                raise ValueError(
                    "TRADING_MODE=paper requires ALPACA_API_KEY and ALPACA_SECRET_KEY"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """
    Returns cached Settings instance.
    Called once at startup; subsequent calls return the same object.
    Use get_settings.cache_clear() in tests to reset between test cases.
    """
    return Settings()
