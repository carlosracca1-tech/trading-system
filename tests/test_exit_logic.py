"""
Tests for _exit_logic.evaluate_partial_tp — refactor de Task 2.

Criterio clave: que las decisiones coincidan con la lógica inline de
standalone_*.py (+5% → 50%, +7.5% → 50% del remanente, breakeven stop
post-TP1, respeto de min_notional, idempotencia por stage).
"""
from __future__ import annotations

import math

import pytest

from _exit_logic import (
    PartialTPAction,
    evaluate_partial_tp,
    floor_int_qty,
    make_crypto_round_qty,
)


TP1 = 0.05
TP2 = 0.075
RATIO = 0.50
MIN_NOTIONAL = 10.0


# ─── TP1 ─────────────────────────────────────────────────────────────────────

def test_tp1_fires_on_stage0_above_threshold_etf():
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=105.0, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is not None
    assert a.stage == 1
    assert a.sell_qty == 5
    assert a.new_stop == 100.0  # breakeven
    assert a.reason.startswith("partial_tp1_")


def test_tp1_does_not_fire_below_threshold():
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=104.99, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is None


def test_tp1_idempotent_when_already_stage1():
    """Si stage==1 ya se ejecutó TP1: no debe re-dispararse aunque siga encima del 5%."""
    a = evaluate_partial_tp(
        stage=1, entry_price=100.0, current_price=106.0, current_qty=5,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    # 106 no supera el 7.5% entonces TP2 tampoco; None
    assert a is None


# ─── TP2 ─────────────────────────────────────────────────────────────────────

def test_tp2_fires_on_stage1_above_threshold():
    a = evaluate_partial_tp(
        stage=1, entry_price=100.0, current_price=108.0, current_qty=5,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is not None
    assert a.stage == 2
    assert a.sell_qty == 2  # floor(5*0.5)=2
    assert a.new_stop is None  # TP2 no modifica el stop
    assert a.reason.startswith("partial_tp2_")


def test_tp2_never_fires_from_stage0():
    """Aunque el precio esté por encima del 7.5%, stage==0 solo evalúa TP1."""
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=108.0, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    # TP1 firea — hay que pasar por TP1 antes que TP2
    assert a is not None and a.stage == 1


def test_no_fire_on_stage2():
    a = evaluate_partial_tp(
        stage=2, entry_price=100.0, current_price=200.0, current_qty=5,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is None


# ─── Guardas ─────────────────────────────────────────────────────────────────

def test_respects_min_notional():
    """Venta de 0.05 shares * $1 = $0.05 < $10 → skip."""
    a = evaluate_partial_tp(
        stage=0, entry_price=1.0, current_price=1.10, current_qty=0.1,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL,
        round_qty=lambda q: round(q, 4),
    )
    assert a is None


def test_skips_when_round_returns_zero():
    """Si round_qty redondea a 0 (posición de 1 share, floor(0.5)=0) no firea."""
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=105.0, current_qty=1,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is None


def test_skips_when_round_returns_full_qty():
    """No vender 100%: si el ratio redondeado == current_qty, no firea."""
    # ratio=1.0 fuerza sell_qty == current_qty → skip.
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=105.0, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=1.0, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is None


def test_zero_or_negative_inputs_return_none():
    assert evaluate_partial_tp(
        stage=0, entry_price=0, current_price=100, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    ) is None
    assert evaluate_partial_tp(
        stage=0, entry_price=100, current_price=0, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    ) is None
    assert evaluate_partial_tp(
        stage=0, entry_price=100, current_price=105, current_qty=0,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    ) is None


# ─── Cripto ──────────────────────────────────────────────────────────────────

def test_crypto_round_respects_min_tick():
    r = make_crypto_round_qty(0.0001)
    assert r(0.12345) == 0.1234
    assert r(0.00005) == 0.0
    assert r(1.99999) == 1.9999


def test_tp1_fires_with_crypto_round():
    r = make_crypto_round_qty(0.001)
    a = evaluate_partial_tp(
        stage=0, entry_price=50000.0, current_price=52500.0, current_qty=0.002,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=r,
    )
    # floor(0.001 / 0.001) * 0.001 = 0.001
    assert a is not None
    assert a.sell_qty == pytest.approx(0.001)
    assert a.new_stop == pytest.approx(50000.0)


def test_tp1_skipped_when_crypto_notional_too_small():
    """0.0001 BTC * $1 = $0.0001 < min_notional 10 → skip."""
    r = make_crypto_round_qty(0.0001)
    a = evaluate_partial_tp(
        stage=0, entry_price=1.0, current_price=1.10, current_qty=0.0002,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=r,
    )
    assert a is None


# ─── Coherencia con los bots inline ──────────────────────────────────────────

def test_reason_label_matches_bot_format():
    """El bot usa el formato `partial_tp{N}_{pct:.1f}pct:{pnl:.2%}`."""
    a = evaluate_partial_tp(
        stage=0, entry_price=100.0, current_price=105.5, current_qty=10,
        tp1_pct=TP1, tp2_pct=TP2, tp1_ratio=RATIO, tp2_ratio=RATIO,
        min_notional=MIN_NOTIONAL, round_qty=floor_int_qty,
    )
    assert a is not None
    # "partial_tp1_5.0pct:5.50%"
    assert "partial_tp1_" in a.reason
    assert "pct:" in a.reason
