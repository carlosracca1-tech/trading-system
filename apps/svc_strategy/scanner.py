"""
apps/svc_strategy/scanner.py
RFTM Strategy Scanner — pure computation layer.

Takes pre-fetched market rows and returns signal decisions.
No DB dependencies — inject data, get signals back.

Strategy: Regime-Filtered Trend Momentum (RFTM)
============================================================
Entry conditions (ALL must hold):
  0. Regime filter: SPY close > SPY EMA200
  1. close > EMA50 > EMA200       (trend alignment)
  2. 50 <= RSI14 <= 70            (momentum zone)
  3. close >= high_20d            (20-day breakout)
  4. volume >= volume_ma_20 * 1.2 (volume confirmation)
  5. 0.01 <= atr_14_pct <= 0.05  (volatility filter)

Exit conditions (any one triggers EXIT, checked in priority order):
  E1. EMA50 < EMA200  (death cross)
  E2. close < EMA50   (trend broken)
  E3. close <= entry_price - ATR_STOP_MULTIPLIER * atr_14  (stop loss)
  E4. RSI > 80        (overbought / take profit)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from apps.svc_data.indicators import validate_signal_conditions
from packages.shared.enums import Direction, RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────────
STRATEGY_PARAMS = {
    "RSI_EXIT_OVERBOUGHT": 80.0,   # Exit when RSI exceeds this (take-profit)
    "ATR_STOP_MULTIPLIER": 2.0,    # Stop loss = entry_price - N * ATR14
    "VOLUME_CONFIRM_MULT": 1.2,    # Volume must be >= this × volume_ma_20
}


# ── Signal decision dataclass ─────────────────────────────────────────────────

@dataclass
class SignalDecision:
    """Result of scanning one symbol on one date.

    signal_type: SignalType enum value (ENTER / EXIT / HOLD)
    reason:      why an entry was rejected or what triggered an exit.
                 Empty string means "no issue / valid signal".
    """
    symbol: str
    signal_date: date
    signal_type: str          # SignalType.value
    close_price: float
    risk_decision: str = field(default_factory=lambda: RiskDecision.PENDING.value)
    reason: str = ""
    # Indicator snapshot at decision time
    atr_14: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    rsi_14: Optional[float] = None
    volume_ratio: Optional[float] = None   # volume / volume_ma_20
    regime_ok: bool = False


# ── Core scanning functions ───────────────────────────────────────────────────

def check_entry_signal(
    symbol: str,
    row: "pd.Series | dict",
    signal_date: date,
    regime_bullish: bool,
) -> SignalDecision:
    """
    Evaluate RFTM entry conditions for one symbol row.

    Args:
        symbol:          ticker symbol
        row:             dict/Series with keys: close, ema_50, ema_200, rsi_14,
                         atr_14, atr_14_pct, volume, volume_ma_20, high_20d
        signal_date:     date of the signal
        regime_bullish:  True = SPY close > SPY EMA200

    Returns:
        SignalDecision with type ENTER (all conditions met)
        or HOLD (any condition failed, reason populated).
    """
    close = _float(row.get("close")) or 0.0
    atr_14 = _float(row.get("atr_14"))
    ema_50 = _float(row.get("ema_50"))
    ema_200 = _float(row.get("ema_200"))
    rsi_14 = _float(row.get("rsi_14"))
    volume = _float(row.get("volume"))
    volume_ma_20 = _float(row.get("volume_ma_20"))
    vol_ratio = (
        round(volume / volume_ma_20, 4)
        if volume is not None and volume_ma_20 is not None and volume_ma_20 > 0
        else None
    )

    def _hold(reason: str) -> SignalDecision:
        return SignalDecision(
            symbol=symbol,
            signal_date=signal_date,
            signal_type=SignalType.HOLD.value,
            close_price=close,
            reason=reason,
            atr_14=atr_14,
            ema_50=ema_50,
            ema_200=ema_200,
            rsi_14=rsi_14,
            volume_ratio=vol_ratio,
            regime_ok=regime_bullish,
        )

    # Regime gate: no entries in bear market
    if not regime_bullish:
        return _hold("bearish_regime")

    # Delegate to the shared 6-condition validator
    if isinstance(row, dict):
        row_series = pd.Series(row)
    else:
        row_series = row

    ok, reject_reason = validate_signal_conditions(row_series)
    if not ok:
        return _hold(reject_reason)

    return SignalDecision(
        symbol=symbol,
        signal_date=signal_date,
        signal_type=SignalType.ENTER.value,
        close_price=close,
        atr_14=atr_14,
        ema_50=ema_50,
        ema_200=ema_200,
        rsi_14=rsi_14,
        volume_ratio=vol_ratio,
        regime_ok=regime_bullish,
    )


def check_exit_signal(
    symbol: str,
    row: "pd.Series | dict",
    signal_date: date,
    entry_price: float,
) -> SignalDecision:
    """
    Evaluate RFTM exit conditions for an open position.

    Args:
        symbol:       ticker symbol
        row:          dict/Series with keys: close, ema_50, ema_200, rsi_14, atr_14
        signal_date:  date of the signal
        entry_price:  price at which the position was entered (for stop calc)

    Returns:
        SignalDecision with type EXIT (exit now) or HOLD (keep holding).

    Exit priority: E1 (death cross) > E2 (close<EMA50) > E3 (stop) > E4 (RSI)
    """
    close = _float(row.get("close")) or 0.0
    ema_50 = _float(row.get("ema_50"))
    ema_200 = _float(row.get("ema_200"))
    rsi_14 = _float(row.get("rsi_14"))
    atr_14 = _float(row.get("atr_14"))

    def _exit(reason: str) -> SignalDecision:
        return SignalDecision(
            symbol=symbol,
            signal_date=signal_date,
            signal_type=SignalType.EXIT.value,
            close_price=close,
            reason=reason,
            atr_14=atr_14,
            ema_50=ema_50,
            ema_200=ema_200,
            rsi_14=rsi_14,
        )

    # E1: Death cross — EMA50 crosses below EMA200
    if ema_50 is not None and ema_200 is not None and ema_50 < ema_200:
        return _exit("death_cross")

    # E2: Close breaks below EMA50 (trend broken)
    if ema_50 is not None and close < ema_50:
        return _exit("close_below_ema50")

    # E3: Stop loss hit — close <= entry_price - 2 * ATR14
    if atr_14 is not None and atr_14 > 0:
        stop_price = entry_price - STRATEGY_PARAMS["ATR_STOP_MULTIPLIER"] * atr_14
        if close <= stop_price:
            return _exit(f"stop_loss_hit:{stop_price:.4f}")

    # E4: RSI overbought — take profit
    if rsi_14 is not None and rsi_14 > STRATEGY_PARAMS["RSI_EXIT_OVERBOUGHT"]:
        return _exit(f"rsi_overbought:{rsi_14:.2f}")

    # No exit condition triggered — hold the position
    return SignalDecision(
        symbol=symbol,
        signal_date=signal_date,
        signal_type=SignalType.HOLD.value,
        close_price=close,
        atr_14=atr_14,
        ema_50=ema_50,
        ema_200=ema_200,
        rsi_14=rsi_14,
    )


def is_regime_bullish(spy_row: "pd.Series | dict | None") -> bool:
    """
    Market regime filter: True if SPY close > SPY EMA200.
    Returns False (conservative / bearish) whenever data is missing or NaN.
    """
    if spy_row is None:
        return False
    close = _float(spy_row.get("close"))
    ema_200 = _float(spy_row.get("ema_200"))
    if close is None or ema_200 is None:
        return False
    return close > ema_200


# ── Helpers ───────────────────────────────────────────────────────────────────

def _float(val) -> Optional[float]:
    """Safe float conversion; returns None on NaN / None / non-numeric input."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f  # NaN self-inequality check
    except (TypeError, ValueError):
        return None
