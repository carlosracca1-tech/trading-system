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

# ── Strategy parameters (AGGRESSIVE 8/10 RISK) ──────────────────────────────
STRATEGY_PARAMS = {
    # EXIT params — trailing stop replaces old E1/E2/E4
    "ATR_STOP_MULTIPLIER": 1.5,          # Stop loss = entry - 1.5×ATR (tighter)
    "TRAILING_ACTIVATION_ATR": 0.5,      # Activate trail at 0.5×ATR profit
    "TRAILING_AGGRESSIVE_ATR": 1.5,      # Phase 3 trail at 1.5×ATR profit
    "TRAILING_DISTANCE_ATR": 1.0,        # Trail distance: 1×ATR from high
    "TIME_STOP_BARS_NO_NEW_HIGH": 20,    # Exit if 20 bars without new high

    # ENTRY params — relaxed for more signals
    "RSI_ENTRY_MIN": 35.0,              # Was 50.0 — RSI 35+ is positive momentum
    "RSI_ENTRY_MAX": 75.0,              # Was 70.0 — allow stronger momentum
    "ATR_PCT_MIN": 0.003,               # Was 0.01 — include low-vol assets
    "ATR_PCT_MAX": 0.10,                # Was 0.05 — include crypto/high-vol
    "VOLUME_PERCENTILE_MIN": 60,        # Adaptive: top 40% volume days
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

    # ── AGGRESSIVE 8/10: 3 core conditions only (was 6 strict conditions) ────
    ema_21 = _float(row.get("ema_21"))
    atr_14_pct = _float(row.get("atr_14_pct"))
    volume_percentile = _float(row.get("volume_percentile"))

    # C1: Trend — close > EMA21 (was: close > EMA50 > EMA200)
    if ema_21 is not None and close <= ema_21:
        return _hold("no_trend_ema21")
    # Fallback: if EMA21 not computed yet, use EMA50
    if ema_21 is None and ema_50 is not None and close <= ema_50:
        return _hold("no_trend_ema50_fallback")

    # C2: Momentum — RSI 35-75 (was: 50-70)
    if rsi_14 is None:
        return _hold("rsi_not_ready")
    if not (STRATEGY_PARAMS["RSI_ENTRY_MIN"] <= rsi_14 <= STRATEGY_PARAMS["RSI_ENTRY_MAX"]):
        return _hold(f"rsi_out_of_range:{rsi_14:.1f}")

    # C3: Volatility — ATR% 0.3%-10% (was: 1%-5%)
    if atr_14_pct is not None:
        if not (STRATEGY_PARAMS["ATR_PCT_MIN"] <= atr_14_pct <= STRATEGY_PARAMS["ATR_PCT_MAX"]):
            return _hold(f"atr_pct_out_of_range:{atr_14_pct:.4f}")

    # Volume percentile is a BOOST, not a hard filter
    # Logged for analysis but does not block entry

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
    highest_since_entry: Optional[float] = None,
    bars_since_last_high: Optional[int] = None,
) -> SignalDecision:
    """
    Evaluate RFTM exit conditions for an open position.

    AGGRESSIVE 8/10 — trailing stop replaces old E1/E2/E4:
      - E1 (death cross) and E2 (close<EMA50) REMOVED as forced exits
      - E4 (RSI>80) REMOVED — trailing stop captures profits instead
      - NEW: 3-phase trailing stop (fixed → breakeven → trail)
      - NEW: time stop (20 bars without new high)

    Args:
        symbol:               ticker symbol
        row:                  dict/Series with keys: close, ema_50, ema_200, rsi_14, atr_14
        signal_date:          date of the signal
        entry_price:          price at which the position was entered
        highest_since_entry:  highest close since position opened (for trailing)
        bars_since_last_high: how many bars since the last new high was made

    Returns:
        SignalDecision with type EXIT (exit now) or HOLD (keep holding).

    Exit priority: E3 (stop/trailing) > E6 (time stop)
    """
    close = _float(row.get("close")) or 0.0
    ema_50 = _float(row.get("ema_50"))
    ema_200 = _float(row.get("ema_200"))
    rsi_14 = _float(row.get("rsi_14"))
    atr_14 = _float(row.get("atr_14"))

    params = STRATEGY_PARAMS
    high = highest_since_entry or close

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

    # ── E3: Stop loss / Trailing stop (3 phases) ────────────────────────────
    if atr_14 is not None and atr_14 > 0:
        unrealized_atr = (close - entry_price) / atr_14

        if unrealized_atr >= params["TRAILING_AGGRESSIVE_ATR"]:
            # Phase 3: aggressive trail from highest price
            trail_stop = high - params["TRAILING_DISTANCE_ATR"] * atr_14
            if close <= trail_stop:
                return _exit(f"trailing_stop_phase3:{trail_stop:.4f}")

        elif unrealized_atr >= params["TRAILING_ACTIVATION_ATR"]:
            # Phase 2: breakeven stop — can't lose money anymore
            if close <= entry_price:
                return _exit(f"breakeven_stop:{entry_price:.4f}")

        else:
            # Phase 1: fixed stop loss
            stop_price = entry_price - params["ATR_STOP_MULTIPLIER"] * atr_14
            if close <= stop_price:
                return _exit(f"stop_loss_hit:{stop_price:.4f}")

    # ── E6: Time stop — 20 bars without making a new high ────────────────────
    if bars_since_last_high is not None:
        if bars_since_last_high >= params["TIME_STOP_BARS_NO_NEW_HIGH"]:
            return _exit(f"time_stop:{bars_since_last_high}_bars_no_new_high")

    # ── E1/E2 REMOVED: death cross and close<EMA50 no longer force exit ──────
    # They are now logged as warnings only (in the pipeline), not exit triggers.
    # The trailing stop and bracket orders handle risk management instead.

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
    Market regime filter: True if SPY close > SPY EMA100.

    AGGRESSIVE 8/10: Changed from EMA200 to EMA100 for faster regime
    detection. EMA200 was too slow — kept us out of the market during
    recoveries for weeks after the trend had already turned bullish.

    Falls back to EMA200 if EMA100 is not yet computed.
    Returns False (conservative / bearish) whenever data is missing or NaN.
    """
    if spy_row is None:
        return False
    close = _float(spy_row.get("close"))
    # Prefer EMA100 (faster), fallback to EMA200
    ema = _float(spy_row.get("ema_100"))
    if ema is None:
        ema = _float(spy_row.get("ema_200"))
    if close is None or ema is None:
        return False
    return close > ema


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
