#!/usr/bin/env python3
"""
seed_missing_positions.py — One-shot: inserta en las DBs locales las posiciones
que Alpaca tiene pero RFTM / MREV no venían trackeando, y ajusta el stage del
partial take-profit al nuevo esquema de 2 etapas (5% / 7.5%).

Uso:
    python3 seed_missing_positions.py --dry-run   # preview
    python3 seed_missing_positions.py             # aplica cambios

Qué hace:
  1. Consulta GET /positions de Alpaca.
  2. Clasifica cada posición:
       - Cripto ("/" en el symbol o AVAX/SOL/ETH/etc) → mrev_paper.db
       - ETFs → trading_paper.db (RFTM)
  3. Si la posición no está en la DB correspondiente, la inserta con:
       - entry_price = avg_entry_price real de Alpaca
       - qty, initial_qty = qty real
       - partial_tp_taken = 0  (stage 0 — ninguna TP parcial aún)
  4. Si ya existe, actualiza entry_price y qty a los valores reales de Alpaca.
  5. Fuerza partial_tp_taken=2 (finalizado) en cualquier posición cuyo qty
     ya sea tan chico que no se pueda seguir partiendo (qty <= 1 con stage=1).

Idempotente: se puede correr las veces que quieras.
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

CRYPTO_ROOTS = ("BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK", "DOT", "ADA", "MATIC", "XRP")


# ── .env.paper loader ────────────────────────────────────────────────────────
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
        print(f"ERROR Alpaca /positions: {e}")
        return []


def _is_crypto(sym: str) -> bool:
    if "/" in sym:
        return True
    # Solo cuenta como cripto si empieza por un root conocido Y termina en USD/USDT/USDC.
    # Esto evita confundir ETFs como "USO" con cripto.
    if sym.endswith(("USD", "USDT", "USDC")):
        return any(sym.startswith(c) for c in CRYPTO_ROOTS)
    return False


def _crypto_norm(sym: str) -> str:
    """Normaliza AVAXUSD → AVAX/USD (formato que usa MREV)."""
    if "/" in sym:
        return sym
    for base in ("USD", "USDT", "USDC"):
        if sym.endswith(base) and len(sym) > len(base):
            root = sym[:-len(base)]
            if any(root == c for c in CRYPTO_ROOTS):
                return f"{root}/{base}"
    return sym


def ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except Exception:
        pass


def cleanup_crypto_from_rftm(dry: bool) -> int:
    """Cierra (status='closed') cualquier posición cripto que haya quedado
    mezclada en trading_paper.db — las migramos a mrev_paper.db.

    El email de RFTM la está mostrando con un 'Take Profit' calculado al 2:1
    (entry + 2 × riesgo) que no tiene sentido para cripto. MREV tiene su propia
    lógica de salida basada en SMA(20) + 1.5 × ATR. Cada posición vive en una
    sola DB.
    """
    if not RFTM_DB.exists():
        return 0
    conn = sqlite3.connect(str(RFTM_DB))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, symbol, qty, entry_price FROM positions WHERE status='open'"
    ))
    moved = 0
    for r in rows:
        if not _is_crypto(r["symbol"]):
            continue
        print(f"  RFTM  MARK_CLOSED (era cripto, va a MREV)  {r['symbol']:<10} qty={r['qty']}")
        if not dry:
            conn.execute(
                "UPDATE positions SET status='closed', close_reason='migrated_to_mrev', closed_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), r["id"])
            )
        moved += 1
    if not dry:
        conn.commit()
    conn.close()
    return moved


# ── RFTM upsert ──────────────────────────────────────────────────────────────
def upsert_rftm(positions: list, dry: bool) -> int:
    conn = sqlite3.connect(str(RFTM_DB))
    conn.row_factory = sqlite3.Row
    # Schema safety net (mirror standalone_paper_trader.py)
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
    ensure_column(conn, "positions", "partial_tp_taken", "INTEGER DEFAULT 0")
    ensure_column(conn, "positions", "initial_qty", "INTEGER")
    ensure_column(conn, "positions", "highest_since_entry", "REAL DEFAULT 0.0")

    run_row = conn.execute("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if run_row:
        run_id = run_row[0]
    else:
        run_id = str(uuid.uuid4())
        if not dry:
            conn.execute(
                "INSERT INTO runs (id, started_at, initial_capital, status, cash) VALUES (?,?,?,?,?)",
                (run_id, datetime.now(timezone.utc).isoformat(), 75000.0, "running", 0.0))

    changed = 0
    for ap in positions:
        sym_raw = ap.get("symbol", "")
        if _is_crypto(sym_raw):
            continue  # cripto la maneja MREV
        qty = int(float(ap.get("qty", 0)))
        entry = float(ap.get("avg_entry_price", 0))
        if qty <= 0 or entry <= 0:
            continue
        cur = conn.execute(
            "SELECT id, qty, entry_price, stop_loss, partial_tp_taken, initial_qty "
            "FROM positions WHERE symbol=? AND status='open'",
            (sym_raw,)
        ).fetchone()
        if cur:
            # UPDATE: fix entry_price + qty si hace falta
            new_entry = entry if abs(cur["entry_price"] - entry) > 0.0001 else cur["entry_price"]
            new_qty = qty
            stage = int(cur["partial_tp_taken"] or 0)
            # Si stage=1 pero qty ya no se puede partir más → marcar stage=2
            if stage == 1 and new_qty <= 1:
                stage = 2
            # initial_qty: si es None, usar qty actual como baseline
            init_q = cur["initial_qty"] or new_qty

            # Breakeven raise en seed: si ya se tomó al menos TP1 (stage>=1) y
            # el stop viejo sigue por debajo del entry, subirlo al breakeven.
            # Normalmente esto pasa en el fill post-orden real, pero posiciones
            # sembradas antes de existir ese código quedaron con stops bajos.
            cur_stop = float(cur["stop_loss"] or 0)
            new_stop = cur_stop
            if stage >= 1 and cur_stop > 0 and cur_stop < new_entry:
                new_stop = new_entry
                print(f"  RFTM  RAISE_STOP {sym_raw:<6} ${cur_stop:.4f} → ${new_stop:.4f} (breakeven post-TP1)")

            # Raise stage-aware del highest_since_entry. Si stage>=1, el
            # high tuvo que llegar al floor implícito del TP — si quedó
            # más bajo (típicamente igual a entry tras un re-seed previo),
            # el trailing stop está roto. Solo SUBE el high.
            new_high_raise = None
            if stage >= 1:
                try:
                    cur_high_full = conn.execute(
                        "SELECT highest_since_entry FROM positions WHERE id=?",
                        (cur["id"],)
                    ).fetchone()
                    cur_high = float(cur_high_full[0] or 0) if cur_high_full else 0.0
                except Exception:
                    cur_high = 0.0
                try:
                    from _exit_logic import stage_implied_high_floor
                    floor_high = stage_implied_high_floor(
                        entry_price=new_entry, stage=stage)
                except Exception:
                    floor_high = new_entry * (1.05 if stage == 1 else 1.075)
                if floor_high > cur_high:
                    new_high_raise = floor_high
                    print(f"  RFTM  RAISE_HIGH {sym_raw:<6} ${cur_high:.4f} → ${floor_high:.4f} "
                          f"(stage={stage} floor)")

            print(f"  RFTM  UPDATE {sym_raw:<6} qty={new_qty:<4} entry=${new_entry:.4f}  stop=${new_stop:.4f}  stage={stage}  initial_qty={init_q}")
            if not dry:
                if new_high_raise is not None:
                    conn.execute(
                        "UPDATE positions SET qty=?, entry_price=?, stop_loss=?, "
                        "partial_tp_taken=?, initial_qty=COALESCE(initial_qty, ?), "
                        "highest_since_entry=? WHERE id=?",
                        (new_qty, new_entry, new_stop, stage, init_q,
                         round(new_high_raise, 4), cur["id"])
                    )
                else:
                    conn.execute(
                        "UPDATE positions SET qty=?, entry_price=?, stop_loss=?, "
                        "partial_tp_taken=?, initial_qty=COALESCE(initial_qty, ?) WHERE id=?",
                        (new_qty, new_entry, new_stop, stage, init_q, cur["id"])
                    )
            changed += 1
        else:
            # F3.3: stop ATR-based con fallback al 5%. Antes hardcodeaba
            # entry*0.95 — perdía info si la posición venía con TP1 ya
            # tomado (caso COPX mayo 2026).
            try:
                from _exit_logic import recalc_stop_for_stage
                # ATR no disponible en este script (no tiene market_data).
                # Caemos al fallback 5% que es lo mismo que antes pero
                # respetando el invariante por si más adelante leemos ATR.
                seed_stop = recalc_stop_for_stage(
                    entry_price=entry, stage=0, atr=None, current_stop=None,
                )
            except Exception:
                seed_stop = entry * 0.95
            print(f"  RFTM  INSERT {sym_raw:<6} qty={qty:<4} entry=${entry:.4f}  "
                  f"stop=${seed_stop:.4f}  stage=0  initial_qty={qty}")
            if not dry:
                conn.execute(
                    """INSERT INTO positions
                       (id, run_id, symbol, status, qty, entry_price, stop_loss,
                        unrealized_pnl, opened_at, highest_since_entry,
                        partial_tp_taken, initial_qty)
                       VALUES (?,?,?,?,?,?,?,0,?,?,0,?)""",
                    (str(uuid.uuid4()), run_id, sym_raw, "open", qty, entry,
                     round(seed_stop, 4),
                     datetime.now(timezone.utc).isoformat(),
                     entry, qty)
                )
            changed += 1

    if not dry:
        conn.commit()
    conn.close()
    return changed


# ── MREV upsert (cripto + ETFs MREV) ─────────────────────────────────────────
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

    run_row = conn.execute("SELECT id FROM mrev_runs WHERE status='RUNNING' LIMIT 1").fetchone()
    if run_row:
        run_id = run_row[0]
    else:
        run_id = str(uuid.uuid4())[:8]
        if not dry:
            conn.execute(
                "INSERT INTO mrev_runs (id, started_at, initial_capital, status) VALUES (?,?,?,?)",
                (run_id, datetime.now(timezone.utc).isoformat(), 25000.0, "RUNNING"))

    changed = 0
    for ap in positions:
        sym_raw = ap.get("symbol", "")
        if not _is_crypto(sym_raw):
            continue
        sym = _crypto_norm(sym_raw)
        qty = float(ap.get("qty", 0))
        entry = float(ap.get("avg_entry_price", 0))
        if qty <= 0 or entry <= 0:
            continue
        cur = conn.execute(
            "SELECT id, qty, entry_price, stop_loss, partial_tp_taken, initial_qty "
            "FROM mrev_positions WHERE symbol=? AND status='OPEN'",
            (sym,)
        ).fetchone()
        if cur:
            stage = int(cur["partial_tp_taken"] or 0)
            init_q = cur["initial_qty"] or qty

            # Breakeven raise (ver nota en upsert_rftm).
            cur_stop = float(cur["stop_loss"] or 0)
            new_stop = cur_stop
            if stage >= 1 and cur_stop > 0 and cur_stop < entry:
                new_stop = entry
                print(f"  MREV  RAISE_STOP {sym:<10} ${cur_stop:.4f} → ${new_stop:.4f} (breakeven post-TP1)")

            # Raise stage-aware del highest_since_entry (mismo bug que en RFTM).
            new_high_raise = None
            if stage >= 1:
                try:
                    cur_high_full = conn.execute(
                        "SELECT highest_since_entry FROM mrev_positions WHERE id=?",
                        (cur["id"],)
                    ).fetchone()
                    cur_high = float(cur_high_full[0] or 0) if cur_high_full else 0.0
                except Exception:
                    cur_high = 0.0
                try:
                    from _exit_logic import stage_implied_high_floor
                    floor_high = stage_implied_high_floor(
                        entry_price=entry, stage=stage)
                except Exception:
                    floor_high = entry * (1.05 if stage == 1 else 1.075)
                if floor_high > cur_high:
                    new_high_raise = floor_high
                    print(f"  MREV  RAISE_HIGH {sym:<10} ${cur_high:.4f} → ${floor_high:.4f} "
                          f"(stage={stage} floor)")

            print(f"  MREV  UPDATE {sym:<10} qty={qty:<12} entry=${entry:.4f}  stop=${new_stop:.4f}  stage={stage}  initial_qty={init_q}")
            if not dry:
                if new_high_raise is not None:
                    conn.execute(
                        "UPDATE mrev_positions SET qty=?, entry_price=?, stop_loss=?, "
                        "initial_qty=COALESCE(initial_qty, ?), highest_since_entry=? "
                        "WHERE id=?",
                        (qty, entry, new_stop, init_q,
                         round(new_high_raise, 6), cur["id"])
                    )
                else:
                    conn.execute(
                        "UPDATE mrev_positions SET qty=?, entry_price=?, stop_loss=?, "
                        "initial_qty=COALESCE(initial_qty, ?) WHERE id=?",
                        (qty, entry, new_stop, init_q, cur["id"])
                    )
            changed += 1
        else:
            # F3.3: stop ATR-based con fallback al 5% — antes hardcodeaba.
            try:
                from _exit_logic import recalc_stop_for_stage
                seed_stop = recalc_stop_for_stage(
                    entry_price=entry, stage=0, atr=None, current_stop=None,
                )
            except Exception:
                seed_stop = entry * 0.95
            print(f"  MREV  INSERT {sym:<10} qty={qty:<12} entry=${entry:.4f}  "
                  f"stop=${seed_stop:.4f}  stage=0  initial_qty={qty}")
            if not dry:
                conn.execute(
                    """INSERT INTO mrev_positions
                       (id, run_id, symbol, qty, entry_price, stop_loss, entry_dt,
                        status, highest_since_entry, partial_tp_taken, initial_qty)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4())[:8], run_id, sym, qty, entry,
                     round(seed_stop, 6),
                     datetime.now(timezone.utc).isoformat(),
                     "OPEN", entry, 0, qty)
                )
            changed += 1

    if not dry:
        conn.commit()
    conn.close()
    return changed


def show_state() -> None:
    """Imprime el estado final de ambas DBs."""
    print("\n── Estado final ──")
    try:
        con = sqlite3.connect(str(RFTM_DB))
        con.row_factory = sqlite3.Row
        rows = list(con.execute(
            "SELECT symbol, qty, entry_price, initial_qty, partial_tp_taken "
            "FROM positions WHERE status='open' ORDER BY symbol"))
        print(f"\nRFTM ({len(rows)} open):")
        print(f"  {'SYM':<8} {'qty':>6} {'entry':>12} {'init':>6} {'stage':>5}")
        for r in rows:
            print(f"  {r['symbol']:<8} {r['qty']:>6} ${r['entry_price']:>10.4f} {r['initial_qty']:>6} {r['partial_tp_taken']:>5}")
        con.close()
    except Exception as e:
        print(f"RFTM read error: {e}")

    try:
        con = sqlite3.connect(str(MREV_DB))
        con.row_factory = sqlite3.Row
        rows = list(con.execute(
            "SELECT symbol, qty, entry_price, initial_qty, partial_tp_taken "
            "FROM mrev_positions WHERE status='OPEN' ORDER BY symbol"))
        print(f"\nMREV ({len(rows)} open):")
        print(f"  {'SYM':<10} {'qty':>12} {'entry':>12} {'init':>8} {'stage':>5}")
        for r in rows:
            print(f"  {r['symbol']:<10} {r['qty']:>12} ${r['entry_price']:>10.4f} {str(r['initial_qty']):>8} {r['partial_tp_taken']:>5}")
        con.close()
    except Exception as e:
        print(f"MREV read error: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview sin tocar nada")
    args = ap.parse_args()

    if not KEY or not SECRET:
        print("ERROR: faltan ALPACA_API_KEY / ALPACA_SECRET_KEY en .env.paper")
        return 1

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Consultando Alpaca…")
    positions = alpaca_positions()
    if not positions:
        print("no hay posiciones en Alpaca")
        return 0
    print(f"Alpaca: {len(positions)} posiciones abiertas\n")

    print("== Limpieza: sacar cripto atrapada en trading_paper.db ==")
    n0 = cleanup_crypto_from_rftm(args.dry_run)
    print(f"\n== RFTM (trading_paper.db) ==")
    n1 = upsert_rftm(positions, args.dry_run)
    print(f"\n== MREV (mrev_paper.db) ==")
    n2 = upsert_mrev(positions, args.dry_run)

    tag = "DRY-RUN" if args.dry_run else "DONE"
    print(f"\n[{tag}] cripto migrada: {n0} · RFTM: {n1} filas · MREV: {n2} filas")

    if not args.dry_run:
        show_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())
