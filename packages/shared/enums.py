"""
All system enums. Single source of truth.
These are used in both SQLAlchemy models and Pydantic schemas.
"""
from __future__ import annotations

from enum import Enum


class AssetType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"


class DataQuality(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    SUSPECT = "suspect"


class SignalType(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Direction(str, Enum):
    LONG = "long"
    # SHORT is prohibited in V1 — not defined to prevent accidental use


class RunType(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RiskDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    PENDING = "PENDING"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    FORCED_CLOSED = "forced_closed"


class SystemState(str, Enum):
    RUNNING = "running"
    WARNING = "warning"
    PAUSED = "paused"
    STOPPED = "stopped"


class KillSwitchTrigger(str, Enum):
    MANUAL = "manual"
    DRAWDOWN_LIMIT = "drawdown_limit"
    LOSS_STREAK = "loss_streak"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    DATA_FAILURE = "data_failure"
    BROKER_FAILURE = "broker_failure"
    EXECUTION_ERROR = "execution_error"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SnapshotType(str, Enum):
    HOURLY = "hourly"
    DAILY_OPEN = "daily_open"
    DAILY_CLOSE = "daily_close"
    MANUAL = "manual"
