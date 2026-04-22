"""
tests/test_strategy.py
Unit tests for the RFTM Strategy Scanner.

Pure computation — no DB required.
Tests cover:
  - is_regime_bullish()
  - check_entry_signal()
  - check_exit_signal()
  - SignalDecision field population
"""
from __future__ import annotations

from datetime import date

import pytest

from apps.svc_strategy.scanner import (
    STRATEGY_PARAMS,
    SignalDecision,
    check_entry_signal,
    check_exit_signal,
    is_regime_bullish,
)
from packages.shared.enums import RiskDecision, SignalType


# ── Helpers ────────────────────────────────────────────────────────────────────

TODAY = date(2024, 6, 15)

def _perfect_entry_row() -> dict:
    """A row that satisfies all 6 RFTM entry conditions."""
    return {
        "date": TODAY,
        "close": 220.0,
        "open": 218.0,
        "high": 222.0,
        "low": 217.0,
        "volume": 2_000_000,        # >= volume_ma_20 * 1.2  (1_500_000 * 1.2 = 1_800_000)
        "ema_50": 210.0,            # close(220) > ema_50(210) > ema_200(190)
        "ema_200": 190.0,
        "rsi_14": 60.0,             # 50 <= 60 <= 70
        "atr_14": 3.0,
        "atr_14_pct": 0.0136,       # 3 / 220 ≈ 0.0136  (0.01..0.05)
        "volume_ma_20": 1_500_000,
        "high_20d": 219.0,          # close(220) >= high_20d(219)
    }


def _perfect_hold_row(entry_price: float = 200.0) -> dict:
    """A row for a held position — trend intact, no exit triggers."""
    return {
        "date": TODAY,
        "close": 215.0,
        "ema_50": 210.0,
        "ema_200": 190.0,
        "rsi_14": 58.0,
        "atr_14": 3.0,
    }


def _spy_bullish_row() -> dict:
    return {"close": 500.0, "ema_200": 450.0}


def _spy_bearish_row() -> dict:
    return {"close": 400.0, "ema_200": 450.0}


# ── TestIsRegimeBullish ────────────────────────────────────────────────────────

class TestIsRegimeBullish:
    def test_spy_above_ema200_is_bullish(self):
        assert is_regime_bullish(_spy_bullish_row()) is True

    def test_spy_below_ema200_is_bearish(self):
        assert is_regime_bullish(_spy_bearish_row()) is False

    def test_spy_equal_ema200_is_bearish(self):
        """Regime requires STRICTLY above EMA200."""
        row = {"close": 450.0, "ema_200": 450.0}
        assert is_regime_bullish(row) is False

    def test_none_row_is_bearish(self):
        assert is_regime_bullish(None) is False

    def test_missing_ema200_is_bearish(self):
        assert is_regime_bullish({"close": 500.0}) is False

    def test_missing_close_is_bearish(self):
        assert is_regime_bullish({"ema_200": 450.0}) is False

    def test_nan_ema200_is_bearish(self):
        import math
        assert is_regime_bullish({"close": 500.0, "ema_200": float("nan")}) is False

    def test_nan_close_is_bearish(self):
        import math
        assert is_regime_bullish({"close": float("nan"), "ema_200": 450.0}) is False


# ── TestCheckEntrySignal ───────────────────────────────────────────────────────

class TestCheckEntrySignal:
    def test_all_conditions_met_returns_enter(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.signal_type == SignalType.ENTER.value
        assert result.symbol == "SPY"
        assert result.signal_date == TODAY
        assert result.close_price == pytest.approx(220.0)

    def test_bearish_regime_returns_hold(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=False)
        assert result.signal_type == SignalType.HOLD.value
        assert result.reason == "bearish_regime"

    def test_enter_signal_carries_indicators(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.atr_14 == pytest.approx(3.0)
        assert result.ema_50 == pytest.approx(210.0)
        assert result.ema_200 == pytest.approx(190.0)
        assert result.rsi_14 == pytest.approx(60.0)

    def test_enter_carries_regime_ok_true(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.regime_ok is True

    def test_hold_carries_regime_ok_false(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=False)
        assert result.regime_ok is False

    def test_close_below_ema50_returns_hold(self):
        row = _perfect_entry_row()
        row["close"] = 200.0   # below ema_50 = 210
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.signal_type == SignalType.HOLD.value

    def test_rsi_boundary_50_enters(self):
        row = _perfect_entry_row()
        row["rsi_14"] = 50.0
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.signal_type == SignalType.ENTER.value

    def test_rsi_boundary_70_enters(self):
        row = _perfect_entry_row()
        row["rsi_14"] = 70.0
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.signal_type == SignalType.ENTER.value

    def test_risk_decision_defaults_to_pending(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        assert result.risk_decision == RiskDecision.PENDING.value

    def test_volume_ratio_computed(self):
        row = _perfect_entry_row()
        result = check_entry_signal("SPY", row, TODAY, regime_bullish=True)
        # volume(2_000_000) / volume_ma_20(1_500_000) = 1.3333
        assert result.volume_ratio == pytest.approx(2_000_000 / 1_500_000, rel=1e-3)


# ── TestCheckExitSignal ────────────────────────────────────────────────────────

class TestCheckExitSignal:
    def test_no_exit_condition_returns_hold(self):
        row = _perfect_hold_row()
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.signal_type == SignalType.HOLD.value

    def test_stop_loss_triggers_exit(self):
        # entry_price=200, atr=3, stop = 200 - 2*3 = 194
        row = _perfect_hold_row()
        row["close"] = 193.0    # below stop (194)
        row["ema_50"] = 192.0   # below close → E2 doesn't fire; isolates E3
        row["ema_200"] = 190.0  # ema_50 > ema_200 → no death cross
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.signal_type == SignalType.EXIT.value
        assert "stop_loss_hit" in result.reason

    def test_stop_loss_exact_boundary_exits(self):
        # close == stop_price → should exit (<=)
        row = _perfect_hold_row()
        row["close"] = 194.0    # exactly at stop (200 - 2*3)
        row["ema_50"] = 192.0   # below close → E2 doesn't fire; isolates E3
        row["ema_200"] = 190.0
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.signal_type == SignalType.EXIT.value

    def test_rsi_exactly_80_holds(self):
        """RSI = 80.0 does NOT trigger; only strictly > 80."""
        row = _perfect_hold_row()
        row["rsi_14"] = 80.0
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.signal_type == SignalType.HOLD.value

    def test_exit_carries_indicator_snapshot(self):
        """When check_exit returns a decision, the indicator snapshot is populated."""
        row = _perfect_hold_row()
        row["ema_50"] = 185.0
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.ema_50 == pytest.approx(185.0)
        assert result.ema_200 == pytest.approx(190.0)
        assert result.atr_14 == pytest.approx(3.0)

    def test_missing_indicators_no_false_exit(self):
        """All indicators None → no exit condition → HOLD."""
        row = {"close": 215.0, "ema_50": None, "ema_200": None, "rsi_14": None, "atr_14": None}
        result = check_exit_signal("SPY", row, TODAY, entry_price=200.0)
        assert result.signal_type == SignalType.HOLD.value
