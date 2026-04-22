"""
Tests for MREV-1H scanner (entry and exit signals).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from apps.svc_strategy_mrev.scanner import (
    MrevSignalDecision,
    check_mrev_entry_signal,
    check_mrev_exit_signal,
)
from packages.shared.enums import SignalType


NOW = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)


def _oversold_row() -> dict:
    """A row that satisfies all MREV entry conditions."""
    return {
        "close": 95.0,
        "sma_20": 100.0,
        "bb_upper": 104.0,
        "bb_lower": 96.0,
        "rsi_14": 25.0,
        "atr_14": 2.0,
        "atr_14_pct": 0.021,
        "volume": 1500.0,
        "volume_ma_20": 1000.0,
    }


def _open_position_row() -> dict:
    """A row for an open position (mean-reverted back to SMA)."""
    return {
        "close": 101.0,
        "sma_20": 100.0,
        "bb_upper": 104.0,
        "bb_lower": 96.0,
        "rsi_14": 52.0,
        "atr_14": 2.0,
        "atr_14_pct": 0.02,
        "volume": 1200.0,
        "volume_ma_20": 1000.0,
    }


class TestMrevEntrySignal:
    """Test MREV entry signal generation."""

    def test_valid_entry_returns_enter(self):
        result = check_mrev_entry_signal("BTC/USD", _oversold_row(), NOW)
        assert result.signal_type == SignalType.ENTER.value
        assert result.symbol == "BTC/USD"
        assert result.close_price == 95.0

    def test_rsi_not_oversold_returns_hold(self):
        row = _oversold_row()
        row["rsi_14"] = 55.0
        result = check_mrev_entry_signal("BTC/USD", row, NOW)
        assert result.signal_type == SignalType.HOLD.value
        assert "rsi_not_oversold" in result.reason

    def test_close_above_bb_lower_returns_hold(self):
        row = _oversold_row()
        row["close"] = 98.0  # above bb_lower of 96
        result = check_mrev_entry_signal("SPY", row, NOW)
        assert result.signal_type == SignalType.HOLD.value
        assert "close_above_bb_lower" in result.reason

    def test_indicator_snapshot_populated(self):
        result = check_mrev_entry_signal("ETH/USD", _oversold_row(), NOW)
        assert result.atr_14 == 2.0
        assert result.rsi_14 == 25.0
        assert result.sma_20 == 100.0
        assert result.bb_lower == 96.0

    def test_accepts_pandas_series(self):
        row = pd.Series(_oversold_row())
        result = check_mrev_entry_signal("BTC/USD", row, NOW)
        assert result.signal_type == SignalType.ENTER.value


class TestMrevExitSignal:
    """Test MREV exit signal generation."""

    def test_stop_loss_triggered(self):
        """Exit when close drops below entry - 1.5 × ATR."""
        row = _open_position_row()
        row["close"] = 91.0  # entry=95, stop=95-1.5*2=92 → 91 < 92
        row["sma_20"] = 100.0  # keep sma above close so TP doesn't trigger
        result = check_mrev_exit_signal(
            "BTC/USD", row, NOW, entry_price=95.0, entry_datetime=NOW - timedelta(hours=5),
        )
        assert result.signal_type == SignalType.EXIT.value
        assert "stop_loss_hit" in result.reason

    def test_hold_when_no_exit_conditions(self):
        """Hold when price is between entry and SMA, RSI below 40, recent entry."""
        row = _open_position_row()
        row["close"] = 97.0     # below SMA
        row["sma_20"] = 100.0
        row["rsi_14"] = 35.0    # below normalized range
        entry_dt = NOW - timedelta(hours=3)  # recent entry
        result = check_mrev_exit_signal(
            "BTC/USD", row, NOW, entry_price=95.0, entry_datetime=entry_dt,
        )
        assert result.signal_type == SignalType.HOLD.value
