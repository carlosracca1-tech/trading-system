"""
apps/svc_execution/executor.py
Execution logic — pure computation layer.

Responsibilities:
  - Build Order ORM objects from signals + sizing
  - Apply broker fills to Order objects
  - Open / close Position objects
  - Build PortfolioSnapshot from current state

No DB calls here. All DB writes are handled by repository.py.
This separation keeps execution logic fully unit-testable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from apps.svc_execution.broker import BrokerOrder
from apps.svc_risk.engine import EvaluationResult
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.enums import (
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    SnapshotType,
)
from packages.shared.models.order import Order
from packages.shared.models.portfolio_snapshot import PortfolioSnapshot
from packages.shared.models.position import Position


# ── Order builders ────────────────────────────────────────────────────────────

def build_entry_order(
    *,
    signal: SignalDecision,
    evaluation: EvaluationResult,
    run_id: str,
    signal_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> Order:
    """
    Create an Order ORM object for an ENTER signal.

    Args:
        signal:        approved ENTER signal from svc_strategy
        evaluation:    APPROVED evaluation result (must contain sizing)
        run_id:        UUID of the active TradingRun
        signal_id:     UUID of the Signal row (optional — may not be written yet)
        correlation_id: audit trail UUID; generated if not provided

    Returns:
        Order (unsaved) — caller must session.add() + session.commit()
    """
    if evaluation.sizing is None:
        raise ValueError("EvaluationResult.sizing must not be None for entry orders")

    return Order(
        id=str(uuid.uuid4()),
        run_id=run_id,
        signal_id=signal_id,
        symbol=signal.symbol,
        side=OrderSide.BUY.value,
        qty=evaluation.sizing.shares,
        order_type=OrderType.MARKET.value,
        stop_price=evaluation.sizing.stop_price,
        submitted_price=signal.close_price,
        status=OrderStatus.PENDING.value,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )


def build_exit_order(
    *,
    signal: SignalDecision,
    position: Position,
    run_id: str,
    signal_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> Order:
    """
    Create an Order ORM object for an EXIT signal.

    Args:
        signal:   EXIT signal for the symbol
        position: open Position to close
        run_id:   UUID of the active TradingRun

    Returns:
        Order (unsaved)
    """
    return Order(
        id=str(uuid.uuid4()),
        run_id=run_id,
        signal_id=signal_id,
        symbol=signal.symbol,
        side=OrderSide.SELL.value,
        qty=int(position.qty),
        order_type=OrderType.MARKET.value,
        submitted_price=signal.close_price,
        status=OrderStatus.PENDING.value,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )


# ── Order lifecycle ───────────────────────────────────────────────────────────

def apply_broker_fill(order: Order, broker_order: BrokerOrder) -> Order:
    """
    Update an Order ORM object with fill data from the broker response.

    Mutates and returns the same `order` object.
    """
    order.broker_order_id = broker_order.broker_order_id
    order.status = broker_order.status
    order.filled_qty = broker_order.filled_qty
    order.filled_price = broker_order.filled_avg_price
    order.submitted_at = broker_order.submitted_at or datetime.now(tz=timezone.utc)

    if broker_order.is_filled:
        order.filled_at = broker_order.filled_at or datetime.now(tz=timezone.utc)

    if broker_order.rejection_reason:
        order.rejection_reason = broker_order.rejection_reason

    return order


def cancel_order(order: Order) -> Order:
    """
    Mark an order as cancelled.

    Mutates and returns the same `order` object.
    Raises ValueError if the order is already in a terminal state.
    """
    if order.is_terminal:
        raise ValueError(
            f"Cannot cancel order {order.id}: already in terminal state {order.status!r}"
        )
    order.status = OrderStatus.CANCELLED.value
    order.cancelled_at = datetime.now(tz=timezone.utc)
    return order


# ── Position management ───────────────────────────────────────────────────────

def open_position(
    *,
    order: Order,
    run_id: str,
) -> Position:
    """
    Create a Position ORM object from a filled BUY order.

    Args:
        order:   filled Order (side=buy, status=filled)
        run_id:  UUID of the active TradingRun

    Returns:
        Position (unsaved)

    Raises:
        ValueError if order is not a filled BUY order
    """
    if order.side != OrderSide.BUY.value:
        raise ValueError(f"Can only open LONG positions from BUY orders, got {order.side!r}")
    if not order.is_filled:
        raise ValueError(f"Cannot open position from unfilled order (status={order.status!r})")
    if order.filled_price is None:
        raise ValueError("Order must have a filled_price to open a position")

    return Position(
        id=str(uuid.uuid4()),
        run_id=run_id,
        symbol=order.symbol,
        status=PositionStatus.OPEN.value,
        direction=Direction.LONG.value,
        entry_order_id=order.id,
        qty=order.filled_qty,
        entry_price=float(order.filled_price),
        stop_loss=float(order.stop_price) if order.stop_price else 0.0,
        opened_at=order.filled_at or datetime.now(tz=timezone.utc),
    )


def close_position(
    *,
    position: Position,
    exit_order: Order,
    close_reason: str,
) -> Position:
    """
    Update a Position with exit details from a filled SELL order.

    Mutates and returns the same `position` object.

    Args:
        position:    open Position to close
        exit_order:  filled Order (side=sell, status=filled)
        close_reason: e.g. "signal_exit" | "stop_loss" | "kill_switch"
    """
    if not exit_order.is_filled:
        raise ValueError(
            f"Cannot close position from unfilled exit order (status={exit_order.status!r})"
        )
    if exit_order.filled_price is None:
        raise ValueError("Exit order must have a filled_price to close a position")

    exit_price = float(exit_order.filled_price)
    entry_price = float(position.entry_price)
    qty = int(position.qty)
    commission = float(position.commission_total or 0)

    realized_pnl = (exit_price - entry_price) * qty - commission

    position.status = PositionStatus.CLOSED.value
    position.exit_order_id = exit_order.id
    position.exit_price = exit_price
    position.realized_pnl = realized_pnl
    position.closed_at = exit_order.filled_at or datetime.now(tz=timezone.utc)
    position.close_reason = close_reason

    return position


def update_unrealized_pnl(position: Position, current_price: float) -> Position:
    """
    Refresh the mark-to-market unrealized P&L for an open position.

    Mutates and returns the same `position` object.
    """
    entry_price = float(position.entry_price)
    qty = int(position.qty)
    position.current_price = current_price
    position.unrealized_pnl = (current_price - entry_price) * qty
    return position


# ── Portfolio snapshot ────────────────────────────────────────────────────────

def build_portfolio_snapshot(
    *,
    run_id: str,
    cash: float,
    open_positions: list[Position],
    initial_capital: float,
    peak_equity: float,
    snapshot_type: str = SnapshotType.DAILY_CLOSE.value,
    snapshot_at: Optional[datetime] = None,
    positions_detail_json: Optional[str] = None,
) -> PortfolioSnapshot:
    """
    Build a PortfolioSnapshot from current system state.

    Args:
        run_id:              UUID of the active TradingRun
        cash:                current available cash
        open_positions:      list of open Position objects (mark-to-market)
        initial_capital:     starting capital (for cumulative return calc)
        peak_equity:         running high-water mark equity
        snapshot_type:       SnapshotType.value
        snapshot_at:         snapshot timestamp (defaults to now)
        positions_detail_json: serialised JSON of position list

    Returns:
        PortfolioSnapshot (unsaved)
    """
    now = snapshot_at or datetime.now(tz=timezone.utc)

    positions_value = sum(
        float(p.unrealized_pnl or 0) + float(p.entry_price) * int(p.qty)
        for p in open_positions
    )
    total_equity = cash + positions_value
    drawdown_pct = (
        (peak_equity - total_equity) / peak_equity if peak_equity > 0 else 0.0
    )
    cumulative_return_pct = (
        (total_equity - initial_capital) / initial_capital if initial_capital > 0 else 0.0
    )

    return PortfolioSnapshot(
        id=str(uuid.uuid4()),
        run_id=run_id,
        snapshot_type=snapshot_type,
        snapshot_at=now,
        cash=cash,
        positions_value=positions_value,
        total_equity=total_equity,
        open_positions_count=len(open_positions),
        peak_equity=peak_equity,
        drawdown_pct=max(drawdown_pct, 0.0),  # guard against negative (equity above peak)
        cumulative_return_pct=cumulative_return_pct,
        positions_detail=positions_detail_json,
    )
