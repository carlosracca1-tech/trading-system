"""
packages/shared/models/order.py
Order — one broker order. Created by the Execution Service.
Tracks the full lifecycle from pending → submitted → filled/cancelled.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import OrderSide, OrderStatus, OrderType
from packages.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = {"comment": "Broker order lifecycle record"}

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="Null for manual/kill-switch orders",
    )

    # Broker reference (set after successful submission)
    broker_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
        comment="Alpaca order UUID",
    )

    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    order_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default=OrderType.MARKET.value,
    )
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    # Pricing
    limit_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    submitted_price: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="Last trade price at submission time",
    )
    filled_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Status
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrderStatus.PENDING.value, index=True,
    )

    # Timestamps
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Cost tracking
    commission: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    slippage: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)

    # Correlation for audit trail
    correlation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
        comment="UUID linking this order to its audit log entries",
    )

    def __repr__(self) -> str:
        return (
            f"<Order {self.symbol} {self.side} qty={self.qty} "
            f"status={self.status} broker_id={self.broker_order_id}>"
        )

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED.value

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.EXPIRED.value,
        )

    @property
    def total_cost(self) -> float:
        if self.filled_price is None or self.filled_qty == 0:
            return 0.0
        base = float(self.filled_price) * self.filled_qty
        return base + float(self.commission) + float(self.slippage)
