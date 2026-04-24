"""
Tests for _db_health.assert_db_health — Fix D.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _db_health import (
    DBHealthError,
    MREV_REQUIRED_COLUMNS,
    RFTM_REQUIRED_COLUMNS,
    assert_db_health,
)


def _build_mrev_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE mrev_runs (
            id TEXT PRIMARY KEY, started_at TEXT, initial_capital REAL,
            status TEXT DEFAULT 'RUNNING'
        );
        CREATE TABLE mrev_positions (
            id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL,
            entry_price REAL, stop_loss REAL, entry_dt TEXT,
            status TEXT DEFAULT 'OPEN', exit_price REAL, exit_dt TEXT,
            pnl REAL, exit_reason TEXT,
            highest_since_entry REAL DEFAULT 0.0,
            partial_tp_taken INTEGER DEFAULT 0,
            initial_qty REAL
        );
        """
    )
    conn.commit()
    conn.close()


def test_missing_file_is_warn_not_error(tmp_path):
    missing = tmp_path / "nope.db"
    # strict_missing=False (default) — no raise
    report = assert_db_health(str(missing), required_columns={"foo": {"id"}})
    assert report["existed"] is False


def test_missing_file_strict_raises(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(DBHealthError):
        assert_db_health(str(missing), strict_missing=True)


def test_healthy_mrev_db(tmp_path):
    db = tmp_path / "mrev.db"
    _build_mrev_db(str(db))
    report = assert_db_health(
        str(db),
        required_columns=MREV_REQUIRED_COLUMNS,
        open_run_table="mrev_runs",
        open_run_value="RUNNING",
    )
    assert report["integrity_check"] == "ok"
    assert report["tables"]["mrev_positions"]["columns"] >= 15
    assert report["closed_stale_runs"] == 0


def test_schema_drift_detected(tmp_path):
    """Si falta initial_qty (caso real: DB vieja pre-migration), raise."""
    db = tmp_path / "drift.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE mrev_positions (
            id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL,
            entry_price REAL, stop_loss REAL, entry_dt TEXT, status TEXT,
            exit_price REAL, exit_dt TEXT, pnl REAL, exit_reason TEXT,
            highest_since_entry REAL, partial_tp_taken INTEGER
        )"""
    )
    conn.commit()
    conn.close()
    with pytest.raises(DBHealthError, match="initial_qty"):
        assert_db_health(str(db), required_columns=MREV_REQUIRED_COLUMNS)


def test_missing_table_raises(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    with pytest.raises(DBHealthError, match="Table missing"):
        assert_db_health(str(db), required_columns=MREV_REQUIRED_COLUMNS)


def test_closes_stale_open_runs(tmp_path):
    db = tmp_path / "stale.db"
    _build_mrev_db(str(db))
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO mrev_runs (id, started_at, initial_capital, status) VALUES (?, ?, ?, ?)",
                 ("r1", "2026-04-22T00:00:00Z", 1000.0, "RUNNING"))
    conn.execute("INSERT INTO mrev_runs (id, started_at, initial_capital, status) VALUES (?, ?, ?, ?)",
                 ("r2", "2026-04-23T00:00:00Z", 1000.0, "RUNNING"))
    conn.commit()
    conn.close()

    report = assert_db_health(
        str(db),
        required_columns=MREV_REQUIRED_COLUMNS,
        open_run_table="mrev_runs",
        open_run_value="RUNNING",
        stale_run_value="CLOSED",
    )
    assert report["closed_stale_runs"] == 1

    # El más reciente sigue RUNNING, el viejo pasa a CLOSED
    conn = sqlite3.connect(str(db))
    statuses = dict(conn.execute("SELECT id, status FROM mrev_runs").fetchall())
    conn.close()
    # El ordering en la DB es por rowid; r2 se insertó después, así que r2 sigue RUNNING
    assert statuses["r2"] == "RUNNING"
    assert statuses["r1"] == "CLOSED"


def test_rftm_required_columns_are_sane():
    assert "partial_tp_taken" in RFTM_REQUIRED_COLUMNS["positions"]
    assert "initial_qty" in RFTM_REQUIRED_COLUMNS["positions"]
    assert "highest_since_entry" in RFTM_REQUIRED_COLUMNS["positions"]


def test_rftm_db_path_default_is_next_to_script(monkeypatch):
    """Default DB_PATH debe vivir junto al script (alineado con cache de CI)."""
    import importlib
    import sys

    monkeypatch.delenv("RFTM_DB_PATH", raising=False)
    sys.modules.pop("standalone_paper_trader", None)
    rftm = importlib.import_module("standalone_paper_trader")

    expected = Path(rftm.__file__).parent / "trading_paper.db"
    assert rftm.DB_PATH == expected


def test_rftm_db_path_honors_env_var(monkeypatch, tmp_path):
    """RFTM_DB_PATH override debe ganar al default."""
    import importlib
    import sys

    custom = tmp_path / "custom.db"
    monkeypatch.setenv("RFTM_DB_PATH", str(custom))
    sys.modules.pop("standalone_paper_trader", None)
    rftm = importlib.import_module("standalone_paper_trader")

    assert rftm.DB_PATH == custom
    # parent dir queda creado para que sqlite pueda abrir el archivo
    assert custom.parent.exists()
