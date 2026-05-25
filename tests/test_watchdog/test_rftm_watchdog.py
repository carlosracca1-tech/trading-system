"""
Tests para rftm_watchdog — Task 4 del plan watchdog.

Estrategia: mockear Alpaca (alpaca_submit_order / alpaca_get_positions /
_alpaca_request) y sembrar la DB con posiciones conocidas. Verificar que:

- TP1 dispara a +5%, vende 50% y sube stop a breakeven.
- TP1 no re-dispara si ya stage=1.
- TP2 dispara a +7.5% desde stage=1.
- Stop loss post-breakeven: vende el remanente si close ≤ entry.
- Idempotencia: dos runs seguidos con el mismo snapshot no duplican ventas.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture
def seeded_rftm(tmp_path, monkeypatch):
    """Carga standalone_paper_trader contra una DB fresca + mocks Alpaca."""
    # Apuntar RFTM_DB_PATH a un tmp_path controlado
    monkeypatch.setenv("RFTM_DB_PATH", str(tmp_path / "trading_paper.db"))
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("DRY_RUN", "false")  # queremos probar el flujo de fill

    for mod in ("standalone_paper_trader", "rftm_watchdog"):
        sys.modules.pop(mod, None)
    import standalone_paper_trader as rftm
    import rftm_watchdog as wd

    rftm.init_db()
    # Crear un run
    run_id = rftm.create_run()
    yield rftm, wd, run_id


def _insert_pos(rftm, run_id, *, symbol, qty, entry, stop, stage=0, highest=None):
    pos_id = str(uuid.uuid4())
    with rftm.get_db() as db:
        db.execute(
            """INSERT INTO positions
               (id, run_id, symbol, status, qty, entry_price, stop_loss,
                highest_since_entry, partial_tp_taken, initial_qty, opened_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pos_id, run_id, symbol, "open", qty, entry, stop,
             highest if highest is not None else entry,
             stage, qty, datetime.now(tz=timezone.utc).isoformat()),
        )
    return pos_id


def _fake_alpaca_pos(symbol: str, qty: int, entry: float, current: float) -> dict:
    return {
        "symbol": symbol, "qty": str(qty),
        "avg_entry_price": f"{entry:.4f}",
        "current_price": f"{current:.4f}",
    }


def test_tp1_fires_sells_50_raises_stop_to_entry(seeded_rftm):
    rftm, wd, run_id = seeded_rftm
    pos_id = _insert_pos(rftm, run_id, symbol="SPY", qty=10, entry=100.0, stop=95.0)

    # +5% → 105.0 dispara TP1
    alpaca_pos = _fake_alpaca_pos("SPY", 10, 100.0, 105.0)
    order_result = {"id": "o1", "status": "filled", "filled_avg_price": "105.00"}

    with mock.patch.object(rftm, "alpaca_submit_order", return_value=order_result) as m_submit, \
         mock.patch.object(wd, "fetch_atr14", return_value=(2.0, 1)):
        row = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row, alpaca_pos)

    m_submit.assert_called_once_with("SPY", 5, "sell")

    # DB: qty=5, stage=1, stop=100.0 (breakeven)
    with rftm.get_db() as db:
        r = db.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    assert r["qty"] == 5
    assert r["partial_tp_taken"] == 1
    assert abs(r["stop_loss"] - 100.0) < 0.001


def test_tp1_does_not_refire_on_stage1(seeded_rftm):
    rftm, wd, run_id = seeded_rftm
    # Ya stage=1 (TP1 se ejecutó previamente)
    pos_id = _insert_pos(rftm, run_id, symbol="QQQ", qty=5, entry=100.0, stop=100.0,
                         stage=1, highest=106.0)
    alpaca_pos = _fake_alpaca_pos("QQQ", 5, 100.0, 106.0)  # +6%, bajo TP2 (7.5%)

    with mock.patch.object(rftm, "alpaca_submit_order") as m_submit, \
         mock.patch.object(wd, "fetch_atr14", return_value=(1.5, 0)):
        row = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row, alpaca_pos)
    m_submit.assert_not_called()


def test_tp2_fires_from_stage1(seeded_rftm):
    rftm, wd, run_id = seeded_rftm
    pos_id = _insert_pos(rftm, run_id, symbol="IWM", qty=8, entry=100.0, stop=100.0,
                         stage=1, highest=107.0)
    alpaca_pos = _fake_alpaca_pos("IWM", 8, 100.0, 108.0)
    order_result = {"id": "o2", "status": "filled", "filled_avg_price": "108.00"}

    with mock.patch.object(rftm, "alpaca_submit_order", return_value=order_result) as m_submit, \
         mock.patch.object(wd, "fetch_atr14", return_value=(1.5, 0)):
        row = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row, alpaca_pos)

    m_submit.assert_called_once_with("IWM", 4, "sell")  # floor(8*0.5)=4
    with rftm.get_db() as db:
        r = db.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    assert r["qty"] == 4
    assert r["partial_tp_taken"] == 2
    # Fix 2026-05-21: TP2 sube el stop a entry × (1 + TP1_pct) = 100 × 1.05 = 105 (lock TP1).
    assert abs(r["stop_loss"] - 105.0) < 0.001


def test_stop_loss_after_breakeven_closes_position(seeded_rftm):
    rftm, wd, run_id = seeded_rftm
    # Stage=1, stop subido a entry=100, precio cae a 99
    pos_id = _insert_pos(rftm, run_id, symbol="DIA", qty=5, entry=100.0, stop=100.0,
                         stage=1, highest=105.0)
    alpaca_pos = _fake_alpaca_pos("DIA", 5, 100.0, 99.0)
    order_result = {"id": "o3", "status": "filled", "filled_avg_price": "99.00"}

    with mock.patch.object(rftm, "alpaca_submit_order", return_value=order_result) as m_submit, \
         mock.patch.object(wd, "fetch_atr14", return_value=(1.5, 2)):
        row = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row, alpaca_pos)

    m_submit.assert_called_once_with("DIA", 5, "sell")
    with rftm.get_db() as db:
        r = db.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    assert r["status"] == "closed"
    assert r["qty"] == 0
    assert "E3_stop_loss" in r["close_reason"]


def test_idempotent_two_runs_no_double_sell(seeded_rftm):
    rftm, wd, run_id = seeded_rftm
    pos_id = _insert_pos(rftm, run_id, symbol="SPY", qty=10, entry=100.0, stop=95.0)
    order_result = {"id": "o4", "status": "filled", "filled_avg_price": "105.00"}

    alpaca_pos = _fake_alpaca_pos("SPY", 10, 100.0, 105.0)
    calls = []

    def _fake_submit(sym, qty, side):
        calls.append((sym, qty, side))
        return order_result

    with mock.patch.object(rftm, "alpaca_submit_order", side_effect=_fake_submit), \
         mock.patch.object(wd, "fetch_atr14", return_value=(2.0, 1)):
        # Run 1: dispara TP1
        row = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row, alpaca_pos)

        # Run 2: mismo precio, stage ya es 1, precio aún <= 107.5 → no vuelve a vender
        alpaca_pos2 = _fake_alpaca_pos("SPY", 5, 100.0, 105.0)  # qty ajustada
        row2 = [r for r in rftm.get_db().execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,))][0]
        wd.process_position(row2, alpaca_pos2)

    assert len(calls) == 1, f"Watchdog no idempotente: {calls}"
