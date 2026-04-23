"""
_db_health — Health check para las DBs de los bots.

Se llama al inicio de cualquier script (entry bots, watchdogs, auditoría)
para detectar drift de schema, integridad de SQLite y runs abiertos huérfanos
antes de que un bug los cause.

Uso típico:

    from _db_health import assert_db_health

    assert_db_health(
        db_path="mrev_paper.db",
        required_columns={
            "mrev_positions": {"id", "run_id", "symbol", "qty", "entry_price",
                               "stop_loss", "entry_dt", "status",
                               "highest_since_entry", "partial_tp_taken",
                               "initial_qty"},
        },
        open_run_table="mrev_runs",
    )

Principio: si la DB está rota, mejor abortar temprano con un mensaje claro
que seguir y ensuciar más el estado.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterable


class DBHealthError(RuntimeError):
    """Raised when the DB is in a state the code cannot safely proceed with."""


def assert_db_health(
    db_path: str,
    *,
    required_columns: dict[str, Iterable[str]] | None = None,
    open_run_table: str | None = None,
    open_run_column: str = "status",
    open_run_id_column: str = "id",
    open_run_value: str = "open",
    stale_run_value: str = "closed",
    strict_missing: bool = False,
) -> dict:
    """
    Run a set of health checks on a SQLite DB.

    - required_columns: mapping of table → iterable of column names que el
      código espera. Si falta alguna, lanza DBHealthError.
    - open_run_table: si se pasa, cerramos (status='closed') runs viejos
      si hay más de uno con status='open'. El más reciente se deja.
    - strict_missing: si True y el archivo no existe, lanza error. Si False
      (default), solo warn — el bot mismo creará el schema.

    Devuelve un dict con métricas útiles (para logging).
    """
    report: dict = {"db_path": db_path, "existed": os.path.exists(db_path),
                    "tables": {}, "closed_stale_runs": 0}

    if not report["existed"]:
        if strict_missing:
            raise DBHealthError(f"DB file missing: {db_path}")
        # No existe: el bot lo creará. Nada más que chequear.
        return report

    conn = sqlite3.connect(db_path)
    try:
        # 1. PRAGMA integrity_check
        row = conn.execute("PRAGMA integrity_check").fetchone()
        integ = row[0] if row else "unknown"
        report["integrity_check"] = integ
        if integ != "ok":
            raise DBHealthError(f"integrity_check failed: {integ}")

        # 2. Column presence por tabla
        for tbl, wanted in (required_columns or {}).items():
            cols_rows = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            if not cols_rows:
                raise DBHealthError(
                    f"Table missing: {tbl} — schema drift. Re-run init_db or restore cache."
                )
            actual = {r[1] for r in cols_rows}
            missing = set(wanted) - actual
            if missing:
                raise DBHealthError(
                    f"Schema drift on {tbl}: missing columns {sorted(missing)}. "
                    f"Run the ALTER TABLE migrations in init_db."
                )
            report["tables"][tbl] = {"columns": len(actual)}

        # 3. Open runs — si hay más de uno, cerrar los viejos
        if open_run_table:
            try:
                rows = conn.execute(
                    f"SELECT {open_run_id_column} FROM {open_run_table} "
                    f"WHERE {open_run_column}=? "
                    f"ORDER BY rowid DESC",
                    (open_run_value,),
                ).fetchall()
            except sqlite3.OperationalError:
                # Tabla no tiene esas columnas — skippear
                rows = []
            if len(rows) > 1:
                stale_ids = [r[0] for r in rows[1:]]
                placeholders = ",".join("?" * len(stale_ids))
                conn.execute(
                    f"UPDATE {open_run_table} SET {open_run_column}=? "
                    f"WHERE {open_run_id_column} IN ({placeholders})",
                    (stale_run_value, *stale_ids),
                )
                conn.commit()
                report["closed_stale_runs"] = len(stale_ids)

        return report
    finally:
        conn.close()


# Schemas canónicos — los bots pasan estos al invocar assert_db_health.
RFTM_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "positions": {
        "id", "run_id", "symbol", "status", "qty", "entry_price", "stop_loss",
        "exit_price", "realized_pnl", "unrealized_pnl", "close_reason",
        "opened_at", "closed_at", "highest_since_entry",
        "partial_tp_taken", "initial_qty",
    },
}

MREV_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "mrev_positions": {
        "id", "run_id", "symbol", "qty", "entry_price", "stop_loss",
        "entry_dt", "status", "exit_price", "exit_dt", "pnl", "exit_reason",
        "highest_since_entry", "partial_tp_taken", "initial_qty",
    },
}
