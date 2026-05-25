"""Forensic of orders on 2026-04-22 and 2026-04-23."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent

# RFTM params from CLAUDE.md
RFTM_TP1 = 0.05
RFTM_TP2 = 0.075

# MREV params
MREV_TP1 = 0.05
MREV_TP2 = 0.075


def _ts(s):
    return s[:19]  # trim microseconds


def classify_exit(pnl_pct, bot):
    if pnl_pct is None:
        return "UNKNOWN"
    if bot == "RFTM":
        if 4.5 <= pnl_pct <= 5.8:
            return "TP1_EXACT"
        if 7.0 <= pnl_pct <= 8.5:
            return "TP2_EXACT"
        if pnl_pct < 0:
            return "STOP_OR_EXIT_SIGNAL"
        if 0 <= pnl_pct < 4.5:
            return "EARLY_EXIT_POSITIVE"
        return "ABOVE_TP2"
    else:
        if 4.0 <= pnl_pct <= 5.8:
            return "TP1_EXACT"
        if 7.0 <= pnl_pct <= 8.5:
            return "TP2_EXACT"
        if pnl_pct < 0:
            return "STOP_OR_EXIT_SIGNAL"
        if 0 <= pnl_pct < 4.0:
            return "EARLY_EXIT_POSITIVE"
        return "ABOVE_TP2"


def main():
    orders = json.loads((OUT / "orders_60d.json").read_text())
    fills = [o for o in orders if o.get("status") == "filled"]
    fills.sort(key=lambda o: o["filled_at"])

    # Per symbol running position (FIFO)
    from collections import defaultdict, deque
    lots = defaultdict(deque)

    relevant = []
    for o in fills:
        sym = o["symbol"]
        side = o["side"]
        qty = float(o.get("filled_qty") or 0)
        px = float(o.get("filled_avg_price") or 0)
        ts = o["filled_at"]
        if side == "buy":
            lots[sym].append({"ts": ts, "qty": qty, "price": px})
            matched_entry = None
            pnl_pct = None
        else:
            rem = qty
            matched_prices = []
            while rem > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                m = min(rem, lot["qty"])
                matched_prices.append((lot["price"], m))
                rem -= m
                lot["qty"] -= m
                if lot["qty"] <= 1e-9:
                    lots[sym].popleft()
            if matched_prices:
                w_entry = sum(p * q for p, q in matched_prices) / sum(q for _, q in matched_prices)
                pnl_pct = (px / w_entry - 1.0) * 100.0
                matched_entry = round(w_entry, 4)
            else:
                pnl_pct = None
                matched_entry = None

        # Filter to window of interest
        dts = ts[:10]
        if dts in ("2026-04-22", "2026-04-23", "2026-04-21"):
            bot = "MREV" if "/USD" in sym else "RFTM"
            relevant.append({
                "ts_utc": ts, "symbol": sym, "side": side, "qty": round(qty, 6),
                "price": round(px, 6), "entry_avg": matched_entry,
                "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
                "bot": bot,
                "classification": classify_exit(pnl_pct, bot) if side == "sell" else "ENTRY",
                "order_id": o["id"],
            })

    # Remaining open lots currently (per FIFO)
    current_open = {sym: sum(l["qty"] for l in d) for sym, d in lots.items() if d}

    # Print
    print(f"# Orders filled 2026-04-21 through 2026-04-23 UTC")
    print(f"Total: {len(relevant)}")
    print()
    print("| ts_utc | symbol | side | qty | price | entry_avg | pnl_% | classification |")
    print("| --- | --- | --- | ---: | ---: | ---: | ---: | --- |")
    for r in relevant:
        print(f"| {r['ts_utc']} | {r['symbol']} | {r['side']} | {r['qty']} | "
              f"{r['price']} | {r['entry_avg'] or ''} | "
              f"{r['pnl_pct'] if r['pnl_pct'] is not None else ''} | {r['classification']} |")

    # Focus on 16:18 cascade
    cascade = [r for r in relevant if r["ts_utc"].startswith("2026-04-22T16:18")]
    print(f"\n## 16:18 UTC cascade — {len(cascade)} fills")
    for r in cascade:
        print(f"  {r['ts_utc']} {r['symbol']:<10} {r['side']:<4} qty={r['qty']:<10} "
              f"px={r['price']:<10} entry={r['entry_avg']} pnl%={r['pnl_pct']} {r['classification']}")

    # Remaining qty after all fills
    print("\n## Remaining open qty per symbol (FIFO)")
    for sym in sorted(current_open):
        print(f"  {sym}: {current_open[sym]:.6f}")


if __name__ == "__main__":
    main()
