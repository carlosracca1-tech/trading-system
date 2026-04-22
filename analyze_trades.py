#!/usr/bin/env python3
"""
analyze_trades.py — Extrae TODA la data de Alpaca para análisis completo.
Ejecutar desde la carpeta del proyecto:
    cd ~/Desktop/trading-system && python3 analyze_trades.py
"""
import json, os, sqlite3, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

# ── Load .env ────────────────────────────────────────────────────────────────
for name in (".env.paper", ".env"):
    p = Path(name)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

KEY = os.environ["ALPACA_API_KEY"]
SECRET = os.environ["ALPACA_SECRET_KEY"]
BASE = "https://paper-api.alpaca.markets/v2"

def api_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": KEY,
        "APCA-API-SECRET-KEY": SECRET,
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

result = {}

# 1. Account
print("Fetching account...")
result["account"] = api_get("/account")

# 2. Open positions
print("Fetching positions...")
result["positions"] = api_get("/positions")

# 3. ALL orders (paginated)
print("Fetching all orders...")
all_orders = []
after = None
for _ in range(20):  # max 20 pages
    params = {"status": "all", "limit": 500, "direction": "asc"}
    if after:
        params["after"] = after
    batch = api_get("/orders", params)
    if not batch:
        break
    all_orders.extend(batch)
    after = batch[-1]["id"]
    if len(batch) < 500:
        break
print(f"  Total orders: {len(all_orders)}")
result["orders"] = all_orders

# 4. Portfolio history
print("Fetching portfolio history...")
try:
    result["portfolio_history"] = api_get("/account/portfolio/history", {
        "period": "1M", "timeframe": "1D"
    })
except Exception as e:
    print(f"  Portfolio history failed: {e}")
    result["portfolio_history"] = None

# 5. Activities (fills, dividends, etc.)
print("Fetching activities...")
try:
    activities = api_get("/account/activities/FILL", {"direction": "asc", "page_size": 500})
    result["activities"] = activities
    print(f"  Total fill activities: {len(activities)}")
except Exception as e:
    print(f"  Activities failed: {e}")
    result["activities"] = []

# 6. Local DB data (RFTM)
rftm_db = Path(os.environ.get("TMPDIR", "/tmp")) / "rftm_trader" / "trading_paper.db"
if rftm_db.exists():
    print(f"Reading RFTM DB: {rftm_db}")
    conn = sqlite3.connect(str(rftm_db))
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    result["rftm_db"] = {"path": str(rftm_db), "tables": {}}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        result["rftm_db"]["tables"][t] = [dict(r) for r in rows]
        print(f"  {t}: {len(rows)} rows")
    conn.close()
else:
    print(f"RFTM DB not found at {rftm_db}")
    result["rftm_db"] = None

# 7. Local DB data (MREV)
mrev_db = Path("mrev_paper.db")
if mrev_db.exists():
    print(f"Reading MREV DB: {mrev_db}")
    conn = sqlite3.connect(str(mrev_db))
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    result["mrev_db"] = {"path": str(mrev_db), "tables": {}}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        result["mrev_db"]["tables"][t] = [dict(r) for r in rows]
        print(f"  {t}: {len(rows)} rows")
    conn.close()
else:
    print(f"MREV DB not found at {mrev_db}")
    result["mrev_db"] = None

# ── Save output ──────────────────────────────────────────────────────────────
out = Path("trade_analysis_data.json")
with open(out, "w") as f:
    json.dump(result, f, indent=2, default=str)

print(f"\n✅ Data saved to {out.resolve()}")
print(f"   File size: {out.stat().st_size / 1024:.1f} KB")

# ── Quick summary ────────────────────────────────────────────────────────────
acct = result["account"]
print(f"\n{'='*60}")
print(f"RESUMEN RÁPIDO DE LA CUENTA")
print(f"{'='*60}")
print(f"Equity:          ${float(acct['equity']):>12,.2f}")
print(f"Cash:            ${float(acct['cash']):>12,.2f}")
print(f"Portfolio value: ${float(acct['portfolio_value']):>12,.2f}")
print(f"Long mkt value:  ${float(acct['long_market_value']):>12,.2f}")
print(f"Buying power:    ${float(acct['buying_power']):>12,.2f}")

filled = [o for o in all_orders if o["status"] == "filled"]
buys = [o for o in filled if o["side"] == "buy"]
sells = [o for o in filled if o["side"] == "sell"]
print(f"\nÓrdenes ejecutadas: {len(filled)} (compras: {len(buys)}, ventas: {len(sells)})")

total_bought = sum(float(o.get("filled_avg_price", 0)) * float(o.get("filled_qty", 0)) for o in buys)
total_sold = sum(float(o.get("filled_avg_price", 0)) * float(o.get("filled_qty", 0)) for o in sells)
print(f"Total comprado: ${total_bought:>12,.2f}")
print(f"Total vendido:  ${total_sold:>12,.2f}")

print(f"\nPosiciones abiertas: {len(result['positions'])}")
for p in result["positions"]:
    sym = p["symbol"]
    qty = float(p["qty"])
    entry = float(p["avg_entry_price"])
    current = float(p["current_price"])
    pnl = float(p["unrealized_pl"])
    pnl_pct = float(p["unrealized_plpc"]) * 100
    mkt_val = float(p["market_value"])
    print(f"  {sym}: {qty} @ ${entry:.4f} → ${current:.4f} | valor: ${mkt_val:.2f} | P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")

print(f"\n{'='*60}")
print("Detalle de TODAS las órdenes ejecutadas:")
print(f"{'='*60}")
for o in filled:
    sym = o["symbol"]
    side = o["side"].upper()
    qty = float(o.get("filled_qty", o["qty"]))
    price = float(o.get("filled_avg_price", 0))
    total = qty * price
    filled_at = o.get("filled_at", "")[:19]
    print(f"  {filled_at} | {side:4s} | {sym:<10s} | {qty:>10.4f} @ ${price:>10.4f} = ${total:>12.2f}")

# Portfolio history
if result.get("portfolio_history"):
    ph = result["portfolio_history"]
    if ph.get("equity") and ph.get("timestamp"):
        print(f"\n{'='*60}")
        print("Historial de equity diario:")
        print(f"{'='*60}")
        for ts, eq, pnl in zip(ph["timestamp"], ph["equity"], ph.get("profit_loss", [0]*len(ph["equity"]))):
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            print(f"  {dt}: equity=${eq:>12,.2f} | daily P&L=${pnl:>10,.2f}")
