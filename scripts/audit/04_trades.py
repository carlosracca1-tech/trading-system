"""Reconstruct closed trades via FIFO on Alpaca fills. Produce per-bot P&L stats."""
from __future__ import annotations
import csv
import json
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent

# From CLAUDE.md + standalone_*.py universe
CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "DOGEUSD", "LINKUSD"}


def bot_of(sym: str) -> str:
    if sym in CRYPTO or (sym.endswith("USD") and len(sym) <= 8):
        return "MREV"
    return "RFTM"


def _parse(ts):
    s = ts.replace("Z", "+00:00")
    # Normalize fractional seconds to 6 digits (Py 3.9 is strict)
    import re
    m = re.match(r"^(.*?)(\.\d+)?([+-]\d{2}:\d{2})?$", s)
    if m and m.group(2):
        frac = m.group(2)[1:]
        if len(frac) > 6:
            frac = frac[:6]
        else:
            frac = frac.ljust(6, "0")
        s = f"{m.group(1)}.{frac}{m.group(3) or ''}"
    return datetime.fromisoformat(s)


def main():
    orders = json.loads((OUT / "orders_60d.json").read_text())
    fills = [o for o in orders if o.get("status") == "filled"]
    # Sort ascending by filled_at (FIFO requires chronological order)
    fills.sort(key=lambda o: o.get("filled_at") or o.get("submitted_at"))

    open_lots = defaultdict(deque)  # sym -> deque of (ts, qty_remaining, price)
    closed = []

    for o in fills:
        sym = o["symbol"]
        side = o["side"]
        qty = float(o.get("filled_qty") or 0)
        px = float(o.get("filled_avg_price") or 0)
        ts = _parse(o["filled_at"])
        if qty <= 0 or px <= 0:
            continue

        if side == "buy":
            open_lots[sym].append({"ts": ts, "qty": qty, "price": px, "order_id": o["id"]})
        else:  # sell
            remaining = qty
            while remaining > 1e-9 and open_lots[sym]:
                lot = open_lots[sym][0]
                matched = min(remaining, lot["qty"])
                hold_h = (ts - lot["ts"]).total_seconds() / 3600.0
                gross = (px - lot["price"]) * matched
                pct = (px / lot["price"] - 1.0) * 100
                closed.append({
                    "symbol": sym,
                    "bot": bot_of(sym),
                    "entry_dt": lot["ts"].isoformat(),
                    "entry_price": round(lot["price"], 6),
                    "exit_dt": ts.isoformat(),
                    "exit_price": round(px, 6),
                    "qty_closed": round(matched, 6),
                    "gross_pnl_usd": round(gross, 4),
                    "gross_pnl_pct": round(pct, 4),
                    "holding_hours": round(hold_h, 2),
                    "entry_order_id": lot["order_id"],
                    "exit_order_id": o["id"],
                })
                lot["qty"] -= matched
                remaining -= matched
                if lot["qty"] <= 1e-9:
                    open_lots[sym].popleft()
            if remaining > 1e-6:
                # Over-sell without buy lots visible in 60d window — shouldn't happen but flag
                closed.append({
                    "symbol": sym, "bot": bot_of(sym),
                    "entry_dt": "UNKNOWN_PRIOR_TO_60D", "entry_price": "",
                    "exit_dt": ts.isoformat(), "exit_price": round(px, 6),
                    "qty_closed": round(remaining, 6),
                    "gross_pnl_usd": "", "gross_pnl_pct": "",
                    "holding_hours": "", "entry_order_id": "",
                    "exit_order_id": o["id"],
                })

    # Write CSV
    fields = ["symbol", "bot", "entry_dt", "entry_price", "exit_dt", "exit_price",
              "qty_closed", "gross_pnl_usd", "gross_pnl_pct", "holding_hours",
              "entry_order_id", "exit_order_id"]
    with (OUT / "trades_closed.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(closed)

    print(f"# Closed trades: {len(closed)}")

    # Filter only those with numeric P&L
    numeric = [t for t in closed if isinstance(t["gross_pnl_usd"], (int, float))]
    print(f"# With computable P&L (both entry+exit in 60d): {len(numeric)}")

    # Currently open lots value
    print("\n## Unmatched open lots (qty currently open per FIFO)")
    for sym, lots in sorted(open_lots.items()):
        total_qty = sum(l["qty"] for l in lots)
        if total_qty > 1e-6:
            print(f"  {sym}: {len(lots)} lots, total_qty={total_qty:.4f}")

    # Top winners / losers
    ranked = sorted(numeric, key=lambda t: t["gross_pnl_pct"], reverse=True)
    print("\n## Top 10 winners (% basis)")
    print("| symbol | bot | entry_dt | exit_dt | entry | exit | qty | pnl_usd | pnl_% | hold_h |")
    print("| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |")
    for t in ranked[:10]:
        print(f"| {t['symbol']} | {t['bot']} | {t['entry_dt']} | {t['exit_dt']} | "
              f"{t['entry_price']} | {t['exit_price']} | {t['qty_closed']} | "
              f"{t['gross_pnl_usd']:+.2f} | {t['gross_pnl_pct']:+.3f} | {t['holding_hours']} |")
    print("\n## Top 10 losers")
    print("| symbol | bot | entry_dt | exit_dt | entry | exit | qty | pnl_usd | pnl_% | hold_h |")
    print("| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |")
    for t in ranked[-10:][::-1]:
        print(f"| {t['symbol']} | {t['bot']} | {t['entry_dt']} | {t['exit_dt']} | "
              f"{t['entry_price']} | {t['exit_price']} | {t['qty_closed']} | "
              f"{t['gross_pnl_usd']:+.2f} | {t['gross_pnl_pct']:+.3f} | {t['holding_hours']} |")

    # Stats by bot
    print("\n## Stats by bot")
    print("| bot | trades | win_rate_% | avg_winner_% | avg_loser_% | "
          "expectancy_% | profit_factor | total_pnl_usd | avg_trade_usd | median_hold_h | p90_hold_h |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for bot in ("RFTM", "MREV"):
        ts = [t for t in numeric if t["bot"] == bot]
        if not ts:
            continue
        wins = [t for t in ts if t["gross_pnl_pct"] > 0]
        losses = [t for t in ts if t["gross_pnl_pct"] <= 0]
        wr = len(wins) / len(ts) * 100
        avg_w = statistics.mean([t["gross_pnl_pct"] for t in wins]) if wins else 0.0
        avg_l = statistics.mean([t["gross_pnl_pct"] for t in losses]) if losses else 0.0
        exp = wr / 100 * avg_w + (1 - wr / 100) * avg_l
        pf = (sum(t["gross_pnl_usd"] for t in wins) /
              abs(sum(t["gross_pnl_usd"] for t in losses))) if losses and sum(t["gross_pnl_usd"] for t in losses) < 0 else float("inf")
        total_pnl = sum(t["gross_pnl_usd"] for t in ts)
        holds = [t["holding_hours"] for t in ts]
        print(f"| {bot} | {len(ts)} | {wr:.1f} | {avg_w:+.3f} | {avg_l:+.3f} | "
              f"{exp:+.3f} | {pf:.2f} | {total_pnl:+.2f} | {total_pnl/len(ts):+.2f} | "
              f"{statistics.median(holds):.1f} | {sorted(holds)[int(len(holds)*0.9)]:.1f} |")

    # Stats by symbol
    print("\n## Stats by symbol")
    print("| symbol | bot | trades | win_rate_% | total_pnl_usd | avg_pct | best_% | worst_% |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    by_sym = defaultdict(list)
    for t in numeric:
        by_sym[t["symbol"]].append(t)
    for sym, ts in sorted(by_sym.items(), key=lambda x: -sum(t["gross_pnl_usd"] for t in x[1])):
        wr = sum(1 for t in ts if t["gross_pnl_pct"] > 0) / len(ts) * 100
        total = sum(t["gross_pnl_usd"] for t in ts)
        avg = statistics.mean(t["gross_pnl_pct"] for t in ts)
        best = max(t["gross_pnl_pct"] for t in ts)
        worst = min(t["gross_pnl_pct"] for t in ts)
        print(f"| {sym} | {bot_of(sym)} | {len(ts)} | {wr:.1f} | {total:+.2f} | "
              f"{avg:+.3f} | {best:+.3f} | {worst:+.3f} |")

    # Hold time distribution
    holds = [t["holding_hours"] for t in numeric]
    if holds:
        holds_sorted = sorted(holds)
        print(f"\n## Hold time — median: {statistics.median(holds):.1f}h   "
              f"mean: {statistics.mean(holds):.1f}h   "
              f"p90: {holds_sorted[int(len(holds)*0.9)]:.1f}h   "
              f"max: {max(holds):.1f}h")


if __name__ == "__main__":
    main()
