"""
tests/test_models.py
Unit tests for SQLAlchemy model instantiation and business logic.
No database required — tests only Python object behavior.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from packages.shared.enums import (
    AlertSeverity,
    Direction,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecision,
    RunStatus,
    RunType,
    SignalType,
    SnapshotType,
)
from packages.shared.models.audit_log import AuditLog
from packages.shared.models.indicator import IndicatorCache
from packages.shared.models.market_data import MarketDataDaily
from packages.shared.models.order import Order
from packages.shared.models.portfolio_snapshot import PortfolioSnapshot
from packages.shared.models.position import Position
from packages.shared.models.risk_event import RiskEvent
from packages.shared.models.signal import Signal
from packages.shared.models.symbol import Symbol
from packages.shared.models.trading_run import TradingRun


# ── Helpers ────────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Symbol ─────────────────────────────────────────────────────────────────────

class TestSymbol:
    def test_basic_creation(self):
        s = Symbol(symbol="SPY", name="SPDR S&P 500 ETF Trust", sector="Equity-Broad")
        assert s.symbol == "SPY"
        assert s.name == "SPDR S&P 500 ETF Trust"
        assert s.is_active is True  # default

    def test_repr(self):
        s = Symbol(symbol="QQQ", name="Invesco QQQ", is_active=True)
        assert "QQQ" in repr(s)
        assert "active=True" in repr(s)

    def test_inactive_symbol(self):
        s = Symbol(symbol="UNUSED", name="Test ETF", is_active=False)
        assert s.is_active is False


# ── MarketDataDaily ────────────────────────────────────────────────────────────

class TestMarketDataDaily:
    def test_creation(self):
        bar = MarketDataDaily(
            symbol_id=_uuid(),
            symbol="SPY",
            date=date(2024, 1, 15),
            open=470.0,
            high=475.5,
            low=469.0,
            close=474.2,
            volume=85_000_000,
        )
        assert bar.symbol == "SPY"
        assert bar.close == 474.2
        assert bar.volume == 85_000_000

    def test_repr_contains_symbol_and_date(self):
        bar = MarketDataDaily(
            symbol_id=_uuid(),
            symbol="GLD",
            date=date(2024, 3, 1),
            open=180.0, high=182.0, low=179.5, close=181.0, volume=5_000_000,
        )
        r = repr(bar)
        assert "GLD" in r
        assert "181.0" in r


# ── IndicatorCache ─────────────────────────────────────────────────────────────

class TestIndicatorCache:
    def test_creation(self):
        ind = IndicatorCache(
            symbol_id=_uuid(),
            symbol="SPY",
            date=date(2024, 1, 15),
            ema_50=460.0,
            ema_200=430.0,
            rsi_14=62.5,
            atr_14=5.2,
            atr_14_pct=0.011,
            volume_ma_20=75_000_000,
            high_20d=473.0,
        )
        assert ind.ema_50 == 460.0
        assert ind.rsi_14 == 62.5

    def test_null_indicators_allowed(self):
        """Null indicators are valid — not enough history to compute."""
        ind = IndicatorCache(
            symbol_id=_uuid(),
            symbol="SPY",
            date=date(2020, 1, 2),
            ema_50=None,
            ema_200=None,
        )
        assert ind.ema_200 is None


# ── TradingRun ─────────────────────────────────────────────────────────────────

class TestTradingRun:
    def test_creation(self):
        run = TradingRun(
            run_type=RunType.PAPER.value,
            status=RunStatus.RUNNING.value,
            started_at=_now(),
            initial_capital=100_000.0,
        )
        assert run.run_type == "PAPER"
        assert run.total_trades == 0

    def test_win_rate_zero_trades(self):
        run = TradingRun(
            run_type=RunType.PAPER.value,
            status=RunStatus.RUNNING.value,
            started_at=_now(),
            initial_capital=100_000.0,
            total_trades=0,
            winning_trades=0,
        )
        assert run.win_rate is None

    def test_win_rate_calculation(self):
        run = TradingRun(
            run_type=RunType.PAPER.value,
            status=RunStatus.COMPLETED.value,
            started_at=_now(),
            initial_capital=100_000.0,
            total_trades=10,
            winning_trades=7,
            losing_trades=3,
        )
        assert run.win_rate == pytest.approx(0.7)

    def test_enum_properties(self):
        run = TradingRun(
            run_type=RunType.LIVE.value,
            status=RunStatus.PAUSED.value,
            started_at=_now(),
            initial_capital=50_000.0,
        )
        assert run.run_type_enum == RunType.LIVE
        assert run.status_enum == RunStatus.PAUSED


# ── Signal ─────────────────────────────────────────────────────────────────────

class TestSignal:
    def test_creation(self):
        sig = Signal(
            run_id=_uuid(),
            symbol="XLK",
            signal_date=date(2024, 2, 1),
            signal_type=SignalType.ENTER.value,
            direction=Direction.LONG.value,
            close_price=195.0,
            risk_decision=RiskDecision.APPROVED.value,
        )
        assert sig.signal_type == "ENTER"
        assert sig.risk_decision == "APPROVED"

    def test_enum_properties(self):
        sig = Signal(
            run_id=_uuid(),
            symbol="SPY",
            signal_date=date(2024, 1, 1),
            signal_type=SignalType.HOLD.value,
            direction=Direction.LONG.value,
            close_price=470.0,
            risk_decision=RiskDecision.REJECTED.value,
        )
        assert sig.signal_type_enum == SignalType.HOLD
        assert sig.risk_decision_enum == RiskDecision.REJECTED


# ── Order ──────────────────────────────────────────────────────────────────────

class TestOrder:
    def _make_order(self, **kwargs) -> Order:
        defaults = dict(
            run_id=_uuid(),
            symbol="SPY",
            side=OrderSide.BUY.value,
            qty=10,
            status=OrderStatus.PENDING.value,
            correlation_id=_uuid(),
        )
        defaults.update(kwargs)
        return Order(**defaults)

    def test_creation(self):
        o = self._make_order()
        assert o.symbol == "SPY"
        assert o.qty == 10
        assert o.filled_qty == 0

    def test_is_filled_false_by_default(self):
        o = self._make_order(status=OrderStatus.PENDING.value)
        assert o.is_filled is False

    def test_is_filled_true(self):
        o = self._make_order(
            status=OrderStatus.FILLED.value,
            filled_price=470.0,
            filled_qty=10,
        )
        assert o.is_filled is True

    def test_is_terminal_states(self):
        for terminal_status in [
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED,
        ]:
            o = self._make_order(status=terminal_status.value)
            assert o.is_terminal is True

    def test_non_terminal_states(self):
        for non_terminal in [OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL]:
            o = self._make_order(status=non_terminal.value)
            assert o.is_terminal is False

    def test_total_cost_unfilled(self):
        o = self._make_order()
        assert o.total_cost == 0.0

    def test_total_cost_filled(self):
        o = self._make_order(
            status=OrderStatus.FILLED.value,
            filled_price=100.0,
            filled_qty=5,
            commission=2.5,
            slippage=0.5,
        )
        assert o.total_cost == pytest.approx(503.0)  # 500 + 2.5 + 0.5


# ── Position ────────────────────────────────────────────────────────────────────

class TestPosition:
    def _make_position(self, **kwargs) -> Position:
        defaults = dict(
            run_id=_uuid(),
            symbol="QQQ",
            status=PositionStatus.OPEN.value,
            direction=Direction.LONG.value,
            entry_order_id=_uuid(),
            qty=5,
            entry_price=400.0,
            stop_loss=380.0,
            opened_at=_now(),
        )
        defaults.update(kwargs)
        return Position(**defaults)

    def test_creation(self):
        p = self._make_position()
        assert p.symbol == "QQQ"
        assert p.is_open is True

    def test_is_open_false_when_closed(self):
        p = self._make_position(
            status=PositionStatus.CLOSED.value,
            exit_price=420.0,
            realized_pnl=100.0,
            closed_at=_now(),
            close_reason="signal_exit",
        )
        assert p.is_open is False

    def test_net_pnl_with_commission(self):
        p = self._make_position(
            realized_pnl=100.0,
            commission_total=7.5,
        )
        assert p.net_pnl == pytest.approx(92.5)

    def test_net_pnl_no_realized(self):
        p = self._make_position(realized_pnl=None)
        assert p.net_pnl == 0.0


# ── PortfolioSnapshot ──────────────────────────────────────────────────────────

class TestPortfolioSnapshot:
    def test_creation(self):
        snap = PortfolioSnapshot(
            run_id=_uuid(),
            snapshot_type=SnapshotType.DAILY_CLOSE.value,
            snapshot_at=_now(),
            cash=50_000.0,
            positions_value=55_000.0,
            total_equity=105_000.0,
            open_positions_count=3,
            peak_equity=110_000.0,
            drawdown_pct=0.045,
            cumulative_return_pct=0.05,
        )
        assert snap.total_equity == 105_000.0
        assert snap.drawdown_pct == pytest.approx(0.045)


# ── AuditLog ───────────────────────────────────────────────────────────────────

class TestAuditLog:
    def test_write_factory(self):
        entry = AuditLog.write(
            event_type="order.submitted",
            actor="svc_execution",
            correlation_id=_uuid(),
            severity=AlertSeverity.INFO,
            entity_type="order",
            entity_id=_uuid(),
            payload='{"symbol": "SPY", "qty": 10}',
        )
        assert entry.event_type == "order.submitted"
        assert entry.actor == "svc_execution"
        assert entry.severity == "INFO"
        assert entry.occurred_at is not None
        assert entry.occurred_at.tzinfo is not None  # timezone-aware

    def test_write_critical(self):
        entry = AuditLog.write(
            event_type="kill_switch.triggered",
            actor="risk_engine",
            correlation_id=_uuid(),
            severity=AlertSeverity.CRITICAL,
        )
        assert entry.severity == "CRITICAL"

    def test_repr(self):
        entry = AuditLog.write(
            event_type="order.filled",
            actor="svc_execution",
            correlation_id=_uuid(),
        )
        assert "order.filled" in repr(entry)


# ── RiskEvent ──────────────────────────────────────────────────────────────────

class TestRiskEvent:
    def test_rejected_factory(self):
        ev = RiskEvent.rejected(
            rule_code="P1_MAX_DRAWDOWN",
            rule_priority="P1",
            correlation_id=_uuid(),
            rejection_reason="Drawdown 16.2% exceeds 15% warning threshold",
            symbol="SPY",
        )
        assert ev.decision == RiskDecision.REJECTED.value
        assert ev.rule_code == "P1_MAX_DRAWDOWN"
        assert ev.triggered_at is not None

    def test_repr(self):
        ev = RiskEvent.rejected(
            rule_code="P2_POSITION_LIMIT",
            rule_priority="P2",
            correlation_id=_uuid(),
            rejection_reason="Max 10 positions reached",
        )
        assert "P2_POSITION_LIMIT" in repr(ev)
        assert "REJECTED" in repr(ev)
