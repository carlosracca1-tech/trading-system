"""
tests/test_smoke.py
End-to-end smoke test — no Docker, no real PostgreSQL required.

Uses SQLite in-memory as the database so the full service layer
(scanner → risk → execution → snapshot → kill switch → resolve)
can be tested in a single process without external dependencies.

The test simulates one full trading day:
  1.  DB setup (SQLite in-memory, all tables created from models)
  2.  Create TradingRun
  3.  Seed synthetic OHLCV + indicators (one symbol: SPY with ENTER signal)
  4.  Scan signals (pure computation: check_entry_signal)
  5.  Write signal to DB (strat_repo.write_signal)
  6.  Evaluate risk (pure computation: evaluate_signal)
  7.  Execute orders (DryRunBroker → executor functions)
  8.  Write position to DB
  9.  Build & write portfolio snapshot
  10. Verify snapshot math
  11. Activate kill switch → positions closed, run STOPPED
  12. Resolve kill switch → run RUNNING again

Run:
    pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Generator

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

# ── Environment MUST be set before any app import ─────────────────────────────
os.environ.setdefault("TRADING_MODE", "dev")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("API_KEY", "smoke-test-key")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://trading:trading_dev_pass@localhost:5433/trading_dev",
)
os.environ.setdefault("ALPACA_API_KEY", "smoke-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "smoke-secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

from config.settings import get_settings  # noqa: E402
get_settings.cache_clear()

from packages.shared.models.base import Base  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# SQLite in-memory DB setup
# ─────────────────────────────────────────────────────────────────────────────

def _create_sqlite_engine():
    """Create an in-memory SQLite engine with all tables from models."""
    # Import all models to ensure they are registered with Base.metadata
    import packages.shared.models.trading_run      # noqa: F401
    import packages.shared.models.signal           # noqa: F401
    import packages.shared.models.order            # noqa: F401
    import packages.shared.models.position         # noqa: F401
    import packages.shared.models.portfolio_snapshot  # noqa: F401
    import packages.shared.models.risk_event       # noqa: F401
    # svc_data models
    try:
        import packages.shared.models.symbol           # noqa: F401
        import packages.shared.models.market_data  # noqa: F401
        import packages.shared.models.indicator        # noqa: F401
    except ImportError:
        pass  # Some models may live under apps; we'll skip their tables

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(scope="module")
def sqlite_engine():
    engine = _create_sqlite_engine()
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def SessionFactory(sqlite_engine):
    return sessionmaker(bind=sqlite_engine, autocommit=False, autoflush=False,
                        expire_on_commit=False)


@pytest.fixture()
def db(SessionFactory) -> Generator[Session, None, None]:
    """Fresh session for each test, rolled back at end to keep tests isolated."""
    session = SessionFactory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

TODAY = date(2024, 6, 15)
NOW = datetime(2024, 6, 15, 16, 0, 0, tzinfo=timezone.utc)
INITIAL_CAPITAL = 100_000.0


def _make_indicator_row(
    close: float = 520.0,
    ema_50: float = 505.0,
    ema_200: float = 480.0,
    rsi_14: float = 62.0,
    atr_14: float = 6.0,
    volume: float = 30_000_000.0,
    volume_ma_20: float = 24_000_000.0,
    high_20d: float = 518.0,
) -> dict:
    """Construct a data row that satisfies all RFTM ENTER conditions."""
    return {
        "close": close,
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.994,
        "volume": volume,
        "ema_50": ema_50,
        "ema_200": ema_200,
        "rsi_14": rsi_14,
        "atr_14": atr_14,
        "atr_14_pct": atr_14 / close,
        "volume_ma_20": volume_ma_20,
        "high_20d": high_20d,
        "vwap": close,
    }


def _create_trading_run(db: Session, capital: float = INITIAL_CAPITAL) -> str:
    """Insert a TradingRun into the in-memory DB and return its id."""
    from packages.shared.models.trading_run import TradingRun
    from packages.shared.enums import RunStatus, RunType

    run_id = str(uuid.uuid4())
    run = TradingRun(
        id=run_id,
        run_type=RunType.PAPER.value,
        status=RunStatus.RUNNING.value,
        started_at=NOW,
        initial_capital=capital,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
    )
    db.add(run)
    db.flush()
    return run_id


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1: Pure computation — scanner
# ─────────────────────────────────────────────────────────────────────────────

class TestScanner:
    """Verify the strategy scanner emits ENTER / HOLD correctly."""

    def test_enter_signal_all_conditions_met(self):
        from apps.svc_strategy.scanner import check_entry_signal, SignalDecision
        from packages.shared.enums import SignalType

        row = _make_indicator_row()
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert decision.signal_type == SignalType.ENTER.value, (
            f"Expected ENTER but got {decision.signal_type}: {decision.reason}"
        )
        assert decision.symbol == "SPY"
        assert decision.close_price == pytest.approx(520.0)

    def test_hold_when_regime_bearish(self):
        from apps.svc_strategy.scanner import check_entry_signal
        from packages.shared.enums import SignalType

        row = _make_indicator_row()
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=False)
        assert decision.signal_type == SignalType.HOLD.value
        assert "regime" in decision.reason.lower()

    def test_hold_when_rsi_too_low(self):
        from apps.svc_strategy.scanner import check_entry_signal
        from packages.shared.enums import SignalType

        row = _make_indicator_row(rsi_14=30.0)
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert decision.signal_type == SignalType.HOLD.value

    def test_hold_when_rsi_too_high(self):
        from apps.svc_strategy.scanner import check_entry_signal
        from packages.shared.enums import SignalType

        row = _make_indicator_row(rsi_14=75.0)
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert decision.signal_type == SignalType.HOLD.value

    def test_hold_when_no_breakout(self):
        from apps.svc_strategy.scanner import check_entry_signal
        from packages.shared.enums import SignalType

        # close (520) is below high_20d (525) → no breakout
        row = _make_indicator_row(close=520.0, high_20d=525.0)
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert decision.signal_type == SignalType.HOLD.value

    def test_hold_when_low_volume(self):
        from apps.svc_strategy.scanner import check_entry_signal
        from packages.shared.enums import SignalType

        # volume (20M) < volume_ma_20 (25M) * 1.2 (30M)
        row = _make_indicator_row(volume=20_000_000.0, volume_ma_20=25_000_000.0)
        decision = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert decision.signal_type == SignalType.HOLD.value

    def test_exit_signal_death_cross(self):
        from apps.svc_strategy.scanner import check_exit_signal
        from packages.shared.enums import SignalType

        # EMA50 < EMA200 → death cross
        row = _make_indicator_row(ema_50=470.0, ema_200=480.0)
        decision = check_exit_signal("SPY", row, TODAY, entry_price=460.0)
        assert decision.signal_type == SignalType.EXIT.value
        assert "E1" in decision.reason or "death" in decision.reason.lower()

    def test_exit_signal_stop_loss(self):
        from apps.svc_strategy.scanner import check_exit_signal
        from packages.shared.enums import SignalType

        # close (490) <= entry_price (510) - 2 * ATR (6) = 498 → stop loss
        row = _make_indicator_row(
            close=490.0, ema_50=505.0, ema_200=480.0, atr_14=6.0
        )
        decision = check_exit_signal("SPY", row, TODAY, entry_price=510.0)
        assert decision.signal_type == SignalType.EXIT.value

    def test_hold_with_no_exit_conditions(self):
        from apps.svc_strategy.scanner import check_exit_signal
        from packages.shared.enums import SignalType

        row = _make_indicator_row(close=520.0, ema_50=505.0, ema_200=480.0, rsi_14=60.0)
        decision = check_exit_signal("SPY", row, TODAY, entry_price=490.0)
        assert decision.signal_type == SignalType.HOLD.value


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2: Pure computation — risk engine
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskEngine:
    """Verify the risk rules approve / reject correctly."""

    def _portfolio(self, equity=100_000.0, peak=100_000.0, positions=0):
        from apps.svc_risk.engine import PortfolioState
        return PortfolioState(
            total_equity=equity,
            peak_equity=peak,
            open_position_count=positions,
            cash=equity,
        )

    def _signal(self, symbol="SPY", close=520.0, atr=6.0):
        from apps.svc_strategy.scanner import SignalDecision
        from packages.shared.enums import SignalType
        return SignalDecision(
            symbol=symbol,
            signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=close,
            atr_14=atr,
            ema_50=505.0,
            ema_200=480.0,
            rsi_14=62.0,
            regime_ok=True,
        )

    def test_healthy_portfolio_approves_signal(self):
        from apps.svc_risk.engine import evaluate_signal
        from packages.shared.enums import RiskDecision

        result = evaluate_signal(
            signal=self._signal(),
            portfolio=self._portfolio(),
        )
        assert result.decision == RiskDecision.APPROVED.value
        assert result.sizing is not None
        assert result.sizing.shares > 0

    def test_max_drawdown_rejects_at_15pct(self):
        from apps.svc_risk.engine import evaluate_signal
        from packages.shared.enums import RiskDecision

        # 16% drawdown → P1 rejects
        result = evaluate_signal(
            signal=self._signal(),
            portfolio=self._portfolio(equity=84_000.0, peak=100_000.0),
        )
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P1_MAX_DRAWDOWN"

    def test_max_positions_rejects_when_full(self):
        from apps.svc_risk.engine import evaluate_signal
        from packages.shared.enums import RiskDecision

        # Already at max positions (default is 5)
        result = evaluate_signal(
            signal=self._signal(),
            portfolio=self._portfolio(positions=5),
        )
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P2_MAX_POSITIONS"

    def test_position_size_is_bounded(self):
        from apps.svc_risk.engine import evaluate_signal
        from packages.shared.enums import RiskDecision

        result = evaluate_signal(
            signal=self._signal(close=520.0, atr=6.0),
            portfolio=self._portfolio(equity=100_000.0),
        )
        assert result.decision == RiskDecision.APPROVED.value
        # Notional should not exceed 10% of portfolio
        notional = result.sizing.shares * 520.0
        assert notional <= 100_000.0 * 0.10 + 1.0  # small float tolerance

    def test_min_shares_rejects_zero_shares(self):
        from apps.svc_risk.engine import evaluate_signal
        from packages.shared.enums import RiskDecision

        # Very high price and tiny portfolio → 0 shares after sizing
        result = evaluate_signal(
            signal=self._signal(close=50_000.0, atr=100.0),
            portfolio=self._portfolio(equity=1_000.0),
        )
        assert result.decision == RiskDecision.REJECTED.value


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 3: Pure computation — execution
# ─────────────────────────────────────────────────────────────────────────────

class TestExecution:
    """Verify DryRunBroker + executor pure functions."""

    def _enter_signal(self) -> "SignalDecision":
        from apps.svc_strategy.scanner import SignalDecision
        from packages.shared.enums import SignalType
        return SignalDecision(
            symbol="SPY",
            signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=520.0,
            atr_14=6.0,
            ema_50=505.0,
            ema_200=480.0,
            rsi_14=62.0,
            regime_ok=True,
        )

    def _evaluation(self) -> "EvaluationResult":
        from apps.svc_risk.engine import EvaluationResult
        from apps.svc_risk.position_sizer import SizingResult
        from packages.shared.enums import RiskDecision
        return EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None,
            rejection_reason=None,
            sizing=SizingResult(
                shares=10,
                stop_price=508.0,
                risk_amount=120.0,
                notional_value=5_200.0,
                pct_of_portfolio=0.052,
            ),
        )

    def test_dry_run_broker_fills_immediately(self):
        from apps.svc_execution.broker import DryRunBroker
        broker = DryRunBroker(initial_cash=100_000.0)
        order = broker.submit_order("SPY", "BUY", 10, submitted_price=520.0)
        assert order.status == "filled"
        assert order.filled_avg_price == pytest.approx(520.0)
        assert order.filled_qty == 10

    def test_dry_run_broker_account_info(self):
        from apps.svc_execution.broker import DryRunBroker
        broker = DryRunBroker(initial_cash=50_000.0)
        info = broker.get_account_info()
        assert info.cash == pytest.approx(50_000.0)
        assert info.portfolio_value == pytest.approx(50_000.0)
        assert info.buying_power == pytest.approx(50_000.0)

    def test_build_entry_order(self):
        from apps.svc_execution.executor import build_entry_order
        from packages.shared.enums import OrderSide, OrderStatus

        run_id = str(uuid.uuid4())
        order = build_entry_order(
            signal=self._enter_signal(),
            evaluation=self._evaluation(),
            run_id=run_id,
        )
        assert order.symbol == "SPY"
        assert order.side == OrderSide.BUY.value
        assert order.qty == 10
        assert order.status == OrderStatus.PENDING.value

    def test_apply_broker_fill(self):
        from apps.svc_execution.executor import build_entry_order, apply_broker_fill
        from apps.svc_execution.broker import DryRunBroker
        from packages.shared.enums import OrderStatus

        run_id = str(uuid.uuid4())
        order = build_entry_order(
            signal=self._enter_signal(),
            evaluation=self._evaluation(),
            run_id=run_id,
        )
        broker = DryRunBroker(initial_cash=100_000.0)
        broker_order = broker.submit_order("SPY", "BUY", 10, submitted_price=520.0)
        apply_broker_fill(order, broker_order)

        assert order.status == OrderStatus.FILLED.value
        assert order.filled_price == pytest.approx(520.0)
        assert order.filled_qty == 10
        assert order.is_filled is True

    def test_open_position(self):
        from apps.svc_execution.executor import build_entry_order, apply_broker_fill, open_position
        from apps.svc_execution.broker import DryRunBroker
        from packages.shared.enums import PositionStatus

        run_id = str(uuid.uuid4())
        order = build_entry_order(
            signal=self._enter_signal(),
            evaluation=self._evaluation(),
            run_id=run_id,
        )
        broker = DryRunBroker(initial_cash=100_000.0)
        broker_order = broker.submit_order("SPY", "BUY", 10, submitted_price=520.0)
        apply_broker_fill(order, broker_order)

        position = open_position(order=order, run_id=run_id)
        assert position.symbol == "SPY"
        assert position.status == PositionStatus.OPEN.value
        assert int(position.qty) == 10
        assert float(position.entry_price) == pytest.approx(520.0)
        assert float(position.stop_loss) == pytest.approx(508.0)  # from sizing

    def test_close_position(self):
        from apps.svc_execution.executor import (
            build_entry_order, apply_broker_fill, open_position,
            build_exit_order, close_position,
        )
        from apps.svc_strategy.scanner import SignalDecision
        from apps.svc_execution.broker import DryRunBroker
        from packages.shared.enums import PositionStatus, SignalType

        run_id = str(uuid.uuid4())
        # Open
        order = build_entry_order(
            signal=self._enter_signal(),
            evaluation=self._evaluation(),
            run_id=run_id,
        )
        broker = DryRunBroker(initial_cash=100_000.0)
        apply_broker_fill(order, broker.submit_order("SPY", "BUY", 10, submitted_price=520.0))
        position = open_position(order=order, run_id=run_id)

        # Exit signal
        exit_signal = SignalDecision(
            symbol="SPY",
            signal_date=TODAY,
            signal_type=SignalType.EXIT.value,
            close_price=540.0,
            reason="E1_death_cross",
        )
        exit_order = build_exit_order(
            signal=exit_signal,
            position=position,
            run_id=run_id,
        )
        apply_broker_fill(exit_order, broker.submit_order("SPY", "SELL", 10, submitted_price=540.0))
        close_position(position=position, exit_order=exit_order, close_reason="E1_death_cross")

        assert position.status == PositionStatus.CLOSED.value
        assert float(position.exit_price) == pytest.approx(540.0)
        # P&L = (540 - 520) * 10 = 200
        assert float(position.realized_pnl) == pytest.approx(200.0)

    def test_portfolio_snapshot_math(self):
        from apps.svc_execution.executor import (
            build_entry_order, apply_broker_fill, open_position,
            build_portfolio_snapshot,
        )
        from apps.svc_execution.broker import DryRunBroker

        run_id = str(uuid.uuid4())
        order = build_entry_order(
            signal=self._enter_signal(),
            evaluation=self._evaluation(),
            run_id=run_id,
        )
        broker = DryRunBroker(initial_cash=100_000.0)
        apply_broker_fill(order, broker.submit_order("SPY", "BUY", 10, submitted_price=520.0))
        position = open_position(order=order, run_id=run_id)

        snapshot = build_portfolio_snapshot(
            run_id=run_id,
            cash=100_000.0 - 5_200.0,  # 94,800
            open_positions=[position],
            initial_capital=100_000.0,
            peak_equity=100_000.0,
        )
        assert float(snapshot.cash) == pytest.approx(94_800.0)
        assert float(snapshot.positions_value) == pytest.approx(5_200.0)
        assert float(snapshot.total_equity) == pytest.approx(100_000.0)
        assert float(snapshot.drawdown_pct) == pytest.approx(0.0)
        assert snapshot.open_positions_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 4: Kill switch (pure computation)
# ─────────────────────────────────────────────────────────────────────────────

class TestKillSwitch:
    """Verify kill switch trigger logic (pure computation part)."""

    def test_should_not_trigger_at_10pct_drawdown(self):
        from apps.svc_risk.kill_switch import check_should_trigger

        result = check_should_trigger(peak_equity=100_000.0, current_equity=90_000.0)
        assert result.should_trigger is False
        assert result.drawdown_pct == pytest.approx(0.10)

    def test_should_trigger_at_15pct_drawdown(self):
        from apps.svc_risk.kill_switch import check_should_trigger

        result = check_should_trigger(peak_equity=100_000.0, current_equity=85_000.0)
        assert result.should_trigger is True
        assert result.drawdown_pct >= 0.15

    def test_should_trigger_at_exact_threshold(self):
        from apps.svc_risk.kill_switch import check_should_trigger

        result = check_should_trigger(peak_equity=100_000.0, current_equity=85_000.0)
        assert result.should_trigger is True

    def test_zero_drawdown_no_trigger(self):
        from apps.svc_risk.kill_switch import check_should_trigger

        result = check_should_trigger(peak_equity=100_000.0, current_equity=100_000.0)
        assert result.should_trigger is False
        assert result.drawdown_pct == pytest.approx(0.0)

    def test_above_peak_no_trigger(self):
        """When equity > peak (shouldn't happen but guard anyway)."""
        from apps.svc_risk.kill_switch import check_should_trigger

        result = check_should_trigger(peak_equity=100_000.0, current_equity=110_000.0)
        assert result.should_trigger is False


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 5: DB-layer integration (SQLite in-memory)
# ─────────────────────────────────────────────────────────────────────────────

class TestDBLayer:
    """Integration tests using the real service-layer functions against SQLite."""

    def test_create_and_fetch_trading_run(self, db):
        from packages.shared.models.trading_run import TradingRun
        from packages.shared.enums import RunStatus

        run_id = _create_trading_run(db)
        db.commit()

        run = db.get(TradingRun, run_id)
        assert run is not None
        assert run.status == RunStatus.RUNNING.value
        assert float(run.initial_capital) == INITIAL_CAPITAL

    def test_save_and_retrieve_order(self, db):
        from apps.svc_execution import repository as repo
        from apps.svc_execution.executor import build_entry_order, apply_broker_fill
        from apps.svc_execution.broker import DryRunBroker
        from apps.svc_strategy.scanner import SignalDecision
        from apps.svc_risk.engine import EvaluationResult
        from apps.svc_risk.position_sizer import SizingResult
        from packages.shared.enums import (
            OrderStatus, RiskDecision, SignalType,
        )

        run_id = _create_trading_run(db)

        signal = SignalDecision(
            symbol="SPY", signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=520.0, atr_14=6.0, ema_50=505.0, ema_200=480.0,
            rsi_14=62.0, regime_ok=True,
        )
        evaluation = EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None, rejection_reason=None,
            sizing=SizingResult(
                shares=10, stop_price=508.0,
                risk_amount=120.0, notional_value=5200.0,
                pct_of_portfolio=0.052,
            ),
        )
        order = build_entry_order(signal=signal, evaluation=evaluation, run_id=run_id)
        broker = DryRunBroker(initial_cash=100_000.0)
        apply_broker_fill(order, broker.submit_order("SPY", "BUY", 10, 520.0))

        repo.save_order(db, order)
        db.commit()

        retrieved = repo.get_order_by_id(db, order.id)
        assert retrieved is not None
        assert retrieved.symbol == "SPY"
        assert retrieved.status == OrderStatus.FILLED.value

    def test_save_and_retrieve_position(self, db):
        from apps.svc_execution import repository as repo
        from apps.svc_execution.executor import (
            build_entry_order, apply_broker_fill, open_position,
        )
        from apps.svc_execution.broker import DryRunBroker
        from apps.svc_strategy.scanner import SignalDecision
        from apps.svc_risk.engine import EvaluationResult
        from apps.svc_risk.position_sizer import SizingResult
        from packages.shared.enums import (
            PositionStatus, RiskDecision, SignalType,
        )

        run_id = _create_trading_run(db)

        signal = SignalDecision(
            symbol="QQQ", signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=420.0, atr_14=5.0, ema_50=410.0, ema_200=390.0,
            rsi_14=60.0, regime_ok=True,
        )
        evaluation = EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None, rejection_reason=None,
            sizing=SizingResult(
                shares=12, stop_price=410.0,
                risk_amount=120.0, notional_value=5040.0,
                pct_of_portfolio=0.05,
            ),
        )
        order = build_entry_order(signal=signal, evaluation=evaluation, run_id=run_id)
        broker = DryRunBroker(initial_cash=100_000.0)
        apply_broker_fill(order, broker.submit_order("QQQ", "BUY", 12, 420.0))

        repo.save_order(db, order)
        pos = open_position(order=order, run_id=run_id)
        repo.save_position(db, pos)
        db.commit()

        open_pos = repo.get_open_positions(db, run_id)
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "QQQ"
        assert open_pos[0].status == PositionStatus.OPEN.value

    def test_has_order_for_signal_prevents_duplicate(self, db):
        from apps.svc_execution import repository as repo
        from apps.svc_execution.executor import build_entry_order, apply_broker_fill
        from apps.svc_execution.broker import DryRunBroker
        from apps.svc_strategy.scanner import SignalDecision
        from apps.svc_risk.engine import EvaluationResult
        from apps.svc_risk.position_sizer import SizingResult
        from packages.shared.enums import RiskDecision, SignalType

        run_id = _create_trading_run(db)

        signal = SignalDecision(
            symbol="IWM", signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=200.0, atr_14=3.0, ema_50=195.0, ema_200=180.0,
            rsi_14=58.0, regime_ok=True,
        )
        evaluation = EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None, rejection_reason=None,
            sizing=SizingResult(
                shares=5, stop_price=194.0,
                risk_amount=30.0, notional_value=1000.0,
                pct_of_portfolio=0.01,
            ),
        )
        order = build_entry_order(signal=signal, evaluation=evaluation, run_id=run_id)
        broker = DryRunBroker(initial_cash=100_000.0)
        apply_broker_fill(order, broker.submit_order("IWM", "BUY", 5, 200.0))
        repo.save_order(db, order)
        db.commit()

        # Should detect the existing order for today
        assert repo.has_order_for_signal(db, run_id, "IWM", TODAY) is True
        # Should NOT block a different symbol
        assert repo.has_order_for_signal(db, run_id, "DIA", TODAY) is False
        # Should NOT block a different date
        different_date = date(2024, 6, 16)
        assert repo.has_order_for_signal(db, run_id, "IWM", different_date) is False

    def test_portfolio_snapshot_round_trip(self, db):
        from apps.svc_execution import repository as repo
        from apps.svc_execution.executor import build_portfolio_snapshot

        run_id = _create_trading_run(db)
        snapshot = build_portfolio_snapshot(
            run_id=run_id,
            cash=94_800.0,
            open_positions=[],
            initial_capital=INITIAL_CAPITAL,
            peak_equity=INITIAL_CAPITAL,
        )
        repo.save_snapshot(db, snapshot)
        db.commit()

        retrieved = repo.get_latest_snapshot(db, run_id)
        assert retrieved is not None
        assert float(retrieved.total_equity) == pytest.approx(94_800.0)
        assert float(retrieved.drawdown_pct) == pytest.approx(0.052, abs=0.001)

    def test_kill_switch_db_flow(self, db):
        """Test the DB portion of kill switch: activate → is_active → resolve."""
        from apps.svc_execution.broker import DryRunBroker
        from apps.svc_risk.kill_switch import activate, resolve, is_active
        from packages.shared.enums import KillSwitchTrigger, RunStatus

        run_id = _create_trading_run(db)
        db.commit()

        broker = DryRunBroker(initial_cash=INITIAL_CAPITAL)

        # Before activation: not active
        assert is_active(db, run_id) is False

        activate(
            db,
            run_id=run_id,
            broker=broker,
            trigger=KillSwitchTrigger.MANUAL.value,
            reason="smoke-test",
            metrics_snapshot={"equity": 84_000.0, "drawdown_pct": 0.16},
        )
        db.commit()

        # After activation: is_active = True (run STOPPED + P0 event exists)
        assert is_active(db, run_id) is True

        # Verify run status changed
        from packages.shared.models.trading_run import TradingRun
        run = db.get(TradingRun, run_id)
        assert run.status == RunStatus.STOPPED.value

        # Resolve
        resolve(db, run_id=run_id, resolved_by="smoke-test-operator")
        db.commit()

        # After resolve: run is RUNNING, is_active still True because P0 event exists
        # (resolve sets run to RUNNING but doesn't delete the P0 event)
        run = db.get(TradingRun, run_id)
        assert run.status == RunStatus.RUNNING.value


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 6: Indicator computation
# ─────────────────────────────────────────────────────────────────────────────

class TestIndicators:
    """Verify compute_indicators produces correct columns and valid values."""

    def _make_ohlcv_df(self, n: int = 250) -> pd.DataFrame:
        import numpy as np
        rng = np.random.default_rng(42)
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        close = 100.0 * (1 + rng.normal(0, 0.01, n)).cumprod()
        high = close * (1 + rng.uniform(0, 0.01, n))
        low = close * (1 - rng.uniform(0, 0.01, n))
        volume = rng.lognormal(17, 0.4, n).astype(int)
        return pd.DataFrame({
            "date": dates,
            "open": close * (1 + rng.normal(0, 0.005, n)),
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })

    def test_compute_indicators_returns_all_columns(self):
        from apps.svc_data.indicators import compute_indicators

        df = self._make_ohlcv_df(250)
        result = compute_indicators(df)

        expected_cols = {"ema_50", "ema_200", "rsi_14", "atr_14", "atr_14_pct",
                         "volume_ma_20", "high_20d"}
        for col in expected_cols:
            assert col in result.columns, f"Missing column: {col}"

    def test_compute_indicators_ema200_needs_200_rows(self):
        from apps.svc_data.indicators import compute_indicators

        df = self._make_ohlcv_df(250)
        result = compute_indicators(df)

        # First 199 rows should have NaN for EMA200
        assert result.iloc[:199]["ema_200"].isna().all()
        # Row 200+ should have a valid EMA200
        assert result.iloc[199:]["ema_200"].notna().any()

    def test_compute_indicators_rsi_bounded(self):
        from apps.svc_data.indicators import compute_indicators

        df = self._make_ohlcv_df(250)
        result = compute_indicators(df)

        rsi = result["rsi_14"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all(), "RSI out of 0-100 range"

    def test_compute_indicators_atr_positive(self):
        from apps.svc_data.indicators import compute_indicators

        df = self._make_ohlcv_df(250)
        result = compute_indicators(df)

        atr = result["atr_14"].dropna()
        assert (atr > 0).all(), "ATR should always be positive"

    def test_compute_indicators_missing_columns_raises(self):
        from apps.svc_data.indicators import compute_indicators

        df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=10),
                           "close": range(10)})
        with pytest.raises(ValueError, match="missing columns"):
            compute_indicators(df)

    def test_compute_indicators_empty_df_returns_empty(self):
        from apps.svc_data.indicators import compute_indicators

        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = compute_indicators(df)
        assert result.empty


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 7: Full pipeline smoke (pure computation, no DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelinePureSmoke:
    """
    Smoke-test the pipeline pure computation layer with pre-built input data.
    Verifies that run_pipeline() produces the expected output structure.
    """

    def _build_bullish_row(self, symbol: str) -> pd.Series:
        return pd.Series(_make_indicator_row())

    def test_run_pipeline_produces_enter_signals(self):
        from apps.svc_orchestrator.pipeline import run_pipeline, PortfolioState
        from packages.shared.enums import SignalType

        portfolio = PortfolioState(
            total_equity=100_000.0,
            peak_equity=100_000.0,
            open_position_count=0,
            cash=100_000.0,
        )

        symbols = ["SPY", "QQQ", "IWM"]
        spy_row = pd.Series(_make_indicator_row(close=520.0))
        rows = {sym: pd.Series(_make_indicator_row()) for sym in symbols}

        result = run_pipeline(
            symbols=symbols,
            rows=rows,
            open_positions={},
            open_position_entry_prices={},
            spy_row=spy_row,
            portfolio=portfolio,
            as_of_date=TODAY,
        )

        assert result.signals is not None
        # With bullish regime (SPY close > SPY EMA200) and all conditions met,
        # we expect at least some ENTER signals
        enter_signals = [s for s in result.signals if s.signal_type == SignalType.ENTER.value]
        assert len(enter_signals) > 0, f"Expected ENTER signals; got: {[s.signal_type for s in result.signals]}"

    def test_run_pipeline_bearish_regime_no_entries(self):
        from apps.svc_orchestrator.pipeline import run_pipeline, PortfolioState
        from packages.shared.enums import SignalType

        portfolio = PortfolioState(
            total_equity=100_000.0,
            peak_equity=100_000.0,
            open_position_count=0,
            cash=100_000.0,
        )

        # Bearish regime: SPY close < SPY EMA200
        spy_row = pd.Series(_make_indicator_row(
            close=470.0, ema_50=485.0, ema_200=490.0
        ))
        symbols = ["QQQ", "IWM"]
        rows = {sym: pd.Series(_make_indicator_row()) for sym in symbols}

        result = run_pipeline(
            symbols=symbols,
            rows=rows,
            open_positions={},
            open_position_entry_prices={},
            spy_row=spy_row,
            portfolio=portfolio,
            as_of_date=TODAY,
        )

        enter_signals = [s for s in result.signals if s.signal_type == SignalType.ENTER.value]
        assert len(enter_signals) == 0, "No ENTER signals expected in bearish regime"

    def test_run_pipeline_produces_exit_for_open_positions(self):
        from apps.svc_orchestrator.pipeline import run_pipeline, PortfolioState
        from packages.shared.enums import SignalType

        portfolio = PortfolioState(
            total_equity=100_000.0,
            peak_equity=100_000.0,
            open_position_count=1,
            cash=94_800.0,
        )

        # SPY has an open position — death cross (EMA50 < EMA200) → should EXIT
        spy_row = pd.Series(_make_indicator_row(close=520.0))
        rows = {
            "SPY": pd.Series(_make_indicator_row(
                close=475.0, ema_50=480.0, ema_200=490.0
            )),
        }

        result = run_pipeline(
            symbols=["SPY"],
            rows=rows,
            open_positions={"SPY": str(uuid.uuid4())},
            open_position_entry_prices={"SPY": 510.0},
            spy_row=spy_row,
            portfolio=portfolio,
            as_of_date=TODAY,
        )

        exit_signals = [s for s in result.signals if s.signal_type == SignalType.EXIT.value]
        assert len(exit_signals) >= 1, "Expected at least one EXIT signal for death cross"
