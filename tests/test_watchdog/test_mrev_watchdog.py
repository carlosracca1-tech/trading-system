"""
Tests para mrev_watchdog — Task 5 del plan.

Cobertura:
- TP1 dispara y sube stop a breakeven; actualiza qty y stage.
- TP1 no re-dispara si ya stage=1.
- TP2 dispara desde stage=1 y NO toca el stop.
- Stop post-breakeven cierra la posición.
- Cooldown: tras stop_loss, INSERT en mrev_cooldowns.
- Cooldown NO se registra tras TP1.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture
def seeded_mrev(tmp_path, monkeypatch):
    db = tmp_path / "mrev.db"
    monkeypatch.setenv("MREV_DB_PATH", str(db))
    monkeypatch.setenv("ALPACA_API_KEY", "fake")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake")
    monkeypatch.setenv("DRY_RUN", "false")

    for mod in ("standalone_mrev_trader", "mrev_watchdog"):
        sys.modules.pop(mod, None)
    import standalone_mrev_trader as mrev
    import mrev_watchdog as wd

    conn = mrev.get_db()
    run_id = mrev.get_or_create_run(conn)
    yield mrev, wd, conn, run_id
    conn.close()


def _insert_pos(conn, run_id, *, symbol, qty, entry, stop, stage=0, highest=None,
                entry_dt=None):
    pos_id = str(uuid.uuid4())[:8]
    conn.execute(
        """INSERT INTO mrev_positions
           (id, run_id, symbol, qty, entry_price, stop_loss, entry_dt, status,
            highest_since_entry, partial_tp_taken, initial_qty)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (pos_id, run_id, symbol, qty, entry, stop,
         (entry_dt or datetime.now(tz=timezone.utc)).isoformat(),
         "OPEN", highest or entry, stage, qty),
    )
    conn.commit()
    return pos_id


def _ap(symbol, qty, entry, current):
    return {
        "symbol": symbol.replace("/", ""),
        "qty": str(qty),
        "avg_entry_price": f"{entry:.4f}",
        "current_price": f"{current:.4f}",
    }


def test_tp1_fires_sells_50_and_raises_stop(seeded_mrev):
    mrev, wd, conn, run_id = seeded_mrev
    pos_id = _insert_pos(conn, run_id, symbol="BTC/USD", qty=0.01,
                         entry=50000.0, stop=48000.0)
    order = {"id": "o1", "status": "filled", "filled_avg_price": "52500"}
    pos = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()

    with mock.patch.object(mrev, "alpaca_submit_order", return_value=order) as m_sub, \
         mock.patch.object(wd, "fetch_crypto_atr", return_value=500.0):
        wd.process_position(conn, pos, _ap("BTC/USD", 0.01, 50000, 52500.0),
                            datetime.now(tz=timezone.utc))

    # qty * ratio = 0.01 * 0.5 = 0.005; round a 0.0001
    args, _ = m_sub.call_args
    assert args[0] == "BTC/USD"
    assert args[1] == pytest.approx(0.005, abs=1e-4)
    assert args[2] == "sell"

    r = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()
    assert r["partial_tp_taken"] == 1
    assert abs(r["stop_loss"] - 50000.0) < 0.01


def test_tp1_no_refire_stage1(seeded_mrev):
    mrev, wd, conn, run_id = seeded_mrev
    pos_id = _insert_pos(conn, run_id, symbol="ETH/USD", qty=0.1,
                         entry=3000.0, stop=3000.0, stage=1, highest=3180.0)
    pos = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()
    with mock.patch.object(mrev, "alpaca_submit_order") as m_sub, \
         mock.patch.object(wd, "fetch_crypto_atr", return_value=30.0):
        wd.process_position(conn, pos, _ap("ETH/USD", 0.1, 3000, 3180.0),
                            datetime.now(tz=timezone.utc))
    m_sub.assert_not_called()


def test_tp2_fires_from_stage1(seeded_mrev):
    mrev, wd, conn, run_id = seeded_mrev
    pos_id = _insert_pos(conn, run_id, symbol="SOL/USD", qty=10.0,
                         entry=100.0, stop=100.0, stage=1, highest=107.0)
    order = {"id": "o2", "status": "filled", "filled_avg_price": "108"}
    pos = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()
    with mock.patch.object(mrev, "alpaca_submit_order", return_value=order) as m_sub, \
         mock.patch.object(wd, "fetch_crypto_atr", return_value=1.0):
        wd.process_position(conn, pos, _ap("SOL/USD", 10.0, 100, 108.0),
                            datetime.now(tz=timezone.utc))
    m_sub.assert_called_once()
    args, _ = m_sub.call_args
    assert args[1] == pytest.approx(5.0, abs=1e-2)  # floor(10*0.5)=5

    r = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()
    assert r["partial_tp_taken"] == 2
    # Fix 2026-05-21: TP2 sube el stop a entry × (1 + TP1_pct) = 100 × 1.05 = 105 (lock TP1).
    assert abs(r["stop_loss"] - 105.0) < 0.01


def test_stop_post_breakeven_closes_and_records_cooldown(seeded_mrev):
    mrev, wd, conn, run_id = seeded_mrev
    pos_id = _insert_pos(conn, run_id, symbol="DOGE/USD", qty=1000.0,
                         entry=0.10, stop=0.10, stage=1, highest=0.105,
                         entry_dt=datetime.now(tz=timezone.utc) - timedelta(hours=2))
    # atr=0.005, stop=entry-2*atr=0.09; precio 0.089 → stop_loss
    order = {"id": "o3", "status": "filled", "filled_avg_price": "0.089"}
    pos = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()

    with mock.patch.object(mrev, "alpaca_submit_order", return_value=order) as m_sub, \
         mock.patch.object(wd, "fetch_crypto_atr", return_value=0.005):
        wd.process_position(conn, pos, _ap("DOGE/USD", 1000, 0.10, 0.089),
                            datetime.now(tz=timezone.utc))

    m_sub.assert_called_once()
    r = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()
    assert r["status"] == "CLOSED"
    assert r["qty"] == 0
    assert "stop_loss" in r["exit_reason"]

    # Cooldown registrado
    cd = conn.execute("SELECT * FROM mrev_cooldowns WHERE symbol=?", ("DOGE/USD",)).fetchone()
    assert cd is not None
    assert "stop_loss" in cd["reason"]


def test_tp1_does_not_record_cooldown(seeded_mrev):
    mrev, wd, conn, run_id = seeded_mrev
    pos_id = _insert_pos(conn, run_id, symbol="LINK/USD", qty=10.0,
                         entry=20.0, stop=19.0)
    order = {"id": "o4", "status": "filled", "filled_avg_price": "21.00"}
    pos = conn.execute("SELECT * FROM mrev_positions WHERE id=?", (pos_id,)).fetchone()

    with mock.patch.object(mrev, "alpaca_submit_order", return_value=order), \
         mock.patch.object(wd, "fetch_crypto_atr", return_value=0.2):
        wd.process_position(conn, pos, _ap("LINK/USD", 10.0, 20.0, 21.0),
                            datetime.now(tz=timezone.utc))

    cd = conn.execute("SELECT * FROM mrev_cooldowns WHERE symbol=?", ("LINK/USD",)).fetchone()
    assert cd is None, "TP1 no debería registrar cooldown"


def test_is_cooldown_reason_classifies_correctly():
    sys.modules.pop("mrev_watchdog", None)
    import mrev_watchdog as wd
    assert wd._is_cooldown_reason("stop_loss (close=...)") is True
    assert wd._is_cooldown_reason("trailing_stop (...)") is True
    assert wd._is_cooldown_reason("time_stop (120h)") is True
    assert wd._is_cooldown_reason("take_profit (close≥sma+1.5atr)") is False
    assert wd._is_cooldown_reason("partial_tp1_5.0pct:5.00%") is False
