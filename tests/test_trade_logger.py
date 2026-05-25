"""Tests para _trade_logger.

Verifica el contrato F5.0:
- Cada llamada a log_trade_event escribe SIEMPRE una línea JSON al
  archivo apuntado por TRADE_EVENTS_JSONL_PATH.
- El payload incluye todos los campos del schema base.
- El campo `source` se persiste tal cual.
- El campo `extra` se serializa como `enriched` subkey.
- Excepciones de Sheets NO rompen el log al JSONL.
- make_trade_id / make_event_id son drop-in compatibles con
  _sheets_logger (mismo formato).

Corre como `python3 -m unittest tests.test_trade_logger` — no requiere
pytest porque _trade_logger no debe tener dependencias externas
nuevas; usamos solo stdlib.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TradeLoggerTests(unittest.TestCase):
    def setUp(self):
        # Cada test usa su propio JSONL aislado
        self.tmp = tempfile.NamedTemporaryFile(
            prefix="trade_test_", suffix=".jsonl", delete=False
        )
        self.tmp.close()
        os.environ["TRADE_EVENTS_JSONL_PATH"] = self.tmp.name
        os.environ["TRADE_EVENTS_DISABLE_SHEETS"] = "1"
        # Re-importar para que el módulo respete las env vars del test
        if "_trade_logger" in sys.modules:
            del sys.modules["_trade_logger"]
        import _trade_logger

        self._tl = _trade_logger

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass
        os.environ.pop("TRADE_EVENTS_JSONL_PATH", None)
        os.environ.pop("TRADE_EVENTS_DISABLE_SHEETS", None)

    def _read_lines(self):
        with open(self.tmp.name) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_make_trade_id_short_uuid(self):
        # position_id con guiones se compacta a 8 chars
        tid = self._tl.make_trade_id("RFTM", "abc12345-6789-defg")
        self.assertEqual(tid, "RFTM-abc12345")

    def test_make_event_id_with_suffix(self):
        eid = self._tl.make_event_id("RFTM-x", "BUY", suffix="abc")
        self.assertEqual(eid, "RFTM-x-BUY-abc")

    def test_make_event_id_no_suffix(self):
        eid = self._tl.make_event_id("RFTM-x", "BUY")
        self.assertEqual(eid, "RFTM-x-BUY")

    def test_basic_buy_event_persisted(self):
        ok = self._tl.log_trade_event(
            bot="RFTM",
            symbol="SPY",
            side="BUY",
            qty=10,
            price=500.25,
            trade_id="RFTM-test001",
            stage=0,
            running_qty=10,
            initial_qty=10,
            entry_price=500.25,
            reason="entry_breakout",
            broker_order_id="ord-001",
            source="rftm_entry",
        )
        self.assertTrue(ok)
        rows = self._read_lines()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["bot"], "RFTM")
        self.assertEqual(r["symbol"], "SPY")
        self.assertEqual(r["side"], "BUY")
        self.assertEqual(r["qty"], 10.0)
        self.assertEqual(r["price"], 500.25)
        self.assertEqual(r["notional"], 5002.5)
        self.assertEqual(r["stage"], 0)
        self.assertEqual(r["source"], "rftm_entry")
        self.assertIn("timestamp_utc", r)
        # event_id se genera si no se pasa
        self.assertTrue(r["event_id"].startswith("RFTM-test001-BUY-"))

    def test_explicit_event_id_respected(self):
        eid = "RFTM-test002-SELL_TP1-fixed"
        self._tl.log_trade_event(
            bot="RFTM", symbol="QQQ", side="SELL_TP1",
            qty=5, price=410.0, trade_id="RFTM-test002",
            event_id=eid, stage=1, running_qty=5, initial_qty=10,
            entry_price=400.0, realized_pnl_event=50.0,
            reason="partial_tp1", broker_order_id="ord-002",
            source="rftm_watchdog",
        )
        rows = self._read_lines()
        self.assertEqual(rows[0]["event_id"], eid)
        self.assertEqual(rows[0]["realized_pnl_event"], 50.0)

    def test_extra_field_persisted_as_enriched(self):
        self._tl.log_trade_event(
            bot="RFTM", symbol="SPY", side="BUY",
            qty=1, price=500, trade_id="RFTM-test003",
            extra={"rsi14": 72.5, "atr_pct": 0.018, "vol_ratio": 1.4},
            source="rftm_entry",
        )
        rows = self._read_lines()
        self.assertIn("enriched", rows[0])
        self.assertAlmostEqual(rows[0]["enriched"]["rsi14"], 72.5)
        self.assertAlmostEqual(rows[0]["enriched"]["atr_pct"], 0.018)

    def test_no_extra_means_no_enriched_key(self):
        self._tl.log_trade_event(
            bot="MREV", symbol="BTCUSD", side="BUY",
            qty=0.1, price=50000, trade_id="MREV-test004",
            source="mrev_entry",
        )
        rows = self._read_lines()
        self.assertNotIn("enriched", rows[0])

    def test_multiple_appends_serialized(self):
        for i in range(5):
            self._tl.log_trade_event(
                bot="RFTM", symbol=f"SYM{i}", side="BUY",
                qty=1, price=100 + i, trade_id=f"RFTM-test{i}",
            )
        rows = self._read_lines()
        self.assertEqual(len(rows), 5)
        for i, r in enumerate(rows):
            self.assertEqual(r["symbol"], f"SYM{i}")
            self.assertEqual(r["price"], 100.0 + i)

    def test_none_values_become_null(self):
        self._tl.log_trade_event(
            bot="RFTM", symbol="SPY", side="BUY",
            qty=10, price=500, trade_id="RFTM-x",
            # realized_pnl_event, initial_qty, entry_price omitted
        )
        rows = self._read_lines()
        r = rows[0]
        self.assertIsNone(r["realized_pnl_event"])
        self.assertIsNone(r["initial_qty"])
        self.assertIsNone(r["entry_price"])

    def test_bot_uppercased(self):
        self._tl.log_trade_event(
            bot="rftm", symbol="SPY", side="BUY",
            qty=1, price=100, trade_id="rftm-x",
        )
        rows = self._read_lines()
        self.assertEqual(rows[0]["bot"], "RFTM")

    def test_creates_logs_dir_if_missing(self):
        nested = os.path.join(
            tempfile.gettempdir(), f"trade_test_nested_{uuid.uuid4().hex[:6]}", "deep", "log.jsonl"
        )
        os.environ["TRADE_EVENTS_JSONL_PATH"] = nested
        # Reload con el nuevo path
        del sys.modules["_trade_logger"]
        import _trade_logger as tl2

        try:
            ok = tl2.log_trade_event(
                bot="RFTM", symbol="SPY", side="BUY",
                qty=1, price=100, trade_id="RFTM-nested",
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(nested))
        finally:
            try:
                os.unlink(nested)
                os.rmdir(os.path.dirname(nested))
                os.rmdir(os.path.dirname(os.path.dirname(nested)))
            except OSError:
                pass

    def test_sheets_disabled_via_env(self):
        # Si TRADE_EVENTS_DISABLE_SHEETS=1, no se intenta importar _sheets_logger.
        # Si el import falla, esto NO debe romper el JSONL.
        # Lo verificamos mockeando _sheets_logger.log_trade_event con uno que
        # lanza excepción y confirmando que el retorno sigue siendo True.
        os.environ.pop("TRADE_EVENTS_DISABLE_SHEETS", None)
        # Re-import for fresh module state
        del sys.modules["_trade_logger"]
        import _trade_logger as tl3

        import _sheets_logger

        original = _sheets_logger.log_trade_event

        def boom(**_kwargs):
            raise RuntimeError("simulated sheets failure")

        _sheets_logger.log_trade_event = boom
        try:
            ok = tl3.log_trade_event(
                bot="RFTM", symbol="SPY", side="BUY",
                qty=1, price=100, trade_id="RFTM-boom",
            )
            # JSONL persistió aunque Sheets explotó
            self.assertTrue(ok)
            rows = self._read_lines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trade_id"], "RFTM-boom")
        finally:
            _sheets_logger.log_trade_event = original
            os.environ["TRADE_EVENTS_DISABLE_SHEETS"] = "1"


if __name__ == "__main__":
    unittest.main()
