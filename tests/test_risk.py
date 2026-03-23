"""
tests/test_risk.py
Unit tests for the Risk Engine (position sizer + rules + engine).

Pure computation — no DB required.
Tests cover:
  - calculate_position_size()
  - P1MaxDrawdown.check()
  - P2MaxPositions.check()
  - P3MaxPositionSize.check()
  - P4MinShares.check()
  - evaluate_signal()  (full engine)
"""
from __future__ import annotations

from datetime import date

import pytest

from apps.svc_risk.engine import EvaluationResult, PortfolioState, evaluate_signal
from apps.svc_risk.position_sizer import RISK_PARAMS, SizingResult, calculate_position_size
from apps.svc_risk.rules import (
    P1MaxDrawdown,
    P2MaxPositions,
    P3MaxPositionSize,
    P4MinShares,
)
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.enums import RiskDecision, SignalType

TODAY = date(2024, 6, 15)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _healthy_portfolio(
    total_equity: float = 100_000.0,
    peak_equity: float = 100_000.0,
    open_positions: int = 0,
    cash: float = 100_000.0,
) -> PortfolioState:
    return PortfolioState(
        total_equity=total_equity,
        peak_equity=peak_equity,
        open_position_count=open_positions,
        cash=cash,
    )


def _enter_signal(
    symbol: str = "SPY",
    close_price: float = 200.0,
    atr_14: float = 3.0,
) -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.ENTER.value,
        close_price=close_price,
        atr_14=atr_14,
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


def _hold_signal(symbol: str = "SPY") -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        signal_date=TODAY,
        signal_type=SignalType.HOLD.value,
        close_price=200.0,
    )


# ── TestPositionSizer ──────────────────────────────────────────────────────────

class TestPositionSizer:
    def test_basic_sizing(self):
        """Verify the risk-based formula (large ATR keeps risk-based below cap).

        portfolio=100_000, risk=1% → risk_amount=1_000
        atr=15, stop_dist=2*15=30 → shares_risk = 1000/30 = 33.33 → 33
        cap = 10%*100k / 200 = 50 shares  →  min(33, 50) = 33  (risk-based wins)
        """
        result = calculate_position_size(
            portfolio_value=100_000.0,
            close_price=200.0,
            atr_14=15.0,
        )
        assert result.rejection_reason is None
        assert result.shares == 33

    def test_capped_by_max_position_pct(self):
        """When risk-based size exceeds 10 % of portfolio, cap applies.

        atr=3, stop_dist=6 → shares_risk = 1000/6 = 166 → but cap = 50
        """
        result = calculate_position_size(
            portfolio_value=100_000.0,
            close_price=200.0,
            atr_14=3.0,
        )
        assert result.shares == 50    # capped at 10 % of portfolio
        assert result.pct_of_portfolio == pytest.approx(0.10, abs=0.001)

    def test_stop_price_calculation(self):
        """stop_price = close - stop_multiplier * atr."""
        result = calculate_position_size(100_000.0, 200.0, atr_14=3.0)
        # stop = 200 - 2*3 = 194
        assert result.stop_price == pytest.approx(194.0)

    def test_notional_value(self):
        # Use atr=15 so risk-based wins (33 shares); notional = 33 * 200 = 6_600
        result = calculate_position_size(100_000.0, 200.0, atr_14=15.0)
        assert result.notional_value == pytest.approx(result.shares * 200.0)

    def test_pct_of_portfolio(self):
        result = calculate_position_size(100_000.0, 200.0, atr_14=15.0)
        expected_pct = (result.shares * 200.0) / 100_000.0
        assert result.pct_of_portfolio == pytest.approx(expected_pct, rel=1e-4)

    def test_zero_atr_rejected(self):
        result = calculate_position_size(100_000.0, 200.0, atr_14=0.0)
        assert result.shares == 0
        assert result.rejection_reason == "atr_non_positive"

    def test_negative_atr_rejected(self):
        result = calculate_position_size(100_000.0, 200.0, atr_14=-1.0)
        assert result.shares == 0
        assert result.rejection_reason is not None

    def test_zero_portfolio_rejected(self):
        result = calculate_position_size(0.0, 200.0, atr_14=3.0)
        assert result.shares == 0
        assert "portfolio" in result.rejection_reason

    def test_tiny_portfolio_rounds_to_zero(self):
        # portfolio=100, risk=1%, atr=10 → risk_amount=1, stop=20 → 0.05 shares → floor → 0
        result = calculate_position_size(100.0, 200.0, atr_14=10.0)
        assert result.shares == 0
        assert result.rejection_reason == "position_size_rounds_to_zero"

    def test_custom_risk_pct(self):
        """Custom 2% risk per trade."""
        result = calculate_position_size(
            100_000.0, 200.0, atr_14=3.0, risk_pct_per_trade=0.02
        )
        # risk = 2_000, stop_dist = 6 → 333 shares (capped at 50 by 10% max)
        assert result.shares == 50   # 333 risk-based > 50 cap

    def test_larger_atr_means_fewer_shares(self):
        # atr=3 → capped at 50;  atr=20 → risk-based: 1000/40=25 (not capped)
        result_small_atr = calculate_position_size(100_000.0, 200.0, atr_14=3.0)
        result_large_atr = calculate_position_size(100_000.0, 200.0, atr_14=20.0)
        # Larger ATR → larger stop distance → fewer risk-based shares
        assert result_large_atr.shares < result_small_atr.shares


# ── TestP1MaxDrawdown ──────────────────────────────────────────────────────────

class TestP1MaxDrawdown:
    def test_within_limit_passes(self):
        rule = P1MaxDrawdown(max_drawdown_pct=0.15)
        result = rule.check(peak_equity=100_000, current_equity=90_000)  # 10% dd
        assert result.passed is True

    def test_exceeds_limit_fails(self):
        rule = P1MaxDrawdown(max_drawdown_pct=0.15)
        result = rule.check(peak_equity=100_000, current_equity=84_000)  # 16% dd
        assert result.passed is False
        assert "drawdown" in result.reason
        assert result.rule_code == "P1_MAX_DRAWDOWN"

    def test_exactly_at_limit_fails(self):
        """At exactly 15% drawdown the kill switch fires (>=)."""
        rule = P1MaxDrawdown(max_drawdown_pct=0.15)
        result = rule.check(peak_equity=100_000, current_equity=85_000)  # exactly 15%
        assert result.passed is False

    def test_one_dollar_under_limit_passes(self):
        rule = P1MaxDrawdown(max_drawdown_pct=0.15)
        # 14.999% drawdown
        result = rule.check(peak_equity=100_000, current_equity=85_001)
        assert result.passed is True

    def test_zero_peak_equity_passes(self):
        """Guard against division by zero."""
        rule = P1MaxDrawdown()
        result = rule.check(peak_equity=0, current_equity=0)
        assert result.passed is True

    def test_custom_threshold(self):
        rule = P1MaxDrawdown(max_drawdown_pct=0.10)
        result = rule.check(peak_equity=100_000, current_equity=89_000)  # 11% > 10%
        assert result.passed is False


# ── TestP2MaxPositions ─────────────────────────────────────────────────────────

class TestP2MaxPositions:
    def test_below_max_passes(self):
        rule = P2MaxPositions(max_positions=10)
        assert rule.check(9).passed is True

    def test_at_max_fails(self):
        rule = P2MaxPositions(max_positions=10)
        result = rule.check(10)
        assert result.passed is False
        assert result.rule_code == "P2_MAX_POSITIONS"

    def test_above_max_fails(self):
        rule = P2MaxPositions(max_positions=10)
        assert rule.check(11).passed is False

    def test_zero_positions_passes(self):
        rule = P2MaxPositions()
        assert rule.check(0).passed is True

    def test_custom_max(self):
        rule = P2MaxPositions(max_positions=5)
        assert rule.check(4).passed is True
        assert rule.check(5).passed is False


# ── TestP3MaxPositionSize ──────────────────────────────────────────────────────

class TestP3MaxPositionSize:
    def test_within_limit_passes(self):
        rule = P3MaxPositionSize(max_position_pct=0.10)
        # 9_000 / 100_000 = 9% < 10%
        assert rule.check(9_000, 100_000).passed is True

    def test_exactly_at_limit_passes(self):
        rule = P3MaxPositionSize(max_position_pct=0.10)
        assert rule.check(10_000, 100_000).passed is True

    def test_exceeds_limit_fails(self):
        rule = P3MaxPositionSize(max_position_pct=0.10)
        result = rule.check(10_001, 100_000)
        assert result.passed is False
        assert result.rule_code == "P3_MAX_POSITION_SIZE"
        assert "position_pct" in result.reason

    def test_zero_portfolio_fails(self):
        rule = P3MaxPositionSize()
        result = rule.check(1_000, 0)
        assert result.passed is False


# ── TestP4MinShares ────────────────────────────────────────────────────────────

class TestP4MinShares:
    def test_positive_shares_passes(self):
        rule = P4MinShares()
        assert rule.check(1).passed is True
        assert rule.check(100).passed is True

    def test_zero_shares_fails(self):
        rule = P4MinShares()
        result = rule.check(0)
        assert result.passed is False
        assert result.rule_code == "P4_MIN_SHARES"

    def test_zero_with_reason_propagates(self):
        rule = P4MinShares()
        result = rule.check(0, rejection_reason="atr_non_positive")
        assert result.reason == "atr_non_positive"


# ── TestEvaluateSignal (full engine) ───────────────────────────────────────────

class TestEvaluateSignal:
    def test_enter_all_rules_pass_approved(self):
        signal = _enter_signal()
        portfolio = _healthy_portfolio()
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.APPROVED.value
        assert result.sizing is not None
        assert result.sizing.shares > 0

    def test_exit_auto_approved(self):
        """EXIT signals bypass all rules."""
        signal = _exit_signal()
        # Even with a "broken" portfolio
        portfolio = _healthy_portfolio(total_equity=0, peak_equity=100_000)
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.APPROVED.value
        assert result.sizing is None

    def test_hold_deferred(self):
        signal = _hold_signal()
        portfolio = _healthy_portfolio()
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.DEFERRED.value

    def test_p1_drawdown_rejects_enter(self):
        signal = _enter_signal()
        # 20% drawdown (peak=100k, current=80k) → P1 fires
        portfolio = _healthy_portfolio(total_equity=80_000, peak_equity=100_000)
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P1_MAX_DRAWDOWN"

    def test_p2_max_positions_rejects_enter(self):
        signal = _enter_signal()
        portfolio = _healthy_portfolio(open_positions=10)   # at max
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P2_MAX_POSITIONS"

    def test_p4_zero_atr_rejects_enter(self):
        signal = _enter_signal(atr_14=0.0)
        portfolio = _healthy_portfolio()
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.REJECTED.value

    def test_approved_result_has_sizing(self):
        signal = _enter_signal(close_price=200.0, atr_14=3.0)
        portfolio = _healthy_portfolio(total_equity=100_000.0)
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.APPROVED.value
        assert result.sizing.stop_price == pytest.approx(194.0)

    def test_approved_result_has_no_rejection_reason(self):
        signal = _enter_signal()
        portfolio = _healthy_portfolio()
        result = evaluate_signal(signal, portfolio)
        assert result.rejection_reason is None
        assert result.rule_code is None

    def test_rejected_result_has_rule_code(self):
        signal = _enter_signal()
        portfolio = _healthy_portfolio(total_equity=80_000, peak_equity=100_000)
        result = evaluate_signal(signal, portfolio)
        assert result.rule_code is not None
        assert result.rejection_reason is not None

    def test_exit_bypasses_p1_even_with_max_drawdown(self):
        """EXIT signals must never be blocked, even when kill switch would fire."""
        signal = _exit_signal()
        portfolio = _healthy_portfolio(total_equity=50_000, peak_equity=100_000)  # 50% dd
        result = evaluate_signal(signal, portfolio)
        assert result.decision == RiskDecision.APPROVED.value

    def test_approved_sizing_fields_populated(self):
        signal = _enter_signal(close_price=200.0, atr_14=3.0)
        portfolio = _healthy_portfolio()
        result = evaluate_signal(signal, portfolio)
        sizing = result.sizing
        assert sizing.shares > 0
        assert sizing.stop_price < 200.0
        assert sizing.notional_value == pytest.approx(sizing.shares * 200.0)
        assert 0 < sizing.pct_of_portfolio <= 0.10
