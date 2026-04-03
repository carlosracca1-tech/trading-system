"""
Tests for the MREV-1H full pipeline (scan → risk → exec plan).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from apps.svc_orchestrator_mrev.pipeline import (
    MrevPipelineRun,
    MrevPortfolioState,
    run_mrev_pipeline,
)
from packages.shared.enums import RiskDecision, SignalType


NOW = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)


def _make_oversold_row(close=95.0) -> pd.Series:
    return pd.Series({
        "close": close,
        "sma_20": 100.0,
        "bb_upper": 104.0,
        "bb_lower": 96.0,
        "rsi_14": 25.0,
        "atr_14": 2.0,
        "atr_14_pct": 0.021,
        "volume": 1500.0,
        "volume_ma_20": 1000.0,
    })


def _make_neutral_row(close=100.0) -> pd.Series:
    return pd.Series({
        "close": close,
        "sma_20": 100.0,
        "bb_upper": 104.0,
        "bb_lower": 96.0,
        "rsi_14": 50.0,
        "atr_14": 2.0,
        "atr_14_pct": 0.02,
        "volume": 1200.0,
        "volume_ma_20": 1000.0,
    })


class TestMrevPipeline:
    """Integration tests for the full MREV pipeline."""

    def test_entry_signal_flows_through_pipeline(self):
        """An oversold symbol should generate ENTER → APPROVED → BUY intent."""
        rows = {"SPY": _make_oversold_row()}
        portfolio = MrevPortfolioState(
            total_equity=1000.0, peak_equity=1000.0,
            open_position_count=0, cash=1000.0,
        )

        result = run_mrev_pipeline(
            symbols=["SPY"],
            rows=rows,
            open_positions={},
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        assert len(result.enter_signals) == 1
        assert result.enter_signals[0].symbol == "SPY"
        assert len(result.approved_entries) == 1
        assert len(result.exec_plan) == 1
        assert result.exec_plan[0].is_entry is True

    def test_no_signal_for_neutral_market(self):
        """A symbol at fair value (RSI ~50, close ~SMA) should generate HOLD."""
        rows = {"SPY": _make_neutral_row()}
        portfolio = MrevPortfolioState(1000.0, 1000.0, 0, 1000.0)

        result = run_mrev_pipeline(
            symbols=["SPY"],
            rows=rows,
            open_positions={},
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        assert len(result.enter_signals) == 0
        assert len(result.exec_plan) == 0

    def test_exit_when_mean_reverts(self):
        """An open position where price reverted to SMA should trigger EXIT."""
        row = _make_neutral_row(close=101.0)  # close > sma_20=100 → take profit
        rows = {"BTC/USD": row}

        open_positions = {
            "BTC/USD": {
                "position_id": "pos-123",
                "entry_price": 95.0,
                "entry_datetime": NOW - timedelta(hours=5),
            }
        }
        portfolio = MrevPortfolioState(1000.0, 1000.0, 1, 900.0)

        result = run_mrev_pipeline(
            symbols=["BTC/USD"],
            rows=rows,
            open_positions=open_positions,
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        assert len(result.exit_signals) == 1
        assert len(result.exec_plan) == 1
        assert result.exec_plan[0].is_exit is True
        assert "take_profit" in result.exec_plan[0].signal.reason

    def test_multiple_symbols_mixed(self):
        """Pipeline handles mix of entry, exit, and hold signals."""
        rows = {
            "BTC/USD": _make_oversold_row(),   # should trigger ENTER
            "ETH/USD": _make_neutral_row(),     # should HOLD
        }
        portfolio = MrevPortfolioState(2000.0, 2000.0, 0, 2000.0)

        result = run_mrev_pipeline(
            symbols=["BTC/USD", "ETH/USD"],
            rows=rows,
            open_positions={},
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        assert len(result.enter_signals) == 1
        assert result.enter_signals[0].symbol == "BTC/USD"

    def test_summary_returns_dict(self):
        rows = {"SPY": _make_neutral_row()}
        portfolio = MrevPortfolioState(1000.0, 1000.0, 0, 1000.0)

        result = run_mrev_pipeline(
            symbols=["SPY"],
            rows=rows,
            open_positions={},
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        summary = result.summary()
        assert isinstance(summary, dict)
        assert "datetime" in summary
        assert "stages" in summary
        assert len(summary["stages"]) == 3

    def test_exits_come_before_entries(self):
        """Exec plan should order exits before entries."""
        # BTC has an open position at mean → exit
        # SPY is oversold → enter
        exit_row = _make_neutral_row(close=101.0)
        enter_row = _make_oversold_row()

        rows = {"BTC/USD": exit_row, "SPY": enter_row}
        open_positions = {
            "BTC/USD": {
                "position_id": "pos-1",
                "entry_price": 95.0,
                "entry_datetime": NOW - timedelta(hours=3),
            }
        }
        portfolio = MrevPortfolioState(2000.0, 2000.0, 1, 1500.0)

        result = run_mrev_pipeline(
            symbols=["BTC/USD", "SPY"],
            rows=rows,
            open_positions=open_positions,
            portfolio=portfolio,
            as_of_datetime=NOW,
        )

        if len(result.exec_plan) >= 2:
            assert result.exec_plan[0].is_exit is True
            assert result.exec_plan[1].is_entry is True
