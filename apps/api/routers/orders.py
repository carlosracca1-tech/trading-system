"""
apps/api/routers/orders.py
Order history endpoints.

GET /api/v1/orders        → paginated order list
GET /api/v1/orders/{id}   → single order detail
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import require_api_key
from apps.api.schemas import OrderListOut, OrderOut
from packages.shared.db import get_db

router = APIRouter(prefix="/orders", tags=["orders"])


def _get_db():
    yield from get_db()


@router.get("", response_model=OrderListOut, dependencies=[Depends(require_api_key)])
def list_orders(
    run_id: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    status: str | None = Query(default=None,
                                description="pending | submitted | filled | cancelled | rejected"),
    side: str | None = Query(default=None, description="buy | sell"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
):
    """List orders with optional filters. Newest first."""
    from sqlalchemy import select, func
    from packages.shared.models.order import Order
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus

    if run_id is None:
        run_id = db.scalar(
            select(TradingRun.id)
            .where(TradingRun.status == RunStatus.RUNNING.value)
            .limit(1)
        )

    q = select(Order)
    if run_id:
        q = q.where(Order.run_id == run_id)
    if symbol:
        q = q.where(Order.symbol == symbol.upper())
    if status:
        q = q.where(Order.status == status.lower())
    if side:
        q = q.where(Order.side == side.lower())

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    orders = list(
        db.scalars(q.order_by(Order.created_at.desc()).limit(limit).offset(offset)).all()
    )
    return OrderListOut(orders=orders, total=total)


@router.get("/{order_id}", response_model=OrderOut, dependencies=[Depends(require_api_key)])
def get_order(order_id: str, db: Session = Depends(_get_db)):
    """Get a single order by ID."""
    from packages.shared.models.order import Order

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return order
