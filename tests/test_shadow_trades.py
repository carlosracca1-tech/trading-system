"""Tests para _shadow_trades — F6.1."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _shadow_trades import (  # noqa: E402
    ShadowTradeParams,
    SLIPPAGE_PCT,
    aggregate_by_rule,
    apply_tick_to_db,
    create_shadow_trade,
    ensure_table,
    tick_shadow_trade,
)


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    return conn


class CreateTests(unittest.TestCase):
    def test_creates_running_with_tps(self):
        conn = _open_db()
        sid = create_shadow_trade(conn, ShadowTradeParams(
            bot="RFTM", symbol="SPY", rule_id="K_x",
            entry_price=100.0, qty_simulated=10, stop_loss=95.0,
            atr_at_entry=2.0,
        ))
        row = conn.execute(
            "SELECT * FROM kaizen_shadow_trades WHERE id=?", (sid,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertAlmostEqual(row["tp1_price"], 105.0)
        self.assertAlmostEqual(row["tp2_price"], 107.5)
        self.assertEqual(row["partial_tp_stage"], 0)
        self.assertEqual(row["highest_since_entry"], 100.0)


class TickTests(unittest.TestCase):
    def setUp(self):
        self.conn = _open_db()
        self.sid = create_shadow_trade(self.conn, ShadowTradeParams(
            bot="RFTM", symbol="SPY", rule_id="K_x",
            entry_price=100.0, qty_simulated=10, stop_loss=95.0,
            atr_at_entry=2.0,
        ))
        self.row = self.conn.execute(
            "SELECT * FROM kaizen_shadow_trades WHERE id=?", (self.sid,)
        ).fetchone()

    def _refresh(self):
        return self.conn.execute(
            "SELECT * FROM kaizen_shadow_trades WHERE id=?", (self.sid,)
        ).fetchone()

    def test_stop_hit_closes_with_negative_pnl(self):
        # bar low 94 < stop 95 → hit
        res = tick_shadow_trade(self.row, current_price=94.5,
                                low=94.0, high=95.0)
        self.assertTrue(res.closed)
        self.assertEqual(res.exit_reason, "stop_loss")
        # pnl = (94.5*(1-slip) - 100) * 10 (todo qty_init)
        # ≈ (94.45 - 100) * 10 ≈ -55.5
        # Pero el cálculo usa min(stop=95, current=94.5) = 94.5 con slip
        expected_exit = 94.5 * (1 - SLIPPAGE_PCT)
        expected_pnl = (expected_exit - 100) * 10
        self.assertAlmostEqual(res.pnl, expected_pnl, places=2)

    def test_tp1_promotes_stage(self):
        # bar high 105.5 ≥ tp1 105 → stage 0→1
        res = tick_shadow_trade(self.row, current_price=105.5,
                                low=104.0, high=105.5)
        self.assertFalse(res.closed)
        self.assertEqual(res.new_stage, 1)

    def test_after_tp1_stop_moves_to_breakeven(self):
        # Aplicamos tick TP1 a la DB
        res = tick_shadow_trade(self.row, current_price=105.5,
                                low=104.0, high=105.5)
        apply_tick_to_db(self.conn, self.row, res, current_price=105.5)
        row2 = self._refresh()
        self.assertEqual(row2["partial_tp_stage"], 1)
        self.assertEqual(row2["stop_loss"], 100.0)  # breakeven

    def test_tp2_promotes_to_stage_2(self):
        # primero TP1
        res = tick_shadow_trade(self.row, current_price=105.5, high=105.5, low=104)
        apply_tick_to_db(self.conn, self.row, res, current_price=105.5)
        # ahora TP2
        row2 = self._refresh()
        res2 = tick_shadow_trade(row2, current_price=108.0, high=108.0, low=106.0)
        self.assertEqual(res2.new_stage, 2)

    def test_time_stop_closes_after_20_days(self):
        # Insertar shadow con entry_dt hace 21 días
        old = (datetime.now(tz=timezone.utc) - timedelta(days=21)).isoformat()
        self.conn.execute(
            "UPDATE kaizen_shadow_trades SET entry_dt_utc=? WHERE id=?",
            (old, self.sid),
        )
        row2 = self._refresh()
        res = tick_shadow_trade(row2, current_price=98.0, low=97.0, high=99.0)
        self.assertTrue(res.closed)
        self.assertEqual(res.exit_reason, "time_stop")

    def test_no_change_when_price_in_range(self):
        # current entre stop y tp1, primer tick
        res = tick_shadow_trade(self.row, current_price=101.0,
                                low=100.5, high=101.5)
        self.assertFalse(res.closed)
        self.assertIsNone(res.new_stage)


class AggregationTests(unittest.TestCase):
    def test_aggregate_by_rule(self):
        conn = _open_db()
        now = datetime.now(tz=timezone.utc).isoformat()
        # 3 shadows cerrados, 2 perdedores (= regla ahorró), 1 ganador
        # Insertamos directamente
        for i, (rule, pnl) in enumerate([
            ("K_A", -100), ("K_A", -50), ("K_A", 80),
            ("K_B", -200),
        ]):
            conn.execute(
                """INSERT INTO kaizen_shadow_trades
                   (id, rule_id, bot, symbol, entry_dt_utc, entry_price,
                    qty_simulated, stop_loss, status, pnl_simulated,
                    created_at)
                   VALUES (?,?,?,?,?,100,10,95,?,?,?)""",
                (f"sid{i}", rule, "RFTM", "SPY", now, "closed", pnl, now),
            )
        conn.commit()

        agg = aggregate_by_rule(conn)
        by_rid = {a["rule_id"]: a for a in agg}
        # K_A: gross_saved=150, gross_missed=80, net=70
        ka = by_rid["K_A"]
        self.assertEqual(ka["n_shadows_closed"], 3)
        self.assertEqual(ka["n_winners"], 1)
        self.assertEqual(ka["n_losers"], 2)
        self.assertAlmostEqual(ka["gross_saved_usd"], 150)
        self.assertAlmostEqual(ka["gross_missed_usd"], 80)
        self.assertAlmostEqual(ka["net_impact_usd"], 70)
        # win_rate = 1/3 (que la regla fue MALA en 1/3)
        self.assertAlmostEqual(ka["win_rate"], 1/3, places=3)

        kb = by_rid["K_B"]
        self.assertEqual(kb["n_winners"], 0)
        self.assertAlmostEqual(kb["net_impact_usd"], 200)


if __name__ == "__main__":
    unittest.main()
