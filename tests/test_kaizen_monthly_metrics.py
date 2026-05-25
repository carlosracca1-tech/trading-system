"""Tests para _kaizen_monthly_metrics — F6.5."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _kaizen_monthly_metrics import (  # noqa: E402
    compute_month_aggregate,
    current_month_str,
    ensure_table,
    get_rule_history,
    rules_needing_decision,
    snapshot_monthly_metrics,
)
from _shadow_trades import ensure_table as ensure_shadow_table  # noqa: E402


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    ensure_shadow_table(conn)
    return conn


def _insert_shadow(conn, sid, rule_id, exit_dt, pnl):
    """Helper para insertar un shadow cerrado en un mes específico."""
    conn.execute(
        """INSERT INTO kaizen_shadow_trades
           (id, rule_id, bot, symbol, entry_dt_utc, entry_price,
            qty_simulated, stop_loss, status, pnl_simulated,
            exit_dt_utc, created_at)
           VALUES (?,?,?,?,?,100,10,95,?,?,?,?)""",
        (sid, rule_id, "RFTM", "SPY", exit_dt, "closed",
         pnl, exit_dt, exit_dt),
    )
    conn.commit()


class ComputeAggregateTests(unittest.TestCase):
    def test_filters_by_month(self):
        conn = _open_db()
        _insert_shadow(conn, "a1", "K_A", "2026-05-10T12:00:00+00:00", -100)
        _insert_shadow(conn, "a2", "K_A", "2026-05-15T12:00:00+00:00", -50)
        _insert_shadow(conn, "a3", "K_A", "2026-04-15T12:00:00+00:00", -200)
        agg = compute_month_aggregate(conn, month="2026-05")
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["n_shadows_closed"], 2)
        self.assertEqual(agg[0]["gross_saved_usd"], 150)

    def test_winners_vs_losers(self):
        conn = _open_db()
        _insert_shadow(conn, "a1", "K_A", "2026-05-10T12:00:00+00:00", -100)
        _insert_shadow(conn, "a2", "K_A", "2026-05-11T12:00:00+00:00", 50)
        agg = compute_month_aggregate(conn, month="2026-05")
        a = agg[0]
        self.assertEqual(a["n_shadows_winners"], 1)
        self.assertEqual(a["n_shadows_losers"], 1)
        self.assertEqual(a["gross_saved_usd"], 100)
        self.assertEqual(a["gross_missed_usd"], 50)
        self.assertEqual(a["net_impact_usd"], 50)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_upserts(self):
        conn = _open_db()
        _insert_shadow(conn, "a1", "K_A", "2026-05-10T12:00:00+00:00", -100)
        n = snapshot_monthly_metrics(conn, month="2026-05",
                                      n_blocks_by_rule={"K_A": 5})
        self.assertEqual(n, 1)
        rows = conn.execute(
            "SELECT * FROM kaizen_monthly_metrics WHERE month='2026-05'"
        ).fetchall()
        self.assertEqual(rows[0]["rule_id"], "K_A")
        self.assertEqual(rows[0]["n_blocks"], 5)
        self.assertEqual(rows[0]["gross_saved_usd"], 100)

    def test_rerun_overwrites(self):
        conn = _open_db()
        _insert_shadow(conn, "a1", "K_A", "2026-05-10T12:00:00+00:00", -100)
        snapshot_monthly_metrics(conn, month="2026-05",
                                  n_blocks_by_rule={"K_A": 3})
        # Otro shadow cerró después
        _insert_shadow(conn, "a2", "K_A", "2026-05-20T12:00:00+00:00", -50)
        snapshot_monthly_metrics(conn, month="2026-05",
                                  n_blocks_by_rule={"K_A": 7})
        rows = conn.execute(
            "SELECT * FROM kaizen_monthly_metrics WHERE month='2026-05'"
        ).fetchall()
        # Solo 1 fila — UPSERT
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n_blocks"], 7)
        self.assertEqual(rows[0]["gross_saved_usd"], 150)


class RulesNeedingDecisionTests(unittest.TestCase):
    def test_2_consecutive_negatives_with_blocks(self):
        conn = _open_db()
        # K_BAD: 2 meses negativos consecutivos con >=10 blocks
        conn.execute("""INSERT INTO kaizen_monthly_metrics
            (month, rule_id, n_blocks, net_impact_usd)
            VALUES ('2026-04', 'K_BAD', 15, -200)""")
        conn.execute("""INSERT INTO kaizen_monthly_metrics
            (month, rule_id, n_blocks, net_impact_usd)
            VALUES ('2026-05', 'K_BAD', 12, -100)""")
        # K_OK: solo 1 mes negativo
        conn.execute("""INSERT INTO kaizen_monthly_metrics
            (month, rule_id, n_blocks, net_impact_usd)
            VALUES ('2026-05', 'K_OK', 15, -50)""")
        # K_LOW_N: 2 meses negativos pero pocos blocks
        conn.execute("""INSERT INTO kaizen_monthly_metrics
            (month, rule_id, n_blocks, net_impact_usd)
            VALUES ('2026-04', 'K_LOW_N', 3, -100)""")
        conn.execute("""INSERT INTO kaizen_monthly_metrics
            (month, rule_id, n_blocks, net_impact_usd)
            VALUES ('2026-05', 'K_LOW_N', 4, -100)""")
        conn.commit()
        flagged = rules_needing_decision(conn, n_blocks_min=10)
        self.assertIn("K_BAD", flagged)
        self.assertNotIn("K_OK", flagged)
        self.assertNotIn("K_LOW_N", flagged)


class HistoryTests(unittest.TestCase):
    def test_get_rule_history_ordered_desc(self):
        conn = _open_db()
        for m, net in [("2026-03", 100), ("2026-04", -50), ("2026-05", 30)]:
            conn.execute("""INSERT INTO kaizen_monthly_metrics
                (month, rule_id, net_impact_usd) VALUES (?, ?, ?)""",
                (m, "K_X", net))
        conn.commit()
        hist = get_rule_history(conn, "K_X", n_months=3)
        self.assertEqual([h["month"] for h in hist],
                         ["2026-05", "2026-04", "2026-03"])


if __name__ == "__main__":
    unittest.main()
