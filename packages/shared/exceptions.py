"""
Domain exceptions for the trading system.
All exceptions include a human-readable message and optional context dict.
Rule: always raise with context, never with bare strings when data is available.

Usage:
    raise DataFreshnessError("SPY data is 26h old", asset="SPY", age_hours=26)
    raise RiskError("Daily risk limit reached", daily_loss_pct=0.031)
"""
from __future__ import annotations

from typing import Any


class TradingSystemError(Exception):
    """Base exception for all trading system errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __repr__(self) -> str:
        ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.__class__.__name__}({self.message!r}" + (f", {ctx}" if ctx else "") + ")"


# ── Data errors ───────────────────────────────────────────────────────────────
class DataError(TradingSystemError):
    """Base for data-related errors."""


class DataFreshnessError(DataError):
    """Market data is too old to generate valid signals."""


class DataValidationError(DataError):
    """OHLCV data failed validation (OHLC inconsistency, negative prices, etc.)."""


class DataUnavailableError(DataError):
    """Required data is missing (e.g., no data for a given symbol/date)."""


# ── Risk errors ───────────────────────────────────────────────────────────────
class RiskError(TradingSystemError):
    """Base for risk management errors."""


class KillSwitchError(RiskError):
    """Kill switch is active — all operations are blocked."""


class DrawdownLimitError(RiskError):
    """Portfolio drawdown has reached the maximum allowed threshold."""


class DailyRiskLimitError(RiskError):
    """Daily risk limit has been reached."""


class PositionLimitError(RiskError):
    """Maximum number of simultaneous positions reached."""


# ── Execution errors ──────────────────────────────────────────────────────────
class ExecutionError(TradingSystemError):
    """Base for order execution errors."""


class DuplicateOrderError(ExecutionError):
    """An active order already exists for this asset."""


class BrokerConnectionError(ExecutionError):
    """Cannot reach the broker API."""


class BrokerRejectionError(ExecutionError):
    """Broker rejected the order (e.g., insufficient funds)."""


class PartialFillError(ExecutionError):
    """Order was partially filled below the acceptable threshold."""


class OrderTimeoutError(ExecutionError):
    """Order confirmation timed out; state is UNKNOWN."""


# ── Reconciliation errors ─────────────────────────────────────────────────────
class ReconciliationError(TradingSystemError):
    """Base for reconciliation errors."""


class DivergenceError(ReconciliationError):
    """Local position state diverges from broker state."""


# ── Configuration errors ──────────────────────────────────────────────────────
class ConfigurationError(TradingSystemError):
    """System configuration is invalid or inconsistent."""
