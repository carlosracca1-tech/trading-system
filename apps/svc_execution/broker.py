"""
apps/svc_execution/broker.py
Broker abstraction layer.

Two implementations:
  DryRunBroker  — simulates all orders in memory (fills immediately at submitted price)
  AlpacaBroker  — calls Alpaca paper/live REST API

Both implement AbstractBroker so the executor is broker-agnostic.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from packages.shared.enums import OrderSide, OrderStatus, OrderType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Broker-level data types ───────────────────────────────────────────────────

@dataclass
class BrokerOrder:
    """Snapshot of an order returned by the broker API."""
    broker_order_id: str
    symbol: str
    side: str                              # OrderSide.value
    qty: int
    order_type: str                        # OrderType.value
    status: str                            # OrderStatus.value
    filled_qty: int = 0
    filled_avg_price: Optional[float] = None
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED.value

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.EXPIRED.value,
        )


@dataclass
class AccountInfo:
    """Broker account snapshot."""
    cash: float
    portfolio_value: float
    buying_power: float
    currency: str = "USD"


# ── Abstract broker interface ─────────────────────────────────────────────────

class AbstractBroker(ABC):
    """All broker implementations must satisfy this interface."""

    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: str,          # OrderSide.value
        qty: int,
        order_type: str = OrderType.MARKET.value,
        limit_price: Optional[float] = None,
        submitted_price: Optional[float] = None,
    ) -> BrokerOrder:
        """Submit an order. Returns a BrokerOrder reflecting the current state."""

    @abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """Fetch the current state of an existing order."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending order. Returns True if successful."""

    @abstractmethod
    def get_account_info(self) -> AccountInfo:
        """Fetch current account balances."""


# ── Dry-run broker ────────────────────────────────────────────────────────────

class DryRunBroker(AbstractBroker):
    """
    Simulated broker for dry-run and testing.

    Behaviour:
    - Orders fill IMMEDIATELY at submitted_price (or 0.0 if not provided)
    - No latency, no partial fills, no rejections (unless force_reject is used)
    - All orders are stored in memory; the store is reset between instances
    - Account starts with `initial_cash`
    """

    def __init__(self, initial_cash: float = 100_000.0):
        self.initial_cash = initial_cash
        self._orders: dict[str, BrokerOrder] = {}
        self._cash = initial_cash

    # ── Public interface ──────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = OrderType.MARKET.value,
        limit_price: Optional[float] = None,
        submitted_price: Optional[float] = None,
    ) -> BrokerOrder:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        now = datetime.now(tz=timezone.utc)
        fill_price = submitted_price or limit_price or 0.0

        order = BrokerOrder(
            broker_order_id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            status=OrderStatus.FILLED.value,
            filled_qty=qty,
            filled_avg_price=fill_price,
            submitted_at=now,
            filled_at=now,
        )
        self._orders[order.broker_order_id] = order

        # Update internal cash position
        notional = fill_price * qty
        if side == OrderSide.BUY.value:
            self._cash -= notional
        else:
            self._cash += notional

        log.debug(
            "dryrun_order_filled",
            symbol=symbol, side=side, qty=qty, price=fill_price,
            broker_id=order.broker_order_id,
        )
        return order

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise KeyError(f"Order {broker_order_id} not found in dry-run store")
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise KeyError(f"Order {broker_order_id} not found")
        if order.is_terminal:
            return False  # already terminal — cannot cancel
        order.status = OrderStatus.CANCELLED.value
        log.debug("dryrun_order_cancelled", broker_id=broker_order_id)
        return True

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            cash=self._cash,
            portfolio_value=self._cash,   # positions not tracked at broker level
            buying_power=max(self._cash, 0.0),
        )

    # ── Test helpers ──────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all stored orders; reset cash. Useful between test cases."""
        self._orders.clear()
        self._cash = self.initial_cash

    @property
    def order_count(self) -> int:
        return len(self._orders)


# ── Alpaca broker ─────────────────────────────────────────────────────────────

class AlpacaBroker(AbstractBroker):
    """
    Live/paper Alpaca REST broker.

    Requires env vars:
      ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self._client = self._build_client()

    def _build_client(self):
        """Build the alpaca-py REST client (lazy import to keep dep optional)."""
        try:
            from alpaca.trading.client import TradingClient  # type: ignore[import]
            return TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=("paper" in self.base_url),
            )
        except ImportError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaBroker. "
                "Install it with: pip install alpaca-py"
            ) from exc

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = OrderType.MARKET.value,
        limit_price: Optional[float] = None,
        submitted_price: Optional[float] = None,
    ) -> BrokerOrder:
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest  # type: ignore[import]
        from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce  # type: ignore[import]

        alpaca_side = AlpacaSide.BUY if side == OrderSide.BUY.value else AlpacaSide.SELL

        if order_type == OrderType.MARKET.value:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                time_in_force=TimeInForce.DAY,
            )
        else:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )

        resp = self._client.submit_order(req)
        return self._map_alpaca_order(resp)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        resp = self._client.get_order_by_id(broker_order_id)
        return self._map_alpaca_order(resp)

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(broker_order_id)
            return True
        except Exception:
            return False

    def get_account_info(self) -> AccountInfo:
        acct = self._client.get_account()
        return AccountInfo(
            cash=float(acct.cash),
            portfolio_value=float(acct.portfolio_value),
            buying_power=float(acct.buying_power),
        )

    @staticmethod
    def _map_alpaca_order(resp) -> BrokerOrder:
        """Convert Alpaca order response to our BrokerOrder."""
        _STATUS_MAP = {
            "new": OrderStatus.SUBMITTED.value,
            "partially_filled": OrderStatus.PARTIAL.value,
            "filled": OrderStatus.FILLED.value,
            "done_for_day": OrderStatus.EXPIRED.value,
            "canceled": OrderStatus.CANCELLED.value,
            "expired": OrderStatus.EXPIRED.value,
            "replaced": OrderStatus.CANCELLED.value,
            "pending_cancel": OrderStatus.SUBMITTED.value,
            "pending_replace": OrderStatus.SUBMITTED.value,
            "held": OrderStatus.SUBMITTED.value,
            "accepted": OrderStatus.SUBMITTED.value,
            "pending_new": OrderStatus.SUBMITTED.value,
        }
        status = _STATUS_MAP.get(str(resp.status), OrderStatus.UNKNOWN.value)
        filled_avg = (
            float(resp.filled_avg_price) if resp.filled_avg_price is not None else None
        )
        return BrokerOrder(
            broker_order_id=str(resp.id),
            symbol=str(resp.symbol),
            side=str(resp.side.value).lower(),
            qty=int(resp.qty or 0),
            order_type=str(resp.order_type.value).lower(),
            status=status,
            filled_qty=int(resp.filled_qty or 0),
            filled_avg_price=filled_avg,
            submitted_at=resp.submitted_at,
            filled_at=resp.filled_at,
        )
