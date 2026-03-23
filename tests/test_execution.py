"""
tests/test_execution.py
Unit tests for the Execution Service.

Pure computation — no DB required.
Tests cover:
  - DryRunBroker.submit_order()  / get_order() / cancel_order()
  - build_entry_order()          (order field population)
  - build_exit_order()
  - apply_broker_fill()          (order lifecycle)
  - cancel_order()
  - open_position()              (position creation from filled order)
  - close_position()             (position lifecycle + realized P&L)
  - update_unrealized_pnl()
  - build_portfolio_snapshot()   (equity / drawdown / cumulative return)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import pytest

from apps.svc_execution.broker import AccountInfo, BrokerOrder, DryRunBroker
from apps.svc_execution.executor import (
    apply_broker_fill,
    build_entry_order,
    build_exit_order,
    build_portfolio_snapshot,
    cancel_order,
    close_position,
    open_position,
    update_unrealized_pnl,
)
from apps.svc_risk.engine import EvaluationResult
from apps.svc_risk.position_sizer import SizingResult
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.enums import (
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskDecision,
    SignalType,
    SnapshotType,
)
from packages.shared.models.order import Order
from packages.shared.models.position import Position

TODAY = date(2024, 6, 15)
RUN_ID = "run-abc-123"
NOW = datetime(2024, 6, 15, 16, 0, 0, tzinfo=timezone.utc)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _enter_signal(
    symbol: str = "SPY",
    close: float = 200.0,
    atr: float = 3.0,
) -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.ENTER.value,
        close_price=close,
        atr_14=atr,
        ema_50=195.0,
        ema_200=180.0,
        rsi_14=62.0,
        regime_ok=True,
    )


def _exit_signal(symbol: str = "SPY") -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.EXIT.value,
        close_price=190.0,
        reason="close_below_ema50",
    )


def _sizing(shares: int = 50, stop_price: float = 194.0) -> SizingResult:
    return SizingResult(
        shares=shares,
        stop_price=stop_price,
        risk_amount=1_000.0,
        notional_value=shares * 200.0,
        pct_of_portfolio=0.10,
        rejection_reason=None,
    )


def _approved_evaluation(shares: int = 50) -> EvaluationResult:
    return EvaluationResult(
        decision=RiskDecision.APPROVED.value,
        rule_code=None,
        rejection_reason=None,
        sizing=_sizing(shares),
    )


def _filled_buy_order(
    symbol: str = "SPY",
    qty: int = 50,
    filled_price: float = 200.0,
    stop_price: float = 194.0,
) -> Order:
    """Build a pre-filled BUY order for use in position tests."""
    order = Order(
        id="order-buy-001",
        run_id=RUN_ID,
        symbol=symbol,
        side=OrderSide.BUY.value,
        qty=qty,
        order_type=OrderType.MARKET.value,
        stop_price=stop_price,
        submitted_price=filled_price,
        status=OrderStatus.FILLED.value,
        correlation_id="corr-001",
    )
    order.filled_qty = qty
    order.filled_price = filled_price
    order.filled_at = NOW
    return order


def _open_position(
    symbol: str = "SPY",
    qty: int = 50,
    entry_price: float = 200.0,
    stop_loss: float = 194.0,
) -> Position:
    """Build an open Position directly (no order needed)."""
    return Position(
        id="pos-001",
        run_id=RUN_ID,
        symbol=symbol,
        status=PositionStatus.OPEN.value,
        direction=Direction.LONG.value,
        entry_order_id="order-buy-001",
        qty=qty,
        entry_price=entry_price,
        stop_loss=stop_loss,
        opened_at=NOW,
    )


def _filled_sell_order(
    symbol: str = "SPY",
    qty: int = 50,
    filled_price: float = 210.0,
) -> Order:
    order = Order(
        id="order-sell-001",
        run_id=RUN_ID,
        symbol=symbol,
        side=OrderSide.SELL.value,
        qty=qty,
        order_type=OrderType.MARKET.value,
        submitted_price=filled_price,
        status=OrderStatus.FILLED.value,
        correlation_id="corr-002",
    )
    order.filled_qty = qty
    order.filled_price = filled_price
    order.filled_at = NOW
    return order


# ── TestDryRunBroker ───────────────────────────────────────────────────────────

class TestDryRunBroker:
    def test_submit_buy_fills_immediately(self):
        broker = DryRunBroker()
        result = broker.submit_order("SPY", OrderSide.BUY.value, 10, submitted_price=200.0)
        assert result.is_filled
        assert result.status == OrderStatus.FILLED.value
        assert result.filled_qty == 10
        assert result.filled_avg_price == pytest.approx(200.0)

    def test_submit_sell_fills_immediately(self):
        broker = DryRunBroker()
        result = broker.submit_order("SPY", OrderSide.SELL.value, 5, submitted_price=210.0)
        assert result.is_filled
        assert result.filled_qty == 5

    def test_broker_order_id_is_uuid(self):
        broker = DryRunBroker()
        result = broker.submit_order("QQQ", OrderSide.BUY.value, 3, submitted_price=400.0)
        import uuid
        # Should not raise
        uuid.UUID(result.broker_order_id)

    def test_get_order_returns_same_order(self):
        broker = DryRunBroker()
        submitted = broker.submit_order("SPY", OrderSide.BUY.value, 5, submitted_price=200.0)
        fetched = broker.get_order(submitted.broker_order_id)
        assert fetched.broker_order_id == submitted.broker_order_id
        assert fetched.filled_qty == 5

    def test_get_order_unknown_id_raises(self):
        broker = DryRunBroker()
        with pytest.raises(KeyError):
            broker.get_order("nonexistent-id")

    def test_cancel_terminal_order_returns_false(self):
        broker = DryRunBroker()
        order = broker.submit_order("SPY", OrderSide.BUY.value, 5, submitted_price=200.0)
        # Already filled — cancel should return False
        result = broker.cancel_order(order.broker_order_id)
        assert result is False

    def test_cancel_unknown_raises(self):
        broker = DryRunBroker()
        with pytest.raises(KeyError):
            broker.cancel_order("does-not-exist")

    def test_cash_decreases_on_buy(self):
        broker = DryRunBroker(initial_cash=100_000.0)
        broker.submit_order("SPY", OrderSide.BUY.value, 10, submitted_price=200.0)
        info = broker.get_account_info()
        assert info.cash == pytest.approx(100_000.0 - 10 * 200.0)

    def test_cash_increases_on_sell(self):
        broker = DryRunBroker(initial_cash=50_000.0)
        broker.submit_order("SPY", OrderSide.SELL.value, 5, submitted_price=210.0)
        info = broker.get_account_info()
        assert info.cash == pytest.approx(50_000.0 + 5 * 210.0)

    def test_multiple_orders_tracked(self):
        broker = DryRunBroker()
        broker.submit_order("SPY", OrderSide.BUY.value, 10, submitted_price=200.0)
        broker.submit_order("QQQ", OrderSide.BUY.value, 5, submitted_price=400.0)
        assert broker.order_count == 2

    def test_reset_clears_orders_and_cash(self):
        broker = DryRunBroker(initial_cash=100_000.0)
        broker.submit_order("SPY", OrderSide.BUY.value, 10, submitted_price=200.0)
        broker.reset()
        assert broker.order_count == 0
        assert broker.get_account_info().cash == pytest.approx(100_000.0)

    def test_zero_qty_raises(self):
        broker = DryRunBroker()
        with pytest.raises(ValueError):
            broker.submit_order("SPY", OrderSide.BUY.value, 0, submitted_price=200.0)

    def test_is_terminal_property(self):
        broker_order = BrokerOrder(
            broker_order_id="x",
            symbol="SPY",
            side="buy",
            qty=10,
            order_type="market",
            status=OrderStatus.FILLED.value,
        )
        assert broker_order.is_terminal is True

    def test_is_not_terminal_submitted(self):
        broker_order = BrokerOrder(
            broker_order_id="x",
            symbol="SPY",
            side="buy",
            qty=10,
            order_type="market",
            status=OrderStatus.SUBMITTED.value,
        )
        assert broker_order.is_terminal is False


# ── TestBuildEntryOrder ────────────────────────────────────────────────────────

class TestBuildEntryOrder:
    def test_side_is_buy(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.side == OrderSide.BUY.value

    def test_qty_matches_sizing_shares(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(shares=33),
            run_id=RUN_ID,
        )
        assert order.qty == 33

    def test_symbol_matches_signal(self):
        order = build_entry_order(
            signal=_enter_signal(symbol="QQQ"),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.symbol == "QQQ"

    def test_order_type_is_market(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.order_type == OrderType.MARKET.value

    def test_status_is_pending(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.status == OrderStatus.PENDING.value

    def test_stop_price_from_sizing(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        # _sizing() returns stop_price=194.0
        assert float(order.stop_price) == pytest.approx(194.0)

    def test_submitted_price_is_close_price(self):
        order = build_entry_order(
            signal=_enter_signal(close=205.5),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert float(order.submitted_price) == pytest.approx(205.5)

    def test_run_id_set(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.run_id == RUN_ID

    def test_no_sizing_raises(self):
        bad_eval = EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None,
            rejection_reason=None,
            sizing=None,
        )
        with pytest.raises(ValueError, match="sizing"):
            build_entry_order(signal=_enter_signal(), evaluation=bad_eval, run_id=RUN_ID)

    def test_correlation_id_auto_generated(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
        )
        assert order.correlation_id is not None
        assert len(order.correlation_id) == 36  # UUID format

    def test_custom_correlation_id(self):
        order = build_entry_order(
            signal=_enter_signal(),
            evaluation=_approved_evaluation(),
            run_id=RUN_ID,
            correlation_id="my-corr-id",
        )
        assert order.correlation_id == "my-corr-id"


# ── TestBuildExitOrder ────────────────────────────────────────────────────────

class TestBuildExitOrder:
    def test_side_is_sell(self):
        order = build_exit_order(
            signal=_exit_signal(),
            position=_open_position(),
            run_id=RUN_ID,
        )
        assert order.side == OrderSide.SELL.value

    def test_qty_matches_position(self):
        order = build_exit_order(
            signal=_exit_signal(),
            position=_open_position(qty=33),
            run_id=RUN_ID,
        )
        assert order.qty == 33

    def test_submitted_price_is_close(self):
        signal = _exit_signal()
        order = build_exit_order(signal=signal, position=_open_position(), run_id=RUN_ID)
        assert float(order.submitted_price) == pytest.approx(190.0)

    def test_status_is_pending(self):
        order = build_exit_order(
            signal=_exit_signal(), position=_open_position(), run_id=RUN_ID
        )
        assert order.status == OrderStatus.PENDING.value


# ── TestApplyBrokerFill ───────────────────────────────────────────────────────

class TestApplyBrokerFill:
    def _pending_order(self) -> Order:
        return Order(
            id="order-001",
            run_id=RUN_ID,
            symbol="SPY",
            side=OrderSide.BUY.value,
            qty=50,
            order_type=OrderType.MARKET.value,
            status=OrderStatus.PENDING.value,
            correlation_id="corr-001",
        )

    def _broker_fill(self, status=OrderStatus.FILLED.value) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id="broker-abc",
            symbol="SPY",
            side="buy",
            qty=50,
            order_type="market",
            status=status,
            filled_qty=50 if status == OrderStatus.FILLED.value else 0,
            filled_avg_price=200.5 if status == OrderStatus.FILLED.value else None,
            submitted_at=NOW,
            filled_at=NOW if status == OrderStatus.FILLED.value else None,
        )

    def test_broker_id_applied(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert order.broker_order_id == "broker-abc"

    def test_status_updated_to_filled(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert order.status == OrderStatus.FILLED.value

    def test_filled_qty_set(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert order.filled_qty == 50

    def test_filled_price_set(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert float(order.filled_price) == pytest.approx(200.5)

    def test_filled_at_set(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert order.filled_at is not None

    def test_unfilled_order_no_filled_at(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill(status=OrderStatus.SUBMITTED.value))
        assert order.filled_at is None

    def test_is_filled_property(self):
        order = self._pending_order()
        apply_broker_fill(order, self._broker_fill())
        assert order.is_filled is True


# ── TestCancelOrder ───────────────────────────────────────────────────────────

class TestCancelOrder:
    def test_pending_order_cancels(self):
        order = Order(
            id="order-001",
            run_id=RUN_ID,
            symbol="SPY",
            side=OrderSide.BUY.value,
            qty=50,
            order_type=OrderType.MARKET.value,
            status=OrderStatus.PENDING.value,
            correlation_id="corr-001",
        )
        result = cancel_order(order)
        assert result.status == OrderStatus.CANCELLED.value
        assert result.cancelled_at is not None

    def test_filled_order_raises(self):
        order = _filled_buy_order()
        with pytest.raises(ValueError, match="terminal"):
            cancel_order(order)


# ── TestOpenPosition ──────────────────────────────────────────────────────────

class TestOpenPosition:
    def test_creates_open_position(self):
        order = _filled_buy_order()
        pos = open_position(order=order, run_id=RUN_ID)
        assert pos.status == PositionStatus.OPEN.value

    def test_direction_is_long(self):
        order = _filled_buy_order()
        pos = open_position(order=order, run_id=RUN_ID)
        assert pos.direction == Direction.LONG.value

    def test_entry_price_from_filled_price(self):
        order = _filled_buy_order(filled_price=201.5)
        pos = open_position(order=order, run_id=RUN_ID)
        assert float(pos.entry_price) == pytest.approx(201.5)

    def test_qty_from_filled_qty(self):
        order = _filled_buy_order(qty=33)
        pos = open_position(order=order, run_id=RUN_ID)
        assert int(pos.qty) == 33

    def test_stop_loss_from_order(self):
        order = _filled_buy_order(stop_price=190.0)
        pos = open_position(order=order, run_id=RUN_ID)
        assert float(pos.stop_loss) == pytest.approx(190.0)

    def test_entry_order_id_set(self):
        order = _filled_buy_order()
        pos = open_position(order=order, run_id=RUN_ID)
        assert pos.entry_order_id == order.id

    def test_run_id_set(self):
        pos = open_position(order=_filled_buy_order(), run_id=RUN_ID)
        assert pos.run_id == RUN_ID

    def test_sell_order_raises(self):
        sell_order = _filled_sell_order()
        with pytest.raises(ValueError, match="BUY"):
            open_position(order=sell_order, run_id=RUN_ID)

    def test_unfilled_order_raises(self):
        order = Order(
            id="order-001",
            run_id=RUN_ID,
            symbol="SPY",
            side=OrderSide.BUY.value,
            qty=50,
            order_type=OrderType.MARKET.value,
            status=OrderStatus.PENDING.value,
            correlation_id="corr-001",
        )
        with pytest.raises(ValueError, match="unfilled"):
            open_position(order=order, run_id=RUN_ID)


# ── TestClosePosition ─────────────────────────────────────────────────────────

class TestClosePosition:
    def test_position_is_closed(self):
        pos = _open_position()
        sell = _filled_sell_order(filled_price=210.0)
        close_position(position=pos, exit_order=sell, close_reason="signal_exit")
        assert pos.status == PositionStatus.CLOSED.value

    def test_exit_price_set(self):
        pos = _open_position()
        sell = _filled_sell_order(filled_price=210.0)
        close_position(position=pos, exit_order=sell, close_reason="signal_exit")
        assert float(pos.exit_price) == pytest.approx(210.0)

    def test_realized_pnl_profit(self):
        # entry=200, exit=210, qty=50 → PnL = (210-200)*50 = 500
        pos = _open_position(entry_price=200.0, qty=50)
        sell = _filled_sell_order(filled_price=210.0, qty=50)
        close_position(position=pos, exit_order=sell, close_reason="signal_exit")
        assert float(pos.realized_pnl) == pytest.approx(500.0)

    def test_realized_pnl_loss(self):
        # entry=200, exit=190, qty=50 → PnL = (190-200)*50 = -500
        pos = _open_position(entry_price=200.0, qty=50)
        sell = _filled_sell_order(filled_price=190.0, qty=50)
        close_position(position=pos, exit_order=sell, close_reason="stop_loss")
        assert float(pos.realized_pnl) == pytest.approx(-500.0)

    def test_close_reason_stored(self):
        pos = _open_position()
        sell = _filled_sell_order()
        close_position(position=pos, exit_order=sell, close_reason="death_cross")
        assert pos.close_reason == "death_cross"

    def test_exit_order_id_set(self):
        pos = _open_position()
        sell = _filled_sell_order()
        close_position(position=pos, exit_order=sell, close_reason="signal_exit")
        assert pos.exit_order_id == sell.id

    def test_unfilled_exit_raises(self):
        pos = _open_position()
        unfilled_sell = Order(
            id="order-sell-bad",
            run_id=RUN_ID,
            symbol="SPY",
            side=OrderSide.SELL.value,
            qty=50,
            order_type=OrderType.MARKET.value,
            status=OrderStatus.SUBMITTED.value,
            correlation_id="corr-003",
        )
        with pytest.raises(ValueError, match="unfilled"):
            close_position(position=pos, exit_order=unfilled_sell, close_reason="exit")


# ── TestUpdateUnrealizedPnl ───────────────────────────────────────────────────

class TestUpdateUnrealizedPnl:
    def test_unrealized_profit(self):
        pos = _open_position(entry_price=200.0, qty=50)
        update_unrealized_pnl(pos, current_price=210.0)
        assert float(pos.unrealized_pnl) == pytest.approx(500.0)

    def test_unrealized_loss(self):
        pos = _open_position(entry_price=200.0, qty=50)
        update_unrealized_pnl(pos, current_price=195.0)
        assert float(pos.unrealized_pnl) == pytest.approx(-250.0)

    def test_current_price_stored(self):
        pos = _open_position()
        update_unrealized_pnl(pos, current_price=205.0)
        assert float(pos.current_price) == pytest.approx(205.0)

    def test_breakeven_zero_pnl(self):
        pos = _open_position(entry_price=200.0, qty=50)
        update_unrealized_pnl(pos, current_price=200.0)
        assert float(pos.unrealized_pnl) == pytest.approx(0.0)


# ── TestBuildPortfolioSnapshot ────────────────────────────────────────────────

class TestBuildPortfolioSnapshot:
    def test_basic_equity_calculation(self):
        """total_equity = cash + positions_value"""
        pos = _open_position(entry_price=200.0, qty=50)
        pos.unrealized_pnl = 500.0   # +$500 unrealized
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=80_000.0,
            open_positions=[pos],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        # positions_value = 500 + 200*50 = 10_500
        assert float(snapshot.positions_value) == pytest.approx(10_500.0)
        assert float(snapshot.total_equity) == pytest.approx(90_500.0)

    def test_empty_portfolio(self):
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=100_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        assert float(snapshot.positions_value) == pytest.approx(0.0)
        assert float(snapshot.total_equity) == pytest.approx(100_000.0)

    def test_drawdown_calculation(self):
        """drawdown = (peak - equity) / peak"""
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=90_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        # (100k - 90k) / 100k = 10%
        assert float(snapshot.drawdown_pct) == pytest.approx(0.10, abs=0.001)

    def test_drawdown_never_negative(self):
        """When equity > peak, drawdown should be 0 (not negative)."""
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=110_000.0,        # above peak
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,  # stale peak (snapshot built before peak update)
            snapshot_at=NOW,
        )
        assert float(snapshot.drawdown_pct) >= 0.0

    def test_cumulative_return_positive(self):
        """cumulative_return = (equity - initial) / initial"""
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=110_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=110_000.0,
            snapshot_at=NOW,
        )
        assert float(snapshot.cumulative_return_pct) == pytest.approx(0.10, abs=0.001)

    def test_cumulative_return_negative(self):
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=95_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        assert float(snapshot.cumulative_return_pct) == pytest.approx(-0.05, abs=0.001)

    def test_open_positions_count(self):
        pos1 = _open_position(symbol="SPY")
        pos2 = _open_position(symbol="QQQ")
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=80_000.0,
            open_positions=[pos1, pos2],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        assert snapshot.open_positions_count == 2

    def test_run_id_set(self):
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=100_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        assert snapshot.run_id == RUN_ID

    def test_snapshot_type_default_daily_close(self):
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=100_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
        )
        assert snapshot.snapshot_type == SnapshotType.DAILY_CLOSE.value

    def test_custom_snapshot_type(self):
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=100_000.0,
            open_positions=[],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
            snapshot_at=NOW,
            snapshot_type=SnapshotType.HOURLY.value,
        )
        assert snapshot.snapshot_type == SnapshotType.HOURLY.value

    def test_zero_initial_capital_no_crash(self):
        """Guard against division by zero on initial_capital."""
        snapshot = build_portfolio_snapshot(
            run_id=RUN_ID,
            cash=0.0,
            open_positions=[],
            initial_capital=0.0,
            peak_equity=0.0,
            snapshot_at=NOW,
        )
        assert snapshot.cumulative_return_pct == pytest.approx(0.0)
