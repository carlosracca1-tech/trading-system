#!/usr/bin/env python3
"""
rftm_watchdog.py — watchdog de exits para el bot RFTM.

Arquitectura:
- El entry bot (standalone_paper_trader.py en modo entry_only) evalúa
  nuevas entradas cada día. No ejecuta exits.
- Este watchdog corre cada 5 min durante horario de mercado (9:30–16:00 ET)
  y es la única defensa contra gaps adversos para las posiciones abiertas.

Qué hace en cada run:
  1. Health check de la DB + .env.paper.
  2. /v2/clock — si el mercado está cerrado y FORCE_RUN!=1, sale.
  3. Sync con Alpaca (sync_with_alpaca del bot).
  4. Por cada posición abierta:
     - actualiza highest_since_entry
     - evaluate_partial_tp: TP1 (+5%→50%) o TP2 (+7.5%→50% remanente)
     - check_exit: stop, trailing, time, E7 take-profit
  5. Submit de orders — reusa alpaca_submit_order (ya pollea el fill).
  6. Si no hay fill en 10s, cancela la order y no toca la DB.
  7. WAL checkpoint al final.

Por política: NO modifica check_entry, size_position, check_exit ni
_calc_take_profit. Solo las consume.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Reutilizar helpers del bot entry — NO duplicamos lógica.
import standalone_paper_trader as rftm
from _db_health import RFTM_REQUIRED_COLUMNS, assert_db_health
from _exit_logic import (
    ExitAction,
    PartialTPAction,
    evaluate_final_tp,
    evaluate_partial_tp,
    floor_int_qty,
)


DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
FORCE_RUN = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
FILL_TIMEOUT_S = float(os.environ.get("WATCHDOG_FILL_TIMEOUT_S", "10"))
BARS_LOOKBACK = int(os.environ.get("WATCHDOG_BARS_LOOKBACK", "40"))


# ── Alpaca helpers que faltan en el bot entry ────────────────────────────────

def alpaca_cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    res = rftm._alpaca_request("DELETE", f"/orders/{order_id}")
    return res is not None


def wait_for_fill(order_id: str, timeout_s: float = 10.0) -> Optional[dict]:
    """Poll /orders/{id} cada 1s hasta status==filled o timeout.
    Devuelve el order si quedó filled, None si timeout o error."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        o = rftm._alpaca_request("GET", f"/orders/{order_id}")
        if not o:
            return None
        status = o.get("status")
        if status == "filled":
            return o
        if status in ("canceled", "expired", "rejected"):
            return None
        time.sleep(1)
    return None


def fetch_atr14(symbol: str, bars_back: int = 40) -> tuple[Optional[float], Optional[int]]:
    """Fetch recent daily bars and return (atr14, bars_since_last_high).
    Devuelve (None, None) si no hay data suficiente."""
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=int(bars_back * 1.6))  # margen para weekends
    path = (
        f"/v2/stocks/{symbol}/bars?timeframe=1Day"
        f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&limit=10000&adjustment=split&feed=iex&sort=asc"
    )
    url = f"https://data.alpaca.markets{path}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": rftm.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": rftm.ALPACA_SECRET_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        rftm.warn(f"fetch bars {symbol}: {e}")
        return None, None

    bars = data.get("bars", [])
    if len(bars) < 15:
        return None, None

    highs = np.array([float(b["h"]) for b in bars])
    lows = np.array([float(b["l"]) for b in bars])
    closes = np.array([float(b["c"]) for b in bars])
    prev_close = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum.reduce([highs - lows, np.abs(highs - prev_close), np.abs(lows - prev_close)])
    # EMA 14 de TR (Wilder's)
    atr = pd.Series(tr).ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1]
    atr14 = float(atr) if not np.isnan(atr) else None

    # bars_since_last_high: cuántas barras desde el máximo de los últimos 20
    window = highs[-20:]
    last_high_idx = int(window.argmax())
    bars_no_high = int(len(window) - 1 - last_high_idx)

    return atr14, bars_no_high


# ── Acciones watchdog ────────────────────────────────────────────────────────

def _execute_sell(symbol: str, qty: int, reason: str) -> Optional[dict]:
    """Submit sell + esperar fill. Devuelve el order filled o None."""
    if DRY_RUN:
        rftm.info(f"[DRY] SELL {qty} {symbol} ({reason})")
        return {"symbol": symbol, "qty": qty, "filled_avg_price": None, "status": "filled_dry"}

    # alpaca_submit_order ya pollea fill brevemente.
    order = rftm.alpaca_submit_order(symbol, qty, "sell")
    if not order:
        rftm.err(f"SELL submit failed for {symbol}")
        return None

    if order.get("status") == "filled" and order.get("filled_avg_price"):
        return order

    # Aún no filled — esperar hasta WATCHDOG_FILL_TIMEOUT_S
    order_id = order.get("id")
    if not order_id:
        return None
    filled = wait_for_fill(order_id, timeout_s=FILL_TIMEOUT_S)
    if filled:
        return filled

    # Timeout → cancel
    rftm.warn(f"SELL {symbol} no-fill {FILL_TIMEOUT_S:.0f}s, canceling {order_id[:8]}")
    alpaca_cancel_order(order_id)
    return None


def process_position(pos_row, alpaca_pos: dict) -> None:
    """Evalúa TPs/stops para una posición y ejecuta si corresponde."""
    symbol = pos_row["symbol"]
    entry_price = float(pos_row["entry_price"])
    qty = int(pos_row["qty"])
    try:
        stage = int(pos_row["partial_tp_taken"] or 0)
    except Exception:
        stage = 0
    stop_loss = float(pos_row["stop_loss"]) if pos_row["stop_loss"] is not None else 0.0

    try:
        current_price = float(alpaca_pos.get("current_price", 0))
    except Exception:
        current_price = 0.0
    if current_price <= 0:
        rftm.warn(f"{symbol}: sin precio de Alpaca, skipping")
        return

    prev_high = float(pos_row["highest_since_entry"] or entry_price)
    highest = max(prev_high, current_price)
    if highest > prev_high:
        with rftm.get_db() as db:
            db.execute("UPDATE positions SET highest_since_entry=? WHERE id=?",
                       (highest, pos_row["id"]))

    # ── 1a. Hard final TP (+FINAL_TP_PCT) — preempta la cascada ──
    # Si el unrealized supera el umbral, vende TODO el remanente sin importar
    # el stage. Pensado para cortar runners en super-profit.
    final_action = evaluate_final_tp(
        entry_price=entry_price,
        current_price=current_price,
        current_qty=qty,
        final_tp_pct=rftm.FINAL_TP_PCT,
        min_notional=rftm.PARTIAL_MIN_NOTIONAL_USD,
    )
    if final_action is not None:
        _handle_full_exit(pos_row, current_price, final_action.reason)
        return

    # ── 1b. Partial TP (stage 0→1 o 1→2) ──
    action = evaluate_partial_tp(
        stage=stage,
        entry_price=entry_price,
        current_price=current_price,
        current_qty=qty,
        tp1_pct=rftm.PARTIAL_TP1_PCT,
        tp2_pct=rftm.PARTIAL_TP2_PCT,
        tp1_ratio=rftm.PARTIAL_TP1_SELL_RATIO,
        tp2_ratio=rftm.PARTIAL_TP2_SELL_RATIO,
        min_notional=rftm.PARTIAL_MIN_NOTIONAL_USD,
        round_qty=floor_int_qty,
    )
    if action is not None:
        _handle_partial_tp(pos_row, action, current_price)
        return  # un solo evento por posición por run

    # ── 2. check_exit: stop, trailing, time, E7 ──
    atr14, bars_no_high = fetch_atr14(symbol, bars_back=BARS_LOOKBACK)
    indicator_row = pd.Series({
        "close": current_price,
        "atr14": atr14 or 0.0,
        "bars_since_last_high": bars_no_high or 0,
    })
    should_exit, reason = rftm.check_exit(indicator_row, pos_row, highest_since_entry=highest)
    if should_exit:
        _handle_full_exit(pos_row, current_price, reason)


def _handle_partial_tp(pos_row, action: PartialTPAction, price: float) -> None:
    symbol = pos_row["symbol"]
    sell_qty = int(action.sell_qty)
    rftm.ok(f"{symbol}: partial_tp{action.stage} — sell {sell_qty} @ ${price:.2f}")
    order = _execute_sell(symbol, sell_qty, action.reason)
    if not order:
        rftm.warn(f"{symbol}: partial TP no ejecutado")
        return
    # DB update
    new_qty = int(pos_row["qty"]) - sell_qty
    new_stop = action.new_stop if action.new_stop is not None else float(pos_row["stop_loss"] or 0)
    with rftm.get_db() as db:
        db.execute(
            """UPDATE positions
               SET qty=?, partial_tp_taken=?, stop_loss=?
               WHERE id=?""",
            (new_qty, action.stage, new_stop, pos_row["id"]),
        )

    # Log a Google Sheets
    try:
        from _sheets_logger import log_trade_event, make_trade_id, make_event_id
        fill_px = float(order.get("filled_avg_price") or price)
        entry_px = float(pos_row["entry_price"])
        trade_id = make_trade_id("RFTM", pos_row["id"])
        side = f"SELL_TP{action.stage}"
        log_trade_event(
            bot="RFTM",
            symbol=symbol,
            side=side,
            qty=sell_qty,
            price=fill_px,
            trade_id=trade_id,
            event_id=make_event_id(trade_id, side),
            stage=action.stage,
            running_qty=new_qty,
            initial_qty=float(pos_row["initial_qty"] or sell_qty),
            entry_price=entry_px,
            realized_pnl_event=(fill_px - entry_px) * sell_qty,
            reason=action.reason,
            broker_order_id=str(order.get("id") or ""),
        )
    except Exception as _e:
        rftm.warn(f"sheets log failed (non-fatal): {_e}")


def _handle_full_exit(pos_row, price: float, reason: str) -> None:
    symbol = pos_row["symbol"]
    qty = int(pos_row["qty"])
    rftm.ok(f"{symbol}: EXIT ({reason}) — sell {qty} @ ${price:.2f}")
    order = _execute_sell(symbol, qty, reason)
    if not order:
        rftm.warn(f"{symbol}: exit no ejecutado")
        return
    entry_price = float(pos_row["entry_price"])
    fill_px = float(order.get("filled_avg_price") or price)
    realized = (fill_px - entry_price) * qty
    with rftm.get_db() as db:
        db.execute(
            """UPDATE positions
               SET status='closed', qty=0, exit_price=?, realized_pnl=?,
                   close_reason=?, closed_at=?
               WHERE id=?""",
            (fill_px, round(realized, 2), reason,
             datetime.now(tz=timezone.utc).isoformat(), pos_row["id"]),
        )

    # Log a Google Sheets
    try:
        from _sheets_logger import log_trade_event, make_trade_id, make_event_id
        # Mapeo reason → side normalizado
        if reason.startswith("final_tp"):
            side = "SELL_FINAL_TP"
        elif reason.startswith("E3_stop") or reason.startswith("E5_breakeven"):
            side = "SELL_STOP"
        elif reason.startswith("E5_trailing"):
            side = "SELL_TRAIL"
        elif reason.startswith("E6_time"):
            side = "SELL_TIME"
        elif reason.startswith("E7"):
            side = "SELL_FINAL_TP"
        else:
            side = "SELL_FINAL_TP"
        trade_id = make_trade_id("RFTM", pos_row["id"])
        log_trade_event(
            bot="RFTM",
            symbol=symbol,
            side=side,
            qty=qty,
            price=fill_px,
            trade_id=trade_id,
            event_id=make_event_id(trade_id, side),
            stage=int(pos_row["partial_tp_taken"] or 0),
            running_qty=0,
            initial_qty=float(pos_row["initial_qty"] or qty),
            entry_price=entry_price,
            realized_pnl_event=realized,
            reason=reason,
            broker_order_id=str(order.get("id") or ""),
        )
    except Exception as _e:
        rftm.warn(f"sheets log failed (non-fatal): {_e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    rftm.hdr(f"RFTM Watchdog — {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    rftm.info(f"DRY_RUN={DRY_RUN} FORCE_RUN={FORCE_RUN}")

    rftm.init_db()

    try:
        report = assert_db_health(
            db_path=str(rftm.DB_PATH),
            required_columns=RFTM_REQUIRED_COLUMNS,
            open_run_table="runs",
            open_run_value="running",
            stale_run_value="closed",
        )
        if report.get("closed_stale_runs"):
            rftm.warn(f"DB health: closed {report['closed_stale_runs']} stale runs")
    except Exception as e:
        rftm.err(f"DB health check failed: {e}")
        return 3

    if not rftm.ALPACA_API_KEY or not rftm.ALPACA_SECRET_KEY:
        rftm.err("No Alpaca keys — watchdog requires live reads")
        return 2

    # Market hours gate
    if not FORCE_RUN:
        clock = rftm._alpaca_request("GET", "/clock")
        if clock and not clock.get("is_open", False):
            rftm.info("Market closed — skipping watchdog run")
            return 0

    # Sync y leer posiciones abiertas
    run = rftm.get_active_run()
    if not run:
        rftm.warn("No active run — watchdog sin posiciones locales")
        return 0
    run_id = run["id"]

    rftm.sync_with_alpaca(run_id)

    alpaca_positions = {p["symbol"]: p for p in rftm.alpaca_get_positions() or []}
    if not alpaca_positions:
        rftm.info("No open positions in Alpaca")
        return 0

    with rftm.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE run_id=? AND status='open'",
            (run_id,),
        ).fetchall()

    evaluated = 0
    for pos in rows:
        sym = pos["symbol"]
        if sym not in alpaca_positions:
            continue  # ya lo cerraste fuera del bot — sync siguiente lo limpiará
        try:
            process_position(pos, alpaca_positions[sym])
            evaluated += 1
        except Exception as e:
            rftm.err(f"watchdog error on {sym}: {e}")

    rftm.ok(f"Watchdog evaluated {evaluated} positions")

    # WAL checkpoint
    try:
        import sqlite3
        c = sqlite3.connect(str(rftm.DB_PATH))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
