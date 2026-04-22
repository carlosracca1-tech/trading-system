#!/usr/bin/env python3
"""
sell_half_profits.py — Toma de ganancias manual: vende 50% de cada posición
abierta en Alpaca que esté en positivo.

Uso:
    python3 sell_half_profits.py --dry-run    # preview sin enviar órdenes
    python3 sell_half_profits.py              # ejecuta ventas reales (paper)

Qué hace:
    1. Consulta GET /positions de Alpaca (paper).
    2. Filtra las que tienen unrealized_plpc > 0 (cualquier ganancia).
    3. Vende floor(qty/2) shares de cada una (market, day).
    4. Imprime resumen.

NO toca las posiciones en pérdida. NO cierra posiciones enteras.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env.paper"
ALPACA_URL = "https://paper-api.alpaca.markets/v2"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env()

KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")


def _req(method: str, path: str, body: dict | None = None) -> dict | list | None:
    if not KEY or not SECRET:
        print("ERROR: faltan ALPACA_API_KEY / ALPACA_SECRET_KEY en .env.paper")
        sys.exit(1)
    url = f"{ALPACA_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "APCA-API-KEY-ID": KEY,
            "APCA-API-SECRET-KEY": SECRET,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"HTTP {e.code} on {method} {path}: {body_text}")
        return None
    except Exception as e:
        print(f"error {method} {path}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no envía órdenes, solo muestra")
    ap.add_argument("--min-profit-pct", type=float, default=0.0,
                    help="vender solo si unrealized >= este umbral (ej: 0.03 para 3%%)")
    args = ap.parse_args()

    positions = _req("GET", "/positions")
    if not isinstance(positions, list):
        print("no se pudieron obtener posiciones")
        return 1

    if not positions:
        print("no hay posiciones abiertas en Alpaca.")
        return 0

    total_realized = 0.0
    rows = []
    for p in positions:
        sym = p.get("symbol", "?")
        qty = float(p.get("qty", 0))
        avg = float(p.get("avg_entry_price", 0))
        px = float(p.get("current_price", 0))
        plpc = float(p.get("unrealized_plpc", 0))  # e.g. 0.045 = +4.5%
        pl = float(p.get("unrealized_pl", 0))
        rows.append((sym, qty, avg, px, plpc, pl))

    rows.sort(key=lambda r: -r[4])

    print(f"{'SYMBOL':<8} {'QTY':>8} {'ENTRY':>10} {'PX':>10} {'P/L %':>9} {'P/L $':>12}  ACCIÓN")
    print("-" * 90)
    to_sell: list[tuple[str, int, float, float]] = []
    for sym, qty, avg, px, plpc, pl in rows:
        if plpc > args.min_profit_pct and qty >= 2:
            half = int(math.floor(qty / 2))
            if half < 1:
                action = "skip (qty<2)"
            else:
                action = f"VENDER {half}"
                to_sell.append((sym, half, px, plpc))
        elif plpc > args.min_profit_pct and qty < 2:
            action = "skip (qty<2)"
        else:
            action = "hold (no positivo / bajo umbral)"
        print(f"{sym:<8} {qty:>8.0f} {avg:>10.2f} {px:>10.2f} {plpc*100:>8.2f}% {pl:>+12.2f}  {action}")

    print()
    if not to_sell:
        print("Nada para vender con los filtros actuales.")
        return 0

    total_half_value = sum(q * px for _, q, px, _ in to_sell)
    print(f"Total a liquidar: {len(to_sell)} posiciones · ~${total_half_value:,.2f} en shares")
    print()

    if args.dry_run:
        print("DRY-RUN — no se enviaron órdenes.")
        return 0

    confirm = input("Ejecutar ventas? (escribí 'SI' para confirmar): ").strip()
    if confirm != "SI":
        print("cancelado.")
        return 0

    ok = 0
    fail = 0
    for sym, qty, px, plpc in to_sell:
        res = _req("POST", "/orders", {
            "symbol": sym, "qty": str(qty), "side": "sell",
            "type": "market", "time_in_force": "day",
        })
        if res and res.get("id"):
            print(f"  ✓ SELL {qty} {sym}  (id={res['id']})")
            ok += 1
            total_realized += qty * px
        else:
            print(f"  ✗ FAIL {qty} {sym}")
            fail += 1
        time.sleep(0.3)

    print()
    print(f"Listo: {ok} órdenes enviadas, {fail} fallaron. Valor bruto ~${total_realized:,.2f}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
