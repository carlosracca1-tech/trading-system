"""Tests para kaizen_review — F5.2.

Verifica:
- load_events filtra por cutoff y deduplica por event_id.
- group_into_trades arma summaries correctos (entry, exits, outcome, pnl).
- merge_rules preserva flags `active`/`created_at` al actualizar.

NO testea call_claude (es red).

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import kaizen_review as kr  # noqa: E402


def _ts(hours_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class LoadEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "events.jsonl"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, events):
        with self.path.open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_filters_by_cutoff(self):
        self._write([
            {"event_id": "old", "timestamp_utc": _ts(hours_ago=48), "side": "BUY"},
            {"event_id": "fresh", "timestamp_utc": _ts(hours_ago=1), "side": "BUY"},
        ])
        cutoff = _ts(hours_ago=24)
        events = kr.load_events([self.path], cutoff)
        ids = [e["event_id"] for e in events]
        self.assertEqual(ids, ["fresh"])

    def test_dedupes_by_event_id(self):
        # Mismo event_id duplicado en líneas distintas (puede pasar)
        self._write([
            {"event_id": "x", "timestamp_utc": _ts(0.5), "side": "BUY"},
            {"event_id": "x", "timestamp_utc": _ts(0.5), "side": "BUY"},
            {"event_id": "y", "timestamp_utc": _ts(0.5), "side": "BUY"},
        ])
        events = kr.load_events([self.path], _ts(24))
        self.assertEqual(len(events), 2)

    def test_skips_missing_file(self):
        nope = Path(self.tmpdir.name) / "does-not-exist.jsonl"
        events = kr.load_events([nope], _ts(24))
        self.assertEqual(events, [])

    def test_skips_invalid_json(self):
        with self.path.open("w") as f:
            f.write('{"event_id": "ok", "timestamp_utc": "' + _ts(1) + '"}\n')
            f.write("garbage line\n")
        events = kr.load_events([self.path], _ts(24))
        self.assertEqual(len(events), 1)


class GroupIntoTradesTests(unittest.TestCase):
    def test_winner_trade_summary(self):
        events = [
            {"trade_id": "T1", "bot": "RFTM", "symbol": "SPY", "side": "BUY",
             "timestamp_utc": _ts(50), "qty": 10, "price": 100},
            {"trade_id": "T1", "bot": "RFTM", "symbol": "SPY", "side": "SELL_TP1",
             "timestamp_utc": _ts(40), "qty": 5, "realized_pnl_event": 25.0,
             "reason": "partial_tp1"},
            {"trade_id": "T1", "bot": "RFTM", "symbol": "SPY", "side": "SELL_FINAL_TP",
             "timestamp_utc": _ts(30), "qty": 5, "realized_pnl_event": 35.0,
             "reason": "final_tp"},
        ]
        trades = kr.group_into_trades(events)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["outcome"], "winner")
        self.assertAlmostEqual(t["total_pnl"], 60.0)
        # primary exit = el con más qty (tie: cualquiera)
        self.assertIn(t["exit_side_primary"], ("SELL_TP1", "SELL_FINAL_TP"))

    def test_loser_trade(self):
        events = [
            {"trade_id": "T2", "bot": "RFTM", "symbol": "XYZ", "side": "BUY",
             "timestamp_utc": _ts(20)},
            {"trade_id": "T2", "bot": "RFTM", "symbol": "XYZ", "side": "SELL_STOP",
             "timestamp_utc": _ts(10), "qty": 10, "realized_pnl_event": -50.0,
             "reason": "E3_stop_loss"},
        ]
        trades = kr.group_into_trades(events)
        self.assertEqual(trades[0]["outcome"], "loser")
        self.assertEqual(trades[0]["exit_reason_primary"], "E3_stop_loss")

    def test_open_trade_excluded(self):
        # Sin SELL — todavía abierto
        events = [
            {"trade_id": "T3", "side": "BUY", "timestamp_utc": _ts(5)},
        ]
        self.assertEqual(kr.group_into_trades(events), [])

    def test_no_entry_excluded(self):
        # Sin BUY (trade que arrancó antes del cutoff)
        events = [
            {"trade_id": "T4", "side": "SELL_TP1", "timestamp_utc": _ts(5),
             "qty": 5, "realized_pnl_event": 10},
        ]
        self.assertEqual(kr.group_into_trades(events), [])


class MergeRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "rules.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_creates_file_when_missing(self):
        out = kr.merge_rules(self.path, {"rules": [
            {"id": "K_x", "description": "X", "n_trades": 10}
        ]})
        self.assertEqual(len(out["rules"]), 1)
        # Nuevas reglas no-active por default (F5.3 decide)
        self.assertEqual(out["rules"][0]["active"], False)
        self.assertIn("created_at", out["rules"][0])

    def test_preserves_active_flag_on_update(self):
        self.path.write_text(json.dumps({
            "rules": [
                {"id": "K_x", "description": "old desc", "active": True,
                 "created_at": "2026-01-01T00:00:00+00:00"}
            ]
        }))
        out = kr.merge_rules(self.path, {"rules": [
            {"id": "K_x", "description": "new desc", "n_trades": 20}
        ]})
        rule = out["rules"][0]
        self.assertEqual(rule["description"], "new desc")
        self.assertEqual(rule["n_trades"], 20)
        # active y created_at PRESERVADOS
        self.assertTrue(rule["active"])
        self.assertEqual(rule["created_at"], "2026-01-01T00:00:00+00:00")

    def test_empty_rules_keeps_existing(self):
        self.path.write_text(json.dumps({
            "rules": [{"id": "K_keep", "active": True}]
        }))
        out = kr.merge_rules(self.path, {"rules": []})
        self.assertEqual(len(out["rules"]), 1)
        self.assertEqual(out["rules"][0]["id"], "K_keep")


if __name__ == "__main__":
    unittest.main()
