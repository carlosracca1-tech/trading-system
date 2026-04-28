#!/usr/bin/env python3
"""
reconcile_position.py — Reconcilia una posición específica entre la DB local
del bot (RFTM o MREV) y lo que dice Alpaca como verdad operativa.

Caso de uso típico: una fila en la DB tiene qty / initial_qty / stage
inconsistentes (ej: qty=438 vs initial_qty=136 con stage=1 → imposible),
posiblemente por crashes durante una operación o por seeds viejos. Antes de
que el watchdog haga algo raro con esa fila, la alineamos contra Alpaca.

Uso:
    python3 scripts/ops/reconcile_position.py XLE              # dry-run, solo imprime
    python3 scripts/ops/reconcile_position.py XLE --apply      # ejecuta el UPDATE
    python3 scripts/ops/reconcile_position.py BTC/USD          # cripto → MREV DB
    python3 scripts/ops/reconcile_position.py --all            # imprime diff de todas las open
    python3 scripts/ops/reconcile_position.py --all --apply    # aplica a todas

Reglas de aplicación (con --apply):
  - Si Alpaca tiene la posición (qty > 0):
      qty           = alpaca_qty
      entry_price   = alpaca_avg_entry_price
      initial_qty   = max(local_initial_qty or 0, alpaca_qty)
      partial_tp_taken (stage), stop_loss, highest_since_entry → no se tocan.
  - Si Alpaca NO tiene la posición (qty == 0):
      status        = 'closed'
      close_reason  = 'reconcile_alpaca_empty'
      closed_at     = now (UTC)

Idempotente. Se puede correr las veces que quieras. Sin --apply nunca
escribe en la DB.

Convención: respeta el patrón de scripts/ops/ (helpers chicos, side-effects
explícitos, dry-run por default).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / ".env.paper"

# Resuelve igual que los bots: RFTM_DB_PATH/MREV_DB_PATH override, default
# junto al script (cwd del repo).
RFTM_DB = Path(os.environ.get("RFTM_DB_PATH", REPO_ROOT / "trading_paper.db"))
MREV_DB = Path(os.environ.get("MREV_DB_PATH", REPO_ROOT / "mrev_paper.db"))

CRYPTO_ROOTS = ("BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK", "DOT", "ADA", "MATIC", "XRP")


# ── .env loader ──────────────────────────────────────────────────────────────
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
_RAW_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_URL = _RAW_URL[:-3] if _RAW_URL.endswith("/v2") else _RAW_URL


# ── Alpaca helpers ───────────────────────────────────────────────────────────
def _alpaca_get(path: str) -> object:
    req = urllib.request.Request(
        f"{ALPACA_URL}{path}",
        headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 404 = posición no existe (Alpaca devuelve 404 para /positions/{sym} sin pos)
        if e.code == 404:
            return None
        body = e.read().decode(errors="replace")
        print(f"  [HTTP {e.code} @ {path}] {body[:200]}")
        raise
    except Exception as e:
        print(f"  [ERROR @ {path}] {e}")
        raise


def alpaca_position(symbol: str) -> dict | None:
    """GET /v2/positions/{symbol}. Devuelve None si no hay posición."""
    # Alpaca usa el formato URL-encoded para cripto (ej BTC%2FUSD).
    safe = urllib.request.quote(symbol, safe="")
    return _alpaca_get(f"/v2/positions/{safe}")


def alpaca_all_positions() -> list:
    return _alpaca_get("/v2/positions") or []


# ── Symbol routing ───────────────────────────────────────────────────────────
def is_crypto(sym: str) -> bool:
    if "/" in sym:
        return True
    if sym.endswith(("USD", "USDT", "USDC")):
        return any(sym.startswith(c) for c in CRYPTO_ROOTS)
    return False


def db_for(symbol: str) -> tuple[Path, str, str, str]:
    """Devuelve (db_path, table_name, status_col_value_for_open, symbol_db).

    RFTM usa positions.status='open' y symbol sin barra (XLE).
    MREV usa mrev_positions.status='OPEN' y symbol con barra (BTC/USD).
    """
    if is_crypto(symbol):
        return MREV_DB, "mrev_positions", "OPEN", _normalize_crypto(symbol)
    return RFTM_DB, "positions", "open", symbol


def close_dialect_for(table: str) -> tuple[str, str, str]:
    """Cada tabla cierra distinto. Devuelve (status_closed_value, reason_col, when_col).

    RFTM positions:      status='closed' close_reason=... closed_at=...
    MREV mrev_positions: status='CLOSED' exit_reason=...  exit_dt=...
    """
    if table == "mrev_positions":
        return "CLOSED", "exit_reason", "exit_dt"
    return "closed", "close_reason", "closed_at"


def _normalize_crypto(sym: str) -> str:
    if "/" in sym:
        return sym
    for base in ("USD", "USDT", "USDC"):
        if sym.endswith(base) and len(sym) > len(base):
            root = sym[: -len(base)]
            if root in CRYPTO_ROOTS:
                return f"{root}/{base}"
    return sym


# ── Local DB helpers ─────────────────────────────────────────────────────────
def _open_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"DB no existe: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def get_local_position(symbol: str) -> tuple[sqlite3.Connection, dict | None, Path, str, str]:
    db_path, table, status_open, db_symbol = db_for(symbol)
    conn = _open_db(db_path)
    row = conn.execute(
        f"SELECT * FROM {table} WHERE symbol = ? AND status = ?",
        (db_symbol, status_open),
    ).fetchone()
    return conn, (dict(row) if row else None), db_path, table, status_open


# ── Print + apply ────────────────────────────────────────────────────────────
def print_diff(symbol: str, local: dict | None, remote: dict | None) -> None:
    print(f"\n  ── {symbol} ──")
    if local is None and remote is None:
        print("    DB local: no abierta · Alpaca: no abierta · NADA QUE HACER")
        return
    if local is None:
        print(f"    DB local: NO EXISTE   ·   Alpaca: qty={remote.get('qty')} avg={remote.get('avg_entry_price')}")
        print("    → reconcile_position no inserta nuevas filas. Usar seed_missing_positions.py para eso.")
        return
    if remote is None:
        print(f"    DB local: qty={local.get('qty')} initial_qty={local.get('initial_qty')} stage={local.get('partial_tp_taken')} entry=${local.get('entry_price'):.4f}")
        print( "    Alpaca:    NO EXISTE")
        print(f"    → cerrar fila local con close_reason='reconcile_alpaca_empty'")
        return

    a_qty = float(remote.get("qty", 0))
    a_entry = float(remote.get("avg_entry_price", 0))
    l_qty = local.get("qty") or 0
    l_initial = local.get("initial_qty") or 0
    l_entry = float(local.get("entry_price") or 0)
    l_stage = local.get("partial_tp_taken") or 0
    l_stop = float(local.get("stop_loss") or 0)

    print(f"    {'campo':<18} {'DB local':>16}    {'Alpaca':>16}")
    print(f"    {'qty':<18} {l_qty:>16}    {a_qty:>16.4f}")
    print(f"    {'initial_qty':<18} {l_initial:>16}    {'(n/a)':>16}")
    print(f"    {'entry_price':<18} {l_entry:>16.4f}    {a_entry:>16.4f}")
    print(f"    {'stage':<18} {l_stage:>16}    {'(n/a)':>16}")
    print(f"    {'stop_loss':<18} {l_stop:>16.4f}    {'(no se toca)':>16}")
    inconsistent = (
        abs(l_qty - a_qty) > 0.0001
        or abs(l_entry - a_entry) > 0.0001
        or (l_initial and l_initial < l_qty)
    )
    if inconsistent:
        new_initial = max(int(l_initial or 0), int(a_qty)) if not is_crypto(symbol) else max(l_initial or 0.0, a_qty)
        print(f"    → APPLY: qty={a_qty} entry=${a_entry:.4f} initial_qty={new_initial} (stage={l_stage} preservado)")
    else:
        print("    → CONSISTENTE, nada que aplicar.")


def apply_reconcile(
    conn: sqlite3.Connection,
    table: str,
    status_open: str,
    local: dict | None,
    remote: dict | None,
    db_symbol: str,
    symbol_for_log: str,
) -> bool:
    """Devuelve True si modificó algo. NO commitea — el caller hace commit."""
    if local is None:
        return False  # nada que reconciliar; print_diff ya avisó
    if remote is None:
        # Cerrar fila local — RFTM y MREV usan nombres de columna distintos.
        status_closed, reason_col, when_col = close_dialect_for(table)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"UPDATE {table} SET status=?, {reason_col}=?, {when_col}=? WHERE id=?",
            (status_closed, "reconcile_alpaca_empty", now, local["id"]),
        )
        print(f"    APPLIED: {symbol_for_log} cerrada (Alpaca vacío)")
        return True

    a_qty_raw = float(remote.get("qty", 0))
    a_entry = float(remote.get("avg_entry_price", 0))
    l_qty = local.get("qty") or 0
    l_initial = local.get("initial_qty") or 0
    l_entry = float(local.get("entry_price") or 0)

    # En cripto qty es float, en ETFs entero. Preservamos el tipo.
    a_qty = a_qty_raw if is_crypto(symbol_for_log) else int(round(a_qty_raw))

    if abs(l_qty - a_qty) <= 0.0001 and abs(l_entry - a_entry) <= 0.0001 and (l_initial or 0) >= l_qty:
        return False  # consistente

    new_initial = max(l_initial or 0, a_qty) if is_crypto(symbol_for_log) else max(int(l_initial or 0), int(a_qty))
    conn.execute(
        f"UPDATE {table} SET qty=?, entry_price=?, initial_qty=? WHERE id=?",
        (a_qty, a_entry, new_initial, local["id"]),
    )
    print(f"    APPLIED: {symbol_for_log} qty={a_qty} entry=${a_entry:.4f} initial_qty={new_initial}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────
def run_one(symbol: str, apply: bool) -> int:
    conn, local, db_path, table, status_open = get_local_position(symbol)
    db_symbol = _normalize_crypto(symbol) if is_crypto(symbol) else symbol
    try:
        remote = alpaca_position(db_symbol)
    except Exception:
        conn.close()
        return 1
    print_diff(symbol, local, remote)
    if apply:
        changed = apply_reconcile(conn, table, status_open, local, remote, db_symbol, symbol)
        if changed:
            conn.commit()
    conn.close()
    return 0


def run_all(apply: bool) -> int:
    # Iteramos sobre las posiciones LOCALES open en ambas DBs y para cada
    # una pedimos su contraparte en Alpaca.
    targets: list[str] = []
    if RFTM_DB.exists():
        c = sqlite3.connect(str(RFTM_DB))
        for (sym,) in c.execute("SELECT symbol FROM positions WHERE status='open' ORDER BY symbol"):
            targets.append(sym)
        c.close()
    if MREV_DB.exists():
        c = sqlite3.connect(str(MREV_DB))
        for (sym,) in c.execute("SELECT symbol FROM mrev_positions WHERE status='OPEN' ORDER BY symbol"):
            targets.append(sym)
        c.close()
    if not targets:
        print("(no hay posiciones abiertas en ninguna DB)")
        return 0
    print(f"Reconciliando {len(targets)} posiciones abiertas (apply={apply})")
    rc = 0
    for sym in targets:
        rc |= run_one(sym, apply)
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbol", nargs="?", help="símbolo a reconciliar (XLE, BTC/USD, etc)")
    p.add_argument("--all", action="store_true", help="iterar todas las open en ambas DBs")
    p.add_argument("--apply", action="store_true", help="ejecutar UPDATE (default es dry-run)")
    args = p.parse_args()

    if not KEY or not SECRET:
        print("ERROR: faltan ALPACA_API_KEY / ALPACA_SECRET_KEY en .env.paper o env.")
        return 2
    if not args.symbol and not args.all:
        p.print_help()
        return 2

    print(f"[reconcile_position] base={ALPACA_URL}  apply={args.apply}")
    if args.all:
        return run_all(args.apply)
    return run_one(args.symbol, args.apply)


if __name__ == "__main__":
    sys.exit(main())
