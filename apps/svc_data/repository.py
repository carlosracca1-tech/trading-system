"""
apps/svc_data/repository.py
Data layer for the Data Service — DB read/write operations.
All functions receive an explicit Session to stay transaction-safe.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from packages.shared.enums import DataQuality
from packages.shared.logging_config import get_logger
from packages.shared.models.indicator import IndicatorCache
from packages.shared.models.market_data import MarketDataDaily
from packages.shared.models.symbol import Symbol

logger = get_logger(__name__)


# ── Symbol ────────────────────────────────────────────────────────────────────

def get_all_active_symbols(session: Session) -> list[Symbol]:
    """Return all active symbols ordered by ticker."""
    stmt = select(Symbol).where(Symbol.is_active == True).order_by(Symbol.symbol)  # noqa: E712
    return list(session.scalars(stmt))


def get_symbol_by_ticker(session: Session, symbol: str) -> Symbol | None:
    stmt = select(Symbol).where(Symbol.symbol == symbol)
    return session.scalar(stmt)


# ── Market Data ────────────────────────────────────────────────────────────────

def get_latest_date(session: Session, symbol: str) -> date | None:
    """Return the most recent date stored for this symbol, or None."""
    stmt = text(
        "SELECT MAX(date) FROM market_data_daily WHERE symbol = :symbol"
    )
    result = session.execute(stmt, {"symbol": symbol}).scalar()
    return result  # date | None


def get_market_data(
    session: Session,
    symbol: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    """
    Load market data for a symbol into a pandas DataFrame.
    Columns: date, open, high, low, close, volume, vwap, num_trades.
    """
    stmt = (
        select(MarketDataDaily)
        .where(MarketDataDaily.symbol == symbol)
        .order_by(MarketDataDaily.date.asc())
    )
    if from_date:
        stmt = stmt.where(MarketDataDaily.date >= from_date)
    if to_date:
        stmt = stmt.where(MarketDataDaily.date <= to_date)

    rows = session.scalars(stmt).all()
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    return pd.DataFrame(
        [
            {
                "date": r.date,
                "open": float(r.open),
                "high": float(r.high),
                "low": float(r.low),
                "close": float(r.close),
                "volume": int(r.volume),
                "vwap": float(r.vwap) if r.vwap else None,
                "num_trades": r.num_trades,
            }
            for r in rows
        ]
    )


def upsert_daily_bars(
    session: Session,
    symbol_id: str,
    symbol: str,
    bars: list[dict[str, Any]],
) -> int:
    """
    Upsert daily bars for one symbol.
    Conflict key: (symbol, date).
    Returns number of rows inserted/updated.
    """
    if not bars:
        return 0

    rows = [
        {
            "id": str(uuid.uuid4()),
            "symbol_id": symbol_id,
            "symbol": symbol,
            "date": b["date"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b["volume"],
            "vwap": b.get("vwap"),
            "num_trades": b.get("num_trades"),
            "data_quality": DataQuality.VALID.value,
            "source": "polygon",
        }
        for b in bars
    ]

    stmt = pg_insert(MarketDataDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_market_data_symbol_date",
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "vwap": stmt.excluded.vwap,
            "num_trades": stmt.excluded.num_trades,
            "data_quality": stmt.excluded.data_quality,
        },
    )
    session.execute(stmt)
    logger.debug("market_data.upserted", symbol=symbol, count=len(rows))
    return len(rows)


# ── Indicators ────────────────────────────────────────────────────────────────

def upsert_indicators(
    session: Session,
    symbol_id: str,
    symbol: str,
    df: pd.DataFrame,
) -> int:
    """
    Upsert indicator rows for one symbol from a computed DataFrame.
    Only inserts rows where at least ema_50 or rsi_14 is not NaN
    (i.e., we have enough history for meaningful values).
    Conflict key: (symbol, date).
    Returns rows inserted/updated.
    """
    # Filter out rows with no meaningful indicators
    valid = df[df["ema_50"].notna() | df["rsi_14"].notna()].copy()
    if valid.empty:
        return 0

    def _safe(val: Any) -> float | None:
        if val is None:
            return None
        try:
            import math
            if math.isnan(float(val)):
                return None
            return float(val)
        except (TypeError, ValueError):
            return None

    rows = [
        {
            "id": str(uuid.uuid4()),
            "symbol_id": symbol_id,
            "symbol": symbol,
            "date": row["date"] if not isinstance(row["date"], str) else row["date"],
            "ema_50": _safe(row.get("ema_50")),
            "ema_200": _safe(row.get("ema_200")),
            "rsi_14": _safe(row.get("rsi_14")),
            "atr_14": _safe(row.get("atr_14")),
            "atr_14_pct": _safe(row.get("atr_14_pct")),
            "volume_ma_20": _safe(row.get("volume_ma_20")),
            "high_20d": _safe(row.get("high_20d")),
        }
        for _, row in valid.iterrows()
    ]

    stmt = pg_insert(IndicatorCache).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_indicators_symbol_date",
        set_={
            "ema_50": stmt.excluded.ema_50,
            "ema_200": stmt.excluded.ema_200,
            "rsi_14": stmt.excluded.rsi_14,
            "atr_14": stmt.excluded.atr_14,
            "atr_14_pct": stmt.excluded.atr_14_pct,
            "volume_ma_20": stmt.excluded.volume_ma_20,
            "high_20d": stmt.excluded.high_20d,
            "computed_at": text("now()"),
        },
    )
    session.execute(stmt)
    logger.debug("indicators.upserted", symbol=symbol, count=len(rows))
    return len(rows)


def get_data_coverage(session: Session) -> list[dict[str, Any]]:
    """
    Return a summary of data coverage per symbol.
    Used by the health endpoint and monitoring dashboard.
    """
    stmt = text("""
        SELECT
            symbol,
            COUNT(*) AS total_bars,
            MIN(date) AS first_date,
            MAX(date) AS last_date,
            MAX(date) < CURRENT_DATE - INTERVAL '1 day' AS is_stale
        FROM market_data_daily
        GROUP BY symbol
        ORDER BY symbol
    """)
    rows = session.execute(stmt).fetchall()
    return [
        {
            "symbol": r.symbol,
            "total_bars": r.total_bars,
            "first_date": r.first_date,
            "last_date": r.last_date,
            "is_stale": r.is_stale,
        }
        for r in rows
    ]
