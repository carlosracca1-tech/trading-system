#!/usr/bin/env python3
"""
shadow_tick.py — F6.1: simula 1 día de mercado para todos los shadow
trades `running`. Fetches el último close (y high/low) de Alpaca y
aplica `tick_shadow_trade`.

Corre diario (cron). Idempotente: si un shadow ya cerró, se ignora.

Uso:
    python3 scripts/shadow_tick.py
    python3 scripts/shadow_tick.py --dry-run

Env:
    RFTM_DB_PATH / MREV_DB_PATH como siempre.
    ALPACA_API_KEY / ALPACA_SECRET_KEY
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _shadow_trades import (
    aggregate_by_rule,
    apply_tick_to_db,
    ensure_table,
    tick_shadow_trade,
)


def _alpaca_get_latest_bar(symbol: str) -> dict | None:
    """Trae el último bar diario de Alpaca data API."""
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        return None
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Day&limit=1"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    bars = data.get("bars", [])
    return bars[-1] if bars else None


def tick_db(db_path: Path, dry_run: bool) -> int:
    """Tickea todos los shadows running de una DB. Devuelve cuántos cerró."""
    if not db_path.exists():
        print(f"  ⚠ {db_path} no existe, skip")
        return 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM kaizen_shadow_trades WHERE status='running'"
    ).fetchall()
    print(f"  {len(rows)} shadows running en {db_path.name}")
    closed = 0
    for row in rows:
        bar = _alpaca_get_latest_bar(row["symbol"])
        if not bar:
            continue
        current = float(bar.get("c", 0))
        high = float(bar.get("h", 0))
        low = float(bar.get("l", 0))
        if current <= 0:
            continue
        result = tick_shadow_trade(row, current_price=current, high=high, low=low)
        if dry_run:
            if result.closed:
                print(f"    [DRY] would close {row['symbol']} sid={row['id']} "
                      f"reason={result.exit_reason} pnl=${result.pnl:.2f}")
            elif result.new_stage is not None:
                print(f"    [DRY] would promote {row['symbol']} → stage {result.new_stage}")
        else:
            apply_tick_to_db(conn, row, result, current_price=current)
            if result.closed:
                closed += 1
                print(f"    closed {row['symbol']} sid={row['id']} "
                      f"reason={result.exit_reason} pnl=${result.pnl:.2f}")
    conn.close()
    return closed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(f"shadow_tick {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")

    rftm_db = Path(os.environ.get("RFTM_DB_PATH", str(ROOT / "trading_paper.db")))
    mrev_db = Path(os.environ.get("MREV_DB_PATH", str(ROOT / "mrev_paper.db")))

    total_closed = 0
    for db in (rftm_db, mrev_db):
        total_closed += tick_db(db, args.dry_run)

    # Aggregate stats al final
    print(f"\n→ shadows cerrados este run: {total_closed}")
    for db in (rftm_db, mrev_db):
        if db.exists():
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            agg = aggregate_by_rule(conn)
            conn.close()
            if agg:
                print(f"\n  Agregado {db.name}:")
                for a in agg:
                    print(f"    {a['rule_id']}: {a['n_shadows_closed']} closed, "
                          f"net ${a['net_impact_usd']:+,.2f} "
                          f"(saved ${a['gross_saved_usd']:.2f}, missed ${a['gross_missed_usd']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
