"""
tests/test_kill_switch.py
Unit tests for the Kill Switch service.

Pure computation tests for check_should_trigger().
DB-layer tests use in-memory mocks (no real DB).
"""
from __future__ import annotations

import pytest

from apps.svc_risk.kill_switch import (
    DRAWDOWN_THRESHOLD,
    KillSwitchCheck,
    check_should_trigger,
)
from packages.shared.enums import KillSwitchTrigger


# ── TestCheckShouldTrigger ────────────────────────────────────────────────────

class TestCheckShouldTrigger:
    def test_no_drawdown_does_not_trigger(self):
        result = check_should_trigger(peak_equity=100_000, current_equity=100_000)
        assert result.should_trigger is False

    def test_small_drawdown_does_not_trigger(self):
        # 10% drawdown < 15% threshold
        result = check_should_trigger(peak_equity=100_000, current_equity=90_000)
        assert result.should_trigger is False
        assert result.drawdown_pct == pytest.approx(0.10)

    def test_exactly_at_threshold_triggers(self):
        # 15% drawdown == threshold → should trigger
        result = check_should_trigger(peak_equity=100_000, current_equity=85_000)
        assert result.should_trigger is True
        assert result.drawdown_pct == pytest.approx(0.15)

    def test_above_threshold_triggers(self):
        # 20% drawdown > 15% threshold
        result = check_should_trigger(peak_equity=100_000, current_equity=80_000)
        assert result.should_trigger is True
        assert result.drawdown_pct == pytest.approx(0.20)

    def test_equity_above_peak_no_trigger(self):
        # Equity exceeds peak (shouldn't happen in practice, but must not crash)
        result = check_should_trigger(peak_equity=100_000, current_equity=110_000)
        assert result.should_trigger is False
        assert result.drawdown_pct == pytest.approx(-0.10)

    def test_zero_peak_equity_no_trigger(self):
        result = check_should_trigger(peak_equity=0, current_equity=0)
        assert result.should_trigger is False
        assert result.reason == "peak_equity_zero_or_negative"

    def test_negative_peak_equity_no_trigger(self):
        result = check_should_trigger(peak_equity=-1000, current_equity=900)
        assert result.should_trigger is False

    def test_custom_threshold(self):
        # Custom 20% threshold
        result = check_should_trigger(
            peak_equity=100_000,
            current_equity=85_000,    # 15% drawdown
            drawdown_threshold=0.20,  # threshold = 20%
        )
        assert result.should_trigger is False  # 15% < 20%

    def test_trigger_is_drawdown_limit(self):
        result = check_should_trigger(peak_equity=100_000, current_equity=80_000)
        assert result.trigger == KillSwitchTrigger.DRAWDOWN_LIMIT.value

    def test_reason_contains_drawdown_pct(self):
        result = check_should_trigger(peak_equity=100_000, current_equity=80_000)
        assert "20.00%" in result.reason

    def test_returns_kill_switch_check(self):
        result = check_should_trigger(peak_equity=100_000, current_equity=90_000)
        assert isinstance(result, KillSwitchCheck)

    def test_drawdown_pct_precision(self):
        # 5000/100000 = 5% exactly
        result = check_should_trigger(peak_equity=100_000, current_equity=95_000)
        assert result.drawdown_pct == pytest.approx(0.05, abs=1e-6)

    def test_large_portfolio(self):
        # Same ratio, bigger numbers
        result = check_should_trigger(
            peak_equity=10_000_000,
            current_equity=8_200_000,  # 18% drawdown
        )
        assert result.should_trigger is True
        assert result.drawdown_pct == pytest.approx(0.18, abs=0.001)

    def test_default_threshold_is_15_pct(self):
        assert DRAWDOWN_THRESHOLD == pytest.approx(0.15)

    def test_just_below_threshold_no_trigger(self):
        # 14.99% drawdown
        result = check_should_trigger(
            peak_equity=100_000,
            current_equity=85_001,   # ~15% minus $1
        )
        assert result.should_trigger is False
