"""Tests para recalc_stop_for_stage — esquema FIXED-PCT (2026-05-21).

Verifica:
- Stage 0: stop = entry × (1 − fallback_pct)   → default entry × 0.95.
- Stage 1: stop = entry (breakeven).
- Stage 2: stop = entry × (1 + tp1_pct)        → default entry × 1.05 (lock TP1).
- INVARIANTE: stop solo SUBE — new = max(current, calculated).
- Los params `atr` / `atr_mult` quedan por compat pero NO afectan el cálculo.

Stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _exit_logic import recalc_stop_for_stage, stage_implied_high_floor  # noqa: E402


class StopRecalcTests(unittest.TestCase):
    # ── Stage 0: −5% fijo (default fallback_pct=0.05) ───────────────────
    def test_stage0_default_is_minus_5pct(self):
        # entry=100 → stop = 100 × 0.95 = 95
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=2.0, current_stop=None
        )
        self.assertEqual(stop, 95.0)

    def test_stage0_atr_is_ignored(self):
        # Aunque pasemos ATR alto, NO afecta — esquema fixed pct.
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=99.0, current_stop=None,
            atr_mult=2.5,
        )
        self.assertEqual(stop, 95.0)

    def test_stage0_none_atr_still_minus_5pct(self):
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=None, current_stop=None
        )
        self.assertEqual(stop, 95.0)

    def test_stage0_custom_fallback_pct(self):
        # entry=100, fallback=0.10 → 90.0
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=2.0, current_stop=None,
            fallback_pct=0.10,
        )
        self.assertEqual(stop, 90.0)

    # ── Stage 1: breakeven ──────────────────────────────────────────────
    def test_stage1_returns_breakeven(self):
        stop = recalc_stop_for_stage(
            entry_price=100, stage=1, atr=2.0, current_stop=98.5
        )
        # Stage 1 → 100, no menos
        self.assertEqual(stop, 100.0)

    def test_stage1_invariant_stop_only_goes_up(self):
        # current_stop > entry (algo raro pero posible) — debe respetar el alto
        stop = recalc_stop_for_stage(
            entry_price=100, stage=1, atr=2.0, current_stop=105.0
        )
        self.assertEqual(stop, 105.0)

    # ── Stage 2: lock TP1 (entry × 1.05) ────────────────────────────────
    def test_stage2_locks_tp1_level(self):
        # stage 2 → stop = entry × (1 + tp1_pct) = 100 × 1.05 = 105
        stop = recalc_stop_for_stage(
            entry_price=100, stage=2, atr=2.0, current_stop=100.0
        )
        self.assertEqual(stop, 105.0)

    def test_stage2_zero_current_locks_tp1(self):
        # Sin current_stop seteado, igual subimos al lock TP1
        stop = recalc_stop_for_stage(
            entry_price=100, stage=2, atr=2.0, current_stop=0.0
        )
        self.assertEqual(stop, 105.0)

    def test_stage2_none_current_locks_tp1(self):
        stop = recalc_stop_for_stage(
            entry_price=100, stage=2, atr=2.0, current_stop=None
        )
        self.assertEqual(stop, 105.0)

    def test_stage2_custom_tp1_pct(self):
        # tp1_pct=0.03 → stop = 100 × 1.03 = 103
        stop = recalc_stop_for_stage(
            entry_price=100, stage=2, atr=2.0, current_stop=None,
            tp1_pct=0.03,
        )
        self.assertEqual(stop, 103.0)

    def test_stage2_invariant_keeps_higher_existing_stop(self):
        # Si por alguna razón el stop ya estaba arriba del nivel TP1, mantenerlo.
        stop = recalc_stop_for_stage(
            entry_price=100, stage=2, atr=2.0, current_stop=110.0
        )
        self.assertEqual(stop, 110.0)

    # ── INVARIANTE crítico: stop solo SUBE ──────────────────────────────
    def test_stage0_invariant_does_not_lower_stop(self):
        # Si la DB ya tenía un stop alto NO debe bajarlo aunque stage=0.
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=2.0, current_stop=99.0
        )
        # Calculated = 95, current = 99 → wins el 99
        self.assertEqual(stop, 99.0)

    def test_stage0_raises_stop_when_current_is_lower(self):
        # current=90 < calculated=95 → wins el 95
        stop = recalc_stop_for_stage(
            entry_price=100, stage=0, atr=2.0, current_stop=90.0
        )
        self.assertEqual(stop, 95.0)

    def test_stage1_invariant_does_not_lower_post_trailing(self):
        # Si el watchdog ya empujó el stop arriba de breakeven, no bajamos
        stop = recalc_stop_for_stage(
            entry_price=100, stage=1, atr=2.0, current_stop=103.0
        )
        self.assertEqual(stop, 103.0)

    def test_zero_negative_current_stop_treated_as_none(self):
        # current_stop negativo o 0 = no había → cualquier calc gana
        for cur in [0.0, -1.0, -100.0]:
            stop = recalc_stop_for_stage(
                entry_price=100, stage=0, atr=2.0, current_stop=cur
            )
            self.assertEqual(stop, 95.0, f"failed for current_stop={cur}")


class StageImpliedHighFloorTests(unittest.TestCase):
    """stage_implied_high_floor — recovery del high reseteado por seeds.

    Si una posición figura con stage>=1 pero highest_since_entry quedó en
    entry_price (porque seed_missing_positions / mark_partial_tp_done /
    sync_with_alpaca re-insertaron sin preservar el high real), el
    trailing stop del watchdog calcula profit_atr=0 y nunca se activa.

    Este helper devuelve el piso CIERTO del high según el stage — el
    precio mínimo que tuvo que haber tocado para que el TP dispare.
    """

    def test_stage0_returns_entry_unchanged(self):
        # stage=0 → no podemos afirmar nada por encima del entry.
        self.assertEqual(stage_implied_high_floor(entry_price=100, stage=0),
                         100.0)

    def test_stage1_with_default_tp_pct(self):
        # stage=1 → high ≥ entry × 1.05 (TP1 default)
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=100, stage=1),
            105.0)

    def test_stage2_with_default_tp_pct(self):
        # stage=2 → high ≥ entry × 1.075 (TP2 default)
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=100, stage=2),
            107.5)

    def test_stage_above_2_uses_tp2_floor(self):
        # stage=3+ no debería existir, pero si pasa → conservador a TP2.
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=100, stage=3),
            107.5)

    def test_custom_tp_pcts(self):
        # MREV usa thresholds distintos → respetamos los args.
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=200, stage=1,
                                     tp1_pct=0.03, tp2_pct=0.06),
            206.0)
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=200, stage=2,
                                     tp1_pct=0.03, tp2_pct=0.06),
            212.0)

    def test_zero_or_negative_entry_returns_zero(self):
        # Defensive: entrada inválida → 0, no NaN ni crash.
        self.assertEqual(stage_implied_high_floor(entry_price=0, stage=1), 0.0)
        self.assertEqual(stage_implied_high_floor(entry_price=-5, stage=2), 0.0)

    def test_real_world_example_argt(self):
        # ARGT en la DB de Charlie: entry=92.35, stage=1, high=92.35
        # Floor esperado: 92.35 × 1.05 = 96.9675
        self.assertAlmostEqual(
            stage_implied_high_floor(entry_price=92.35, stage=1),
            96.9675, places=4)


if __name__ == "__main__":
    unittest.main()
