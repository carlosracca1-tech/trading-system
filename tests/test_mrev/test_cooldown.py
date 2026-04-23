"""
Tests for MREV cooldown — Task 3.

Cuando el watchdog cierra una posición por stop/trailing/time, registra
el exit en mrev_cooldowns. El bot entry debe rechazar entradas sobre
ese symbol hasta que pase `MREV_COOLDOWN_HOURS` (default 6h).

No re-entry post-TP1/TP2: el cooldown SOLO se registra en exits negativos/neutros.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Fresh MREV DB per test."""
    db = tmp_path / "mrev.db"
    # Import standalone_mrev_trader con DB apuntando al tmp_path
    monkeypatch.setenv("MREV_DB_PATH", str(db))
    # Recargar el módulo para que tome la nueva env var
    if "standalone_mrev_trader" in sys.modules:
        del sys.modules["standalone_mrev_trader"]
    import standalone_mrev_trader  # noqa: F401
    c = standalone_mrev_trader.get_db()
    yield c
    c.close()


def test_cooldowns_table_exists(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mrev_cooldowns'"
    ).fetchone()
    assert row is not None


def test_record_cooldown_inserts_row(conn, monkeypatch):
    import standalone_mrev_trader as m
    m.record_cooldown(conn, "BTC/USD", "stop_loss (close=...)")
    row = conn.execute("SELECT symbol, reason FROM mrev_cooldowns WHERE symbol=?",
                       ("BTC/USD",)).fetchone()
    assert row is not None
    assert row["symbol"] == "BTC/USD"
    assert "stop_loss" in row["reason"]


def test_record_cooldown_is_idempotent(conn):
    import standalone_mrev_trader as m
    m.record_cooldown(conn, "ETH/USD", "trailing_stop")
    m.record_cooldown(conn, "ETH/USD", "stop_loss")  # overwrite
    rows = conn.execute("SELECT * FROM mrev_cooldowns WHERE symbol=?",
                        ("ETH/USD",)).fetchall()
    assert len(rows) == 1  # INSERT OR REPLACE
    assert rows[0]["reason"] == "stop_loss"


def test_cooldown_remaining_hours_no_entry(conn):
    import standalone_mrev_trader as m
    assert m._cooldown_remaining_hours(conn, "BTC/USD") == 0.0


def test_cooldown_remaining_hours_fresh_entry(conn, monkeypatch):
    import standalone_mrev_trader as m
    monkeypatch.setenv("MREV_COOLDOWN_HOURS", "6")
    m.record_cooldown(conn, "BTC/USD", "stop_loss")
    remaining = m._cooldown_remaining_hours(conn, "BTC/USD")
    # Acabamos de registrarlo, tiene que quedar ~6h (margen de 10s)
    assert 5.99 < remaining <= 6.0


def test_cooldown_remaining_hours_expired(conn, monkeypatch):
    import standalone_mrev_trader as m
    monkeypatch.setenv("MREV_COOLDOWN_HOURS", "6")
    # Forzar last_exit_dt a hace 10h
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=10)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mrev_cooldowns (symbol, last_exit_dt, reason) VALUES (?, ?, ?)",
        ("SOL/USD", past, "trailing_stop"),
    )
    conn.commit()
    assert m._cooldown_remaining_hours(conn, "SOL/USD") == 0.0


def test_cooldown_respects_env_override(conn, monkeypatch):
    import standalone_mrev_trader as m
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mrev_cooldowns (symbol, last_exit_dt, reason) VALUES (?, ?, ?)",
        ("DOGE/USD", past, "stop_loss"),
    )
    conn.commit()

    # Con 6h: 3h used → 3h restantes
    monkeypatch.setenv("MREV_COOLDOWN_HOURS", "6")
    assert 2.99 < m._cooldown_remaining_hours(conn, "DOGE/USD") <= 3.0

    # Con 2h: ya expiró
    monkeypatch.setenv("MREV_COOLDOWN_HOURS", "2")
    assert m._cooldown_remaining_hours(conn, "DOGE/USD") == 0.0
