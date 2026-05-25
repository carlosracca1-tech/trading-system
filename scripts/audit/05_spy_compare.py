"""Compare portfolio return vs buy-and-hold SPY since first buy."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.parse
import _alpaca as A

OUT = Path(__file__).parent


def main():
    orders = json.loads((OUT / "orders_60d.json").read_text())
    fills = [o for o in orders if o.get("status") == "filled"]
    buys = [o for o in fills if o["side"] == "buy"]
    buys.sort(key=lambda o: o["filled_at"])
    if not buys:
        print("No buys in window.")
        return
    first_buy = buys[0]
    print(f"First buy: {first_buy['symbol']} on {first_buy['filled_at']} @ {first_buy['filled_avg_price']}")

    start_iso = first_buy["filled_at"][:10] + "T00:00:00Z"
    # Alpaca data API needs RFC3339
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Daily bars for SPY
    params = {"timeframe": "1Day", "start": start_iso, "end": now, "limit": 200, "adjustment": "raw", "feed": "iex"}
    data = A.get("/stocks/SPY/bars", params, data_api=True)
    bars = data.get("bars", [])
    if not bars:
        print(f"No SPY bars returned. Response: {list(data)[:5]}")
        return
    spy_start = bars[0]["c"]
    spy_end = bars[-1]["c"]
    spy_ret = (spy_end / spy_start - 1) * 100

    # Portfolio
    acct = json.loads((OUT / "account.json").read_text())
    equity_now = float(acct["equity"])
    # Portfolio start — use the closest equity history point on or after start_iso
    ph = json.loads((OUT / "portfolio_history_90d.json").read_text())
    start_date = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).date()
    eq_start = None
    for t, e in zip(ph["timestamp"], ph["equity"]):
        d = datetime.fromtimestamp(t, tz=timezone.utc).date()
        if d >= start_date and e:
            eq_start = e
            break
    if eq_start is None:
        eq_start = ph["base_value"]
    port_ret = (equity_now / eq_start - 1) * 100

    print(f"\n## Portfolio vs SPY buy-and-hold since {start_iso[:10]}")
    print(f"Portfolio: ${eq_start:,.2f} → ${equity_now:,.2f}  ({port_ret:+.3f}%)")
    print(f"SPY close: ${spy_start:.2f} → ${spy_end:.2f}  ({spy_ret:+.3f}%)")
    print(f"Alpha (port - SPY): {port_ret - spy_ret:+.3f} pp")

    # Info ratio sketch: use daily returns
    daily = []
    spy_daily = {b["t"][:10]: b["c"] for b in bars}
    prev_eq = None
    for t, e in zip(ph["timestamp"], ph["equity"]):
        d = datetime.fromtimestamp(t, tz=timezone.utc).date()
        if d < start_date:
            continue
        if e is None or e == 0:
            continue
        if prev_eq:
            port_r = e / prev_eq - 1.0
            daily.append((d, port_r, e))
        prev_eq = e

    # Build SPY daily returns matching dates
    spy_dates = sorted(spy_daily.keys())
    spy_ret_map = {}
    for i in range(1, len(spy_dates)):
        prev = spy_daily[spy_dates[i-1]]
        cur = spy_daily[spy_dates[i]]
        spy_ret_map[spy_dates[i]] = cur / prev - 1.0

    diffs = []
    for d, port_r, _ in daily:
        s = spy_ret_map.get(d.isoformat())
        if s is not None:
            diffs.append(port_r - s)
    if diffs:
        import statistics
        mean_active = statistics.mean(diffs)
        stdev_active = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        import math
        ir = (mean_active / stdev_active * math.sqrt(252)) if stdev_active else 0.0
        print(f"\nInformation ratio (port vs SPY, {len(diffs)} days): {ir:.2f}")
        print(f"Mean active return: {mean_active * 100:+.4f}% / day  Tracking error: {stdev_active * 100:.4f}%/day")


if __name__ == "__main__":
    main()
