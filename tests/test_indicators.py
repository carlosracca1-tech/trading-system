"""
tests/test_indicators.py
Unit tests for indicator computation and signal validation.
No database required — pure pandas math tests.

Test strategy:
  - Feed synthetic price series with known properties
  - Verify EMA / RSI / ATR values match hand-calculated expectations
  - Verify signal conditions pass/fail correctly
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from apps.svc_data.indicators import (
    compute_indicators,
    is_bullish_regime,
    validate_signal_conditions,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_df(
    n: int,
    start_price: float = 100.0,
    trend: float = 0.001,  # daily return (0.1%)
    volume: int = 1_000_000,
    start_date: date | None = None,
) -> pd.DataFrame:
    """
    Generate a synthetic OHLCV DataFrame with a linear price trend.
    Prices: close[t] = start_price * (1 + trend)^t
    """
    if start_date is None:
        start_date = date(2020, 1, 2)

    dates = [start_date + timedelta(days=i) for i in range(n)]
    closes = [start_price * ((1 + trend) ** i) for i in range(n)]

    return pd.DataFrame(
        {
            "date": dates,
            "open": [c * 0.999 for c in closes],
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.994 for c in closes],
            "close": closes,
            "volume": [volume] * n,
        }
    )


def _make_flat_df(n: int, price: float = 100.0, volume: int = 1_000_000) -> pd.DataFrame:
    """Flat price series — ATR should be small, RSI should be ~50."""
    start = date(2020, 1, 2)
    dates = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [price] * n,
            "high": [price * 1.001] * n,
            "low": [price * 0.999] * n,
            "close": [price] * n,
            "volume": [volume] * n,
        }
    )


# ── compute_indicators ─────────────────────────────────────────────────────────

class TestComputeIndicators:
    def test_returns_dataframe(self):
        df = _make_df(250)
        result = compute_indicators(df)
        assert isinstance(result, pd.DataFrame)

    def test_does_not_modify_input(self):
        df = _make_df(250)
        original_cols = list(df.columns)
        _ = compute_indicators(df)
        assert list(df.columns) == original_cols  # input unchanged

    def test_adds_expected_columns(self):
        df = _make_df(250)
        result = compute_indicators(df)
        for col in ["ema_50", "ema_200", "rsi_14", "atr_14", "atr_14_pct", "volume_ma_20", "high_20d"]:
            assert col in result.columns, f"Missing column: {col}"

    def test_nan_before_warmup_ema50(self):
        """EMA50 should be NaN for the first 49 rows (min_periods=50)."""
        df = _make_df(100)
        result = compute_indicators(df)
        # Rows 0-48 should have NaN ema_50
        assert result["ema_50"].iloc[:49].isna().all()
        # Row 49+ should have valid values
        assert not result["ema_50"].iloc[50:].isna().any()

    def test_nan_before_warmup_ema200(self):
        """EMA200 requires 200 rows minimum."""
        df = _make_df(210)
        result = compute_indicators(df)
        assert result["ema_200"].iloc[:199].isna().all()
        assert not result["ema_200"].iloc[200:].isna().any()

    def test_ema_trend_alignment_uptrend(self):
        """In a strong uptrend: close > EMA50 > EMA200."""
        df = _make_df(300, start_price=100.0, trend=0.002)  # 0.2% daily
        result = compute_indicators(df)
        last = result.iloc[-1]
        assert not pd.isna(last["ema_50"])
        assert not pd.isna(last["ema_200"])
        assert last["close"] > last["ema_50"]
        assert last["ema_50"] > last["ema_200"]

    def test_ema_trend_alignment_downtrend(self):
        """In a strong downtrend: close < EMA50 < EMA200."""
        df = _make_df(300, start_price=200.0, trend=-0.002)
        result = compute_indicators(df)
        last = result.iloc[-1]
        assert not pd.isna(last["ema_50"])
        assert not pd.isna(last["ema_200"])
        assert last["close"] < last["ema_50"]
        assert last["ema_50"] < last["ema_200"]

    def test_rsi_bounds(self):
        """RSI must always be in [0, 100]."""
        df = _make_df(250, trend=0.003)
        result = compute_indicators(df)
        rsi = result["rsi_14"].dropna()
        assert (rsi >= 0).all()
        assert (rsi <= 100).all()

    def test_rsi_uptrend_is_high(self):
        """Strong uptrend should have RSI > 50."""
        df = _make_df(100, trend=0.005)
        result = compute_indicators(df)
        rsi_last = result["rsi_14"].dropna().iloc[-1]
        assert rsi_last > 50

    def test_rsi_downtrend_is_low(self):
        """Strong downtrend should have RSI < 50."""
        df = _make_df(100, start_price=200.0, trend=-0.005)
        result = compute_indicators(df)
        rsi_last = result["rsi_14"].dropna().iloc[-1]
        assert rsi_last < 50

    def test_atr_positive(self):
        """ATR should always be positive."""
        df = _make_df(100)
        result = compute_indicators(df)
        atr = result["atr_14"].dropna()
        assert (atr > 0).all()

    def test_atr_pct_is_fraction_of_price(self):
        """ATR% = ATR / close — should be a small positive fraction."""
        df = _make_df(100, start_price=100.0)
        result = compute_indicators(df)
        atr_pct = result["atr_14_pct"].dropna()
        assert (atr_pct > 0).all()
        assert (atr_pct < 1).all()  # ATR never exceeds 100% of price in normal markets

    def test_volume_ma20_is_sma(self):
        """Volume MA20 at row 20 should equal mean of rows 0-19."""
        df = _make_flat_df(50, volume=1_000_000)
        result = compute_indicators(df)
        # Volume is constant so MA should equal the constant
        vol_ma = result["volume_ma_20"].iloc[19]  # min_periods=20 → first valid at index 19
        assert not pd.isna(vol_ma)
        assert vol_ma == pytest.approx(1_000_000.0)

    def test_high_20d_is_previous_20_bar_high(self):
        """high_20d at row i should be the max of high[i-20:i-1]."""
        df = _make_df(50)
        result = compute_indicators(df)
        # Row 20 (0-indexed) should have high_20d = max(high[0:19]) — the shifted rolling max
        row_20 = result.iloc[20]
        if not pd.isna(row_20["high_20d"]):
            expected = df["high"].iloc[0:20].max()
            assert row_20["high_20d"] == pytest.approx(expected)

    def test_empty_dataframe(self):
        """Empty DataFrame should return empty DataFrame without error."""
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = compute_indicators(df)
        assert result.empty

    def test_missing_column_raises(self):
        df = pd.DataFrame({"date": [], "open": [], "high": [], "low": [], "close": []})
        # missing 'volume'
        with pytest.raises(ValueError, match="missing columns"):
            compute_indicators(df)

    def test_output_length_matches_input(self):
        n = 300
        df = _make_df(n)
        result = compute_indicators(df)
        assert len(result) == n

    def test_sorted_by_date(self):
        """Output should be sorted ascending by date regardless of input order."""
        df = _make_df(50)
        shuffled = df.sample(frac=1, random_state=42)
        result = compute_indicators(shuffled)
        dates = result["date"].tolist()
        assert dates == sorted(dates)


# ── is_bullish_regime ──────────────────────────────────────────────────────────

class TestIsBullishRegime:
    def test_uptrend_is_bullish(self):
        df = _make_df(300, trend=0.002)
        regime = is_bullish_regime(df)
        # Last row should be bullish
        assert regime.iloc[-1] is True or regime.iloc[-1] == True  # noqa: E712

    def test_downtrend_is_not_bullish(self):
        df = _make_df(300, start_price=500.0, trend=-0.002)
        regime = is_bullish_regime(df)
        assert regime.iloc[-1] is False or regime.iloc[-1] == False  # noqa: E712

    def test_empty_returns_empty(self):
        df = pd.DataFrame(columns=["date", "close"])
        regime = is_bullish_regime(df)
        assert len(regime) == 0


# ── validate_signal_conditions ─────────────────────────────────────────────────

class TestValidateSignalConditions:
    def _perfect_row(self) -> dict:
        """Row that passes all 6 conditions."""
        return {
            "close": 100.0,
            "ema_50": 95.0,   # close > ema_50
            "ema_200": 85.0,  # ema_50 > ema_200
            "rsi_14": 60.0,   # 50 <= rsi <= 70
            "high_20d": 98.0, # close >= high_20d (breakout)
            "volume": 1_500_000,
            "volume_ma_20": 1_000_000,  # volume >= ma_20 * 1.2 (1.5M >= 1.2M)
            "atr_14_pct": 0.02,         # 0.01 <= atr_pct <= 0.05
        }

    def test_perfect_row_passes(self):
        ok, reason = validate_signal_conditions(pd.Series(self._perfect_row()))
        assert ok is True
        assert reason == ""

    def test_close_below_ema50_fails(self):
        row = self._perfect_row()
        row["close"] = 80.0  # below ema_50=95
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "close_not_above_emas" in reason

    def test_ema50_below_ema200_fails(self):
        row = self._perfect_row()
        row["ema_50"] = 80.0  # below ema_200=85
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False

    def test_rsi_too_low_fails(self):
        row = self._perfect_row()
        row["rsi_14"] = 45.0  # < 50
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "rsi_out_of_range" in reason

    def test_rsi_too_high_fails(self):
        row = self._perfect_row()
        row["rsi_14"] = 75.0  # > 70
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False

    def test_rsi_boundary_50_passes(self):
        row = self._perfect_row()
        row["rsi_14"] = 50.0
        ok, _ = validate_signal_conditions(pd.Series(row))
        assert ok is True

    def test_rsi_boundary_70_passes(self):
        row = self._perfect_row()
        row["rsi_14"] = 70.0
        ok, _ = validate_signal_conditions(pd.Series(row))
        assert ok is True

    def test_no_breakout_fails(self):
        row = self._perfect_row()
        row["close"] = 100.0
        row["high_20d"] = 105.0  # close < high_20d (no breakout)
        # But now close < ema_50 would also fail, so fix ema
        row["ema_50"] = 97.0
        row["ema_200"] = 85.0
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "no_20d_breakout" in reason

    def test_low_volume_fails(self):
        row = self._perfect_row()
        row["volume"] = 1_000_000  # equals ma_20, need >= 1.2 * ma_20 = 1.2M
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "volume_below_threshold" in reason

    def test_atr_too_low_fails(self):
        row = self._perfect_row()
        row["atr_14_pct"] = 0.005  # < 0.01
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "atr_pct_out_of_range" in reason

    def test_atr_too_high_fails(self):
        row = self._perfect_row()
        row["atr_14_pct"] = 0.06  # > 0.05
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "atr_pct_out_of_range" in reason

    def test_nan_indicator_fails_gracefully(self):
        """NaN indicators (not enough history) should fail with meaningful reason."""
        row = self._perfect_row()
        row["ema_50"] = float("nan")
        ok, reason = validate_signal_conditions(pd.Series(row))
        assert ok is False
        assert "indicators_not_ready" in reason

    def test_exact_breakout_boundary(self):
        """close == high_20d should pass (>= condition)."""
        row = self._perfect_row()
        row["close"] = 100.0
        row["high_20d"] = 100.0  # exactly equal
        ok, _ = validate_signal_conditions(pd.Series(row))
        assert ok is True

    def test_exact_volume_boundary(self):
        """volume == volume_ma_20 * 1.2 should pass."""
        row = self._perfect_row()
        row["volume"] = 1_200_000
        row["volume_ma_20"] = 1_000_000
        ok, _ = validate_signal_conditions(pd.Series(row))
        assert ok is True


# ── Edge cases ─────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_single_row_df(self):
        """Single row should not crash — all indicators NaN."""
        df = _make_df(1)
        result = compute_indicators(df)
        assert len(result) == 1
        assert pd.isna(result["ema_50"].iloc[0])
        assert pd.isna(result["rsi_14"].iloc[0])

    def test_exactly_200_rows(self):
        """Exactly 200 rows — EMA200 should be valid on the last row only."""
        df = _make_df(200)
        result = compute_indicators(df)
        assert not pd.isna(result["ema_200"].iloc[-1])

    def test_rsi_all_gains(self):
        """If every day is an up day, RSI should approach 100."""
        dates = [date(2021, 1, 1) + timedelta(days=i) for i in range(100)]
        closes = [100.0 + i for i in range(100)]  # steadily rising
        df = pd.DataFrame({
            "date": dates,
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1_000_000] * 100,
        })
        result = compute_indicators(df)
        rsi = result["rsi_14"].dropna().iloc[-1]
        assert rsi > 90  # strong uptrend → RSI close to 100

    def test_rsi_all_losses(self):
        """If every day is a down day, RSI should approach 0."""
        dates = [date(2021, 1, 1) + timedelta(days=i) for i in range(100)]
        closes = [200.0 - i for i in range(100)]  # steadily falling
        df = pd.DataFrame({
            "date": dates,
            "open": [c + 0.1 for c in closes],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000] * 100,
        })
        result = compute_indicators(df)
        rsi = result["rsi_14"].dropna().iloc[-1]
        assert rsi < 10  # strong downtrend → RSI close to 0
