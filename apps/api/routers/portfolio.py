"""
apps/api/routers/portfolio.py
Portfolio and position endpoints.

GET /api/v1/portfolio              → current portfolio state (snapshot + open positions)
GET /api/v1/portfolio/snapshots    → equity curve (time series of snapshots)
GET /api/v1/positions              → paginated list of all positions
GET /api/v1/positions/{id}         → single position detail
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import require_api_key
from apps.api.schemas import PortfolioOut, PositionListOut, PositionOut, SnapshotOut
from packages.shared.db import get_db

router = APIRouter(tags=["portfolio"])


def _get_db():
    yield from get_db()


def _active_run_id(db: Session) -> str | None:
    """Return the first RUNNING run_id, or None."""
    from sqlalchemy import select
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus
    return db.scalar(
        select(TradingRun.id)
        .where(TradingRun.status == RunStatus.RUNNING.value)
        .limit(1)
    )


@router.get("/portfolio", response_model=PortfolioOut, dependencies=[Depends(require_api_key)])
def get_portfolio(db: Session = Depends(_get_db)):
    """
    Current portfolio view: latest snapshot + all open positions.
    Uses the first RUNNING TradingRun.
    """
    from sqlalchemy import select
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.models.position import Position
    from packages.shared.enums import RunStatus, PositionStatus
    from apps.svc_execution.repository import get_latest_snapshot

    run_id = _active_run_id(db)
    if run_id is None:
        raise HTTPException(status_code=404, detail="No active RUNNING TradingRun found")

    run = db.get(TradingRun, run_id)
    snapshot = get_latest_snapshot(db, run_id)

    open_positions = list(
        db.scalars(
            select(Position)
            .where(Position.run_id == run_id, Position.status == PositionStatus.OPEN.value)
            .order_by(Position.opened_at.desc())
        ).all()
    )

    return PortfolioOut(
        run_id=run_id,
        run_status=run.status,
        snapshot=snapshot,
        open_positions=open_positions,
    )


@router.get("/portfolio/snapshots", response_model=list[SnapshotOut],
            dependencies=[Depends(require_api_key)])
def get_snapshots(
    run_id: str | None = Query(default=None),
    limit: int = Query(default=90, le=500),
    db: Session = Depends(_get_db),
):
    """
    Equity curve: time-ordered portfolio snapshots.
    If run_id not provided, uses the active run.
    """
    from sqlalchemy import select
    from packages.shared.models.portfolio_snapshot import PortfolioSnapshot

    rid = run_id or _active_run_id(db)
    if rid is None:
        raise HTTPException(status_code=404, detail="No active run found")

    snapshots = list(
        db.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.run_id == rid)
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(limit)
        ).all()
    )
    return list(reversed(snapshots))  # chronological order


@router.get("/positions", response_model=PositionListOut, dependencies=[Depends(require_api_key)])
def list_positions(
    run_id: str | None = Query(default=None),
    status: str | None = Query(default=None, description="open | closed | forced_closed"),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
):
    """List positions with optional filters."""
    from sqlalchemy import select, func
    from packages.shared.models.position import Position

    rid = run_id or _active_run_id(db)
    if rid is None:
        raise HTTPException(status_code=404, detail="No active run found")

    q = select(Position).where(Position.run_id == rid)
    if status:
        q = q.where(Position.status == status)
    if symbol:
        q = q.where(Position.symbol == symbol.upper())

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    positions = list(
        db.scalars(q.order_by(Position.opened_at.desc()).limit(limit).offset(offset)).all()
    )
    return PositionListOut(positions=positions, total=total)


@router.get("/positions/{position_id}", response_model=PositionOut,
            dependencies=[Depends(require_api_key)])
def get_position(position_id: str, db: Session = Depends(_get_db)):
    """Get a single position by ID."""
    from packages.shared.models.position import Position

    pos = db.get(Position, position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
    return pos
