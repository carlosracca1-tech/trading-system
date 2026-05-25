"""Tests para _kaizen_enrichment — F5.1.

Verifica:
- enrich_rftm_indicators extrae todos los campos y calcula derivados.
- enrich_mrev_indicators idem con naming MREV.
- enrich_execution calcula slippage + tiempo en posición correctamente.
- enrich_market_regime cachea + clasifica VIX en bandas.
- build_enriched_extra mergea con prefijos correctos.
- Tolera Nones / NaN sin romper.

Stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RftmIndicatorsTests(unittest.TestCase):
    def setUp(self):
        # Reset caché entre tests
        import _kaizen_enrichment
        _kaizen_enrichment._REGIME_CACHE.clear()
        self.mod = _kaizen_enrichment

    def test_basic_extraction(self):
        row = {
            "close": 100,
            "rsi14": 65.5,
            "atr14": 2.0,
            "atr14_pct": 0.02,
            "ema21": 98.0,
            "ema50": 95.0,
            "ema200": 90.0,
            "volume": 1500000,
            "vol_ma20": 1000000,
            "high20": 99.0,
        }
        out = self.mod.enrich_rftm_indicators(row)
        self.assertEqual(out["rsi14"], 65.5)
        self.assertEqual(out["atr14"], 2.0)
        self.assertEqual(out["atr14_pct"], 0.02)
        self.assertAlmostEqual(out["vol_ratio_20d"], 1.5)
        # dist_to_20d_high: (100 - 99) / 99 = 0.0101
        self.assertAlmostEqual(out["dist_to_20d_high_pct"], 1/99)
        # ema21_dist: (100-98)/98 ≈ 0.0204
        self.assertAlmostEqual(out["ema21_dist_pct"], 2/98)
        self.assertAlmostEqual(out["ema50_dist_pct"], 5/95)

    def test_missing_atr_pct_derived_from_atr(self):
        row = {"close": 100, "atr14": 3.5}
        out = self.mod.enrich_rftm_indicators(row)
        self.assertAlmostEqual(out["atr14_pct"], 0.035)

    def test_handles_missing_fields_gracefully(self):
        out = self.mod.enrich_rftm_indicators({})
        # Todos los campos None, ninguna excepción
        for k, v in out.items():
            self.assertIsNone(v, f"{k} should be None, got {v}")

    def test_zero_division_safe(self):
        # vol_ma20 = 0 — no dividir
        row = {"close": 100, "volume": 100, "vol_ma20": 0}
        out = self.mod.enrich_rftm_indicators(row)
        self.assertIsNone(out["vol_ratio_20d"])

    def test_nan_filtered(self):
        # Si pandas pasa NaN como string "nan", debe filtrarse
        row = {"close": float("nan"), "rsi14": 50}
        out = self.mod.enrich_rftm_indicators(row)
        # close NaN → no podemos calcular distancias, pero rsi14 sí pasa
        self.assertEqual(out["rsi14"], 50.0)


class MrevIndicatorsTests(unittest.TestCase):
    def setUp(self):
        import _kaizen_enrichment
        self.mod = _kaizen_enrichment

    def test_basic_mrev(self):
        row = {
            "close": 50000,
            "rsi_14": 28.0,        # naming MREV
            "atr_14": 1500,
            "atr_14_pct": 0.03,
            "bb_upper": 52000,
            "bb_lower": 48000,
            "sma_20": 50100,
            "volume_ma_20": 100,
            "volume": 250,
        }
        out = self.mod.enrich_mrev_indicators(row)
        self.assertEqual(out["rsi14"], 28.0)
        self.assertEqual(out["atr14"], 1500)
        self.assertEqual(out["atr14_pct"], 0.03)
        # bb_pct: (50000 - 48000) / (52000 - 48000) = 0.5
        self.assertAlmostEqual(out["bb_pct"], 0.5)
        # sma_dist: (50000 - 50100) / 50100 = -0.002
        self.assertAlmostEqual(out["sma_20_dist_pct"], -100/50100)
        self.assertAlmostEqual(out["vol_ratio_20d"], 2.5)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        import _kaizen_enrichment
        self.mod = _kaizen_enrichment

    def test_slippage_positive_means_higher_fill(self):
        # Vendiste a $99 cuando el target era $100 → slippage = -1%
        out = self.mod.enrich_execution(fill_price=99, target_price=100)
        self.assertAlmostEqual(out["slippage_pct"], -0.01)

    def test_no_target_no_slippage(self):
        out = self.mod.enrich_execution(fill_price=99)
        self.assertIsNone(out["slippage_pct"])

    def test_time_in_position_hours(self):
        now = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
        entry = (now - timedelta(hours=24)).isoformat()
        out = self.mod.enrich_execution(fill_price=100, entry_dt_iso=entry, now=now)
        self.assertAlmostEqual(out["time_in_position_hours"], 24.0)

    def test_invalid_iso_returns_none(self):
        out = self.mod.enrich_execution(fill_price=100, entry_dt_iso="not-a-date")
        self.assertIsNone(out["time_in_position_hours"])


class MarketRegimeTests(unittest.TestCase):
    def setUp(self):
        import _kaizen_enrichment
        _kaizen_enrichment._REGIME_CACHE.clear()
        self.mod = _kaizen_enrichment

    def test_no_request_fn_returns_nones(self):
        out = self.mod.enrich_market_regime(alpaca_request_fn=None, use_cache=False)
        for v in out.values():
            self.assertIsNone(v)

    def test_vix_regime_classification(self):
        # Vamos a mockear el fetch para devolver distintos VIX
        def fake_request_factory(spy_closes, vix_value):
            def fake(method, path, body):
                if "SPY" in path:
                    return {"bars": [{"c": c} for c in spy_closes]}
                if "VIXY" in path:
                    return {"bars": [{"c": vix_value}]}
                return None
            return fake

        # 201 closes para que pase el >=200 check
        bull_closes = [400 + i * 0.5 for i in range(220)]  # uptrend
        # VIX = 12 → calm
        out = self.mod.enrich_market_regime(
            alpaca_request_fn=fake_request_factory(bull_closes, 12),
            use_cache=False,
        )
        self.assertTrue(out["spy_above_sma200"])
        self.assertEqual(out["vix_regime"], "calm")

        # VIX = 18 → normal
        self.mod._REGIME_CACHE.clear()
        out = self.mod.enrich_market_regime(
            alpaca_request_fn=fake_request_factory(bull_closes, 18),
            use_cache=False,
        )
        self.assertEqual(out["vix_regime"], "normal")

        # VIX = 25 → elevated
        self.mod._REGIME_CACHE.clear()
        out = self.mod.enrich_market_regime(
            alpaca_request_fn=fake_request_factory(bull_closes, 25),
            use_cache=False,
        )
        self.assertEqual(out["vix_regime"], "elevated")

        # VIX = 45 → panic
        self.mod._REGIME_CACHE.clear()
        out = self.mod.enrich_market_regime(
            alpaca_request_fn=fake_request_factory(bull_closes, 45),
            use_cache=False,
        )
        self.assertEqual(out["vix_regime"], "panic")

    def test_cache_hits_on_second_call(self):
        call_count = [0]
        def fake_request(method, path, body):
            call_count[0] += 1
            if "SPY" in path:
                return {"bars": [{"c": 400 + i} for i in range(220)]}
            return {"bars": [{"c": 15}]}

        self.mod.enrich_market_regime(alpaca_request_fn=fake_request, use_cache=True)
        first_count = call_count[0]
        self.mod.enrich_market_regime(alpaca_request_fn=fake_request, use_cache=True)
        # Segunda call no debe haber hecho fetches nuevos
        self.assertEqual(call_count[0], first_count)


class BuildEnrichedExtraTests(unittest.TestCase):
    def setUp(self):
        import _kaizen_enrichment
        self.mod = _kaizen_enrichment

    def test_rftm_with_indicators_and_execution(self):
        row = {"close": 100, "rsi14": 60, "atr14": 2}
        out = self.mod.build_enriched_extra(
            bot="RFTM",
            market_row=row,
            close=100,
            fill_price=99.5,
            target_price=100,
            entry_dt_iso="2026-05-10T10:00:00+00:00",
            include_regime=False,
        )
        # Prefijos correctos
        self.assertEqual(out["ind_rsi14"], 60.0)
        self.assertAlmostEqual(out["ind_atr14_pct"], 0.02)
        self.assertAlmostEqual(out["exe_slippage_pct"], -0.005)
        self.assertIn("exe_time_in_position_hours", out)

    def test_extra_kv_merged(self):
        out = self.mod.build_enriched_extra(
            bot="RFTM",
            extra_kv={"some_flag": True, "custom": "x"},
            include_regime=False,
        )
        self.assertEqual(out["some_flag"], True)
        self.assertEqual(out["custom"], "x")

    def test_mrev_bot_uses_mrev_indicators(self):
        row = {"close": 50000, "rsi_14": 30, "bb_upper": 51000, "bb_lower": 49000}
        out = self.mod.build_enriched_extra(
            bot="MREV", market_row=row, include_regime=False,
        )
        self.assertEqual(out["ind_rsi14"], 30.0)
        self.assertEqual(out["ind_bb_pct"], 0.5)


if __name__ == "__main__":
    unittest.main()
