#!/usr/bin/env python3
"""
mark_partial_tp_done.py — Marca partial_tp_taken=1 en las DBs locales (RFTM y
MREV) para todas las posiciones actualmente abiertas en Alpaca.

Correr UNA SOLA VEZ después de haber ejecutado sell_half_profits.py,
para evitar que los bots disparen un segundo partial TP sobre la misma
posición en la próxima corrida.

Uso:
    python3 mark_partial_tp_done.py --dry-run
    python3 mark_partial_tp_done.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ENV_PATH = HERE / ".env.paper"
ALPACA_URL = "https://paper-api.alpaca.markets/v2"
RFTM_DB = HERE / "trading_paper.db"
MREV_DB = HERE / "mrev_paper.db"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")


def alpaca_positions() -> list:
    req = urllib.request.Request(
        f"{ALPACA_URL}/positions",
        headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"ERROR Alpaca: {e}")
        return []


def ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except Exception:
        pass


def get_or_make_run_id(conn: sqlite3.Connection, table: str) -> str:
    cur = conn.execute(f"SELECT id FROM {table.replace('_positions','_runs') if 'mrev' in table else 'runs'} ORDER BY started_at DESC LIMIT 1"
                       if 'mrev' in table else
                       f"SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        return row[0]
    rid = str(uuid.uuid4())
    if 'mrev' in table:
        conn.execute("INSERT INTO mrev_runs (id, started_at, initial_capital, status) VALUES (?,?,?,?)",
                     (rid, datetime.now(timezone.utc).isoformat(), 100000.0, "RUNNING"))
    else:
        conn.execute("INSERT INTO runs (id, started_at, initial_capital, status, cash) VALUES (?,?,?,?,?)",
                     (rid, datetime.now(timezone.utc).isoformat(), 100000.0, "running", 0.0))
    return rid


def upsert_rftm(positions: list, dry: bool) -> int:
    conn = sqlite3.connect(str(RFTM_DB))
    conn.row_factory = sqlite3.Row
    # Crear tablas si no existen (mirror de standalone_paper_trader.py)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, started_at TEXT, initial_capital REAL,
            status TEXT DEFAULT 'running', cash REAL
        );
        CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT,
            status TEXT DEFAULT 'open', qty INTEGER, entry_price REAL,
            stop_loss REAL, exit_price REAL, realized_pnl REAL,
            unrealized_pnl REAL DEFAULT 0, close_reason TEXT,
            opened_at TEXT, closed_at TEXT,
            highest_since_entry REAL DEFAULT 0.0
        );
    """)
    # Garantizar columnas nuevas
    ensure_column(conn, "positions", "partial_tp_taken", "INTEGER DEFAULT 0")
    ensure_column(conn, "positions", "initial_qty", "INTEGER")
    ensure_column(conn, "positions", "highest_since_entry", "REAL DEFAULT 0.0")
    # crypto no se trackea en RFTM
    changed = 0
    try:
        run_id = get_or_make_run_id(conn, "positions")
    except Exception:
        run_id = str(uuid.uuid4())
    for ap in positions:
        sym = ap["symbol"]
        if "/" in sym:  # crypto → lo maneja MREV
            continue
        qty = int(float(ap.get("qty", 0)))
        entry = float(ap.get("avg_entry_price", 0))
        if qty <= 0:
            continue
        cur = conn.execute(
            "SELECT id, entry_price, highest_since_entry "
            "FROM positions WHERE symbol=? AND status='open'", (sym,))
        row = cur.fetchone()
        if row:
            # Stage=1 implica high ≥ entry × 1.05. Si la fila tiene
            # high=entry tras un re-seed, subimos al floor.
            try:
                from _exit_logic import stage_implied_high_floor
                cur_entry = float(row[1] or 0)
                cur_high = float(row[2] or 0)
                floor_high = stage_implied_high_floor(
                    entry_price=cur_entry, stage=1) if cur_entry > 0 else 0
                new_high = max(cur_high, floor_high)
            except Exception:
                new_high = float(row[2] or 0)
            print(f"  RFTM  UPDATE {sym:<6} qty={qty} → partial_tp_taken=1 "
                  f"high→${new_high:.4f}")
            if not dry:
                conn.execute(
                    "UPDATE positions SET partial_tp_taken=1, "
                    "initial_qty=COALESCE(initial_qty, qty*2), "
                    "highest_since_entry=? WHERE id=?",
                    (round(new_high, 4), row[0]))
            changed += 1
        else:
            # Stage=1 implica que el high cruzó al menos +TP1_PCT sobre el
            # entry — si dejamos `highest_since_entry = entry`, el trailing
            # stop del watchdog calcula profit_atr=0 y nunca se activa.
            # Usamos el floor stage-aware de _exit_logic.
            try:
                from _exit_logic import stage_implied_high_floor
                seed_high = stage_implied_high_floor(
                    entry_price=entry, stage=1)
            except Exception:
                seed_high = entry * 1.05  # fallback al default histórico
            print(f"  RFTM  INSERT {sym:<6} qty={qty} entry=${entry:.2f} "
                  f"partial_tp_taken=1  high_seed=${seed_high:.4f}")
            if not dry:
                conn.execute("""
                    INSERT INTO positions (id, run_id, symbol, status, qty, entry_price,
                        stop_loss, unrealized_pnl, opened_at, partial_tp_taken, initial_qty,
                        highest_since_entry)
                    VALUES (?,?,?,?,?,?,?,0,?,1,?,?)
                """, (str(uuid.uuid4()), run_id, sym, "open", qty, entry,
                      round(entry, 4),  # stage=1 → stop al breakeven (no entry*0.95)
                      datetime.now(timezone.utc).isoformat(),
                      qty * 2, round(seed_high, 4)))
            changed += 1
    if not dry:
        conn.commit()
    conn.close()
    return changed


def upsert_mrev(positions: list, dry: bool) -> int:
    conn = sqlite3.connect(str(MREV_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mrev_runs (
            id TEXT PRIMARY KEY, started_at TEXT,
            initial_capital REAL, status TEXT DEFAULT 'RUNNING'
        );
        CREATE TABLE IF NOT EXISTS mrev_positions (
            id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL,
            entry_price REAL, stop_loss REAL, entry_dt TEXT,
            status TEXT DEFAULT 'OPEN', exit_price REAL, exit_dt TEXT,
            pnl REAL, exit_reason TEXT, highest_since_entry REAL DEFAULT 0.0
        );
    """)
    ensure_column(conn, "mrev_positions", "partial_tp_taken", "INTEGER DEFAULT 0")
    ensure_column(conn, "mrev_positions", "initial_qty", "REAL")
    ensure_column(conn, "mrev_positions", "highest_since_entry", "REAL DEFAULT 0.0")
    changed = 0
    try:
        run_id = get_or_make_run_id(conn, "mrev_positions")
    except Exception:
        run_id = str(uuid.uuid4())
    for ap in positions:
        sym = ap["symbol"]
        qty = float(ap.get("qty", 0))
        entry = float(ap.get("avg_entry_price", 0))
        if qty <= 0:
            continue
        cur = conn.execute(
            "SELECT id, entry_price, highest_since_entry "
            "FROM mrev_positions WHERE symbol=? AND status='OPEN'", (sym,))
        row = cur.fetchone()
        if row:
            # Si marcamos stage=1, el high tuvo que cruzar +TP1_PCT. Si la
            # fila tiene high=entry (típico tras un re-seed), subimos al
            # floor implícito para que el trailing del watchdog funcione.
            try:
                from _exit_logic import stage_implied_high_floor
                cur_entry = float(row[1] or 0)
                cur_high = float(row[2] or 0)
                floor_high = stage_implied_high_floor(
                    entry_price=cur_entry, stage=1) if cur_entry > 0 else 0
                new_high = max(cur_high, floor_high)
            except Exception:
                new_high = float(row[2] or 0)
            print(f"  MREV  UPDATE {sym:<10} qty={qty} → partial_tp_taken=1 "
                  f"high→${new_high:.4f}")
            if not dry:
                conn.execute(
                    "UPDATE mrev_positions SET partial_tp_taken=1, "
                    "initial_qty=COALESCE(initial_qty, qty*2), "
                    "highest_since_entry=? WHERE id=?",
                    (round(new_high, 6), row[0]))
            changed += 1
        # No insertamos posiciones nuevas en MREV — MREV no reclama acciones
        # que el RFTM compró. Solo protegemos las ya trackeadas por MREV.
    if not dry:
        conn.commit()
    conn.close()
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not KEY or not SECRET:
        print("ERROR: faltan credenciales Alpaca en .env.paper")
        return 1
    positions = alpaca_positions()
    if not positions:
        print("no hay posiciones en Alpaca (nada que marcar).")
        return 0
    print(f"Alpaca reporta {len(positions)} posiciones abiertas.\n")
    print("== RFTM (trading_paper.db) ==")
    n1 = upsert_rftm(positions, args.dry_run)
    print(f"\n== MREV (mrev_paper.db) ==")
    n2 = upsert_mrev(positions, args.dry_run)
    print()
    tag = "DRY-RUN" if args.dry_run else "DONE"
    print(f"[{tag}] RFTM filas tocadas: {n1} · MREV filas tocadas: {n2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
