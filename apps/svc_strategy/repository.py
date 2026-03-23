"""
apps/svc_strategy/repository.py
DB read/write operations for the Strategy Engine.

All reads join indicators_cache + market_data_daily to get the complete
row needed by validate_signal_conditions (which requires close + volume).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from packages.shared.enums import Direction, RiskDecision, SignalType
from packages.shared.models import Signal, Symbol
from packages.shared.models.indicator import IndicatorCache
from packages.shared.models.market_data import MarketDataDaily
from packages.shared.models.position import Position
from packages.shared.logging_config import get_logger
from apps.svc_strategy.scanner import SignalDecision

log = get_logger(__name__)


def get_active_symbols(session: Session) -> list[str]:
    """Return ticker strings for all active symbols, sorted alphabetically."""
    stmt = select(Symbol.symbol).where(Symbol.is_active.is_(True)).order_by(Symbol.symbol)
    return list(session.execute(stmt).scalars().all())


def get_combined_row(
    session: Session, symbol: str, as_of_date: date
) -> Optional[pd.Series]:
    """
    Fetch the most recent (symbol, date <= as_of_date) row from:
        indicators_cache  — ema_50, ema_200, rsi_14, atr_14, atr_14_pct, volume_ma_20, high_20d
        market_data_daily — close, volume

    Returns None if no data found.
    """
    # Fetch indicators
    ind_stmt = (
        select(IndicatorCache)
        .where(IndicatorCache.symbol == symbol)
        .where(IndicatorCache.date <= as_of_date)
        .order_by(IndicatorCache.date.desc())
        .limit(1)
    )
    ind = session.execute(ind_stmt).scalar_one_or_none()
    if ind is None:
        return None

    # Fetch matching market data for the same date
    md_stmt = (
        select(MarketDataDaily)
        .where(MarketDataDaily.symbol == symbol)
        .where(MarketDataDaily.date == ind.date)
        .limit(1)
    )
    md = session.execute(md_stmt).scalar_one_or_none()
    if md is None:
        return None

    return pd.Series({
        "date": ind.date,
        "close": float(md.close),
        "open": float(md.open),
        "high": float(md.high),
        "low": float(md.low),
        "volume": float(md.volume),
        "ema_50": float(ind.ema_50) if ind.ema_50 is not None else None,
        "ema_200": float(ind.ema_200) if ind.ema_200 is not None else None,
        "rsi_14": float(ind.rsi_14) if ind.rsi_14 is not None else None,
        "atr_14": float(ind.atr_14) if ind.atr_14 is not None else None,
        "atr_14_pct": float(ind.atr_14_pct) if ind.atr_14_pct is not None else None,
        "volume_ma_20": float(ind.volume_ma_20) if ind.volume_ma_20 is not None else None,
        "high_20d": float(ind.high_20d) if ind.high_20d is not None else None,
    })


def get_open_positions(session: Session, run_id: str) -> list[Position]:
    """Return all OPEN positions for the given trading run."""
    from packages.shared.enums import PositionStatus
    stmt = (
        select(Position)
        .where(Position.run_id == run_id)
        .where(Position.status == PositionStatus.OPEN.value)
    )
    return list(session.execute(stmt).scalars().all())


def write_signal(
    session: Session,
    run_id: str,
    decision: SignalDecision,
    *,
    stop_loss: Optional[float] = None,
    position_size_shares: Optional[int] = None,
    dry_run: bool = True,
) -> Signal:
    """
    Persist a SignalDecision to the `signals` table.
    If dry_run=True, the object is built but NOT flushed.
    """
    sig = Signal(
        run_id=run_id,
        symbol=decision.symbol,
        signal_date=decision.signal_date,
        signal_type=decision.signal_type,
        direction=Direction.LONG.value,
        close_price=decision.close_price,
        ema_50=decision.ema_50,
        ema_200=decision.ema_200,
        rsi_14=decision.rsi_14,
        atr_14=decision.atr_14,
        volume_ratio=decision.volume_ratio,
        regime_ok=decision.regime_ok,
        entry_price=decision.close_price if decision.signal_type == SignalType.ENTER.value else None,
        stop_loss=stop_loss,
        position_size_shares=str(position_size_shares) if position_size_shares is not None else None,
        risk_decision=decision.risk_decision,
        risk_rejection_reason=decision.reason or None,
    )
    if not dry_run:
        session.add(sig)
        session.flush()
    return sig
