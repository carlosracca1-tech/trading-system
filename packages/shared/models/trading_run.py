"""
packages/shared/models/trading_run.py
TradingRun — one execution context (backtest / paper / live).
All signals, orders, positions, and snapshots belong to a run.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import RunStatus, RunType
from packages.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TradingRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trading_runs"
    __table_args__ = {"comment": "One execution context (backtest / paper / live)"}

    run_type: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="RunType enum: BACKTEST | PAPER | LIVE",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RunStatus.RUNNING.value,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    initial_capital: Mapped[float] = mapped_column(
        Numeric(15, 2), nullable=False,
        comment="Starting capital in USD",
    )
    final_capital: Mapped[float | None] = mapped_column(
        Numeric(15, 2), nullable=True,
    )

    # Summary stats (populated when run completes)
    total_return_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Frozen copy of strategy config at run start (JSON)
    config_snapshot: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON snapshot of STRATEGY_PARAMS + RISK_PARAMS at run time",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<TradingRun {self.run_type} {self.status} started={self.started_at}>"

    @property
    def win_rate(self) -> float | None:
        if self.total_trades == 0:
            return None
        return self.winning_trades / self.total_trades

    @property
    def run_type_enum(self) -> RunType:
        return RunType(self.run_type)

    @property
    def status_enum(self) -> RunStatus:
        return RunStatus(self.status)
