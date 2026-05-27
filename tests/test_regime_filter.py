"""
Tests for _regime_filter (capa C7 — V2-C).
"""
from __future__ import annotations

import os
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from _regime_filter import compute_adx, is_regime_favorable


def _make_bars(closes, n=60):
    """Generate synthetic OHLCV bars from a list of closes."""
    if len(closes) < n:
        # Pad with flat bars at the start
        closes = [closes[0]] * (n - len(closes)) + list(closes)
    df = pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=len(closes), freq="4h"),
        "open": closes,
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "close": closes,
        "volume": [1000.0] * len(closes),
    })
    return df


def _trending_bars(start=100.0, n=60, step=1.5):
    """Strong uptrend — high ADX expected."""
    closes = [start + i * step for i in range(n)]
    df = pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=n, freq="4h"),
        "open": [c - step * 0.3 for c in closes],
        "high": [c + step * 0.5 for c in closes],
        "low": [c - step * 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })
    return df


def _ranging_bars(center=100.0, n=60, amplitude=1.0):
    """Sideways market — low ADX expected."""
    closes = [center + amplitude * np.sin(i * 0.5) for i in range(n)]
    df = pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=n, freq="4h"),
        "open": closes,
        "high": [c + amplitude * 0.3 for c in closes],
        "low": [c - amplitude * 0.3 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })
    return df


# ── Feature flag ──────────────────────────────────────────────────────────

def test_regime_disabled_always_ok():
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "false"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_trending_bars(),
            btc_bars=_make_bars([50.0] * 60),  # would normally block
        )
        assert ok is True
        assert reason == "disabled"


# ── BTC macro ─────────────────────────────────────────────────────────────

def test_btc_drawdown_7pct_blocks():
    """BTC at 90 vs SMA50 ~100 → 10% below → should block (threshold 7%)."""
    btc_bars = _make_bars([100.0] * 50 + [90.0] * 10)
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_BTC_DRAWDOWN_PCT": "0.07"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=btc_bars,
        )
        assert ok is False
        assert "btc_drawdown" in reason


def test_btc_drawdown_4pct_allows():
    """BTC at 96 vs SMA50 ~100 → 4% below → within 7% threshold → allow."""
    btc_bars = _make_bars([100.0] * 50 + [96.0] * 10)
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_BTC_DRAWDOWN_PCT": "0.07"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=btc_bars,
        )
        assert ok is True
        assert reason == "ok"


def test_btc_euphoria_blocks_when_enabled():
    """BTC at 115 vs SMA50 ~100 → 15% above → euphoria blocks."""
    btc_bars = _make_bars([100.0] * 50 + [115.0] * 10)
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_BTC_EUPHORIA_PCT": "0.10"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=btc_bars,
        )
        assert ok is False
        assert "btc_euphoria" in reason


def test_btc_euphoria_disabled_does_not_block():
    """Euphoria threshold 0 → disabled → allow even at +15%."""
    btc_bars = _make_bars([100.0] * 50 + [115.0] * 10)
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_BTC_EUPHORIA_PCT": "0"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=btc_bars,
        )
        assert ok is True


# ── ADX ───────────────────────────────────────────────────────────────────

def test_adx_above_30_blocks():
    """Strong trend → ADX well above 30 → block."""
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_ADX_MAX": "30"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_trending_bars(step=3.0),
            btc_bars=_make_bars([100.0] * 60),
        )
        assert ok is False
        assert "adx_trending" in reason


def test_adx_below_20_allows():
    """Ranging market → ADX low → allow."""
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_ADX_MAX": "30"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=_make_bars([100.0] * 60),
        )
        assert ok is True
        assert reason == "ok"


def test_adx_between_20_25_currently_allows():
    """Zone grise — currently allows (no size reduction implemented yet)."""
    # Use mild trend that produces ADX ~22-28
    mild = _trending_bars(step=0.5)
    adx = compute_adx(mild)
    # Whatever ADX we get, if it's under 30 it should allow
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true",
                                       "MREV_REGIME_ADX_MAX": "30"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=mild,
            btc_bars=_make_bars([100.0] * 60),
        )
        if adx <= 30:
            assert ok is True
        else:
            assert ok is False


# ── compute_adx ───────────────────────────────────────────────────────────

def test_compute_adx_trending_is_high():
    """Strong trend should produce ADX > 25."""
    adx = compute_adx(_trending_bars(step=3.0))
    assert adx > 25, f"Expected ADX > 25 for strong trend, got {adx:.1f}"


def test_compute_adx_ranging_is_low():
    """Ranging market should produce ADX < 25."""
    adx = compute_adx(_ranging_bars())
    assert adx < 25, f"Expected ADX < 25 for ranging market, got {adx:.1f}"


# ── Edge cases ────────────────────────────────────────────────────────────

def test_missing_btc_bars_does_not_block():
    """If BTC bars are None, skip BTC check → only ADX matters."""
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=None,
        )
        assert ok is True
        assert reason == "ok"


def test_short_btc_bars_skips_btc_check():
    """If BTC has < 50 bars, skip BTC check gracefully."""
    short_btc = _make_bars([50.0] * 30, n=30)
    with mock.patch.dict(os.environ, {"MREV_REGIME_FILTER_ENABLED": "true"}):
        ok, reason = is_regime_favorable(
            symbol="ETH/USD",
            symbol_bars=_ranging_bars(),
            btc_bars=short_btc,
        )
        assert ok is True
