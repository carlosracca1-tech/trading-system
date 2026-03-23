"""
tests/test_orchestrator.py
Unit tests for the Orchestrator pipeline.

Pure computation — no DB required.
Tests cover:
  - scan_symbols()       → signal classification per symbol
  - evaluate_risk()      → risk rule application and filtering
  - build_exec_plan()    → exec intent construction and ordering
  - run_pipeline()       → full end-to-end pipeline (pure)
  - PipelineRun helpers  → summary, enter/exit/approved/rejected filters
  - StageResult states   → complete / skip / fail
  - ExecutionIntent      → is_entry / is_exit properties
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import pytest

from apps.svc_orchestrator.pipeline import (
    ExecutionIntent,
    PipelineRun,
    StageResult,
    StageStatus,
    build_exec_plan,
    evaluate_risk,
    run_pipeline,
    scan_symbols,
)
from apps.svc_risk.engine import EvaluationResult, PortfolioState
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.enums import OrderSide, RiskDecision, SignalType

TODAY = date(2024, 6, 15)

# ── Fixtures / helpers ─────────────────────────────────────────────────────────

def _bullish_spy() -> pd.Series:
    """SPY row where close > EMA200 → regime_ok=True."""
    return pd.Series({
        "close": 520.0,
        "ema_200": 480.0,
        "ema_50": 510.0,
    })


def _bearish_spy() -> pd.Series:
    """SPY row where close < EMA200 → regime_ok=False."""
    return pd.Series({
        "close": 460.0,
        "ema_200": 480.0,
        "ema_50": 465.0,
    })


def _strong_entry_row(close: float = 200.0) -> pd.Series:
    """Row satisfying all RFTM entry conditions."""
    return pd.Series({
        "close": close,
        "ema_50": 190.0,
        "ema_200": 175.0,
        "rsi_14": 62.0,
        "atr_14": 3.0,
        "atr_14_pct": 0.015,
        "volume": 2_400_000.0,
        "volume_ma_20": 2_000_000.0,
        "high_20d": close,        # exactly at breakout
    })


def _weak_row(close: float = 200.0) -> pd.Series:
    """Row that fails RSI momentum condition (RSI too low)."""
    return pd.Series({
        "close": close,
        "ema_50": 190.0,
        "ema_200": 175.0,
        "rsi_14": 30.0,           # below minimum 50
        "atr_14": 3.0,
        "atr_14_pct": 0.015,
        "volume": 2_400_000.0,
        "volume_ma_20": 2_000_000.0,
        "high_20d": close,
    })


def _exit_row(entry_price: float = 200.0) -> pd.Series:
    """Row that triggers exit (death cross)."""
    return pd.Series({
        "close": 185.0,
        "ema_50": 170.0,          # below ema_200 → death cross
        "ema_200": 180.0,
        "rsi_14": 45.0,
        "atr_14": 3.0,
        "entry_price": entry_price,
    })


def _healthy_portfolio(
    equity: float = 100_000.0,
    positions: int = 0,
) -> PortfolioState:
    return PortfolioState(
        total_equity=equity,
        peak_equity=equity,
        open_position_count=positions,
        cash=equity,
    )


def _hold_signal(symbol: str = "QQQ") -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.HOLD.value,
        close_price=200.0,
        reason="rsi_below_min",
    )


def _enter_signal(symbol: str = "SPY") -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.ENTER.value,
        close_price=200.0,
        atr_14=3.0,
        ema_50=190.0,
        ema_200=175.0,
        rsi_14=62.0,
        regime_ok=True,
    )


def _exit_signal(symbol: str = "SPY") -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.EXIT.value,
        close_price=185.0,
        reason="death_cross",
    )


def _approved_ev() -> EvaluationResult:
    from apps.svc_risk.position_sizer import SizingResult
    return EvaluationResult(
        decision=RiskDecision.APPROVED.value,
        rule_code=None,
        rejection_reason=None,
        sizing=SizingResult(
            shares=50,
            stop_price=194.0,
            risk_amount=1_000.0,
            notional_value=10_000.0,
            pct_of_portfolio=0.10,
            rejection_reason=None,
        ),
    )


def _rejected_ev(rule: str = "P1_MAX_DRAWDOWN") -> EvaluationResult:
    return EvaluationResult(
        decision=RiskDecision.REJECTED.value,
        rule_code=rule,
        rejection_reason=f"{rule} triggered",
    )


# ── TestStageResult ────────────────────────────────────────────────────────────

class TestStageResult:
    def test_initial_state_is_pending(self):
        stage = StageResult(name="test_stage")
        assert stage.status == StageStatus.PENDING

    def test_complete_sets_status(self):
        stage = StageResult(name="s")
        stage.complete(processed=10, skipped=2)
        assert stage.status == StageStatus.COMPLETE
        assert stage.items_processed == 10
        assert stage.items_skipped == 2

    def test_skip_sets_status(self):
        stage = StageResult(name="s")
        stage.skip("no symbols")
        assert stage.status == StageStatus.SKIPPED
        assert stage.error == "no symbols"

    def test_fail_sets_status(self):
        stage = StageResult(name="s")
        stage.fail("connection refused")
        assert stage.status == StageStatus.FAILED
        assert stage.error == "connection refused"

    def test_complete_returns_self(self):
        stage = StageResult(name="s")
        result = stage.complete()
        assert result is stage


# ── TestExecutionIntent ────────────────────────────────────────────────────────

class TestExecutionIntent:
    def test_buy_is_entry(self):
        intent = ExecutionIntent(
            symbol="SPY",
            side=OrderSide.BUY.value,
            signal=_enter_signal(),
        )
        assert intent.is_entry is True
        assert intent.is_exit is False

    def test_sell_is_exit(self):
        intent = ExecutionIntent(
            symbol="SPY",
            side=OrderSide.SELL.value,
            signal=_exit_signal(),
        )
        assert intent.is_exit is True
        assert intent.is_entry is False


# ── TestScanSymbols ────────────────────────────────────────────────────────────

class TestScanSymbols:
    def test_entry_signal_in_bullish_regime(self):
        signals, stage = scan_symbols(
            symbols=["SPY"],
            rows={"SPY": _strong_entry_row()},
            open_position_symbols=set(),
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        assert len(signals) == 1
        assert signals[0].signal_type == SignalType.ENTER.value
        assert signals[0].symbol == "SPY"

    def test_hold_in_bearish_regime(self):
        """Bearish regime should block all entries → HOLD."""
        signals, stage = scan_symbols(
            symbols=["SPY"],
            rows={"SPY": _strong_entry_row()},
            open_position_symbols=set(),
            spy_row=_bearish_spy(),
            as_of_date=TODAY,
        )
        assert signals[0].signal_type == SignalType.HOLD.value
        assert "bearish_regime" in signals[0].reason

    def test_exit_for_open_position(self):
        row = _exit_row(entry_price=200.0)
        signals, stage = scan_symbols(
            symbols=["SPY"],
            rows={"SPY": row},
            open_position_symbols={"SPY"},
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        assert signals[0].signal_type == SignalType.EXIT.value

    def test_hold_when_row_missing(self):
        """Missing data for a symbol should be skipped."""
        signals, stage = scan_symbols(
            symbols=["SPY"],
            rows={},            # no row for SPY
            open_position_symbols=set(),
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        assert signals == []
        assert stage.items_skipped == 1

    def test_multiple_symbols_independent(self):
        signals, stage = scan_symbols(
            symbols=["SPY", "QQQ", "IWM"],
            rows={
                "SPY": _strong_entry_row(close=200.0),
                "QQQ": _weak_row(close=400.0),    # fails RSI
                "IWM": _strong_entry_row(close=210.0),
            },
            open_position_symbols=set(),
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        assert len(signals) == 3
        enters = [s for s in signals if s.signal_type == SignalType.ENTER.value]
        holds = [s for s in signals if s.signal_type == SignalType.HOLD.value]
        assert len(enters) == 2   # SPY + IWM
        assert len(holds) == 1    # QQQ

    def test_open_position_evaluated_as_exit_candidate(self):
        """Symbol with open position gets exit check, not entry check."""
        signals, _ = scan_symbols(
            symbols=["SPY"],
            rows={"SPY": _strong_entry_row()},   # would pass entry
            open_position_symbols={"SPY"},         # but it's open → check exit
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        # Strong entry row has ema_50 > ema_200 and close > ema_50 → HOLD on exit check
        assert signals[0].signal_type == SignalType.HOLD.value

    def test_stage_complete_after_scan(self):
        _, stage = scan_symbols(
            symbols=["SPY"],
            rows={"SPY": _strong_entry_row()},
            open_position_symbols=set(),
            spy_row=_bullish_spy(),
            as_of_date=TODAY,
        )
        assert stage.status == StageStatus.COMPLETE
        assert stage.items_processed == 1

    def test_spy_row_none_gives_bearish_regime(self):
        """No SPY data → conservative fallback → no entries."""
        signals, _ = scan_symbols(
            symbols=["QQQ"],
            rows={"QQQ": _strong_entry_row(close=400.0)},
            open_position_symbols=set(),
            spy_row=None,       # no SPY data
            as_of_date=TODAY,
        )
        assert signals[0].signal_type == SignalType.HOLD.value


# ── TestEvaluateRisk ──────────────────────────────────────────────────────────

class TestEvaluateRisk:
    def test_hold_signals_skipped(self):
        """HOLD signals should not appear in evaluations."""
        evaluations, stage = evaluate_risk(
            signals=[_hold_signal("QQQ")],
            portfolio=_healthy_portfolio(),
        )
        assert evaluations == []
        assert stage.items_skipped == 1

    def test_exit_auto_approved(self):
        evaluations, _ = evaluate_risk(
            signals=[_exit_signal()],
            portfolio=_healthy_portfolio(),
        )
        assert len(evaluations) == 1
        _, ev = evaluations[0]
        assert ev.decision == RiskDecision.APPROVED.value

    def test_enter_approved_healthy_portfolio(self):
        evaluations, _ = evaluate_risk(
            signals=[_enter_signal()],
            portfolio=_healthy_portfolio(equity=100_000.0, positions=0),
        )
        _, ev = evaluations[0]
        assert ev.decision == RiskDecision.APPROVED.value
        assert ev.sizing is not None

    def test_enter_rejected_p1_drawdown(self):
        """15%+ drawdown triggers P1 kill switch."""
        portfolio = PortfolioState(
            total_equity=80_000.0,   # 20% drawdown
            peak_equity=100_000.0,
            open_position_count=0,
            cash=80_000.0,
        )
        evaluations, _ = evaluate_risk(
            signals=[_enter_signal()],
            portfolio=portfolio,
        )
        _, ev = evaluations[0]
        assert ev.decision == RiskDecision.REJECTED.value
        assert "P1" in (ev.rule_code or "")

    def test_enter_rejected_p2_max_positions(self):
        """10 positions → P2 rejects new entries."""
        portfolio = PortfolioState(
            total_equity=100_000.0,
            peak_equity=100_000.0,
            open_position_count=10,   # at limit
            cash=0.0,
        )
        evaluations, _ = evaluate_risk(
            signals=[_enter_signal()],
            portfolio=portfolio,
        )
        _, ev = evaluations[0]
        assert ev.decision == RiskDecision.REJECTED.value
        assert "P2" in (ev.rule_code or "")

    def test_mixed_signals_correct_counts(self):
        enter1 = _enter_signal("SPY")
        enter2 = _enter_signal("QQQ")
        exit1 = _exit_signal("IWM")
        hold1 = _hold_signal("GLD")

        evaluations, stage = evaluate_risk(
            signals=[enter1, enter2, exit1, hold1],
            portfolio=_healthy_portfolio(),
        )
        # 2 enters + 1 exit evaluated; 1 hold skipped
        assert len(evaluations) == 3
        assert stage.items_skipped == 1

    def test_stage_name_is_evaluate_risk(self):
        _, stage = evaluate_risk(signals=[], portfolio=_healthy_portfolio())
        assert stage.name == "evaluate_risk"

    def test_enter_missing_atr_rejected(self):
        """Signal without ATR should be rejected (can't size position)."""
        no_atr = SignalDecision(
            symbol="SPY",
            signal_date=TODAY,
            signal_type=SignalType.ENTER.value,
            close_price=200.0,
            atr_14=None,         # missing ATR
        )
        evaluations, _ = evaluate_risk(
            signals=[no_atr],
            portfolio=_healthy_portfolio(),
        )
        _, ev = evaluations[0]
        assert ev.decision == RiskDecision.REJECTED.value


# ── TestBuildExecPlan ──────────────────────────────────────────────────────────

class TestBuildExecPlan:
    def test_approved_entry_becomes_buy_intent(self):
        plan, _ = build_exec_plan(
            evaluations=[(_enter_signal(), _approved_ev())],
            open_positions={},
        )
        assert len(plan) == 1
        assert plan[0].is_entry is True
        assert plan[0].side == OrderSide.BUY.value

    def test_approved_exit_becomes_sell_intent(self):
        plan, _ = build_exec_plan(
            evaluations=[(_exit_signal("SPY"), EvaluationResult(
                decision=RiskDecision.APPROVED.value,
                rule_code=None,
                rejection_reason=None,
            ))],
            open_positions={"SPY": "pos-abc"},
        )
        assert len(plan) == 1
        assert plan[0].is_exit is True
        assert plan[0].position_id == "pos-abc"

    def test_rejected_entry_excluded(self):
        plan, _ = build_exec_plan(
            evaluations=[(_enter_signal(), _rejected_ev())],
            open_positions={},
        )
        assert plan == []

    def test_exits_before_entries(self):
        """Exits must come first in the execution plan."""
        plan, _ = build_exec_plan(
            evaluations=[
                (_enter_signal("QQQ"), _approved_ev()),
                (_exit_signal("SPY"), EvaluationResult(
                    decision=RiskDecision.APPROVED.value,
                    rule_code=None,
                    rejection_reason=None,
                )),
            ],
            open_positions={"SPY": "pos-abc"},
        )
        assert len(plan) == 2
        assert plan[0].is_exit is True
        assert plan[1].is_entry is True

    def test_empty_evaluations(self):
        plan, stage = build_exec_plan(evaluations=[], open_positions={})
        assert plan == []
        assert stage.status == StageStatus.COMPLETE

    def test_stage_name_is_build_exec_plan(self):
        _, stage = build_exec_plan(evaluations=[], open_positions={})
        assert stage.name == "build_exec_plan"

    def test_multiple_entries_all_included(self):
        plan, _ = build_exec_plan(
            evaluations=[
                (_enter_signal("SPY"), _approved_ev()),
                (_enter_signal("QQQ"), _approved_ev()),
                (_enter_signal("IWM"), _approved_ev()),
            ],
            open_positions={},
        )
        assert len(plan) == 3
        assert all(i.is_entry for i in plan)


# ── TestPipelineRun ───────────────────────────────────────────────────────────

class TestPipelineRun:
    def test_enter_signals_filter(self):
        pr = PipelineRun(as_of_date=TODAY)
        pr.signals = [_enter_signal(), _exit_signal(), _hold_signal()]
        assert len(pr.enter_signals) == 1
        assert len(pr.exit_signals) == 1

    def test_approved_entries_filter(self):
        pr = PipelineRun(as_of_date=TODAY)
        pr.evaluations = [
            (_enter_signal(), _approved_ev()),
            (_enter_signal("QQQ"), _rejected_ev()),
        ]
        assert len(pr.approved_entries) == 1
        assert len(pr.rejected_entries) == 1

    def test_summary_keys(self):
        pr = PipelineRun(as_of_date=TODAY)
        summary = pr.summary()
        for key in ("date", "signals_enter", "signals_exit", "approved", "rejected", "exec_orders", "stages"):
            assert key in summary

    def test_summary_date_as_string(self):
        pr = PipelineRun(as_of_date=TODAY)
        assert pr.summary()["date"] == str(TODAY)


# ── TestRunPipeline (integration of all 3 stages) ────────────────────────────

class TestRunPipeline:
    def test_bullish_entry_end_to_end(self):
        """Full pipeline with one symbol that should generate an approved entry."""
        result = run_pipeline(
            symbols=["QQQ"],
            rows={"QQQ": _strong_entry_row(close=400.0)},
            open_positions={},
            open_position_entry_prices={},
            spy_row=_bullish_spy(),
            portfolio=_healthy_portfolio(),
            as_of_date=TODAY,
        )
        assert len(result.enter_signals) == 1
        assert len(result.approved_entries) == 1
        assert len(result.exec_plan) == 1
        assert result.exec_plan[0].is_entry is True

    def test_bearish_regime_no_entries(self):
        """Bearish regime → no entries regardless of symbol quality."""
        result = run_pipeline(
            symbols=["SPY", "QQQ"],
            rows={
                "SPY": _strong_entry_row(),
                "QQQ": _strong_entry_row(close=400.0),
            },
            open_positions={},
            open_position_entry_prices={},
            spy_row=_bearish_spy(),
            portfolio=_healthy_portfolio(),
            as_of_date=TODAY,
        )
        assert len(result.enter_signals) == 0
        assert len(result.exec_plan) == 0

    def test_exit_included_in_plan(self):
        """Open position that triggers exit → included in exec plan."""
        result = run_pipeline(
            symbols=["SPY"],
            rows={"SPY": _exit_row(entry_price=200.0)},
            open_positions={"SPY": "pos-001"},
            open_position_entry_prices={"SPY": 200.0},
            spy_row=_bullish_spy(),
            portfolio=_healthy_portfolio(positions=1),
            as_of_date=TODAY,
        )
        assert len(result.exit_signals) == 1
        assert result.exec_plan[0].is_exit is True
        assert result.exec_plan[0].position_id == "pos-001"

    def test_p1_drawdown_blocks_all_entries(self):
        distressed = PortfolioState(
            total_equity=80_000.0,
            peak_equity=100_000.0,
            open_position_count=0,
            cash=80_000.0,
        )
        result = run_pipeline(
            symbols=["QQQ", "IWM"],
            rows={
                "QQQ": _strong_entry_row(close=400.0),
                "IWM": _strong_entry_row(close=210.0),
            },
            open_positions={},
            open_position_entry_prices={},
            spy_row=_bullish_spy(),
            portfolio=distressed,
            as_of_date=TODAY,
        )
        assert len(result.approved_entries) == 0
        assert len(result.exec_plan) == 0

    def test_three_stages_in_result(self):
        result = run_pipeline(
            symbols=[],
            rows={},
            open_positions={},
            open_position_entry_prices={},
            spy_row=None,
            portfolio=_healthy_portfolio(),
            as_of_date=TODAY,
        )
        assert len(result.stages) == 3
        names = [s.name for s in result.stages]
        assert "scan_symbols" in names
        assert "evaluate_risk" in names
        assert "build_exec_plan" in names
