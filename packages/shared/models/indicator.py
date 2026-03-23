"""
packages/shared/models/indicator.py
Computed technical indicators cache — TimescaleDB hypertable partitioned by date.
Computed from MarketDataDaily by the Data Service after each ingestion run.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, PrimaryKeyConstraint, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.models.base import Base


class IndicatorCache(Base):
    """
    One row = all indicators for one symbol on one date.
    Recomputed on every ingestion run (upsert).
    TimescaleDB hypertable on `date` with 1-year chunks.

    NOTE: PK is composite (id, date) — required by TimescaleDB.
    """
    __tablename__ = "indicators_cache"
    __table_args__ = (
        PrimaryKeyConstraint("id", "date"),
        UniqueConstraint("symbol", "date", name="uq_indicators_symbol_date"),
        {"comment": "Computed indicators — TimescaleDB hypertable"},
    )

    id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, index=True,
    )
    symbol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("symbols.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Trend indicators (strategy core)
    ema_50: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="50-day exponential moving average of close",
    )
    ema_200: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="200-day exponential moving average of close",
    )

    # Momentum
    rsi_14: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True,
        comment="14-period RSI (Wilder smoothing)",
    )

    # Volatility / sizing
    atr_14: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="14-period Average True Range (Wilder smoothing)",
    )
    atr_14_pct: Mapped[float | None] = mapped_column(
        Numeric(8, 6), nullable=True,
        comment="ATR as fraction of close price (for relative sizing)",
    )

    # Volume filter
    volume_ma_20: Mapped[float | None] = mapped_column(
        Numeric(16, 2), nullable=True,
        comment="20-day simple moving average of volume",
    )

    # Breakout
    high_20d: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="Highest high over the last 20 sessions (breakout level)",
    )

    # Metadata
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<IndicatorCache {self.symbol} {self.date} "
            f"ema50={self.ema_50} ema200={self.ema_200} rsi={self.rsi_14}>"
        )
