#!/usr/bin/env python3
"""
audit_alpaca_orders.py — Reconstruye el historial real de operaciones
desde Alpaca, sin depender del JSONL local ni del branch state/db.

Útil cuando:
- El branch state/db no existe (state_db_push.sh viene fallando).
- El JSONL de eventos no se sincronizó al Mac.
- Querés ver "qué pasó realmente" sin esperar a un workflow run nuevo.

Qué hace:
1. Lee credenciales de `.env.paper` (igual que check_alpaca_state.py).
2. Pide `/v2/orders?status=all` paginando por fechas.
3. Filtra por fecha (`--since`) y/o símbolo (`--symbol`).
4. Reconstruye el "view de trade" agrupando buys y sells por símbolo:
   - notional total comprado y vendido
   - precios promedio
   - PnL aproximado (sell_avg − buy_avg) × min(qty)
   - reasoning desde el client_order_id si el bot lo dejó
5. Imprime resumen + CSV opcional.

Uso:
    python3 scripts/audit_alpaca_orders.py
    python3 scripts/audit_alpaca_orders.py --since 2026-05-10
    python3 scripts/audit_alpaca_orders.py --since 2026-05-10 --symbol XLE
    python3 scripts/audit_alpaca_orders.py --since 2026-05-10 --out outputs/semana_mala_orders.csv
    python3 scripts/audit_alpaca_orders.py --raw   # imprime cada order, no agrupa

NO ejecuta órdenes ni modifica la cuenta. Solo GET /v2/orders.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent


def _load_env_paper() -> None:
    """Mini parser de .env.paper — sin python-dotenv."""
    f = ROOT / ".env.paper"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_paper()
KEY = os.getenv("ALPACA_API_KEY") or ""
SEC = os.getenv("ALPACA_SECRET_KEY") or ""
_RAW_URL = os.getenv(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
URL = _RAW_URL[:-3] if _RAW_URL.endswith("/v2") else _RAW_URL


def _get(path: str, params: dict | None = None) -> Any:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    full = f"{URL}{path}{qs}"
    req = urllib.request.Request(full)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SEC)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[HTTP {e.code}] {full}\n  {body[:400]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] {full}: {e}", file=sys.stderr)
        return None


def fetch_orders(since_iso: str | None, until_iso: str | None) -> list[dict]:
    """Pagina GET /v2/orders en ventanas para evitar el limit=500 por call.

    Alpaca devuelve hasta 500 orders por request. Vamos pidiendo en
    ventanas de 7 días desde `since` (o desde hace 60 días si no se
    especifica) hasta `until` (o ahora).
    """
    now = datetime.now(tz=timezone.utc)
    if since_iso:
        # Acepta YYYY-MM-DD o ISO completo
        if "T" not in since_iso:
            since_dt = datetime.strptime(since_iso, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        else:
            since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    else:
        since_dt = now - timedelta(days=60)

    if until_iso:
        if "T" not in until_iso:
            until_dt = datetime.strptime(until_iso, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) + timedelta(days=1)
        else:
            until_dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
    else:
        until_dt = now

    print(f"Pidiendo orders desde {since_dt.isoformat()} hasta {until_dt.isoformat()}",
          file=sys.stderr)

    all_orders: list[dict] = []
    seen_ids: set[str] = set()
    window = timedelta(days=7)
    cursor = since_dt
    while cursor < until_dt:
        nxt = min(cursor + window, until_dt)
        params = {
            "status": "all",
            "after": cursor.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "until": nxt.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "direction": "asc",
            "limit": 500,
            "nested": "true",
        }
        batch = _get("/v2/orders", params=params)
        if batch is None:
            print(f"  warn: batch falló para {cursor.date()}—{nxt.date()}, sigo",
                  file=sys.stderr)
            cursor = nxt
            continue
        new_count = 0
        for o in batch:
            oid = o.get("id")
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                all_orders.append(o)
                new_count += 1
        print(f"  {cursor.date()}—{nxt.date()}: {len(batch)} orders ({new_count} nuevos)",
              file=sys.stderr)
        cursor = nxt

    # Orden por filled_at o submitted_at, ascendente
    def _ts(o: dict) -> str:
        return (o.get("filled_at") or o.get("submitted_at")
                or o.get("created_at") or "")
    all_orders.sort(key=_ts)
    return all_orders


def normalize_order(o: dict) -> dict:
    """Aplana un order de Alpaca a un dict tabular legible."""
    fillp = o.get("filled_avg_price")
    qty = o.get("filled_qty") or o.get("qty") or 0
    try:
        fillp_f = float(fillp) if fillp else 0.0
    except (TypeError, ValueError):
        fillp_f = 0.0
    try:
        qty_f = float(qty) if qty else 0.0
    except (TypeError, ValueError):
        qty_f = 0.0
    notional = round(fillp_f * qty_f, 4) if fillp_f and qty_f else 0.0
    return {
        "filled_at": o.get("filled_at") or o.get("submitted_at") or "",
        "submitted_at": o.get("submitted_at") or "",
        "symbol": o.get("symbol", ""),
        "side": o.get("side", ""),
        "type": o.get("type", ""),
        "qty": qty_f,
        "filled_avg_price": fillp_f,
        "notional": notional,
        "status": o.get("status", ""),
        "client_order_id": o.get("client_order_id", "") or "",
        "id": o.get("id", ""),
    }


def filter_orders(orders: list[dict], symbol: Optional[str],
                  since: Optional[str]) -> list[dict]:
    out = orders
    if symbol:
        sym = symbol.upper()
        out = [o for o in out if o.get("symbol", "").upper() == sym]
    if since:
        # Compara timestamps como strings ISO (lexicográfico = cronológico)
        out = [o for o in out if (o.get("filled_at") or
                                   o.get("submitted_at") or "") >= since]
    # Solo filled — los canceled/rejected no ejecutaron
    out = [o for o in out if o.get("status") == "filled"]
    return out


def summarize(orders: list[dict]) -> None:
    """Agrupa por símbolo y reconstruye una vista de trade-level."""
    by_sym: dict[str, dict] = defaultdict(
        lambda: {"buys": 0, "sells": 0, "bought_qty": 0.0, "sold_qty": 0.0,
                 "bought_notional": 0.0, "sold_notional": 0.0,
                 "first": "", "last": ""})
    total_bought = 0.0
    total_sold = 0.0

    for o in orders:
        sym = o["symbol"]
        side = o["side"].lower()
        qty = float(o["qty"] or 0)
        notional = float(o["notional"] or 0)
        ts = o["filled_at"]
        s = by_sym[sym]
        if not s["first"] or ts < s["first"]:
            s["first"] = ts
        if not s["last"] or ts > s["last"]:
            s["last"] = ts
        if side == "buy":
            s["buys"] += 1
            s["bought_qty"] += qty
            s["bought_notional"] += notional
            total_bought += notional
        elif side == "sell":
            s["sells"] += 1
            s["sold_qty"] += qty
            s["sold_notional"] += notional
            total_sold += notional

    print()
    print(f"{'SYMBOL':<12} {'BUY':>3} {'SELL':>4} {'BQTY':>10} {'SQTY':>10} "
          f"{'BUY_AVG':>9} {'SELL_AVG':>9} {'EST_PnL':>11}  {'FIRST':<19}  {'LAST':<19}")
    print("─" * 130)
    total_pnl = 0.0
    for sym, s in sorted(by_sym.items()):
        buy_avg = (s["bought_notional"] / s["bought_qty"]
                   if s["bought_qty"] else 0)
        sell_avg = (s["sold_notional"] / s["sold_qty"]
                    if s["sold_qty"] else 0)
        # PnL aprox: (sell_avg − buy_avg) × qty efectivamente cerrada
        closed_qty = min(s["bought_qty"], s["sold_qty"])
        est_pnl = (sell_avg - buy_avg) * closed_qty if buy_avg and sell_avg else 0
        total_pnl += est_pnl
        print(f"{sym:<12} {s['buys']:>3} {s['sells']:>4} "
              f"{s['bought_qty']:>10.2f} {s['sold_qty']:>10.2f} "
              f"${buy_avg:>7.2f} ${sell_avg:>7.2f} "
              f"${est_pnl:>+10,.2f}  "
              f"{s['first'][:19]:<19}  {s['last'][:19]:<19}")
    print("─" * 130)
    print(f"{'TOTAL':<12} {'':>3} {'':>4} {'':>10} {'':>10} "
          f"{'':>9} {'':>9} ${total_pnl:>+10,.2f}")
    print()
    print(f"Notional buys totales:  ${total_bought:>15,.2f}")
    print(f"Notional sells totales: ${total_sold:>15,.2f}")
    print(f"PnL agregado estimado:  ${total_pnl:>+15,.2f}")
    print()
    print("Nota: 'EST_PnL' es aproximado: (sell_avg − buy_avg) × min(BQTY, SQTY).")
    print("No considera múltiples lotes, FIFO/LIFO, ni posiciones que siguen abiertas.")
    print("Para análisis preciso, usá el CSV con --out y miralo trade por trade.")


def write_csv(orders: list[dict], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["filled_at", "symbol", "side", "qty", "filled_avg_price",
            "notional", "type", "status", "client_order_id", "id"]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for o in orders:
            w.writerow(o)
    return len(orders)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="Fecha YYYY-MM-DD o ISO completo "
                                     "(default: hace 60 días)")
    ap.add_argument("--until", help="Fecha YYYY-MM-DD o ISO (default: ahora)")
    ap.add_argument("--symbol", help="Filtrar por símbolo (case-insensitive)")
    ap.add_argument("--out", help="CSV de salida (si no, solo printea summary)")
    ap.add_argument("--raw", action="store_true",
                    help="Imprime cada order como fila, no agrupa")
    args = ap.parse_args()

    if not KEY or not SEC:
        print("ERROR: faltan ALPACA_API_KEY / ALPACA_SECRET_KEY (.env.paper)",
              file=sys.stderr)
        return 1

    print(f"audit_alpaca_orders.py — {URL}", file=sys.stderr)
    raw_orders = fetch_orders(args.since, args.until)
    print(f"Total orders fetched: {len(raw_orders)}", file=sys.stderr)

    normalized = [normalize_order(o) for o in raw_orders]
    filtered = filter_orders(normalized, args.symbol, args.since)
    print(f"Tras filtros (status=filled, symbol, since): {len(filtered)}",
          file=sys.stderr)

    if args.raw:
        print()
        print(f"{'FILLED_AT':<25} {'SYMBOL':<10} {'SIDE':<4} "
              f"{'QTY':>10} {'PRICE':>10} {'NOTIONAL':>12}  {'REASON':<40}")
        for o in filtered:
            reason = o["client_order_id"]
            print(f"{o['filled_at'][:19]:<25} {o['symbol']:<10} "
                  f"{o['side']:<4} {o['qty']:>10.2f} "
                  f"${o['filled_avg_price']:>8.2f} "
                  f"${o['notional']:>10.2f}  {reason[:40]}")
    else:
        summarize(filtered)

    if args.out:
        out_path = Path(args.out)
        n = write_csv(filtered, out_path)
        print()
        print(f"  ✓ {n} orders escritos en {out_path}")
        print(f"    open '{out_path}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
