"""Tests para _cooldowns — F1 anti-whipsaw.

Verifica:
- ensure_cooldown_table: crea schema + ALTER idempotente para
  last_exit_price (compat con tablas mrev_cooldowns viejas).
- record_cooldown: UPSERT idempotente.
- check_cooldown: combinación temporal + price.

Stdlib only — sin pytest.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _cooldowns import (  # noqa: E402
    CooldownDecision,
    check_cooldown,
    ensure_cooldown_table,
    record_cooldown,
)


def _open_db() -> sqlite3.Connection:
    """Memory DB con row_factory para que las queries devuelvan dict-like."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class TableSetupTests(unittest.TestCase):
    def test_create_rftm_table_fresh(self):
        conn = _open_db()
        ensure_cooldown_table(conn, "rftm_cooldowns")
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(rftm_cooldowns)").fetchall()
        ]
        self.assertEqual(
            set(cols), {"symbol", "last_exit_dt", "last_exit_price", "reason"}
        )

    def test_create_mrev_table_fresh(self):
        conn = _open_db()
        ensure_cooldown_table(conn, "mrev_cooldowns")
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(mrev_cooldowns)").fetchall()
        ]
        self.assertIn("last_exit_price", cols)

    def test_alter_legacy_table_adds_price_column(self):
        """Schema viejo de MREV sin last_exit_price — debe migrar idempotente."""
        conn = _open_db()
        conn.execute("""CREATE TABLE mrev_cooldowns (
            symbol TEXT PRIMARY KEY,
            last_exit_dt TEXT NOT NULL,
            reason TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO mrev_cooldowns VALUES (?,?,?)",
            ("BTC/USD", "2026-05-10T12:00:00+00:00", "stop_loss"),
        )
        conn.commit()
        # Migración
        ensure_cooldown_table(conn, "mrev_cooldowns")
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(mrev_cooldowns)").fetchall()
        ]
        self.assertIn("last_exit_price", cols)
        # Row vieja preservada con NULL en price
        row = conn.execute(
            "SELECT * FROM mrev_cooldowns WHERE symbol='BTC/USD'"
        ).fetchone()
        self.assertEqual(row["reason"], "stop_loss")
        self.assertIsNone(row["last_exit_price"])

    def test_unsafe_table_name_rejected(self):
        conn = _open_db()
        with self.assertRaises(ValueError):
            ensure_cooldown_table(conn, "foo; DROP TABLE bar")
        with self.assertRaises(ValueError):
            record_cooldown(conn, "evil_table", "SPY", 100.0, "x")
        with self.assertRaises(ValueError):
            check_cooldown(
                conn,
                "evil_table",
                "SPY",
                100.0,
                temporal_window=5,
                temporal_unit="business_days",
                max_runup=0.1,
            )


class RecordAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.conn = _open_db()
        ensure_cooldown_table(self.conn, "rftm_cooldowns")
        self.now = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)  # viernes

    def test_no_row_means_no_cooldown(self):
        d = check_cooldown(
            self.conn,
            "rftm_cooldowns",
            "QQQ",
            entry_price=500,
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        self.assertFalse(d.blocked)

    def test_record_upsert(self):
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 480.0, "E3_stop_loss",
            now=self.now - timedelta(days=1),
        )
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 470.0, "E5_trailing",
            now=self.now,
        )
        # El INSERT OR REPLACE debe haber actualizado, no duplicado
        rows = self.conn.execute(
            "SELECT * FROM rftm_cooldowns WHERE symbol='QQQ'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "E5_trailing")
        self.assertEqual(rows[0]["last_exit_price"], 470.0)

    def test_temporal_blocks_fresh_exit(self):
        # Exit hace 1 día hábil, ventana = 5
        last = self.now - timedelta(days=1)
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 500.0, "E3_stop_loss", now=last
        )
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=505,  # poco runup, no debería gatillar precio
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        self.assertTrue(d.blocked)
        self.assertEqual(d.kind, "time")
        self.assertIn("cooldown_time", d.reason)
        self.assertIsNotNone(d.days_since_exit)

    def test_temporal_expires_allows_entry(self):
        # Exit hace 10 días, ventana = 5
        last = self.now - timedelta(days=10)
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 500.0, "E3_stop_loss", now=last
        )
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=505,
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        self.assertFalse(d.blocked)
        self.assertGreater(d.days_since_exit or 0, 5)

    def test_price_blocks_even_when_temporal_expired(self):
        # Exit hace 30 días, ventana = 5, pero precio actual +20% > 10%
        last = self.now - timedelta(days=30)
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 500.0, "E3_stop_loss", now=last
        )
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=600,  # +20% vs 500
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        self.assertTrue(d.blocked)
        self.assertEqual(d.kind, "price")
        self.assertAlmostEqual(d.runup_pct or 0, 0.20, places=4)
        self.assertIn("cooldown_price", d.reason)
        self.assertEqual(d.last_exit_price, 500.0)

    def test_price_within_threshold_allows_entry(self):
        # Exit hace 30 días, precio +8% < 10%
        last = self.now - timedelta(days=30)
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 500.0, "E3_stop_loss", now=last
        )
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=540,  # +8%
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        self.assertFalse(d.blocked)

    def test_hours_unit_for_mrev(self):
        ensure_cooldown_table(self.conn, "mrev_cooldowns")
        # Exit hace 3h, ventana = 6h
        last = self.now - timedelta(hours=3)
        record_cooldown(
            self.conn, "mrev_cooldowns", "BTC/USD", 50000.0, "stop_loss",
            now=last,
        )
        d = check_cooldown(
            self.conn, "mrev_cooldowns", "BTC/USD",
            entry_price=50500,  # +1%
            temporal_window=6,
            temporal_unit="hours",
            max_runup=0.10,
            now=self.now,
        )
        self.assertTrue(d.blocked)
        self.assertEqual(d.kind, "time")
        self.assertIn("h_left", d.reason)

    def test_zero_or_negative_exit_price_skips_price_check(self):
        # Datos viejos sin last_exit_price (LEGACY)
        self.conn.execute(
            "INSERT INTO rftm_cooldowns (symbol, last_exit_dt, last_exit_price, reason) "
            "VALUES (?, ?, ?, ?)",
            ("QQQ", (self.now - timedelta(days=30)).isoformat(), None, "old_data"),
        )
        self.conn.commit()
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=1000,  # cualquier precio
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=self.now,
        )
        # Sin last_exit_price, el cooldown de precio no aplica; el
        # temporal ya expiró → permitido
        self.assertFalse(d.blocked)

    def test_business_days_skip_weekend(self):
        # Exit el viernes a las 14h, ahora es lunes 14h. Eso es ~3 días
        # calendar (Fri→Sat→Sun→Mon) pero ~1 día hábil (Mon).
        fri = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)  # viernes
        mon = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)  # lunes
        record_cooldown(
            self.conn, "rftm_cooldowns", "QQQ", 500.0, "E3_stop_loss", now=fri
        )
        d = check_cooldown(
            self.conn, "rftm_cooldowns", "QQQ",
            entry_price=505,
            temporal_window=5,
            temporal_unit="business_days",
            max_runup=0.10,
            now=mon,
        )
        # Tienen que pasar 5 BD; sólo pasó ~1. Aún bloqueado.
        self.assertTrue(d.blocked)
        self.assertEqual(d.kind, "time")


if __name__ == "__main__":
    unittest.main()
