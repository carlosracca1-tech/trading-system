"""
apps/api/routers/system.py
System state, kill switch, and reconciliation endpoints.

GET    /api/v1/system/status              → current system health + KS state
POST   /api/v1/system/kill-switch         → activate kill switch (closes all positions)
DELETE /api/v1/system/kill-switch         → resolve kill switch (re-enable trading)
GET    /api/v1/system/risk-events         → recent risk events
POST   /api/v1/system/reconcile           → trigger reconciliation pass
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.dependencies import require_api_key
from apps.api.schemas import (
    KillSwitchActivateIn,
    KillSwitchOut,
    KillSwitchResolveIn,
    RiskEventListOut,
    RiskEventOut,
    SystemStateOut,
)
from packages.shared.db import get_db

router = APIRouter(prefix="/system", tags=["system"])


def _get_db():
    yield from get_db()


def _build_broker(initial_cash: float = 100_000.0):
    """Build broker from env (dry-run by default)."""
    import os
    from apps.svc_execution.broker import DryRunBroker, AlpacaBroker
    dry = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
    if dry:
        return DryRunBroker(initial_cash=initial_cash)
    return AlpacaBroker(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        base_url=os.environ["ALPACA_BASE_URL"],
    )


# ── System status ─────────────────────────────────────────────────────────────

@router.get("/status", response_model=SystemStateOut, dependencies=[Depends(require_api_key)])
def get_system_status(db: Session = Depends(_get_db)):
    """
    Current system state: active run, kill switch status, drawdown.
    """
    from sqlalchemy import select
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus
    from apps.svc_execution.repository import get_latest_snapshot, get_open_positions
    from apps.svc_risk.kill_switch import is_active as ks_is_active

    run = db.scalar(
        select(TradingRun)
        .where(TradingRun.status == RunStatus.RUNNING.value)
        .limit(1)
    )
    stopped_run = db.scalar(
        select(TradingRun)
        .where(TradingRun.status == RunStatus.STOPPED.value)
        .order_by(TradingRun.started_at.desc())
        .limit(1)
    )

    if run is None and stopped_run is None:
        return SystemStateOut(
            status="no_active_run",
            message="No TradingRun found. Create one with POST /api/v1/runs.",
        )

    check_run = run or stopped_run
    run_id = check_run.id
    ks_active = ks_is_active(db, run_id)
    snap = get_latest_snapshot(db, run_id)
    open_pos = get_open_positions(db, run_id)

    sys_status = "kill_switch_active" if ks_active else (
        "operational" if run else "stopped"
    )

    return SystemStateOut(
        status=sys_status,
        run_id=run_id,
        run_status=check_run.status,
        kill_switch_active=ks_active,
        drawdown_pct=float(snap.drawdown_pct) if snap else None,
        peak_equity=float(snap.peak_equity) if snap else None,
        total_equity=float(snap.total_equity) if snap else None,
        open_positions=len(open_pos),
        message=(
            "Kill switch active — trading halted" if ks_active
            else f"Running normally — {len(open_pos)} open position(s)"
        ),
    )


# ── Kill switch ───────────────────────────────────────────────────────────────

@router.post("/kill-switch", response_model=KillSwitchOut, status_code=status.HTTP_200_OK,
             dependencies=[Depends(require_api_key)])
def activate_kill_switch(body: KillSwitchActivateIn, db: Session = Depends(_get_db)):
    """
    Manually activate the kill switch for a run.
    Closes ALL open positions immediately and stops the run.
    """
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import KillSwitchTrigger, RunStatus
    from apps.svc_execution.repository import get_latest_snapshot
    from apps.svc_risk.kill_switch import activate as ks_activate

    run = db.get(TradingRun, body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {body.run_id} not found")
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not RUNNING (status={run.status}). Cannot activate kill switch.",
        )

    snap = get_latest_snapshot(db, body.run_id)
    initial_cash = float(snap.cash) if snap else float(run.initial_capital)
    broker = _build_broker(initial_cash)

    ks_activate(
        db,
        run_id=body.run_id,
        broker=broker,
        trigger=KillSwitchTrigger.MANUAL.value,
        reason=body.reason,
        metrics_snapshot=(
            {
                "equity": float(snap.total_equity),
                "drawdown_pct": float(snap.drawdown_pct),
            }
            if snap else {}
        ),
    )
    db.commit()

    return KillSwitchOut(
        activated=True,
        run_id=body.run_id,
        reason=body.reason,
        message="Kill switch activated. All positions closed. Run stopped.",
    )


@router.delete("/kill-switch", response_model=KillSwitchOut,
               dependencies=[Depends(require_api_key)])
def resolve_kill_switch(body: KillSwitchResolveIn, db: Session = Depends(_get_db)):
    """
    Resolve the kill switch — re-enable trading for a stopped run.
    Does NOT reopen closed positions.
    """
    from apps.svc_risk.kill_switch import resolve as ks_resolve, is_active as ks_is_active

    if not ks_is_active(db, body.run_id):
        raise HTTPException(
            status_code=409,
            detail="Kill switch is not active for this run.",
        )

    ks_resolve(db, run_id=body.run_id, resolved_by=body.resolved_by)
    db.commit()

    return KillSwitchOut(
        activated=False,
        run_id=body.run_id,
        reason="resolved",
        message=f"Kill switch resolved by {body.resolved_by}. Run is now RUNNING.",
    )


# ── Risk events ───────────────────────────────────────────────────────────────

@router.get("/risk-events", response_model=RiskEventListOut,
            dependencies=[Depends(require_api_key)])
def list_risk_events(
    run_id: str | None = Query(default=None),
    rule_code: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    db: Session = Depends(_get_db),
):
    """List risk events, newest first."""
    from sqlalchemy import select, func
    from packages.shared.models.risk_event import RiskEvent
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus

    if run_id is None:
        run_id = db.scalar(
            select(TradingRun.id)
            .where(TradingRun.status == RunStatus.RUNNING.value)
            .limit(1)
        )

    q = select(RiskEvent)
    if run_id:
        q = q.where(RiskEvent.run_id == run_id)
    if rule_code:
        q = q.where(RiskEvent.rule_code == rule_code.upper())

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    events = list(
        db.scalars(q.order_by(RiskEvent.triggered_at.desc()).limit(limit)).all()
    )
    return RiskEventListOut(events=events, total=total)


# ── Reconciliation ─────────────────────────────────────────────────────────────

@router.post("/reconcile", status_code=status.HTTP_200_OK,
             dependencies=[Depends(require_api_key)])
def trigger_reconcile(
    run_id: str | None = Query(default=None),
    db: Session = Depends(_get_db),
):
    """
    Trigger a reconciliation pass: poll broker for order updates,
    refresh mark-to-market on open positions.

    Returns a summary of what was updated.
    """
    from sqlalchemy import select
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus, OrderStatus
    from apps.svc_execution.repository import get_pending_orders, get_open_positions
    from apps.svc_execution.executor import apply_broker_fill, update_unrealized_pnl

    rid = run_id or db.scalar(
        select(TradingRun.id)
        .where(TradingRun.status == RunStatus.RUNNING.value)
        .limit(1)
    )
    if rid is None:
        raise HTTPException(status_code=404, detail="No active run found")

    snap = None
    try:
        from apps.svc_execution.repository import get_latest_snapshot
        snap = get_latest_snapshot(db, rid)
    except Exception:
        pass

    initial_cash = float(snap.cash) if snap else 100_000.0
    broker = _build_broker(initial_cash)

    updated_orders = 0
    updated_positions = 0

    # Poll pending/submitted orders
    pending = get_pending_orders(db, rid)
    for order in pending:
        if order.broker_order_id:
            try:
                broker_order = broker.get_order(order.broker_order_id)
                apply_broker_fill(order, broker_order)
                updated_orders += 1
            except Exception:
                pass

    # Refresh MTM on open positions (dry-run uses last known price)
    open_positions = get_open_positions(db, rid)
    for pos in open_positions:
        if pos.current_price:
            update_unrealized_pnl(pos, float(pos.current_price))
            updated_positions += 1

    db.commit()

    return {
        "run_id": rid,
        "orders_polled": len(pending),
        "orders_updated": updated_orders,
        "positions_refreshed": updated_positions,
        "message": "Reconciliation complete",
    }
