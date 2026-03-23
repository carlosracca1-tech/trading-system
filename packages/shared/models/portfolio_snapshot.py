"""
packages/shared/models/portfolio_snapshot.py
PortfolioSnapshot — point-in-time portfolio state.
TimescaleDB hypertable on snapshot_at.
Taken hourly during market hours + at open/close.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, PrimaryKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import SnapshotType
from packages.shared.models.base import Base


class PortfolioSnapshot(Base):
    """
    NOTE: PK is composite (id, snapshot_at) — required by TimescaleDB.
    """
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("id", "snapshot_at"),
        {"comment": "Portfolio state snapshots — TimescaleDB hypertable"},
    )

    id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    snapshot_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SnapshotType.DAILY_CLOSE.value,
    )

    # Time dimension (hypertable partition key — part of composite PK)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    # Portfolio valuation
    cash: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    positions_value: Mapped[float] = mapped_column(
        Numeric(15, 2), nullable=False,
        comment="Mark-to-market value of all open positions",
    )
    total_equity: Mapped[float] = mapped_column(
        Numeric(15, 2), nullable=False,
        comment="cash + positions_value",
    )
    open_positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Drawdown tracking
    peak_equity: Mapped[float] = mapped_column(
        Numeric(15, 2), nullable=False,
        comment="Running all-time high equity for drawdown calculation",
    )
    drawdown_pct: Mapped[float] = mapped_column(
        Numeric(8, 4), nullable=False,
        comment="Current drawdown from peak: (peak - equity) / peak",
    )

    # P&L
    daily_pnl: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    cumulative_return_pct: Mapped[float] = mapped_column(
        Numeric(8, 4), nullable=False,
        comment="(total_equity - initial_capital) / initial_capital",
    )

    # Full position detail for reconstruction (JSON)
    positions_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON list of open positions at snapshot time",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioSnapshot {self.snapshot_at} "
            f"equity={self.total_equity} dd={self.drawdown_pct:.2%}>"
        )
