"""Regression tests for the SIMPLE-FIXED exit scheme (fix 2026-05-21).

Verifica:
- RFTM check_exit solo dispara E3_stop_loss (no E5/E6/E7).
- MREV check_exit solo dispara stop_loss a entry × 0.95.
- recalc_stop_for_stage devuelve cascada fija: 0 → entry × 0.95,
  1 → entry, 2 → entry × 1.05.
- evaluate_partial_tp sube el stop a entry × (1 + tp1_pct) en TP2.

Stdlib only (no pytest, no pandas, no sqlite — usamos dicts/mocks).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeRow(dict):
    """Compat para usarse como pd.Series + sqlite3.Row en check_exit."""
    def __getitem__(self, k):
        return super().__getitem__(k) if k in self else 0
    def get(self, k, default=None):
        return super().get(k, default)


class RftmCheckExitTests(unittest.TestCase):
    """RFTM check_exit ahora solo dispara E3 (hard stop loss DB-side)."""

    def setUp(self):
        # Importar dentro de setUp para evitar costo si el módulo falla
        try:
            import standalone_paper_trader as rftm
        except Exception:
            self.skipTest("standalone_paper_trader import failed")
        self.rftm = rftm

    def _pos(self, entry: float, stop: float, stage: int = 0):
        return _FakeRow(entry_price=entry, stop_loss=stop, partial_tp_taken=stage)

    def _row(self, close: float, atr: float = 0.0, bars_no_high: int = 0):
        return _FakeRow(close=close, atr14=atr, bars_since_last_high=bars_no_high)

    def test_e3_fires_when_close_at_or_below_stop(self):
        row = self._row(close=95.0)
        pos = self._pos(entry=100.0, stop=95.0)
        should, reason = self.rftm.check_exit(row, pos, highest_since_entry=105.0)
        self.assertTrue(should)
        self.assertEqual(reason, "E3_stop_loss")

    def test_no_exit_when_above_stop(self):
        row = self._row(close=99.5)
        pos = self._pos(entry=100.0, stop=95.0)
        should, _ = self.rftm.check_exit(row, pos, highest_since_entry=110.0)
        self.assertFalse(should)

    def test_no_breakeven_stop_at_minor_pullback(self):
        """ANTES (bug): si high subió 0.5×ATR y close vuelve a entry, vendía.
        AHORA: ya no — solo E3 dispara."""
        row = self._row(close=100.0, atr=2.0)
        pos = self._pos(entry=100.0, stop=95.0)
        # highest = 101 (= entry + 0.5×ATR) → ANTES gatillaba E5_breakeven
        should, _ = self.rftm.check_exit(row, pos, highest_since_entry=101.0)
        self.assertFalse(should)

    def test_no_trailing_aggressive_exit(self):
        """ANTES: profit_atr ≥ 1.5 + close ≤ high-1×ATR → vendía.
        AHORA: ese path está removido — solo E3."""
        row = self._row(close=104.0, atr=2.0)
        pos = self._pos(entry=100.0, stop=95.0)
        # highest = 110 → trail_stop antes era 108 → close 104 hubiera vendido
        should, _ = self.rftm.check_exit(row, pos, highest_since_entry=110.0)
        self.assertFalse(should)

    def test_no_time_stop(self):
        """ANTES: 20 barras sin nuevo high → vendía. AHORA: removido."""
        row = self._row(close=100.0, bars_no_high=50)
        pos = self._pos(entry=100.0, stop=95.0)
        should, _ = self.rftm.check_exit(row, pos, highest_since_entry=102.0)
        self.assertFalse(should)

    def test_no_e7_take_profit(self):
        """ANTES: stage 2 + close ≥ 2:1 RR → vendía E7. AHORA: el TP +10%
        lo maneja evaluate_final_tp en el watchdog, no check_exit."""
        row = self._row(close=115.0)
        pos = self._pos(entry=100.0, stop=95.0, stage=2)
        should, _ = self.rftm.check_exit(row, pos, highest_since_entry=120.0)
        self.assertFalse(should)


class MrevCheckExitTests(unittest.TestCase):
    """MREV check_exit ahora solo dispara stop_loss a entry × 0.95."""

    def setUp(self):
        try:
            import standalone_mrev_trader as mrev
        except Exception:
            self.skipTest("standalone_mrev_trader import failed")
        self.mrev = mrev

    def test_stop_fires_at_minus_5pct(self):
        row = _FakeRow(close=95.0)
        now = datetime.now(timezone.utc)
        should, reason = self.mrev.check_exit(
            row, entry_price=100.0, entry_dt=now, now_dt=now,
            highest_since_entry=100.0,
        )
        self.assertTrue(should)
        self.assertTrue(reason.startswith("stop_loss"))

    def test_no_exit_just_above_stop(self):
        row = _FakeRow(close=95.5)
        now = datetime.now(timezone.utc)
        should, _ = self.mrev.check_exit(
            row, entry_price=100.0, entry_dt=now, now_dt=now,
        )
        self.assertFalse(should)

    def test_no_take_profit_via_sma(self):
        """ANTES: X1 vendía si close ≥ SMA + 1.5×ATR. AHORA: removido."""
        row = _FakeRow(close=130.0, sma_20=100.0, atr_14=2.0)
        now = datetime.now(timezone.utc)
        should, _ = self.mrev.check_exit(
            row, entry_price=100.0, entry_dt=now, now_dt=now,
        )
        # Close > stop=95 → no firea. Y X1 ya no existe.
        self.assertFalse(should)

    def test_no_time_stop_at_120h(self):
        """ANTES: 120h sin importar precio → vendía. AHORA: removido."""
        row = _FakeRow(close=100.0)
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        old_entry = now - timedelta(hours=200)
        should, _ = self.mrev.check_exit(
            row, entry_price=100.0, entry_dt=old_entry, now_dt=now,
        )
        self.assertFalse(should)

    def test_no_trailing_below_entry(self):
        """ANTES: high apenas arriba del entry → trail = high-ATR (BAJO entry)
        → close al entry vendía. AHORA: removido."""
        row = _FakeRow(close=99.0, atr_14=2.0)
        now = datetime.now(timezone.utc)
        # highest barely above entry → trailing antes ejecutaba con close=99
        should, _ = self.mrev.check_exit(
            row, entry_price=100.0, entry_dt=now, now_dt=now,
            highest_since_entry=100.5,  # apenas arriba
        )
        self.assertFalse(should)


class StopCascadeTests(unittest.TestCase):
    """Cascada de stops nueva (fixed-pct):
       stage 0 → entry × 0.95
       stage 1 → entry
       stage 2 → entry × (1 + tp1_pct)
    """

    def test_full_cascade_with_default_pcts(self):
        from _exit_logic import recalc_stop_for_stage
        # Stage 0
        self.assertEqual(
            recalc_stop_for_stage(entry_price=100, stage=0, atr=None, current_stop=None),
            95.0,
        )
        # Stage 1
        self.assertEqual(
            recalc_stop_for_stage(entry_price=100, stage=1, atr=None, current_stop=None),
            100.0,
        )
        # Stage 2
        self.assertEqual(
            recalc_stop_for_stage(entry_price=100, stage=2, atr=None, current_stop=None),
            105.0,
        )

    def test_evaluate_partial_tp_locks_tp1_on_stage2(self):
        from _exit_logic import evaluate_partial_tp, floor_int_qty
        a = evaluate_partial_tp(
            stage=1, entry_price=100.0, current_price=108.0, current_qty=10,
            tp1_pct=0.05, tp2_pct=0.075, tp1_ratio=0.5, tp2_ratio=0.5,
            min_notional=10.0, round_qty=floor_int_qty,
        )
        self.assertIsNotNone(a)
        self.assertEqual(a.stage, 2)
        self.assertEqual(a.new_stop, 105.0)  # entry × 1.05


if __name__ == "__main__":
    unittest.main()
