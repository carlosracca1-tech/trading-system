"""
Tests for _exit_logic.evaluate_final_tp — hard final take-profit.

Diseño:
- Dispara con current_price >= entry × (1 + final_tp_pct).
- Vende TODO (sell_qty == current_qty), independientemente del stage.
- Si final_tp_pct <= 0 → desactivado.
- Si notional (qty × current_price) < min_notional → no firea.
"""
from __future__ import annotations

import pytest

from _exit_logic import ExitAction, evaluate_final_tp


FINAL_TP = 0.10        # 10%
MIN_NOTIONAL = 10.0


# ── Fires correctly ──────────────────────────────────────────────────────────

def test_fires_at_exact_threshold():
    """current_price exactamente al +10% → firea."""
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=110.0,
        current_qty=10,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == 10
    assert "final_tp" in action.reason


def test_fires_above_threshold():
    """+17.7% (caso QQQ actual) → firea, vende todo."""
    action = evaluate_final_tp(
        entry_price=605.0,
        current_price=712.0,
        current_qty=3,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == 3
    assert "17" in action.reason  # unrealized_pct cerca de 17.7%


def test_does_not_fire_below_threshold():
    """+9.32% (caso SPY actual) → NO firea."""
    action = evaluate_final_tp(
        entry_price=674.71,
        current_price=737.58,
        current_qty=1,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_does_not_fire_at_breakeven():
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=100.0,
        current_qty=10,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_does_not_fire_at_loss():
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=95.0,
        current_qty=10,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


# ── Stage-independence ───────────────────────────────────────────────────────
# evaluate_final_tp NO recibe stage: el watchdog lo invoca antes que partial_tp,
# así que firea sin importar si stage es 0, 1 o 2. Estos tests dejan claro
# que el sell_qty siempre es "todo el current_qty", no importa de qué stage venga.

def test_full_position_stage_0():
    """Stage 0, qty original = 100, salta de 0 a +12%: vende los 100."""
    action = evaluate_final_tp(
        entry_price=50.0,
        current_price=56.0,
        current_qty=100,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == 100


def test_remainder_after_tp1_stage_1():
    """Stage 1 (ya hubo TP1): current_qty = 50 (de 100 originales).
    A +11% vende los 50 que quedan."""
    action = evaluate_final_tp(
        entry_price=50.0,
        current_price=55.5,
        current_qty=50,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == 50


def test_runner_after_tp2_stage_2():
    """Stage 2 (TP1+TP2 hechos): current_qty = 25.
    A +13% vende los 25 que quedaban como runner."""
    action = evaluate_final_tp(
        entry_price=50.0,
        current_price=56.5,
        current_qty=25,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == 25


# ── Guardas ──────────────────────────────────────────────────────────────────

def test_disabled_when_pct_zero():
    """FINAL_TP_PCT=0 → desactivado."""
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=200.0,
        current_qty=10,
        final_tp_pct=0.0,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_disabled_when_pct_negative():
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=200.0,
        current_qty=10,
        final_tp_pct=-0.05,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_skips_when_notional_below_min():
    """qty × price = 5 < 10 (min_notional) → no firea (orden muy chica)."""
    action = evaluate_final_tp(
        entry_price=1.0,
        current_price=1.2,        # +20%, supera el umbral
        current_qty=4,            # notional 4.8
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_invalid_entry_price():
    action = evaluate_final_tp(
        entry_price=0.0,
        current_price=110.0,
        current_qty=10,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_invalid_qty():
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=110.0,
        current_qty=0,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


def test_invalid_price():
    action = evaluate_final_tp(
        entry_price=100.0,
        current_price=0.0,
        current_qty=10,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is None


# ── Custom thresholds ────────────────────────────────────────────────────────

def test_custom_threshold_15pct():
    """Operador que prefiere 15% en vez del default."""
    # +12% no alcanza
    a1 = evaluate_final_tp(
        entry_price=100.0, current_price=112.0, current_qty=10,
        final_tp_pct=0.15, min_notional=MIN_NOTIONAL,
    )
    assert a1 is None
    # +15.5% sí
    a2 = evaluate_final_tp(
        entry_price=100.0, current_price=115.5, current_qty=10,
        final_tp_pct=0.15, min_notional=MIN_NOTIONAL,
    )
    assert a2 is not None


def test_crypto_fractional_qty():
    """Cripto con qty fraccional — sell_qty == current_qty (no se redondea)."""
    action = evaluate_final_tp(
        entry_price=80.0,
        current_price=90.0,       # +12.5%
        current_qty=31.708598,
        final_tp_pct=FINAL_TP,
        min_notional=MIN_NOTIONAL,
    )
    assert action is not None
    assert action.sell_qty == pytest.approx(31.708598)
