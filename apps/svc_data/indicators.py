"""
apps/svc_data/indicators.py
Technical indicator computation using pandas.

All indicators use Wilder's smoothing (EWM with alpha=1/period, adjust=False),
which matches industry-standard implementations (TradingView, Bloomberg).

Input:  DataFrame with columns [date, open, high, low, close, volume]
        sorted ascending by date.
Output: Same DataFrame with added indicator columns (NaN where insufficient data).

Strategy params (from constants.py):
  EMA:       50, 200 periods
  RSI:       14 periods
  ATR:       14 periods
  Volume MA: 20 periods (SMA)
  Breakout:  20-day high
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Minimum rows required before we trust the indicators
_MIN_ROWS_EMA50 = 50
_MIN_ROWS_EMA200 = 200
_MIN_ROWS_RSI = 14
_MIN_ROWS_ATR = 14
_MIN_ROWS_VOL = 20
_MIN_ROWS_BREAKOUT = 20


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all RFTM strategy indicators for a single symbol.

    Args:
        df: DataFrame with columns [date, open, high, low, close, volume],
            sorted ascending by date. `date` can be date or datetime objects.

    Returns:
        df with added columns:
          ema_50, ema_200, rsi_14, atr_14, atr_14_pct, volume_ma_20, high_20d

    Notes:
      - NaN is returned for rows where there is insufficient history.
      - The input DataFrame is NOT modified in-place; a copy is returned.
    """
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"compute_indicators: missing columns {missing}")

    if df.empty:
        return df.copy()

    df = df.copy().sort_values("date").reset_index(drop=True)

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # ── EMA 21 / 50 / 100 / 200 ────────────────────────────────────────────
    # EMA21 = primary trend for aggressive entries (replaces EMA50 as entry filter)
    # EMA100 = regime filter (replaces EMA200 for faster reaction)
    df["ema_21"] = close.ewm(span=21, adjust=False, min_periods=21).mean()
    df["ema_50"] = close.ewm(span=50, adjust=False, min_periods=_MIN_ROWS_EMA50).mean()
    df["ema_100"] = close.ewm(span=100, adjust=False, min_periods=100).mean()
    df["ema_200"] = close.ewm(span=200, adjust=False, min_periods=_MIN_ROWS_EMA200).mean()

    # ── RSI 14 (Wilder's) ────────────────────────────────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # alpha = 1/14 for Wilder's smoothing
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=_MIN_ROWS_RSI).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=_MIN_ROWS_RSI).mean()

    # When avg_loss == 0 (all-gain streak) → RSI = 100 by definition.
    # Avoid division-by-zero NaN propagation.
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Preserve NaN for warm-up period (where avg_gain itself is NaN)
    rsi = np.where(np.isnan(avg_gain), np.nan, rsi)
    df["rsi_14"] = pd.Series(rsi, index=df.index).round(4)

    # ── ATR 14 (Wilder's) ────────────────────────────────────────────────────
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=_MIN_ROWS_ATR).mean()
    df["atr_14"] = atr.round(4)
    df["atr_14_pct"] = (atr / close).round(6)

    # ── Volume MA 20 (SMA) ────────────────────────────────────────────────────
    df["volume_ma_20"] = volume.rolling(window=_MIN_ROWS_VOL, min_periods=_MIN_ROWS_VOL).mean()

    # ── Volume percentile (adaptive volume filter) ───────────────────────────
    # Rank current volume vs last 50 days. Percentile 60+ = top 40% volume day
    def _vol_percentile(window):
        if len(window) < 50:
            return np.nan
        rank = (window.values < window.values[-1]).sum()
        return round(100.0 * rank / len(window), 1)

    df["volume_percentile"] = volume.rolling(window=50, min_periods=50).apply(
        _vol_percentile, raw=False
    )

    # ── 20-day high (breakout level) ──────────────────────────────────────────
    # Highest high over the past 20 sessions (NOT including today)
    df["high_20d"] = high.shift(1).rolling(window=_MIN_ROWS_BREAKOUT, min_periods=_MIN_ROWS_BREAKOUT).max()

    return df


def is_bullish_regime(spy_df: pd.DataFrame) -> pd.Series:
    """
    Compute the market regime filter for SPY: close > EMA200.

    Args:
        spy_df: DataFrame for SPY with at least [date, close] columns.

    Returns:
        Boolean Series indexed by date: True = bullish regime.
    """
    if spy_df.empty:
        return pd.Series(dtype=bool)

    spy_df = spy_df.copy().sort_values("date").reset_index(drop=True)
    close = spy_df["close"].astype(float)
    ema200 = close.ewm(span=200, adjust=False, min_periods=_MIN_ROWS_EMA200).mean()
    regime = close > ema200
    regime.index = spy_df["date"]
    return regime


def validate_signal_conditions(row: pd.Series) -> tuple[bool, str]:
    """
    Check all RFTM ENTER signal conditions for a single row.

    Returns:
        (is_valid, rejection_reason)  — rejection_reason is "" if valid.

    Conditions (from blueprint):
      1. close > EMA50 > EMA200           (trend alignment)
      2. EMA50 > EMA200                   (golden cross region)
      3. 50 <= RSI <= 70                  (momentum, not overbought)
      4. close >= high_20d                (20-day breakout)
      5. volume >= volume_ma_20 * 1.2     (volume confirmation)
      6. atr_14_pct between 0.01 and 0.05 (volatility filter)
    """
    def _check(condition: bool, reason: str) -> tuple[bool, str]:
        return (True, "") if condition else (False, reason)

    checks = [
        (
            not pd.isna(row.get("ema_50")) and not pd.isna(row.get("ema_200")),
            "indicators_not_ready",
        ),
        (
            float(row["close"]) > float(row["ema_50"]) > float(row["ema_200"]),
            "close_not_above_emas",
        ),
        (
            50.0 <= float(row["rsi_14"]) <= 70.0,
            f"rsi_out_of_range: {row.get('rsi_14', 'nan')}",
        ),
        (
            not pd.isna(row.get("high_20d")) and float(row["close"]) >= float(row["high_20d"]),
            "no_20d_breakout",
        ),
        (
            not pd.isna(row.get("volume_ma_20"))
            and float(row["volume"]) >= float(row["volume_ma_20"]) * 1.2,
            "volume_below_threshold",
        ),
        (
            not pd.isna(row.get("atr_14_pct"))
            and 0.01 <= float(row["atr_14_pct"]) <= 0.05,
            f"atr_pct_out_of_range: {row.get('atr_14_pct', 'nan')}",
        ),
    ]

    for condition, reason in checks:
        ok, msg = _check(condition, reason)
        if not ok:
            return False, msg

    return True, ""
