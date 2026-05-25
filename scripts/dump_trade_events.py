#!/usr/bin/env python3
"""
dump_trade_events.py — Lee los JSONL de eventos de trade y los exporta
a un CSV unificado y legible para auditar localmente sin depender de
Google Sheets.

Contexto:
- `_trade_logger.log_trade_event` escribe SIEMPRE a JSONL local y BEST
  EFFORT a Sheets. Si la cuenta de servicio no está configurada (secret
  faltante, JSON inválido, etc.), Sheets queda vacío pero el JSONL sigue
  siendo la fuente de verdad.
- Estos JSONL viven en el branch `state/db` del repo (los empuja
  `state_db_push.sh` al final de cada workflow). En local, hay que
  correr `make sync-db` (o `bash scripts/sync_db.sh`) para traerlos.

Uso:
    # Defaults: lee logs/trade_events_rftm.jsonl + logs/trade_events_mrev.jsonl
    # y produce trade_events.csv en el mismo directorio.
    python3 scripts/dump_trade_events.py

    # Override paths / output:
    python3 scripts/dump_trade_events.py \\
        --rftm logs/trade_events_rftm.jsonl \\
        --mrev logs/trade_events_mrev.jsonl \\
        --out outputs/trade_events_2026-05.csv

    # Filtrar por símbolo o por fecha:
    python3 scripts/dump_trade_events.py --symbol XLE
    python3 scripts/dump_trade_events.py --since 2026-05-10

    # Resumen rápido por símbolo (no escribe CSV, solo printea):
    python3 scripts/dump_trade_events.py --summary

Idempotente. No modifica los JSONL — solo lee.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Schema canónico — orden de columnas. Match con _sheets_logger.HEADER_ROW
# + algunos extras útiles para auditoría local.
COLS = [
    "timestamp_utc",
    "bot",
    "symbol",
    "side",
    "qty",
    "price",
    "notional",
    "stage",
    "running_qty",
    "initial_qty",
    "entry_price",
    "realized_pnl_event",
    "reason",
    "trade_id",
    "event_id",
    "broker_order_id",
    "source",
]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[warn] {path}:{ln} JSON inválido — skip ({e})",
                      file=sys.stderr)
    return out


def dedup_by_event_id(events: list[dict]) -> list[dict]:
    """KAIZEN dedupe por (trade_id, event_id). Igual lógica acá."""
    seen: dict[tuple, dict] = {}
    for ev in events:
        key = (ev.get("trade_id", ""), ev.get("event_id", ""))
        # Si event_id está vacío, no dedupeamos (mantenemos todos)
        if not key[1]:
            seen[(ev.get("trade_id", ""), id(ev))] = ev
            continue
        # Si ya está, conservamos el más temprano (más fiel a la realidad)
        if key in seen:
            ts_old = seen[key].get("timestamp_utc", "")
            ts_new = ev.get("timestamp_utc", "")
            if ts_new < ts_old:
                seen[key] = ev
        else:
            seen[key] = ev
    return list(seen.values())


def filter_events(events: list[dict], symbol: str | None,
                  since: str | None, bot: str | None) -> list[dict]:
    out = events
    if symbol:
        symbol_norm = symbol.upper()
        out = [e for e in out if e.get("symbol", "").upper() == symbol_norm]
    if bot:
        bot_norm = bot.upper()
        out = [e for e in out if e.get("bot", "").upper() == bot_norm]
    if since:
        out = [e for e in out if e.get("timestamp_utc", "") >= since]
    return out


def flatten(ev: dict) -> dict[str, Any]:
    """Devuelve solo los campos del schema canónico — sin el sub-dict
    `enriched` (que tiene indicadores) para mantener el CSV legible."""
    return {k: ev.get(k, "") for k in COLS}


def write_csv(events: list[dict], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Ordenar por timestamp ascendente — lectura cronológica
    events_sorted = sorted(events, key=lambda e: e.get("timestamp_utc", ""))
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for ev in events_sorted:
            w.writerow(flatten(ev))
    return len(events_sorted)


def print_summary(events: list[dict]) -> None:
    """Resumen rápido por símbolo: cuántos buys/sells, PnL total, último evento."""
    by_sym: dict[str, dict] = defaultdict(
        lambda: {"buys": 0, "sells_tp": 0, "sells_stop": 0,
                 "sells_trail": 0, "sells_time": 0, "sells_other": 0,
                 "pnl": 0.0, "last_ts": "", "last_side": ""})

    for ev in events:
        sym = ev.get("symbol", "?")
        side = (ev.get("side") or "").upper()
        ts = ev.get("timestamp_utc", "")
        pnl = ev.get("realized_pnl_event")
        try:
            pnl_f = float(pnl) if pnl not in (None, "") else 0.0
        except (TypeError, ValueError):
            pnl_f = 0.0

        s = by_sym[sym]
        s["pnl"] += pnl_f
        if ts > s["last_ts"]:
            s["last_ts"] = ts
            s["last_side"] = side

        if side.startswith("BUY"):
            s["buys"] += 1
        elif "TP" in side:
            s["sells_tp"] += 1
        elif "STOP" in side:
            s["sells_stop"] += 1
        elif "TRAIL" in side:
            s["sells_trail"] += 1
        elif "TIME" in side:
            s["sells_time"] += 1
        elif side.startswith("SELL"):
            s["sells_other"] += 1

    print()
    print(f"{'SYMBOL':<12} {'BUY':>4} {'TP':>4} {'STOP':>5} {'TRAIL':>5} "
          f"{'TIME':>5} {'OTR':>4} {'PnL':>12}  {'LAST':<25}")
    print("─" * 90)
    total_pnl = 0.0
    for sym, s in sorted(by_sym.items()):
        total_pnl += s["pnl"]
        print(f"{sym:<12} {s['buys']:>4} {s['sells_tp']:>4} "
              f"{s['sells_stop']:>5} {s['sells_trail']:>5} "
              f"{s['sells_time']:>5} {s['sells_other']:>4} "
              f"${s['pnl']:>+10,.2f}  "
              f"{s['last_ts'][:19]:<19} {s['last_side']}")
    print("─" * 90)
    print(f"{'TOTAL':<12} {'':>4} {'':>4} {'':>5} {'':>5} {'':>5} {'':>4} "
          f"${total_pnl:>+10,.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rftm", default=str(REPO_ROOT / "logs" / "trade_events_rftm.jsonl"),
                    help="JSONL de RFTM")
    ap.add_argument("--mrev", default=str(REPO_ROOT / "logs" / "trade_events_mrev.jsonl"),
                    help="JSONL de MREV")
    ap.add_argument("--out", default=str(REPO_ROOT / "trade_events.csv"),
                    help="CSV de salida")
    ap.add_argument("--symbol", help="Filtrar por símbolo (case-insensitive)")
    ap.add_argument("--bot", choices=["RFTM", "MREV"], help="Filtrar por bot")
    ap.add_argument("--since", help="Timestamp UTC ISO (YYYY-MM-DD o ISO completo)")
    ap.add_argument("--summary", action="store_true",
                    help="Solo imprime el resumen por símbolo, no escribe CSV")
    args = ap.parse_args()

    rftm_path = Path(args.rftm)
    mrev_path = Path(args.mrev)
    out_path = Path(args.out)

    print(f"dump_trade_events.py")
    print(f"  RFTM:   {rftm_path}  ({'EXISTS' if rftm_path.exists() else 'MISSING'})")
    print(f"  MREV:   {mrev_path}  ({'EXISTS' if mrev_path.exists() else 'MISSING'})")

    rftm_events = read_jsonl(rftm_path)
    mrev_events = read_jsonl(mrev_path)
    print(f"  Read: RFTM={len(rftm_events)} eventos, MREV={len(mrev_events)} eventos")

    if not rftm_events and not mrev_events:
        print()
        print("  Sin eventos. Posibles causas:")
        print("   1. Nunca corriste `make sync-db` para traer los JSONL desde el branch state/db.")
        print("   2. Los workflows nunca generaron eventos (¿el bot nunca operó?).")
        print("   3. TRADE_EVENTS_JSONL_PATH apunta a otro lado.")
        return 1

    all_events = rftm_events + mrev_events
    all_events = dedup_by_event_id(all_events)
    print(f"  Tras dedupe por event_id: {len(all_events)} eventos únicos")

    if args.symbol or args.since or args.bot:
        before = len(all_events)
        all_events = filter_events(all_events, args.symbol, args.since, args.bot)
        print(f"  Tras filtros: {len(all_events)} (de {before})")

    if args.summary:
        print_summary(all_events)
        return 0

    n = write_csv(all_events, out_path)
    print()
    print(f"  ✓ {n} eventos escritos en {out_path}")
    print(f"    Abrilo con: open '{out_path}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
