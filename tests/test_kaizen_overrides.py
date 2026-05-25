"""Tests para _kaizen_overrides — F5.5."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _kaizen_overrides import (  # noqa: E402
    get_param,
    load_active_overrides,
    load_overrides,
    merge_overrides,
)


class LoadOverridesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_missing_file_returns_empty(self):
        self.path.unlink()
        self.assertEqual(load_overrides(self.path), [])

    def test_no_overrides_section(self):
        self.path.write_text(json.dumps({"rules": []}))
        self.assertEqual(load_overrides(self.path), [])

    def test_loads_list(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "O_tp1", "param": "PARTIAL_TP1_PCT", "value": 0.03},
                {"id": "O_cool", "param": "RFTM_COOLDOWN_DAYS", "value": 7},
            ]
        }))
        out = load_overrides(self.path)
        self.assertEqual(len(out), 2)

    def test_active_filter(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "A", "param": "X", "value": 1, "active": True,
                 "applies_to_bot": "RFTM"},
                {"id": "B", "param": "Y", "value": 2, "active": False},
                {"id": "C", "param": "Z", "value": 3, "active": True,
                 "applies_to_bot": "MREV"},
                {"id": "D", "param": "W", "value": 4, "active": True,
                 "applies_to_bot": "BOTH"},
            ]
        }))
        rftm_only = load_active_overrides(bot="RFTM", path=self.path)
        ids = {o["id"] for o in rftm_only}
        self.assertEqual(ids, {"A", "D"})

        mrev_only = load_active_overrides(bot="MREV", path=self.path)
        ids = {o["id"] for o in mrev_only}
        self.assertEqual(ids, {"C", "D"})

    def test_active_without_bot_filter(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "A", "active": True},
                {"id": "B", "active": False},
            ]
        }))
        actives = load_active_overrides(path=self.path)
        self.assertEqual({o["id"] for o in actives}, {"A"})


class GetParamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_no_override_returns_default(self):
        self.path.write_text(json.dumps({"param_overrides": []}))
        self.assertEqual(get_param("X", 0.05, path=self.path), 0.05)

    def test_active_override_wins(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "O_x", "param": "PARTIAL_TP1_PCT", "value": 0.03,
                 "active": True, "applies_to_bot": "RFTM",
                 "activated_at": "2026-05-15T00:00:00+00:00"},
            ]
        }))
        v = get_param("PARTIAL_TP1_PCT", 0.05, bot="RFTM", path=self.path)
        self.assertEqual(v, 0.03)

    def test_inactive_override_ignored(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "O_x", "param": "PARTIAL_TP1_PCT", "value": 0.03,
                 "active": False, "applies_to_bot": "RFTM"},
            ]
        }))
        v = get_param("PARTIAL_TP1_PCT", 0.05, bot="RFTM", path=self.path)
        self.assertEqual(v, 0.05)

    def test_bot_mismatch_uses_default(self):
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "O_x", "param": "X", "value": 0.03, "active": True,
                 "applies_to_bot": "MREV"},
            ]
        }))
        v = get_param("X", 0.05, bot="RFTM", path=self.path)
        self.assertEqual(v, 0.05)

    def test_most_recent_wins(self):
        # Dos overrides activos para el mismo param — gana el más nuevo
        self.path.write_text(json.dumps({
            "param_overrides": [
                {"id": "O_old", "param": "X", "value": 0.03, "active": True,
                 "applies_to_bot": "BOTH",
                 "activated_at": "2026-05-01T00:00:00+00:00"},
                {"id": "O_new", "param": "X", "value": 0.07, "active": True,
                 "applies_to_bot": "BOTH",
                 "activated_at": "2026-05-15T00:00:00+00:00"},
            ]
        }))
        v = get_param("X", 0.05, bot="RFTM", path=self.path)
        self.assertEqual(v, 0.07)


class MergeOverridesTests(unittest.TestCase):
    def test_new_override_starts_inactive(self):
        merged = merge_overrides(
            [], [{"id": "O_x", "param": "X", "value": 0.03, "confidence": "high"}]
        )
        self.assertEqual(merged[0]["active"], False)
        self.assertIsNone(merged[0].get("activated_at"))
        self.assertIn("created_at", merged[0])

    def test_existing_active_preserved_on_update(self):
        existing = [{
            "id": "O_x", "param": "X", "value": 0.03,
            "active": True, "activated_at": "2026-04-01T00:00:00+00:00",
            "n_trades": 10,
        }]
        merged = merge_overrides(existing, [
            {"id": "O_x", "param": "X", "value": 0.04, "n_trades": 20}
        ])
        self.assertEqual(merged[0]["value"], 0.04)  # update
        self.assertEqual(merged[0]["n_trades"], 20)  # update
        self.assertTrue(merged[0]["active"])  # PRESERVED
        self.assertEqual(merged[0]["activated_at"], "2026-04-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
