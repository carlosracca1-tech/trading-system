"""
packages/shared/models/position.py
Position — one open or closed trade.
Opened when an entry order fills, closed when an exit order fills or kill switch fires.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import Direction, PositionStatus
from packages.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Position(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = {"comment": "Open and closed trade records"}

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PositionStatus.OPEN.value, index=True,
    )
    direction: Mapped[str] = mapped_column(
        String(10), nullable=False, default=Direction.LONG.value,
    )

    # Order references
    entry_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    exit_order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Trade details
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    stop_loss: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    # Partial take profit tracking
    partial_tp_taken: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True after 50% of position was sold at +3% profit",
    )
    initial_qty: Mapped[int] = mapped_column(
        Integer, nullable=True,
        comment="Original qty at entry (before partial TP reduces qty)",
    )

    # Mark-to-market (updated by reconciliation service)
    current_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    commission_total: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)

    # Lifecycle
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True,
        comment="e.g. signal_exit | stop_loss | kill_switch | manual",
    )

    def __repr__(self) -> str:
        return (
            f"<Position {self.symbol} {self.status} "
            f"entry={self.entry_price} pnl={self.realized_pnl}>"
        )

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN.value

    @property
    def net_pnl(self) -> float:
        """Realized P&L net of commissions (only valid when closed)."""
        if self.realized_pnl is None:
            return 0.0
        return float(self.realized_pnl) - float(self.commission_total)
