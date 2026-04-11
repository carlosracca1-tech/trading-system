"""
apps/svc_risk/kill_switch.py
Kill Switch — P0 emergency stop for the trading system.

Responsibilities
----------------
1. check_should_trigger()  — pure computation: should the kill switch activate?
2. activate()              — DB-aware: close all positions, stop the run, write RiskEvent
3. resolve()               — DB-aware: clear the kill switch to allow trading to resume
4. is_active()             — DB-aware: read current state

Kill switch hierarchy
---------------------
P0_KILL_SWITCH     — manual activation or automatic P1 breach
P1_MAX_DRAWDOWN    — drawdown ≥ DRAWDOWN_THRESHOLD (20%)

When activated:
  - All open positions → submitted as SELL orders (at current price)
  - TradingRun.status  → STOPPED
  - RiskEvent written  → P0_KILL_SWITCH, REJECTED
  - System is halted until manually resolved

When resolved:
  - TradingRun.status  → RUNNING  (if run_id provided)
  - A new RiskEvent written → P0_KILL_SWITCH, APPROVED (cleared)
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from packages.shared.enums import (
    KillSwitchTrigger,
    PositionStatus,
    RiskDecision,
    RunStatus,
)
from packages.shared.logging_config import get_logger
from packages.shared.models.risk_event import RiskEvent

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DRAWDOWN_THRESHOLD: float = 0.20      # 20% → automatic kill switch (was 15%)
RULE_CODE: str = "P0_KILL_SWITCH"
RULE_PRIORITY: str = "P0"


# ── Pure computation ──────────────────────────────────────────────────────────

@dataclass
class KillSwitchCheck:
    """Result of check_should_trigger()."""
    should_trigger: bool
    reason: str
    drawdown_pct: float
    trigger: str  # KillSwitchTrigger.value


def check_should_trigger(
    peak_equity: float,
    current_equity: float,
    *,
    drawdown_threshold: float = DRAWDOWN_THRESHOLD,
) -> KillSwitchCheck:
    """
    Pure computation: determine if the kill switch should fire automatically.

    Args:
        peak_equity:         highest portfolio equity seen in this run
        current_equity:      current mark-to-market equity
        drawdown_threshold:  fraction at which kill switch fires (default 15%)

    Returns:
        KillSwitchCheck with should_trigger=True if threshold is exceeded.
    """
    if peak_equity <= 0:
        return KillSwitchCheck(
            should_trigger=False,
            reason="peak_equity_zero_or_negative",
            drawdown_pct=0.0,
            trigger=KillSwitchTrigger.DRAWDOWN_LIMIT.value,
        )

    drawdown_pct = (peak_equity - current_equity) / peak_equity

    if drawdown_pct >= drawdown_threshold:
        return KillSwitchCheck(
            should_trigger=True,
            reason=(
                f"drawdown_{drawdown_pct:.2%}_exceeds_threshold_{drawdown_threshold:.2%}"
            ),
            drawdown_pct=drawdown_pct,
            trigger=KillSwitchTrigger.DRAWDOWN_LIMIT.value,
        )

    return KillSwitchCheck(
        should_trigger=False,
        reason=f"drawdown_{drawdown_pct:.2%}_within_limit",
        drawdown_pct=drawdown_pct,
        trigger=KillSwitchTrigger.DRAWDOWN_LIMIT.value,
    )


# ── DB-aware activation ───────────────────────────────────────────────────────

def activate(
    session,
    *,
    run_id: str,
    broker,
    trigger: str = KillSwitchTrigger.MANUAL.value,
    reason: str = "manual_activation",
    metrics_snapshot: Optional[dict] = None,
) -> RiskEvent:
    """
    Activate the kill switch:
      1. Submit SELL orders for every open position via broker
      2. Close all Position records in DB
      3. Mark TradingRun as STOPPED
      4. Write a P0 RiskEvent to the DB

    Args:
        session:          SQLAlchemy Session (caller manages commit)
        run_id:           UUID of the active TradingRun
        broker:           broker instance (DryRunBroker or AlpacaBroker)
        trigger:          KillSwitchTrigger.value
        reason:           human-readable reason string
        metrics_snapshot: optional dict of portfolio metrics at trigger time

    Returns:
        RiskEvent that was added to the session (not yet committed)
    """
    from sqlalchemy import select, update
    from apps.svc_execution import executor as exec_mod
    from apps.svc_execution import repository as exec_repo
    from packages.shared.models.position import Position
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import OrderSide

    log.warning(
        "kill_switch_activating",
        run_id=run_id,
        trigger=trigger,
        reason=reason,
    )

    # ── 1. Close all open positions ───────────────────────────────────────────
    open_positions = exec_repo.get_open_positions(session, run_id)
    closed_count = 0

    for position in open_positions:
        symbol = position.symbol
        qty = int(position.qty)
        close_price = float(position.current_price or position.entry_price)

        try:
            broker_order = broker.submit_order(
                symbol=symbol,
                side=OrderSide.SELL.value,
                qty=qty,
                submitted_price=close_price,
            )
        except Exception as exc:
            log.error("kill_switch_sell_failed", symbol=symbol, error=str(exc))
            continue

        # Build a minimal exit order + close the position
        from packages.shared.models.order import Order
        from packages.shared.enums import OrderStatus, OrderType

        exit_order = Order(
            id=str(uuid.uuid4()),
            run_id=run_id,
            symbol=symbol,
            side=OrderSide.SELL.value,
            qty=qty,
            order_type=OrderType.MARKET.value,
            submitted_price=close_price,
            status=OrderStatus.PENDING.value,
            correlation_id=str(uuid.uuid4()),
        )
        session.add(exit_order)
        exec_mod.apply_broker_fill(exit_order, broker_order)

        if exit_order.is_filled:
            exec_mod.close_position(
                position=position,
                exit_order=exit_order,
                close_reason=f"kill_switch:{trigger}",
            )
            closed_count += 1
            log.info(
                "kill_switch_position_closed",
                symbol=symbol,
                qty=qty,
                price=broker_order.filled_avg_price,
            )

    # ── 2. Stop the TradingRun ────────────────────────────────────────────────
    session.execute(
        update(TradingRun)
        .where(TradingRun.id == run_id)
        .values(
            status=RunStatus.STOPPED.value,
            ended_at=datetime.now(tz=timezone.utc),
        )
    )

    # ── 3. Write RiskEvent ────────────────────────────────────────────────────
    risk_event = RiskEvent.rejected(
        rule_code=RULE_CODE,
        rule_priority=RULE_PRIORITY,
        correlation_id=str(uuid.uuid4()),
        rejection_reason=reason,
        run_id=run_id,
        metrics_snapshot=json.dumps({
            **(metrics_snapshot or {}),
            "trigger": trigger,
            "positions_closed": closed_count,
        }),
    )
    session.add(risk_event)

    log.warning(
        "kill_switch_activated",
        run_id=run_id,
        trigger=trigger,
        positions_closed=closed_count,
        reason=reason,
    )
    return risk_event


def resolve(
    session,
    *,
    run_id: Optional[str] = None,
    resolved_by: str = "manual",
) -> RiskEvent:
    """
    Resolve the kill switch — re-enable trading for the run.

    This does NOT automatically reopen positions.
    It sets TradingRun.status back to RUNNING and writes a resolution event.

    Args:
        session:     SQLAlchemy Session
        run_id:      UUID of the TradingRun to resume (required to re-enable)
        resolved_by: who resolved (for audit trail)

    Returns:
        RiskEvent (resolution record)
    """
    from sqlalchemy import update
    from packages.shared.models.trading_run import TradingRun

    if run_id:
        session.execute(
            update(TradingRun)
            .where(TradingRun.id == run_id)
            .values(status=RunStatus.RUNNING.value)
        )
        log.info("kill_switch_resolved", run_id=run_id, resolved_by=resolved_by)

    risk_event = RiskEvent(
        rule_code=RULE_CODE,
        rule_priority=RULE_PRIORITY,
        decision=RiskDecision.APPROVED.value,
        correlation_id=str(uuid.uuid4()),
        rejection_reason=None,
        run_id=run_id,
        metrics_snapshot=json.dumps({"resolved_by": resolved_by}),
        triggered_at=datetime.now(tz=timezone.utc),
    )
    session.add(risk_event)
    return risk_event


def is_active(session, run_id: str) -> bool:
    """
    Check if the kill switch is currently active for a run.
    Proxied through TradingRun.status == STOPPED.

    A run is considered kill-switched if its status is STOPPED and
    it has at least one P0 RiskEvent with decision=REJECTED.
    """
    from sqlalchemy import select
    from packages.shared.models.trading_run import TradingRun

    run = session.get(TradingRun, run_id)
    if run is None:
        return False

    if run.status != RunStatus.STOPPED.value:
        return False

    # Check for a P0 event (distinguish kill switch stop from normal stop)
    stmt = (
        select(RiskEvent)
        .where(
            RiskEvent.run_id == run_id,
            RiskEvent.rule_code == RULE_CODE,
            RiskEvent.decision == RiskDecision.REJECTED.value,
        )
        .limit(1)
    )
    return session.scalars(stmt).first() is not None
