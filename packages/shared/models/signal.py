"""
packages/shared/models/signal.py
Signal — output of the Strategy Engine for one symbol on one day.
Records both ENTER and EXIT decisions plus the Risk Engine verdict.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import Direction, RiskDecision, SignalType
from packages.shared.models.base import Base, UUIDPrimaryKeyMixin


class Signal(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "signals"
    __table_args__ = {"comment": "Strategy output — one row per symbol per evaluation"}

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    signal_type: Mapped[str] = mapped_column(
        String(10), nullable=False,
        comment="ENTER | EXIT | HOLD",
    )
    direction: Mapped[str] = mapped_column(
        String(10), nullable=False, default=Direction.LONG.value,
    )

    # Market snapshot at signal generation
    close_price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    ema_50: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    ema_200: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    atr_14: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True,
        comment="volume / volume_ma_20 at signal time",
    )

    # Regime filter result
    regime_ok: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True,
        comment="True = SPY > EMA200 (bullish regime)",
    )

    # Sizing (populated for ENTER signals)
    entry_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    position_size_shares: Mapped[int | None] = mapped_column(
        String(20), nullable=True,
        comment="Number of shares computed by risk engine",
    )

    # Risk Engine verdict
    risk_decision: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RiskDecision.PENDING.value,
    )
    risk_rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Signal {self.symbol} {self.signal_date} "
            f"type={self.signal_type} decision={self.risk_decision}>"
        )

    @property
    def signal_type_enum(self) -> SignalType:
        return SignalType(self.signal_type)

    @property
    def risk_decision_enum(self) -> RiskDecision:
        return RiskDecision(self.risk_decision)
