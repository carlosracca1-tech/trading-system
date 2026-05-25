#!/usr/bin/env python3
"""Diagnóstico rápido: qué dice Alpaca vs qué dicen las DBs locales.

Uso:
    cd ~/Desktop/trading-system
    python3 check_alpaca_state.py
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent


def _load_env_paper():
    """Mini parser para .env.paper sin depender de python-dotenv."""
    f = ROOT / ".env.paper"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env_paper()

KEY = os.getenv("ALPACA_API_KEY")
SEC = os.getenv("ALPACA_SECRET_KEY")
_RAW_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
# Normalizar: la var puede venir con o sin /v2 al final
URL = _RAW_URL[:-3] if _RAW_URL.endswith("/v2") else _RAW_URL


def _get(path, params=None):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    full = f"{URL}{path}{qs}"
    req = urllib.request.Request(full)
    req.add_header("APCA-API-KEY-ID", KEY or "")
    req.add_header("APCA-API-SECRET-KEY", SEC or "")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [HTTP {e.code} @ {full}] {body[:300]}")
        return None
    except Exception as e:
        print(f"  [ERROR @ {full}] {e}")
        return None


def hdr(s): print(f"\n{'='*70}\n  {s}\n{'='*70}")


def main():
    if not KEY or not SEC:
        print("ERROR: no encontré ALPACA_API_KEY / ALPACA_SECRET_KEY en .env.paper")
        sys.exit(1)

    print(f"  Base URL en uso: {URL}")

    hdr("ACCOUNT")
    a = _get("/v2/account") or {}
    for k in ("status", "cash", "buying_power", "equity", "portfolio_value",
             "last_equity", "long_market_value", "short_market_value"):
        if k in a:
            print(f"  {k:22s} {a[k]}")

    hdr("POSITIONS EN ALPACA (lo REAL)")
    pos = _get("/v2/positions") or []
    print(f"  Total: {len(pos)} posiciones")
    if not pos:
        print("  (ninguna posición abierta — Alpaca está vacío)")
    else:
        print(f"  {'Symbol':<12} {'Qty':>18} {'Avg Entry':>12} "
              f"{'Mkt Value':>14} {'Unrealized PnL':>16}")
        for p in sorted(pos, key=lambda x: x["symbol"]):
            sym = p.get("symbol", "?")
            qty = p.get("qty", "?")
            entry = p.get("avg_entry_price", "?")
            mv = p.get("market_value", "?")
            upnl = p.get("unrealized_pl", "?")
            print(f"  {sym:<12} {qty:>18} {entry:>12} {mv:>14} {upnl:>16}")

    hdr("ÓRDENES RECIENTES (últimas 20, incluye cancelled/filled)")
    orders = _get("/v2/orders",
                  {"status": "all", "limit": 20, "direction": "desc"}) or []
    for o in orders:
        print(f"  {o.get('submitted_at', '')[:19]}  "
             f"{o.get('side', ''):5s} {o.get('symbol', ''):10s} "
             f"qty={str(o.get('qty', '')):>12s} "
             f"status={o.get('status', ''):10s} "
             f"filled={str(o.get('filled_avg_price', 'n/a'))}")

    # Comparar con DBs locales
    hdr("LO QUE CREEN LOS BOTS (DBs locales)")
    try:
        c = sqlite3.connect(ROOT / "trading_paper.db")
        rftm = c.execute(
            "SELECT symbol, qty, entry_price FROM positions "
            "WHERE status='open' ORDER BY symbol"
        ).fetchall()
        print(f"\n  RFTM (ETFs): {len(rftm)} abiertas")
        for s, q, e in rftm:
            print(f"    {s:<12} qty={q:>6} entry=${e:.2f}")
    except Exception as e:
        print(f"  [no pude leer trading_paper.db: {e}]")

    try:
        c = sqlite3.connect(ROOT / "mrev_paper.db")
        mrev = c.execute(
            "SELECT symbol, qty, entry_price FROM mrev_positions "
            "WHERE status='OPEN' ORDER BY symbol"
        ).fetchall()
        print(f"\n  MREV (cripto): {len(mrev)} abiertas")
        for s, q, e in mrev:
            print(f"    {s:<14} qty={q:>14.4f} entry=${e:.4f}")
    except Exception as e:
        print(f"  [no pude leer mrev_paper.db: {e}]")

    print("\n" + "="*70)
    print("  Si POSITIONS EN ALPACA está vacío o tiene menos cosas que")
    print("  lo que creen los bots → hay un desincronice. Pegame la salida.")
    print("="*70)


if __name__ == "__main__":
    main()
