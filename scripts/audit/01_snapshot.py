"""Fetch live state from Alpaca and dump to disk for downstream analysis."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import _alpaca as A

OUT = Path(__file__).parent


def run():
    print("== account ==")
    acct = A.get("/account")
    (OUT / "account.json").write_text(json.dumps(acct, indent=2))
    print(f"  status={acct['status']} equity={acct['equity']} last_equity={acct['last_equity']}")
    print(f"  cash={acct['cash']} long_market_value={acct['long_market_value']}")
    print(f"  portfolio_value={acct['portfolio_value']} buying_power={acct['buying_power']}")

    print("\n== positions ==")
    pos = A.get("/positions")
    (OUT / "positions.json").write_text(json.dumps(pos, indent=2))
    for p in pos:
        print(f"  {p['symbol']:<10} qty={p['qty']:<12} avg_entry={p['avg_entry_price']:<10} "
              f"current={p['current_price']:<10} unrealized_pl={p['unrealized_pl']:<12} "
              f"unrealized_plpc={p['unrealized_plpc']}")
    print(f"  ({len(pos)} positions)")

    print("\n== orders (last 60d filled) ==")
    after = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    all_orders = []
    for o in A.iter_orders(status="all", after=after, direction="desc", nested=True):
        all_orders.append(o)
    (OUT / "orders_60d.json").write_text(json.dumps(all_orders, indent=2))
    filled = [o for o in all_orders if o.get("status") == "filled"]
    print(f"  total: {len(all_orders)}   filled: {len(filled)}")

    print("\n== portfolio history 90d ==")
    ph = A.get("/account/portfolio/history", {"period": "90D", "timeframe": "1D"})
    (OUT / "portfolio_history_90d.json").write_text(json.dumps(ph, indent=2))
    print(f"  equity points: {len(ph.get('equity', []))} base={ph.get('base_value')} "
          f"timeframe={ph.get('timeframe')}")

    print("\n== portfolio history 30d/1H (for intraday DD) ==")
    ph_intraday = A.get("/account/portfolio/history", {"period": "30D", "timeframe": "1H"})
    (OUT / "portfolio_history_30d_1h.json").write_text(json.dumps(ph_intraday, indent=2))
    print(f"  equity points: {len(ph_intraday.get('equity', []))}")

    print("\n== account activities (fills last 60d) ==")
    activities = []
    for a in A.iter_activities(activity_types="FILL", after=after):
        activities.append(a)
    (OUT / "activities_fills_60d.json").write_text(json.dumps(activities, indent=2))
    print(f"  fills: {len(activities)}")


if __name__ == "__main__":
    run()
