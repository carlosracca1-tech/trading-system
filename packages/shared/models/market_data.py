"""
packages/shared/models/market_data.py
Daily OHLCV bars — TimescaleDB hypertable partitioned by date.
Source: Polygon.io (adjusted prices).
"""
from __future__ import annotations

from datetime import date, datetime

import uuid

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import DataQuality
from packages.shared.models.base import Base


class MarketDataDaily(Base):
    """
    One row = one trading day for one symbol.
    Prices are split- and dividend-adjusted (Polygon adjusted=true).
    TimescaleDB hypertable on `date` with 1-year chunks.

    NOTE: PK is composite (id, date) — TimescaleDB requires the partition
    column to be part of all unique indexes including the primary key.
    """
    __tablename__ = "market_data_daily"
    __table_args__ = (
        PrimaryKeyConstraint("id", "date"),
        UniqueConstraint("symbol", "date", name="uq_market_data_symbol_date"),
        {"comment": "Daily OHLCV bars — TimescaleDB hypertable"},
    )

    # Composite primary key
    id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, index=True,
    )

    # Symbol (FK + denormalized for fast queries without joins)
    symbol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("symbols.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True,
        comment="Denormalized ticker for fast hypertable queries",
    )

    # Time dimension (hypertable partition key — part of composite PK)
    date: Mapped[date] = mapped_column(
        Date, nullable=False, index=True,
    )

    # OHLCV
    open: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Optional Polygon extras
    vwap: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    num_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Data quality
    data_quality: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DataQuality.VALID.value,
    )
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="polygon",
    )

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<MarketDataDaily {self.symbol} {self.date} close={self.close}>"
