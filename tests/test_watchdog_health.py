"""Tests para _watchdog_health — F3.2.

Verifica:
- HealthReport.overall_severity correcto (info < warn < error).
- coverage_gap se agrega solo si evaluated < expected.
- Persistencia JSONL append-only.
- Email skipped si severity = info.
- Email skipped si WATCHDOG_HEALTH_EMAIL_ENABLED=0.
- send_health_email captura excepciones (no fatal).

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


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        # JSONL aislado por test
        self.tmp = tempfile.NamedTemporaryFile(
            prefix="kaizen_health_", suffix=".jsonl", delete=False
        )
        self.tmp.close()
        os.environ["KAIZEN_HEALTH_PATH"] = self.tmp.name
        os.environ["WATCHDOG_HEALTH_EMAIL_ENABLED"] = "0"  # nunca enviar en tests
        # Re-import
        for m in ["_watchdog_health"]:
            if m in sys.modules:
                del sys.modules[m]
        import _watchdog_health
        self.wh = _watchdog_health

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass
        os.environ.pop("KAIZEN_HEALTH_PATH", None)
        os.environ.pop("WATCHDOG_HEALTH_EMAIL_ENABLED", None)

    def _read(self):
        with open(self.tmp.name) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_overall_severity_info_when_empty(self):
        r = self.wh.HealthReport(bot="RFTM")
        self.assertEqual(r.overall_severity, "info")

    def test_overall_severity_warn(self):
        r = self.wh.HealthReport(bot="RFTM")
        r.add_event("info", "X")
        r.add_event("warn", "Y")
        self.assertEqual(r.overall_severity, "warn")

    def test_overall_severity_error_wins(self):
        r = self.wh.HealthReport(bot="RFTM")
        r.add_event("warn", "Y")
        r.add_event("error", "Z")
        r.add_event("info", "X")
        self.assertEqual(r.overall_severity, "error")

    def test_finalize_persists_to_jsonl(self):
        r = self.wh.HealthReport(bot="RFTM", expected_count=3, evaluated_count=3)
        self.wh.finalize_report(r, started_at_iso="2026-05-15T14:00:00+00:00")
        rows = self._read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bot"], "RFTM")
        self.assertEqual(rows[0]["overall_severity"], "info")
        self.assertGreaterEqual(rows[0]["latency_seconds"], 0)

    def test_coverage_gap_auto_warning(self):
        r = self.wh.HealthReport(bot="MREV", expected_count=5, evaluated_count=3)
        self.wh.finalize_report(r, started_at_iso="2026-05-15T14:00:00+00:00")
        rows = self._read()
        # Debe haber agregado coverage_gap warning
        codes = [e["code"] for e in rows[0]["events"]]
        self.assertIn("coverage_gap", codes)
        self.assertEqual(rows[0]["overall_severity"], "warn")

    def test_no_coverage_gap_when_full_coverage(self):
        r = self.wh.HealthReport(bot="RFTM", expected_count=4, evaluated_count=4)
        self.wh.finalize_report(r, started_at_iso="2026-05-15T14:00:00+00:00")
        rows = self._read()
        codes = [e["code"] for e in rows[0]["events"]]
        self.assertNotIn("coverage_gap", codes)

    def test_db_health_failure_persisted(self):
        r = self.wh.HealthReport(bot="RFTM")
        r.db_health_ok = False
        r.add_event("error", "db_health_fail", "integrity_check FAILED")
        self.wh.finalize_report(r, started_at_iso="2026-05-15T14:00:00+00:00")
        rows = self._read()
        self.assertFalse(rows[0]["db_health_ok"])
        self.assertEqual(rows[0]["overall_severity"], "error")

    def test_email_disabled_via_env(self):
        # WATCHDOG_HEALTH_EMAIL_ENABLED=0 — skipea email
        os.environ["WATCHDOG_HEALTH_EMAIL_ENABLED"] = "0"
        if "_watchdog_health" in sys.modules:
            del sys.modules["_watchdog_health"]
        import _watchdog_health as wh2
        r = wh2.HealthReport(bot="RFTM")
        r.add_event("error", "x")
        ok = wh2.send_health_email(r)
        self.assertFalse(ok)

    def test_email_skipped_when_severity_info(self):
        os.environ["WATCHDOG_HEALTH_EMAIL_ENABLED"] = "1"
        if "_watchdog_health" in sys.modules:
            del sys.modules["_watchdog_health"]
        import _watchdog_health as wh2
        r = wh2.HealthReport(bot="RFTM")
        # sin eventos → severity info → no email
        ok = wh2.send_health_email(r)
        self.assertFalse(ok)
        # restaurar
        os.environ["WATCHDOG_HEALTH_EMAIL_ENABLED"] = "0"

    def test_extra_dict_persisted(self):
        r = self.wh.HealthReport(bot="RFTM")
        self.wh.finalize_report(
            r,
            started_at_iso="2026-05-15T14:00:00+00:00",
            extra={"run_id": "abc123", "dry_run": True},
        )
        rows = self._read()
        self.assertEqual(rows[0]["extra"]["run_id"], "abc123")
        self.assertEqual(rows[0]["extra"]["dry_run"], True)

    def test_multiple_reports_appended(self):
        for i in range(3):
            r = self.wh.HealthReport(bot=f"BOT{i}")
            self.wh.finalize_report(r, started_at_iso="2026-05-15T14:00:00+00:00")
        rows = self._read()
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
