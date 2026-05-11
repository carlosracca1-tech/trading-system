"""
Tests del fix de DB-Alpaca desync en sync_with_alpaca.

Bug arreglado: sync_with_alpaca solo actualizaba `qty` cuando `entry_price`
difería. Si Alpaca había vendido más de lo que la DB sabía (sells manuales
o de otro proceso), la qty quedaba inflada en la DB y el watchdog
intentaba sells por más qty del que Alpaca tenía.

Caso real 2026-05-11:
  IWM   DB qty=12, Alpaca qty=2   (entry coincidía)
  SPY   DB qty=5,  Alpaca qty=1   (entry coincidía)

Estos tests parchean alpaca_get_positions / alpaca_get_account /
alpaca_get_orders_today y comprueban que la DB queda con la qty real.

Diseño: cada test es self-contained — crea su DB temporal, recarga el
módulo, ejecuta el sync, valida. No usa fixtures con monkeypatch para
ser compatible con run_tests.py.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# ─── helpers ─────────────────────────────────────────────────────────────────

def _load_rftm(tmpdir):
    os.environ["RFTM_DB_PATH"] = str(Path(tmpdir) / "rftm.db")
    os.environ["ALPACA_API_KEY"] = "test"
    os.environ["ALPACA_SECRET_KEY"] = "test"
    sys.modules.pop("standalone_paper_trader", None)
    import standalone_paper_trader as m
    m.init_db()
    return m


def _load_mrev(tmpdir):
    os.environ["MREV_DB_PATH"] = str(Path(tmpdir) / "mrev.db")
    os.environ["ALPACA_API_KEY"] = "test"
    os.environ["ALPACA_SECRET_KEY"] = "test"
    sys.modules.pop("standalone_mrev_trader", None)
    import standalone_mrev_trader as m
    # Force schema init via get_db
    _c = m.get_db(); _c.close()
    return m


def _setup_rftm_run(m):
    run_id = str(uuid.uuid4())
    with m.get_db() as conn:
        conn.execute(
            "INSERT INTO runs (id, started_at, initial_capital, cash, status) VALUES (?,?,?,?,?)",
            (run_id, datetime.now(tz=timezone.utc).isoformat(),
             100000.0, 100000.0, "running")
        )
    return run_id


def _insert_rftm_pos(m, run_id, symbol, qty, entry):
    with m.get_db() as conn:
        conn.execute(
            """INSERT INTO positions
               (id, run_id, symbol, status, qty, entry_price, stop_loss,
                unrealized_pnl, opened_at, highest_since_entry,
                partial_tp_taken, initial_qty)
               VALUES (?,?,?,?,?,?,?,0,?,?,1,?)""",
            (str(uuid.uuid4()), run_id, symbol, "open", qty, entry, entry,
             datetime.now(tz=timezone.utc).isoformat(), entry, qty * 4)
        )


def _setup_mrev_run(m):
    conn = m.get_db()
    run_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO mrev_runs (id, started_at, initial_capital, status) VALUES (?,?,?,?)",
        (run_id, datetime.now(tz=timezone.utc).isoformat(), 25000.0, "RUNNING")
    )
    conn.commit()
    return conn, run_id


def _insert_mrev_pos(conn, run_id, symbol, qty, entry):
    conn.execute(
        """INSERT INTO mrev_positions
           (id, run_id, symbol, qty, entry_price, stop_loss, entry_dt, status,
            highest_since_entry, partial_tp_taken, initial_qty)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4())[:8], run_id, symbol, qty, entry, entry * 0.95,
         datetime.now(tz=timezone.utc).isoformat(), "OPEN", entry, 1, qty * 2)
    )
    conn.commit()


# ─── RFTM tests ──────────────────────────────────────────────────────────────

def test_rftm_sync_qty_differs_entry_matches():
    """Caso real IWM: DB qty=12, Alpaca qty=2, entry coincide → DB queda en 2."""
    tmp = tempfile.mkdtemp()
    m = _load_rftm(tmp)
    run_id = _setup_rftm_run(m)
    _insert_rftm_pos(m, run_id, "IWM", qty=12, entry=259.49)

    alpaca_positions = [{
        "symbol": "IWM", "qty": "2",
        "avg_entry_price": "259.49", "current_price": "284.88",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions), \
         patch.object(m, "alpaca_get_account", return_value={"cash": "50000.0"}), \
         patch.object(m, "alpaca_get_orders_today", return_value=[]):
        m.sync_with_alpaca(run_id)

    with m.get_db() as conn:
        row = conn.execute(
            "SELECT qty, entry_price FROM positions WHERE symbol='IWM' AND status='open'"
        ).fetchone()
    assert row is not None
    assert int(row["qty"]) == 2, f"DB qty should be 2 (Alpaca truth), got {row['qty']}"
    assert abs(float(row["entry_price"]) - 259.49) < 0.01


def test_rftm_sync_entry_differs_qty_matches():
    """Path histórico: entry fix sigue funcionando."""
    tmp = tempfile.mkdtemp()
    m = _load_rftm(tmp)
    run_id = _setup_rftm_run(m)
    _insert_rftm_pos(m, run_id, "QQQ", qty=3, entry=700.0)  # entry sintético

    alpaca_positions = [{
        "symbol": "QQQ", "qty": "3",
        "avg_entry_price": "605.00", "current_price": "712.0",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions), \
         patch.object(m, "alpaca_get_account", return_value={"cash": "50000.0"}), \
         patch.object(m, "alpaca_get_orders_today", return_value=[]):
        m.sync_with_alpaca(run_id)

    with m.get_db() as conn:
        row = conn.execute(
            "SELECT qty, entry_price FROM positions WHERE symbol='QQQ' AND status='open'"
        ).fetchone()
    assert int(row["qty"]) == 3
    assert abs(float(row["entry_price"]) - 605.0) < 0.01


def test_rftm_sync_both_differ():
    """Ambos difieren → ambos se actualizan."""
    tmp = tempfile.mkdtemp()
    m = _load_rftm(tmp)
    run_id = _setup_rftm_run(m)
    _insert_rftm_pos(m, run_id, "SPY", qty=20, entry=700.0)

    alpaca_positions = [{
        "symbol": "SPY", "qty": "1",
        "avg_entry_price": "674.71", "current_price": "737.0",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions), \
         patch.object(m, "alpaca_get_account", return_value={"cash": "50000.0"}), \
         patch.object(m, "alpaca_get_orders_today", return_value=[]):
        m.sync_with_alpaca(run_id)

    with m.get_db() as conn:
        row = conn.execute(
            "SELECT qty, entry_price FROM positions WHERE symbol='SPY' AND status='open'"
        ).fetchone()
    assert int(row["qty"]) == 1
    assert abs(float(row["entry_price"]) - 674.71) < 0.01


def test_rftm_sync_no_change_when_match():
    """Todo coincide → no se toca nada."""
    tmp = tempfile.mkdtemp()
    m = _load_rftm(tmp)
    run_id = _setup_rftm_run(m)
    _insert_rftm_pos(m, run_id, "EWJ", qty=71, entry=87.38)

    alpaca_positions = [{
        "symbol": "EWJ", "qty": "71",
        "avg_entry_price": "87.38", "current_price": "92.13",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions), \
         patch.object(m, "alpaca_get_account", return_value={"cash": "50000.0"}), \
         patch.object(m, "alpaca_get_orders_today", return_value=[]):
        m.sync_with_alpaca(run_id)

    with m.get_db() as conn:
        row = conn.execute(
            "SELECT qty, entry_price FROM positions WHERE symbol='EWJ' AND status='open'"
        ).fetchone()
    assert int(row["qty"]) == 71
    assert abs(float(row["entry_price"]) - 87.38) < 0.01


# ─── MREV tests ──────────────────────────────────────────────────────────────

def test_mrev_sync_qty_differs():
    """Cripto: DB qty=100, Alpaca qty=50 → DB se actualiza a 50."""
    tmp = tempfile.mkdtemp()
    m = _load_mrev(tmp)
    conn, run_id = _setup_mrev_run(m)
    _insert_mrev_pos(conn, run_id, "BTC/USD", qty=100.0, entry=80000.0)

    alpaca_positions = [{
        "symbol": "BTCUSD", "qty": "50.0",
        "avg_entry_price": "80000.0", "current_price": "81000.0",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions):
        m.sync_with_alpaca(conn, run_id)

    row = conn.execute(
        "SELECT qty, entry_price FROM mrev_positions WHERE symbol='BTC/USD' AND status='OPEN'"
    ).fetchone()
    assert float(row["qty"]) == pytest.approx(50.0)
    assert float(row["entry_price"]) == pytest.approx(80000.0)
    conn.close()


def test_mrev_sync_entry_differs():
    tmp = tempfile.mkdtemp()
    m = _load_mrev(tmp)
    conn, run_id = _setup_mrev_run(m)
    _insert_mrev_pos(conn, run_id, "ETH/USD", qty=1.17, entry=2400.0)

    alpaca_positions = [{
        "symbol": "ETHUSD", "qty": "1.17",
        "avg_entry_price": "2242.04", "current_price": "2321.20",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions):
        m.sync_with_alpaca(conn, run_id)

    row = conn.execute(
        "SELECT qty, entry_price FROM mrev_positions WHERE symbol='ETH/USD' AND status='OPEN'"
    ).fetchone()
    assert float(row["qty"]) == pytest.approx(1.17)
    assert float(row["entry_price"]) == pytest.approx(2242.04)
    conn.close()


def test_mrev_sync_no_change_when_match():
    tmp = tempfile.mkdtemp()
    m = _load_mrev(tmp)
    conn, run_id = _setup_mrev_run(m)
    _insert_mrev_pos(conn, run_id, "SOL/USD", qty=31.708598, entry=83.21)

    alpaca_positions = [{
        "symbol": "SOLUSD", "qty": "31.708598",
        "avg_entry_price": "83.21", "current_price": "94.50",
    }]
    with patch.object(m, "alpaca_get_positions", return_value=alpaca_positions):
        m.sync_with_alpaca(conn, run_id)

    row = conn.execute(
        "SELECT qty, entry_price FROM mrev_positions WHERE symbol='SOL/USD' AND status='OPEN'"
    ).fetchone()
    assert float(row["qty"]) == pytest.approx(31.708598)
    assert float(row["entry_price"]) == pytest.approx(83.21)
    conn.close()
