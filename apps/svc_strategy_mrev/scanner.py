"""
apps/svc_strategy_mrev/scanner.py
MREV-1H Strategy Scanner — pure computation layer.

Strategy: Mean Reversion on 1-Hour candles
============================================================
Entry conditions (ALL must hold — LONG only in V1):
  1. RSI(14) ≤ 30                    (oversold)
  2. close ≤ lower Bollinger Band    (price stretched below mean)
  3. volume ≥ volume_ma_20 × 1.0     (at least average volume)
  4. 0.003 ≤ atr_14_pct ≤ 0.10      (volatility filter)

Exit conditions (any one triggers EXIT, checked in priority order):
  X1. Take profit — close ≥ SMA(20)               (mean reversion target)
  X2. Stop loss   — close ≤ entry - 1.5 × ATR(14) (tighter stop)
  X3. RSI normalized — 40 ≤ RSI ≤ 60              (momentum exhausted)
  X4. Time stop   — position held > 24 bars        (≈1 day for hourly)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from apps.svc_data_1h.indicators import validate_mrev_entry_conditions
from apps.svc_strategy_mrev.constants import MREV_STRATEGY_PARAMS
from packages.shared.enums import Direction, RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Signal decision dataclass ────────────────────────────────────────────────

@dataclass
class MrevSignalDecision:
    """Result of scanning one symbol on one hourly bar.

    signal_type: SignalType enum value (ENTER / EXIT / HOLD)
    reason:      why an entry was rejected or what triggered an exit.
    """
    symbol: str
    signal_datetime: datetime
    signal_type: str          # SignalType.value
    close_price: float
    risk_decision: str = field(default_factory=lambda: RiskDecision.PENDING.value)
    reason: str = ""
    # Indicator snapshot at decision time
    atr_14: Optional[float] = None
    rsi_14: Optional[float] = None
    sma_20: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    volume_ratio: Optional[float] = None   # volume / volume_ma_20


# ── Core scanning functions ──────────────────────────────────────────────────

def check_mrev_entry_signal(
    symbol: str,
    row: "pd.Series | dict",
    signal_datetime: datetime,
) -> MrevSignalDecision:
    """
    Evaluate MREV-1H entry conditions for one symbol row.

    Args:
        symbol:           ticker symbol
        row:              dict/Series with keys: close, sma_20, bb_lower, bb_upper,
                          rsi_14, atr_14, atr_14_pct, volume, volume_ma_20
        signal_datetime:  datetime of the candle being evaluated

    Returns:
        MrevSignalDecision with type ENTER (all conditions met)
        or HOLD (any condition failed, reason populated).
    """
    close = _float(row.get("close")) or 0.0
    atr_14 = _float(row.get("atr_14"))
    rsi_14 = _float(row.get("rsi_14"))
    sma_20 = _float(row.get("sma_20"))
    bb_upper = _float(row.get("bb_upper"))
    bb_lower = _float(row.get("bb_lower"))
    volume = _float(row.get("volume"))
    volume_ma_20 = _float(row.get("volume_ma_20"))
    vol_ratio = (
        round(volume / volume_ma_20, 4)
        if volume is not None and volume_ma_20 is not None and volume_ma_20 > 0
        else None
    )

    def _hold(reason: str) -> MrevSignalDecision:
        return MrevSignalDecision(
            symbol=symbol,
            signal_datetime=signal_datetime,
            signal_type=SignalType.HOLD.value,
            close_price=close,
            reason=reason,
            atr_14=atr_14,
            rsi_14=rsi_14,
            sma_20=sma_20,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            volume_ratio=vol_ratio,
        )

    # Delegate to the condition validator
    if isinstance(row, dict):
        row_series = pd.Series(row)
    else:
        row_series = row

    ok, reject_reason = validate_mrev_entry_conditions(row_series)
    if not ok:
        return _hold(reject_reason)

    return MrevSignalDecision(
        symbol=symbol,
        signal_datetime=signal_datetime,
        signal_type=SignalType.ENTER.value,
        close_price=close,
        atr_14=atr_14,
        rsi_14=rsi_14,
        sma_20=sma_20,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        volume_ratio=vol_ratio,
    )


def check_mrev_exit_signal(
    symbol: str,
    row: "pd.Series | dict",
    signal_datetime: datetime,
    entry_price: float,
    entry_datetime: datetime,
) -> MrevSignalDecision:
    """
    Evaluate MREV-1H exit conditions for an open position.

    Args:
        symbol:           ticker symbol
        row:              dict/Series with keys: close, sma_20, rsi_14, atr_14
        signal_datetime:  datetime of the candle being evaluated
        entry_price:      price at which the position was entered (for stop calc)
        entry_datetime:   when the position was opened (for time stop calc)

    Returns:
        MrevSignalDecision with type EXIT (exit now) or HOLD (keep holding).

    Exit priority: X1 (take profit) > X2 (stop loss) > X3 (RSI normalized) > X4 (time stop)
    """
    close = _float(row.get("close")) or 0.0
    sma_20 = _float(row.get("sma_20"))
    rsi_14 = _float(row.get("rsi_14"))
    atr_14 = _float(row.get("atr_14"))
    bb_upper = _float(row.get("bb_upper"))
    bb_lower = _float(row.get("bb_lower"))

    params = MREV_STRATEGY_PARAMS

    def _exit(reason: str) -> MrevSignalDecision:
        return MrevSignalDecision(
            symbol=symbol,
            signal_datetime=signal_datetime,
            signal_type=SignalType.EXIT.value,
            close_price=close,
            reason=reason,
            atr_14=atr_14,
            rsi_14=rsi_14,
            sma_20=sma_20,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
        )

    # X1: Take profit — price reverted back to the mean (SMA 20)
    if sma_20 is not None and close >= sma_20:
        return _exit(f"take_profit_mean_reversion:{close:.4f}>={sma_20:.4f}")

    # X2: Stop loss — close ≤ entry_price - 1.5 × ATR14
    if atr_14 is not None and atr_14 > 0:
        stop_price = entry_price - params["stop_atr_multiplier"] * atr_14
        if close <= stop_price:
            return _exit(f"stop_loss_hit:{stop_price:.4f}")

    # X3: RSI normalized — momentum exhausted, no more reversion expected
    if rsi_14 is not None:
        if params["rsi_exit_normalized_min"] <= rsi_14 <= params["rsi_exit_normalized_max"]:
            return _exit(f"rsi_normalized:{rsi_14:.2f}")

    # X4: Time stop — held too long (24 hourly bars ≈ 1 day)
    if entry_datetime is not None:
        bars_held = int((signal_datetime - entry_datetime).total_seconds() / 3600)
        if bars_held >= params["max_hold_bars"]:
            return _exit(f"time_stop:{bars_held}_bars")

    # No exit condition triggered — hold
    return MrevSignalDecision(
        symbol=symbol,
        signal_datetime=signal_datetime,
        signal_type=SignalType.HOLD.value,
        close_price=close,
        atr_14=atr_14,
        rsi_14=rsi_14,
        sma_20=sma_20,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _float(val) -> Optional[float]:
    """Safe float conversion; returns None on NaN / None / non-numeric input."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f  # NaN self-inequality check
    except (TypeError, ValueError):
        return None
