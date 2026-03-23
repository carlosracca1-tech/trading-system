"""
apps/api/routers/signals.py
Signal history endpoints.

GET /api/v1/signals        → paginated signals with filters
GET /api/v1/signals/{id}   → single signal detail
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import require_api_key
from apps.api.schemas import SignalListOut, SignalOut
from packages.shared.db import get_db

router = APIRouter(prefix="/signals", tags=["signals"])


def _get_db():
    yield from get_db()


@router.get("", response_model=SignalListOut, dependencies=[Depends(require_api_key)])
def list_signals(
    run_id: str | None = Query(default=None),
    signal_type: str | None = Query(default=None, description="ENTER | EXIT | HOLD"),
    risk_decision: str | None = Query(default=None, description="APPROVED | REJECTED | PENDING"),
    symbol: str | None = Query(default=None),
    since: date | None = Query(default=None, description="Filter signals on or after this date"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
):
    """List signals with optional filters. Newest first."""
    from sqlalchemy import select, func
    from packages.shared.models.signal import Signal
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus

    # Default to active run
    if run_id is None:
        run_id = db.scalar(
            select(TradingRun.id)
            .where(TradingRun.status == RunStatus.RUNNING.value)
            .limit(1)
        )

    q = select(Signal)
    if run_id:
        q = q.where(Signal.run_id == run_id)
    if signal_type:
        q = q.where(Signal.signal_type == signal_type.upper())
    if risk_decision:
        q = q.where(Signal.risk_decision == risk_decision.upper())
    if symbol:
        q = q.where(Signal.symbol == symbol.upper())
    if since:
        q = q.where(Signal.signal_date >= since)

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    signals = list(
        db.scalars(q.order_by(Signal.signal_date.desc(), Signal.created_at.desc())
                   .limit(limit).offset(offset)).all()
    )
    return SignalListOut(signals=signals, total=total)


@router.get("/{signal_id}", response_model=SignalOut, dependencies=[Depends(require_api_key)])
def get_signal(signal_id: str, db: Session = Depends(_get_db)):
    """Get a single signal by ID."""
    from packages.shared.models.signal import Signal

    sig = db.get(Signal, signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    return sig
