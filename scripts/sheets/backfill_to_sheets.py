#!/usr/bin/env python3
"""
backfill_to_sheets.py — cargar a Google Sheets el historial de trades de
los últimos N días desde Alpaca.

Setup:
  pip install gspread google-auth pandas

  export SHEETS_SPREADSHEET_ID='1abc...'
  export SHEETS_SERVICE_ACCOUNT_JSON="$(cat ~/Downloads/sa.json)"
  export ALPACA_API_KEY=...  ALPACA_SECRET_KEY=...

  python3 scripts/sheets/backfill_to_sheets.py [--days 90] [--dry-run]

Idempotente: cada evento tiene event_id determinístico desde broker_order_id,
así re-correr no duplica filas.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _load_env_paper() -> None:
    env_path = REPO / ".env.paper"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


def alpaca_get_orders(api_key: str, secret: str, start: datetime, limit: int = 500) -> list[dict]:
    base = "https://paper-api.alpaca.markets/v2"
    all_orders: list[dict] = []
    after = start
    while True:
        params = {
            "status": "closed",
            "limit": str(limit),
            "after": after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "direction": "asc",
            "nested": "false",
        }
        url = f"{base}/orders?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret,
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  fetch error: {e}")
            break
        if not batch:
            break
        all_orders.extend(batch)
        if len(batch) < limit:
            break
        try:
            last_ts = batch[-1].get("submitted_at") or batch[-1].get("created_at")
            after = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        except Exception:
            break
        if len(all_orders) > 5000:
            break
    return all_orders


def reconcile_buys_and_sells(orders: list[dict]) -> list[dict]:
    open_trades: dict[str, dict] = {}
    events: list[dict] = []

    def _ts(o):
        return o.get("filled_at") or o.get("submitted_at") or ""
    orders_sorted = sorted([o for o in orders if o.get("status") == "filled"], key=_ts)

    for o in orders_sorted:
        sym = o.get("symbol", "")
        side = o.get("side", "")
        qty_raw = float(o.get("filled_qty", 0) or 0)
        if qty_raw <= 0:
            continue
        price = float(o.get("filled_avg_price", 0) or 0)
        ts = o.get("filled_at") or o.get("submitted_at")
        order_id = o.get("id", "")

        is_crypto = "/" in sym or any(sym.endswith(c) and sym[:-len(c)] in
            ("BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK") for c in ("USD", "USDT", "USDC"))
        bot = "MREV" if is_crypto else "RFTM"
        norm_sym = sym
        if is_crypto and "/" not in sym:
            for c in ("USD", "USDT", "USDC"):
                if sym.endswith(c) and sym[:-len(c)] in ("BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"):
                    norm_sym = f"{sym[:-len(c)]}/{c}"
                    break

        if side == "buy":
            trade_id = f"{bot}-{order_id.replace('-', '')[:8]}"
            open_trades[norm_sym] = {
                "trade_id": trade_id, "entry_price": price,
                "initial_qty": qty_raw, "running_qty": qty_raw, "stage": 0,
            }
            events.append({
                "bot": bot, "symbol": norm_sym, "side": "BUY",
                "qty": qty_raw, "price": price,
                "trade_id": trade_id, "event_id": f"{trade_id}-BUY",
                "stage": 0, "running_qty": qty_raw, "initial_qty": qty_raw,
                "entry_price": price, "timestamp_utc": ts,
                "reason": "entry_breakout" if bot == "RFTM" else "entry_mean_reversion",
                "broker_order_id": order_id,
            })
        elif side == "sell":
            trade = open_trades.get(norm_sym)
            if not trade:
                trade_id = f"{bot}-{order_id.replace('-', '')[:8]}"
                events.append({
                    "bot": bot, "symbol": norm_sym, "side": "SELL_FINAL_TP",
                    "qty": qty_raw, "price": price,
                    "trade_id": trade_id, "event_id": f"{trade_id}-SELL-ORPHAN",
                    "stage": 0, "running_qty": 0,
                    "initial_qty": qty_raw, "entry_price": price,
                    "realized_pnl_event": 0, "timestamp_utc": ts,
                    "reason": "sell_orphan_no_matching_buy",
                    "broker_order_id": order_id,
                })
                continue

            new_running = trade["running_qty"] - qty_raw
            initial = trade["initial_qty"]
            ratio_sold_total = (initial - new_running) / initial if initial else 0

            if new_running <= 0.0001 * initial:
                side_label = "SELL_FINAL"
                stage = trade["stage"]
            elif abs(ratio_sold_total - 0.5) < 0.05:
                side_label = "SELL_TP1"
                trade["stage"] = 1
                stage = 1
            elif abs(ratio_sold_total - 0.75) < 0.05:
                side_label = "SELL_TP2"
                trade["stage"] = 2
                stage = 2
            else:
                side_label = "SELL_PARTIAL"
                stage = trade["stage"]

            pnl = (price - trade["entry_price"]) * qty_raw
            events.append({
                "bot": bot, "symbol": norm_sym, "side": side_label,
                "qty": qty_raw, "price": price,
                "trade_id": trade["trade_id"],
                "event_id": f"{trade['trade_id']}-{side_label}-{order_id[:6]}",
                "stage": stage, "running_qty": max(0, new_running),
                "initial_qty": initial, "entry_price": trade["entry_price"],
                "realized_pnl_event": round(pnl, 4),
                "timestamp_utc": ts,
                "reason": "backfill_partial" if side_label != "SELL_FINAL" else "backfill_close",
                "broker_order_id": order_id,
            })
            trade["running_qty"] = max(0, new_running)
            if trade["running_qty"] <= 0.0001 * initial:
                del open_trades[norm_sym]

    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.6,
                        help="Sleep entre appends (rate limit Sheets API ~100/min)")
    args = parser.parse_args()

    _load_env_paper()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    sid = os.environ.get("SHEETS_SPREADSHEET_ID", "").strip()
    sa = os.environ.get("SHEETS_SERVICE_ACCOUNT_JSON", "").strip()

    if not api_key or not secret:
        print("ERROR: ALPACA_API_KEY/ALPACA_SECRET_KEY no seteados")
        return 1
    if not args.dry_run and (not sid or not sa):
        print("ERROR: SHEETS_SPREADSHEET_ID o SHEETS_SERVICE_ACCOUNT_JSON faltan")
        return 1

    start = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    print(f"Fetching Alpaca orders desde {start.isoformat()} ...")
    orders = alpaca_get_orders(api_key, secret, start)
    print(f"  {len(orders)} orders bajados de Alpaca")

    events = reconcile_buys_and_sells(orders)
    print(f"  {len(events)} eventos derivados")
    by_bot: dict[str, int] = {}
    for e in events:
        by_bot[e["bot"]] = by_bot.get(e["bot"], 0) + 1
    print(f"  Por bot: {by_bot}")

    if args.dry_run:
        for e in events[:10]:
            print(f"  {e.get('timestamp_utc','')}  {e['bot']:4s}  {e['symbol']:10s}  "
                  f"{e['side']:14s}  qty={e['qty']:8.2f}  @${e['price']:8.2f}")
        print(f"  ... ({len(events)} total)")
        return 0

    # Importar el logger AHORA (después de validar env vars)
    from _sheets_logger import log_trade_event

    ok = 0; fail = 0
    for i, e in enumerate(events, 1):
        result = log_trade_event(**e)
        if result:
            ok += 1
        else:
            fail += 1
        if i % 10 == 0:
            print(f"  posted {i}/{len(events)} (ok={ok}, fail={fail})")
        time.sleep(args.sleep)

    print(f"\nTotal: {ok} ok, {fail} fail (incluye duplicados skipados) de {len(events)} eventos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
