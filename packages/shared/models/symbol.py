"""
packages/shared/models/symbol.py
Symbol — master record for each ETF in the trading universe.
18 ETFs are seeded at startup via Alembic data migration.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import AssetType
from packages.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Symbol(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_symbols_symbol"),
        {"comment": "Master record for each tradeable ETF in the universe"},
    )

    symbol: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True,
        comment="Ticker symbol (e.g. SPY, QQQ)",
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False,
        comment="Full ETF name",
    )
    sector: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
        comment="Sector / asset class (e.g. Equity-Broad, Fixed Income)",
    )
    asset_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AssetType.ETF.value,
        comment="AssetType enum value",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
        comment="False = excluded from strategy until re-enabled",
    )

    def __repr__(self) -> str:
        return f"<Symbol {self.symbol} active={self.is_active}>"
