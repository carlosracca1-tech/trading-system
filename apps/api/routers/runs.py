"""
apps/api/routers/runs.py
TradingRun management endpoints.

GET  /api/v1/runs              → list all runs (paginated)
GET  /api/v1/runs/{run_id}     → single run detail
POST /api/v1/runs              → create a new TradingRun
DELETE /api/v1/runs/{run_id}   → stop a running run
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.dependencies import require_api_key
from apps.api.schemas import RunCreateIn, RunListOut, RunOut
from packages.shared.db import get_db

router = APIRouter(prefix="/runs", tags=["runs"])


def _get_db():
    yield from get_db()


@router.get("", response_model=RunListOut, dependencies=[Depends(require_api_key)])
def list_runs(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
):
    """List all TradingRuns, newest first."""
    from sqlalchemy import select, func
    from packages.shared.models.trading_run import TradingRun

    total = db.scalar(select(func.count()).select_from(TradingRun)) or 0
    runs = list(
        db.scalars(
            select(TradingRun)
            .order_by(TradingRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return RunListOut(runs=runs, total=total)


@router.get("/{run_id}", response_model=RunOut, dependencies=[Depends(require_api_key)])
def get_run(run_id: str, db: Session = Depends(_get_db)):
    """Get a single TradingRun by ID."""
    from packages.shared.models.trading_run import TradingRun

    run = db.get(TradingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_api_key)])
def create_run(body: RunCreateIn, db: Session = Depends(_get_db)):
    """Create a new TradingRun."""
    from apps.svc_orchestrator.runner import create_run as _create_run
    from packages.shared.models.trading_run import TradingRun

    try:
        from apps.svc_strategy.scanner import STRATEGY_PARAMS
        from apps.svc_risk.position_sizer import RISK_PARAMS
        config = {"strategy": STRATEGY_PARAMS, "risk": RISK_PARAMS}
        run_id = _create_run(
            run_type=body.run_type,
            initial_capital=body.initial_capital,
            notes=body.notes,
            config_snapshot=config,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    run = db.get(TradingRun, run_id)
    return run


@router.delete("/{run_id}", response_model=RunOut, dependencies=[Depends(require_api_key)])
def stop_run(run_id: str, db: Session = Depends(_get_db)):
    """Stop a running TradingRun."""
    from packages.shared.models.trading_run import TradingRun
    from apps.svc_orchestrator.runner import stop_run as _stop_run
    from packages.shared.enums import RunStatus

    run = db.get(TradingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not RUNNING (current status: {run.status})",
        )

    _stop_run(run_id)
    db.refresh(run)
    return run
