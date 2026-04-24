"""
Schema invariants — protegen contra otro Fix A (INSERT placeholders != columnas).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from _db_health import MREV_REQUIRED_COLUMNS, RFTM_REQUIRED_COLUMNS


REPO = Path(__file__).resolve().parents[1]


def test_mrev_positions_schema_matches_required_columns():
    """Las columnas que los scripts esperan (RUN_COLS) existen tras init_db()."""
    import sys
    sys.path.insert(0, str(REPO))
    import importlib
    import os

    # Tmp DB
    import tempfile
    tmp = tempfile.mkdtemp()
    os.environ["MREV_DB_PATH"] = str(Path(tmp) / "mrev.db")
    if "standalone_mrev_trader" in sys.modules:
        importlib.reload(sys.modules["standalone_mrev_trader"])
    import standalone_mrev_trader as m
    c = m.get_db()
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(mrev_positions)")}
        for expected in MREV_REQUIRED_COLUMNS["mrev_positions"]:
            assert expected in cols, f"mrev_positions missing {expected}"
    finally:
        c.close()


def test_positions_schema_matches_required_columns():
    import sys
    sys.path.insert(0, str(REPO))
    import importlib
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    os.environ["TMPDIR"] = tmp
    if "standalone_paper_trader" in sys.modules:
        importlib.reload(sys.modules["standalone_paper_trader"])
    import standalone_paper_trader as r
    r.init_db()
    with r.get_db() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(positions)")}
    for expected in RFTM_REQUIRED_COLUMNS["positions"]:
        assert expected in cols, f"positions missing {expected}"


def test_mrev_cooldowns_table_exists():
    """Tabla de cooldown introducida en Task 3."""
    import sys
    sys.path.insert(0, str(REPO))
    import importlib
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    os.environ["MREV_DB_PATH"] = str(Path(tmp) / "mrev.db")
    if "standalone_mrev_trader" in sys.modules:
        importlib.reload(sys.modules["standalone_mrev_trader"])
    import standalone_mrev_trader as m
    c = m.get_db()
    try:
        r = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mrev_cooldowns'"
        ).fetchone()
        assert r is not None
    finally:
        c.close()
