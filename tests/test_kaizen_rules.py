"""Tests para _kaizen_rules — F5.3/F5.4.

Verifica:
- load_rules / load_active_rules.
- should_auto_apply respeta los 4 criterios.
- auto_activate muta solo las que pasan + setea activated_at.
- rule_matches sandbox: no permite imports, builtins peligrosos.
- evaluate_entry_rules devuelve la primera regla que matchea.

Stdlib only.
"""
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

from _kaizen_rules import (  # noqa: E402
    auto_activate,
    evaluate_entry_rules,
    load_active_rules,
    load_rules,
    rule_matches,
    save_rules,
    should_auto_apply,
)


class LoadRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        )
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_missing_file_returns_empty(self):
        self.path.unlink()
        self.assertEqual(load_rules(self.path), [])

    def test_invalid_json_returns_empty(self):
        self.path.write_text("not json")
        self.assertEqual(load_rules(self.path), [])

    def test_load_active_only(self):
        self.path.write_text(json.dumps({
            "rules": [
                {"id": "A", "active": True, "applies_to": "entry"},
                {"id": "B", "active": False, "applies_to": "entry"},
                {"id": "C", "active": True, "applies_to": "exit"},
            ]
        }))
        active = load_active_rules(self.path)
        self.assertEqual({r["id"] for r in active}, {"A", "C"})
        entry_active = load_active_rules(self.path, applies_to="entry")
        self.assertEqual({r["id"] for r in entry_active}, {"A"})


class ShouldAutoApplyTests(unittest.TestCase):
    def _rule(self, **overrides) -> dict:
        base = {
            "id": "K_x",
            "condition": "row['rsi14'] > 70",
            "n_trades": 15,
            "win_rate": 0.0,
            "loss_rate": 0.85,
            "confidence": "high",
        }
        base.update(overrides)
        return base

    def test_meets_all_criteria(self):
        ok, _ = should_auto_apply(self._rule())
        self.assertTrue(ok)

    def test_low_n(self):
        ok, why = should_auto_apply(self._rule(n_trades=8))
        self.assertFalse(ok)
        self.assertIn("8", why)

    def test_rate_borderline(self):
        ok, _ = should_auto_apply(self._rule(loss_rate=0.79, win_rate=0.79))
        self.assertFalse(ok)

    def test_high_win_rate_also_qualifies(self):
        ok, _ = should_auto_apply(self._rule(loss_rate=0.0, win_rate=0.85))
        self.assertTrue(ok)

    def test_medium_confidence_rejected(self):
        ok, _ = should_auto_apply(self._rule(confidence="medium"))
        self.assertFalse(ok)

    def test_no_condition_rejected(self):
        ok, _ = should_auto_apply(self._rule(condition=""))
        self.assertFalse(ok)

    def test_dismissed_rejected(self):
        ok, _ = should_auto_apply(self._rule(dismissed_at="2026-01-01"))
        self.assertFalse(ok)


class AutoActivateTests(unittest.TestCase):
    def test_activates_eligible(self):
        rules = [
            {"id": "A", "condition": "True", "n_trades": 15,
             "loss_rate": 0.9, "confidence": "high"},
            {"id": "B", "condition": "True", "n_trades": 5,
             "loss_rate": 0.9, "confidence": "high"},
        ]
        newly = auto_activate(rules)
        self.assertEqual([r["id"] for r in newly], ["A"])
        self.assertTrue(rules[0]["active"])
        self.assertIn("activated_at", rules[0])
        self.assertEqual(rules[0]["activation_mode"], "auto")
        self.assertNotIn("active", rules[1])

    def test_doesnt_reactivate_already_active(self):
        rules = [
            {"id": "A", "active": True, "condition": "True",
             "n_trades": 15, "loss_rate": 0.9, "confidence": "high"},
        ]
        newly = auto_activate(rules)
        self.assertEqual(newly, [])


class RuleMatchesTests(unittest.TestCase):
    def test_simple_match(self):
        rule = {"condition": "row['rsi14'] > 70"}
        self.assertTrue(rule_matches(rule, {"rsi14": 80}))
        self.assertFalse(rule_matches(rule, {"rsi14": 50}))

    def test_uses_row_get(self):
        rule = {"condition": "row.get('rsi14', 0) > 70 and row.get('atr14', 0) > 1"}
        self.assertTrue(rule_matches(rule, {"rsi14": 80, "atr14": 2}))
        self.assertFalse(rule_matches(rule, {"rsi14": 80}))  # atr ausente → 0

    def test_no_imports_allowed(self):
        rule = {"condition": "__import__('os').system('echo hacked')"}
        # No matchea por sandbox — pero más importante: no ejecutó
        self.assertFalse(rule_matches(rule, {}))

    def test_no_open_allowed(self):
        rule = {"condition": "open('/etc/passwd').read()"}
        self.assertFalse(rule_matches(rule, {}))

    def test_invalid_syntax_returns_false(self):
        rule = {"condition": "this is not python"}
        self.assertFalse(rule_matches(rule, {}))

    def test_missing_condition_returns_false(self):
        self.assertFalse(rule_matches({}, {}))

    def test_uses_builtins_safe(self):
        rule = {"condition": "max(row.get('vals', [])) > 10"}
        self.assertTrue(rule_matches(rule, {"vals": [5, 11, 3]}))


class EvaluateEntryRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        )
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_returns_first_matching(self):
        self.path.write_text(json.dumps({"rules": [
            {"id": "K_high_rsi", "active": True, "applies_to": "entry",
             "condition": "row.get('rsi14', 0) > 75"},
            {"id": "K_low_vol", "active": True, "applies_to": "entry",
             "condition": "row.get('vol_ratio_20d', 1) < 0.5"},
        ]}))
        # rsi=80 → matchea la primera
        match = evaluate_entry_rules({"rsi14": 80, "vol_ratio_20d": 1.0},
                                      path=self.path)
        self.assertEqual(match["id"], "K_high_rsi")

    def test_returns_none_if_no_match(self):
        self.path.write_text(json.dumps({"rules": [
            {"id": "K_x", "active": True, "applies_to": "entry",
             "condition": "row['rsi14'] > 99"}
        ]}))
        self.assertIsNone(evaluate_entry_rules({"rsi14": 50}, path=self.path))

    def test_skips_inactive_rules(self):
        self.path.write_text(json.dumps({"rules": [
            {"id": "K_off", "active": False, "applies_to": "entry",
             "condition": "True"}
        ]}))
        self.assertIsNone(evaluate_entry_rules({}, path=self.path))


class SaveRulesTests(unittest.TestCase):
    def test_preserves_other_keys(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        path = Path(tmp.name)
        tmp.close()
        try:
            path.write_text(json.dumps({
                "rules": [],
                "last_review_iso": "2026-05-15T00:00:00+00:00",
            }))
            save_rules([{"id": "X", "active": False}], path=path)
            data = json.loads(path.read_text())
            self.assertEqual(data["last_review_iso"], "2026-05-15T00:00:00+00:00")
            self.assertEqual(data["rules"][0]["id"], "X")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
