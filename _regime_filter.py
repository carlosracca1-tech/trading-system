"""
_regime_filter — filtro de régimen para MREV (capa C7, V2-C).

Mean reversion solo tiene edge en mercados laterales. Este módulo evalúa
dos cosas antes de permitir un entry:

  1. BTC macro: si BTC 4H está más de X% debajo de su SMA50, todo
     fadeo es riesgoso (mercado en colapso, mean reversion no funciona).
  2. ADX símbolo: si el símbolo a entrar está en tendencia fuerte
     (ADX > umbral), la mean reversion va contra el flujo.

Feature flag: MREV_REGIME_FILTER_ENABLED (default 'true'). Permite
desactivar en emergencia sin redeploy.

Configurable via env:
  MREV_REGIME_BTC_DRAWDOWN_PCT   default 0.07  (7%)
  MREV_REGIME_BTC_EUPHORIA_PCT   default 0.10  (10%, 0 = disabled)
  MREV_REGIME_ADX_MAX             default 30
  MREV_REGIME_ADX_PERIOD          default 14
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np
import pandas as pd


def is_regime_filter_enabled() -> bool:
    return os.environ.get("MREV_REGIME_FILTER_ENABLED", "true").lower() in (
        "1", "true", "yes",
    )


def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """ADX clásico de Wilder. Devuelve el último valor."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    if adx.empty or pd.isna(adx.iloc[-1]):
        return float("nan")
    return float(adx.iloc[-1])


def is_regime_favorable(
    *,
    symbol: str,
    symbol_bars: pd.DataFrame,
    btc_bars: Optional[pd.DataFrame],
) -> tuple[bool, str]:
    """Devuelve (True, "ok") o (False, reason).

    reason es un string corto para logging y para el JSONL de KAIZEN.
    """
    if not is_regime_filter_enabled():
        return True, "disabled"

    # Check 1: BTC macro
    if btc_bars is not None and len(btc_bars) >= 50:
        btc_close = float(btc_bars["close"].iloc[-1])
        btc_sma50 = float(btc_bars["close"].rolling(50).mean().iloc[-1])

        drawdown_threshold = float(os.environ.get("MREV_REGIME_BTC_DRAWDOWN_PCT", "0.07"))
        if btc_close < btc_sma50 * (1.0 - drawdown_threshold):
            return False, f"btc_drawdown ({btc_close:.0f} < {btc_sma50 * (1.0 - drawdown_threshold):.0f})"

        euphoria_threshold = float(os.environ.get("MREV_REGIME_BTC_EUPHORIA_PCT", "0.10"))
        if euphoria_threshold > 0 and btc_close > btc_sma50 * (1.0 + euphoria_threshold):
            return False, f"btc_euphoria ({btc_close:.0f} > {btc_sma50 * (1.0 + euphoria_threshold):.0f})"

    # Check 2: ADX del símbolo
    adx_period = int(os.environ.get("MREV_REGIME_ADX_PERIOD", "14"))
    adx_max = float(os.environ.get("MREV_REGIME_ADX_MAX", "30"))

    if len(symbol_bars) >= adx_period * 2:
        adx_val = compute_adx(symbol_bars, period=adx_period)
        if not pd.isna(adx_val) and adx_val > adx_max:
            return False, f"adx_trending ({adx_val:.1f} > {adx_max})"

    return True, "ok"
