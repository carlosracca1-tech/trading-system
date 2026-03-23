"""
apps/api/schemas.py
Pydantic v2 response schemas for all API routers.

Naming convention:
  <Model>Out  — response schema (what the API returns)
  <Model>In   — request body schema (what the API accepts)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Base ──────────────────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── TradingRun ────────────────────────────────────────────────────────────────

class RunOut(_Base):
    id: str
    run_type: str
    status: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    initial_capital: float
    final_capital: Optional[float] = None
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    notes: Optional[str] = None


class RunCreateIn(BaseModel):
    run_type: str = "PAPER"
    initial_capital: float = Field(default=100_000.0, gt=0)
    notes: Optional[str] = None


class RunListOut(_Base):
    runs: list[RunOut]
    total: int


# ── Position ──────────────────────────────────────────────────────────────────

class PositionOut(_Base):
    id: str
    run_id: str
    symbol: str
    status: str
    direction: str
    qty: Any          # stored as String in DB — coerce to int in validator
    entry_price: float
    exit_price: Optional[float] = None
    stop_loss: float
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: Optional[float] = None
    opened_at: datetime
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None

    @property
    def qty_int(self) -> int:
        return int(self.qty)


class PositionListOut(_Base):
    positions: list[PositionOut]
    total: int


# ── Portfolio snapshot ────────────────────────────────────────────────────────

class SnapshotOut(_Base):
    id: str
    run_id: str
    snapshot_type: str
    snapshot_at: datetime
    cash: float
    positions_value: float
    total_equity: float
    open_positions_count: int
    peak_equity: float
    drawdown_pct: float
    daily_pnl: Optional[float] = None
    cumulative_return_pct: float


class PortfolioOut(BaseModel):
    """Aggregated portfolio view — current snapshot + open positions."""
    run_id: str
    run_status: str
    snapshot: Optional[SnapshotOut] = None
    open_positions: list[PositionOut]


# ── Signal ────────────────────────────────────────────────────────────────────

class SignalOut(_Base):
    id: str
    run_id: str
    symbol: str
    signal_date: date
    signal_type: str
    direction: str
    close_price: float
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    rsi_14: Optional[float] = None
    atr_14: Optional[float] = None
    volume_ratio: Optional[float] = None
    regime_ok: Optional[bool] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    position_size_shares: Optional[str] = None
    risk_decision: str
    risk_rejection_reason: Optional[str] = None
    created_at: datetime


class SignalListOut(BaseModel):
    signals: list[SignalOut]
    total: int


# ── Order ─────────────────────────────────────────────────────────────────────

class OrderOut(_Base):
    id: str
    run_id: str
    symbol: str
    side: str
    order_type: str
    qty: int
    status: str
    filled_price: Optional[float] = None
    filled_qty: int = 0
    stop_price: Optional[float] = None
    submitted_price: Optional[float] = None
    broker_order_id: Optional[str] = None
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime


class OrderListOut(BaseModel):
    orders: list[OrderOut]
    total: int


# ── System state ──────────────────────────────────────────────────────────────

class SystemStateOut(BaseModel):
    status: str                          # "operational" | "kill_switch_active" | "no_active_run"
    run_id: Optional[str] = None
    run_status: Optional[str] = None
    kill_switch_active: bool = False
    drawdown_pct: Optional[float] = None
    peak_equity: Optional[float] = None
    total_equity: Optional[float] = None
    open_positions: int = 0
    message: str = ""


class KillSwitchActivateIn(BaseModel):
    run_id: str
    reason: str = "manual_api_activation"


class KillSwitchResolveIn(BaseModel):
    run_id: str
    resolved_by: str = "api"


class KillSwitchOut(BaseModel):
    activated: bool
    run_id: str
    reason: str
    message: str


# ── Risk event ────────────────────────────────────────────────────────────────

class RiskEventOut(_Base):
    id: str
    run_id: Optional[str] = None
    rule_code: str
    rule_priority: str
    decision: str
    symbol: Optional[str] = None
    rejection_reason: Optional[str] = None
    triggered_at: datetime


class RiskEventListOut(BaseModel):
    events: list[RiskEventOut]
    total: int
