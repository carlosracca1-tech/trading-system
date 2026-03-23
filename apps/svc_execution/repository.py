"""
apps/svc_execution/repository.py
Execution Service DB layer.

All SQL lives here. Functions receive a SQLAlchemy Session and return
domain objects. Callers manage commit/rollback.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from packages.shared.enums import OrderStatus, PositionStatus
from packages.shared.models.order import Order
from packages.shared.models.portfolio_snapshot import PortfolioSnapshot
from packages.shared.models.position import Position
from packages.shared.models.signal import Signal
from packages.shared.models.trading_run import TradingRun
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Trading run ───────────────────────────────────────────────────────────────

def get_active_run(session: Session) -> Optional[TradingRun]:
    """Return the single RUNNING TradingRun, or None."""
    from packages.shared.enums import RunStatus
    stmt = (
        select(TradingRun)
        .where(TradingRun.status == RunStatus.RUNNING.value)
        .limit(1)
    )
    return session.scalars(stmt).first()


# ── Orders ────────────────────────────────────────────────────────────────────

def save_order(session: Session, order: Order) -> Order:
    """Persist a new Order (INSERT)."""
    session.add(order)
    session.flush()  # assigns PK without committing
    log.info("order_saved", order_id=order.id, symbol=order.symbol, side=order.side)
    return order


def get_order_by_id(session: Session, order_id: str) -> Optional[Order]:
    return session.get(Order, order_id)


def get_pending_orders(session: Session, run_id: str) -> list[Order]:
    """All orders in PENDING or SUBMITTED state for a given run."""
    stmt = (
        select(Order)
        .where(
            Order.run_id == run_id,
            Order.status.in_([OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value]),
        )
        .order_by(Order.created_at)
    )
    return list(session.scalars(stmt).all())


def update_order_status(
    session: Session,
    order_id: str,
    status: str,
    *,
    broker_order_id: Optional[str] = None,
    filled_price: Optional[float] = None,
    filled_qty: Optional[int] = None,
    filled_at: Optional[datetime] = None,
    rejection_reason: Optional[str] = None,
) -> None:
    """Targeted UPDATE on the orders table (no full object fetch needed)."""
    values: dict = {"status": status, "updated_at": datetime.now(tz=timezone.utc)}
    if broker_order_id is not None:
        values["broker_order_id"] = broker_order_id
    if filled_price is not None:
        values["filled_price"] = filled_price
    if filled_qty is not None:
        values["filled_qty"] = filled_qty
    if filled_at is not None:
        values["filled_at"] = filled_at
    if rejection_reason is not None:
        values["rejection_reason"] = rejection_reason

    session.execute(update(Order).where(Order.id == order_id).values(**values))


# ── Positions ─────────────────────────────────────────────────────────────────

def save_position(session: Session, position: Position) -> Position:
    """Persist a new Position (INSERT)."""
    session.add(position)
    session.flush()
    log.info(
        "position_opened",
        position_id=position.id,
        symbol=position.symbol,
        qty=position.qty,
        entry_price=position.entry_price,
    )
    return position


def get_open_positions(session: Session, run_id: str) -> list[Position]:
    """All open positions for a given TradingRun."""
    stmt = (
        select(Position)
        .where(
            Position.run_id == run_id,
            Position.status == PositionStatus.OPEN.value,
        )
        .order_by(Position.opened_at)
    )
    return list(session.scalars(stmt).all())


def get_open_position_by_symbol(
    session: Session, run_id: str, symbol: str
) -> Optional[Position]:
    """Return the open position for a symbol (at most one per run in V1)."""
    stmt = (
        select(Position)
        .where(
            Position.run_id == run_id,
            Position.symbol == symbol,
            Position.status == PositionStatus.OPEN.value,
        )
        .limit(1)
    )
    return session.scalars(stmt).first()


def update_position_on_close(
    session: Session,
    position_id: str,
    *,
    exit_order_id: str,
    exit_price: float,
    realized_pnl: float,
    closed_at: datetime,
    close_reason: str,
    status: str = PositionStatus.CLOSED.value,
) -> None:
    """Targeted UPDATE to close a position."""
    session.execute(
        update(Position)
        .where(Position.id == position_id)
        .values(
            status=status,
            exit_order_id=exit_order_id,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            closed_at=closed_at,
            close_reason=close_reason,
            updated_at=datetime.now(tz=timezone.utc),
        )
    )


def update_position_mtm(
    session: Session,
    position_id: str,
    current_price: float,
    unrealized_pnl: float,
) -> None:
    """Update mark-to-market fields on an open position."""
    session.execute(
        update(Position)
        .where(Position.id == position_id)
        .values(
            current_price=current_price,
            unrealized_pnl=unrealized_pnl,
            updated_at=datetime.now(tz=timezone.utc),
        )
    )


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(session: Session, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
    """Persist a PortfolioSnapshot (INSERT)."""
    session.add(snapshot)
    session.flush()
    log.info(
        "snapshot_saved",
        run_id=snapshot.run_id,
        equity=snapshot.total_equity,
        drawdown=float(snapshot.drawdown_pct),
    )
    return snapshot


def get_latest_snapshot(
    session: Session, run_id: str
) -> Optional[PortfolioSnapshot]:
    """Return the most recent snapshot for a run."""
    stmt = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.run_id == run_id)
        .order_by(PortfolioSnapshot.snapshot_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


# ── Signals ───────────────────────────────────────────────────────────────────

def get_approved_signals(session: Session, run_id: str) -> list[Signal]:
    """
    Fetch ENTER signals that have been APPROVED by risk for this run.

    Caller is responsible for de-duplicating / filtering already-executed
    signals using the orders table if needed.
    """
    from packages.shared.enums import RiskDecision, SignalType
    stmt = (
        select(Signal)
        .where(
            Signal.run_id == run_id,
            Signal.signal_type == SignalType.ENTER.value,
            Signal.risk_decision == RiskDecision.APPROVED.value,
        )
        .order_by(Signal.signal_date, Signal.created_at)
    )
    return list(session.scalars(stmt).all())


def get_exit_signals(session: Session, run_id: str) -> list[Signal]:
    """Fetch EXIT signals that have been APPROVED for this run."""
    from packages.shared.enums import SignalType, RiskDecision
    stmt = (
        select(Signal)
        .where(
            Signal.run_id == run_id,
            Signal.signal_type == SignalType.EXIT.value,
            Signal.risk_decision == RiskDecision.APPROVED.value,
        )
        .order_by(Signal.signal_date, Signal.created_at)
    )
    return list(session.scalars(stmt).all())


def has_order_for_signal(
    session: Session, run_id: str, symbol: str, signal_date
) -> bool:
    """
    Check whether an entry order already exists for this symbol on signal_date.
    Used to prevent duplicate order submission on re-runs within the same day.
    Filters by BUY side + date to avoid blocking future entries.
    """
    from sqlalchemy import and_, exists, cast
    from sqlalchemy import Date as DateType
    from packages.shared.enums import OrderSide
    stmt = select(
        exists().where(
            and_(
                Order.run_id == run_id,
                Order.symbol == symbol,
                Order.side == OrderSide.BUY.value,
                cast(Order.created_at, DateType) == signal_date,
            )
        )
    )
    return bool(session.scalar(stmt))
