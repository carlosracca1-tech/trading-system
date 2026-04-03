"""
apps/svc_data_1h/indicators.py
Technical indicator computation for 1H Mean Reversion strategy.

Indicators computed (all on 1-hour candles):
  - Bollinger Bands: SMA(20), upper/lower bands at ±2σ
  - RSI(14): Wilder's smoothing (same as RFTM)
  - ATR(14): Wilder's smoothing (same as RFTM)
  - Volume MA(20): simple moving average

Input:  DataFrame with columns [datetime, open, high, low, close, volume]
        sorted ascending by datetime.
Output: Same DataFrame with added indicator columns.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Minimum rows required before indicators are trusted
_MIN_ROWS_BB = 20
_MIN_ROWS_RSI = 14
_MIN_ROWS_ATR = 14
_MIN_ROWS_VOL = 20


def compute_mrev_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all MREV-1H strategy indicators for a single symbol.

    Args:
        df: DataFrame with columns [datetime, open, high, low, close, volume],
            sorted ascending by datetime.

    Returns:
        df with added columns:
          sma_20, bb_upper, bb_lower, rsi_14, atr_14, atr_14_pct, volume_ma_20

    Notes:
      - NaN is returned for rows where there is insufficient history.
      - The input DataFrame is NOT modified in-place; a copy is returned.
    """
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"compute_mrev_indicators: missing columns {missing}")

    if df.empty:
        return df.copy()

    df = df.copy().sort_values("datetime").reset_index(drop=True)

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # ── Bollinger Bands (SMA 20, ±2σ) ────────────────────────────────────────
    sma_20 = close.rolling(window=_MIN_ROWS_BB, min_periods=_MIN_ROWS_BB).mean()
    std_20 = close.rolling(window=_MIN_ROWS_BB, min_periods=_MIN_ROWS_BB).std()

    df["sma_20"] = sma_20.round(6)
    df["bb_upper"] = (sma_20 + 2.0 * std_20).round(6)
    df["bb_lower"] = (sma_20 - 2.0 * std_20).round(6)

    # Bollinger Band width (normalized) — useful for filtering low-vol regimes
    df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["sma_20"]).round(6)

    # ── RSI 14 (Wilder's smoothing — same as RFTM) ──────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=_MIN_ROWS_RSI).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=_MIN_ROWS_RSI).mean()

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = np.where(np.isnan(avg_gain), np.nan, rsi)
    df["rsi_14"] = pd.Series(rsi, index=df.index).round(4)

    # ── ATR 14 (Wilder's smoothing) ──────────────────────────────────────────
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
    df["atr_14"] = atr.round(6)
    df["atr_14_pct"] = (atr / close).round(6)

    # ── Volume MA 20 (SMA) ───────────────────────────────────────────────────
    df["volume_ma_20"] = volume.rolling(
        window=_MIN_ROWS_VOL, min_periods=_MIN_ROWS_VOL
    ).mean()

    return df


def validate_mrev_entry_conditions(row: pd.Series) -> tuple[bool, str]:
    """
    Check all MREV-1H ENTER signal conditions for a single row.

    Returns:
        (is_valid, rejection_reason) — rejection_reason is "" if valid.

    Conditions:
      1. RSI(14) ≤ 30                          (oversold)
      2. close ≤ lower Bollinger Band           (price at/below lower band)
      3. volume ≥ volume_ma_20 × 1.0            (at least average volume)
      4. 0.003 ≤ atr_14_pct ≤ 0.10             (volatility filter)
      5. Indicators must be ready (no NaN)
    """
    # Check indicators are ready
    for col in ("sma_20", "bb_lower", "rsi_14", "atr_14_pct", "volume_ma_20"):
        val = row.get(col)
        if val is None or (isinstance(val, float) and val != val):  # NaN check
            return False, "indicators_not_ready"

    close = float(row["close"])
    rsi_14 = float(row["rsi_14"])
    bb_lower = float(row["bb_lower"])
    volume = float(row["volume"])
    volume_ma_20 = float(row["volume_ma_20"])
    atr_14_pct = float(row["atr_14_pct"])

    # C1: RSI oversold
    if rsi_14 > 30.0:
        return False, f"rsi_not_oversold:{rsi_14:.2f}"

    # C2: Price at or below lower Bollinger Band
    if close > bb_lower:
        return False, f"close_above_bb_lower:{close:.4f}>{bb_lower:.4f}"

    # C3: Volume at least average
    if volume_ma_20 > 0 and volume < volume_ma_20 * 1.0:
        return False, "volume_below_average"

    # C4: Volatility filter
    if not (0.003 <= atr_14_pct <= 0.10):
        return False, f"atr_pct_out_of_range:{atr_14_pct:.6f}"

    return True, ""
