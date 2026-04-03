"""
Tests for MREV-1H indicator computation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from apps.svc_data_1h.indicators import compute_mrev_indicators, validate_mrev_entry_conditions
from apps.svc_data_1h.synthetic import generate_1h_ohlcv


class TestComputeMrevIndicators:
    """Test the 1H indicator computation function."""

    def test_returns_all_expected_columns(self):
        df = generate_1h_ohlcv(bars=100, seed=42)
        result = compute_mrev_indicators(df)

        expected_cols = {"sma_20", "bb_upper", "bb_lower", "bb_width", "rsi_14", "atr_14", "atr_14_pct", "volume_ma_20"}
        assert expected_cols.issubset(set(result.columns))

    def test_bollinger_bands_relationship(self):
        """Upper band > SMA > Lower band always."""
        df = generate_1h_ohlcv(bars=100, seed=42)
        result = compute_mrev_indicators(df)

        valid = result.dropna(subset=["sma_20", "bb_upper", "bb_lower"])
        assert len(valid) > 0

        assert (valid["bb_upper"] >= valid["sma_20"]).all()
        assert (valid["sma_20"] >= valid["bb_lower"]).all()

    def test_rsi_bounded_0_100(self):
        df = generate_1h_ohlcv(bars=200, seed=42)
        result = compute_mrev_indicators(df)

        valid_rsi = result["rsi_14"].dropna()
        assert len(valid_rsi) > 0
        assert (valid_rsi >= 0).all()
        assert (valid_rsi <= 100).all()

    def test_atr_positive(self):
        df = generate_1h_ohlcv(bars=100, seed=42)
        result = compute_mrev_indicators(df)

        valid_atr = result["atr_14"].dropna()
        assert len(valid_atr) > 0
        assert (valid_atr > 0).all()

    def test_warm_up_nans(self):
        """First 19 rows should have NaN for SMA/BB (need 20 periods)."""
        df = generate_1h_ohlcv(bars=50, seed=42)
        result = compute_mrev_indicators(df)

        assert result["sma_20"].iloc[:19].isna().all()
        assert result["sma_20"].iloc[19:].notna().all()

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        result = compute_mrev_indicators(df)
        assert result.empty

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"datetime": [], "close": []})
        try:
            compute_mrev_indicators(df)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "missing columns" in str(e)

    def test_does_not_modify_input(self):
        df = generate_1h_ohlcv(bars=50, seed=42)
        original_cols = set(df.columns)
        compute_mrev_indicators(df)
        assert set(df.columns) == original_cols


class TestValidateMrevEntryConditions:
    """Test the entry condition validator."""

    def test_valid_entry(self):
        row = pd.Series({
            "close": 95.0,
            "sma_20": 100.0,
            "bb_lower": 96.0,
            "bb_upper": 104.0,
            "rsi_14": 25.0,
            "atr_14": 2.0,
            "atr_14_pct": 0.02,
            "volume": 1500.0,
            "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is True
        assert reason == ""

    def test_rejects_rsi_above_30(self):
        row = pd.Series({
            "close": 95.0, "sma_20": 100.0, "bb_lower": 96.0,
            "bb_upper": 104.0, "rsi_14": 45.0, "atr_14": 2.0,
            "atr_14_pct": 0.02, "volume": 1500.0, "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is False
        assert "rsi_not_oversold" in reason

    def test_rejects_close_above_bb_lower(self):
        row = pd.Series({
            "close": 98.0, "sma_20": 100.0, "bb_lower": 96.0,
            "bb_upper": 104.0, "rsi_14": 25.0, "atr_14": 2.0,
            "atr_14_pct": 0.02, "volume": 1500.0, "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is False
        assert "close_above_bb_lower" in reason

    def test_rejects_low_volume(self):
        row = pd.Series({
            "close": 95.0, "sma_20": 100.0, "bb_lower": 96.0,
            "bb_upper": 104.0, "rsi_14": 25.0, "atr_14": 2.0,
            "atr_14_pct": 0.02, "volume": 500.0, "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is False
        assert "volume_below_average" in reason

    def test_rejects_extreme_volatility(self):
        row = pd.Series({
            "close": 95.0, "sma_20": 100.0, "bb_lower": 96.0,
            "bb_upper": 104.0, "rsi_14": 25.0, "atr_14": 20.0,
            "atr_14_pct": 0.20, "volume": 1500.0, "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is False
        assert "atr_pct_out_of_range" in reason

    def test_rejects_nan_indicators(self):
        row = pd.Series({
            "close": 95.0, "sma_20": float("nan"), "bb_lower": 96.0,
            "bb_upper": 104.0, "rsi_14": 25.0, "atr_14": 2.0,
            "atr_14_pct": 0.02, "volume": 1500.0, "volume_ma_20": 1000.0,
        })
        ok, reason = validate_mrev_entry_conditions(row)
        assert ok is False
        assert "indicators_not_ready" in reason
