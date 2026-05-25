"""
Tests for the MREV ENTER INSERT (Fix A — INSERT placeholders must match columns).

El bug histórico: `INSERT INTO mrev_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`
tenía 12 placeholders pero la tabla tiene 15 columnas. Ahora se usa INSERT con
columnas explícitas, y este test bloquea regresiones verificando que el INSERT
del path ENTER mencione el mismo set de columnas que PRAGMA table_info.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "standalone_mrev_trader.py"


def _schema_columns() -> list[str]:
    """Columns the code creates at runtime (CREATE + ALTERs)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE mrev_positions (
            id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL, entry_price REAL,
            stop_loss REAL, entry_dt TEXT, status TEXT DEFAULT 'OPEN',
            exit_price REAL, exit_dt TEXT, pnl REAL, exit_reason TEXT,
            highest_since_entry REAL DEFAULT 0.0
        )"""
    )
    conn.execute("ALTER TABLE mrev_positions ADD COLUMN partial_tp_taken INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE mrev_positions ADD COLUMN initial_qty REAL")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(mrev_positions)").fetchall()]
    conn.close()
    return cols


def _extract_enter_insert(source: str) -> tuple[list[str], int]:
    """Find the INSERT INTO mrev_positions near the 'BOUGHT' path."""
    # Anchor on the ENTER buy loop — no frágil dependencia de un comentario.
    anchor = source.index("for b in buys:")
    tail = source[anchor : anchor + 6000]
    match = re.search(
        r"INSERT INTO mrev_positions\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)",
        tail,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, "No explicit-column INSERT in ENTER buy loop — did Fix A regress?"
    cols = [c.strip() for c in match.group(1).split(",")]
    placeholders = [p.strip() for p in match.group(2).split(",")]
    return cols, len(placeholders)


def test_enter_insert_matches_schema():
    source = SRC.read_text()
    cols, n_placeholders = _extract_enter_insert(source)
    assert n_placeholders == len(cols), (
        f"INSERT placeholders ({n_placeholders}) != column list ({len(cols)})"
    )
    schema = _schema_columns()
    unknown = [c for c in cols if c not in schema]
    assert not unknown, f"INSERT references unknown columns: {unknown}"


def test_enter_insert_covers_stage_counter():
    """partial_tp_taken y initial_qty son columnas críticas — deben estar en el INSERT."""
    source = SRC.read_text()
    cols, _ = _extract_enter_insert(source)
    for required in ("partial_tp_taken", "initial_qty", "highest_since_entry"):
        assert required in cols, f"ENTER INSERT missing required column: {required}"
