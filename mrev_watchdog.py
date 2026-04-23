#!/usr/bin/env python3
"""
mrev_watchdog.py — watchdog de exits para el bot MREV (cripto 24/7).

Arquitectura paralela al RFTM watchdog. Diferencias:
- 24/7: no hay clock check, corre cada 5 min.
- Universo = CRYPTO_SYMBOLS (BTC, ETH, SOL, AVAX, DOGE, LINK).
- Cooldown: cuando cerramos por stop/trailing/time, registramos en
  mrev_cooldowns. El entry bot rechaza re-entradas durante 6h
  (configurable via MREV_COOLDOWN_HOURS). Sin cooldown post-TP1/TP2
  (re-entrar tras un TP válido es OK).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import standalone_mrev_trader as mrev
from _db_health import MREV_REQUIRED_COLUMNS, assert_db_health
from _exit_logic import PartialTPAction, evaluate_partial_tp, make_crypto_round_qty


DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
FILL_TIMEOUT_S = float(os.environ.get("WATCHDOG_FILL_TIMEOUT_S", "10"))

# Razones de exit que deben disparar cooldown (la lista viene del bot: stop_loss,
# trailing_stop, time_stop; NO take_profit ni partial_tp*).
COOLDOWN_REASONS = ("stop_loss", "trailing_stop", "time_stop")


def _is_cooldown_reason(reason: str) -> bool:
    return any(reason.startswith(prefix) for prefix in COOLDOWN_REASONS)


# ── Alpaca helpers ───────────────────────────────────────────────────────────

def alpaca_cancel_order(order_id: str) -> bool:
    try:
        mrev.alpaca_request(f"/orders/{order_id}", method="DELETE")
        return True
    except Exception as e:
        mrev.warn(f"cancel {order_id[:8]} failed: {e}")
        return False


def wait_for_fill(order_id: str, timeout_s: float = 10.0) -> Optional[dict]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            o = mrev.alpaca_request(f"/orders/{order_id}")
        except Exception:
            return None
        if not isinstance(o, dict):
            return None
        status = o.get("status")
        if status == "filled":
            return o
        if status in ("canceled", "expired", "rejected"):
            return None
        time.sleep(1)
    return None


def fetch_crypto_atr(symbol: str, hours_back: int = 60) -> Optional[float]:
    """Fetch 1H bars de cripto y calcular ATR14. None si no hay data."""
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=hours_back)
    encoded = urllib.parse.quote(symbol, safe="")
    path = (
        f"/v1beta3/crypto/us/bars?symbols={encoded}&timeframe=1Hour"
        f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&limit=10000&sort=asc"
    )
    try:
        data = mrev.alpaca_request(path, base="https://data.alpaca.markets")
    except Exception as e:
        mrev.warn(f"fetch bars {symbol}: {e}")
        return None

    bars = data.get("bars", {}).get(symbol, []) if isinstance(data, dict) else []
    if len(bars) < 15:
        return None

    highs = np.array([float(b["h"]) for b in bars])
    lows = np.array([float(b["l"]) for b in bars])
    closes = np.array([float(b["c"]) for b in bars])
    prev_close = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum.reduce([highs - lows, np.abs(highs - prev_close), np.abs(lows - prev_close)])
    atr = pd.Series(tr).ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else None


# ── Ejecución ────────────────────────────────────────────────────────────────

def _execute_sell(conn: sqlite3.Connection, symbol: str, qty: float, reason: str) -> Optional[dict]:
    if DRY_RUN:
        mrev.info(f"[DRY] SELL {qty} {symbol} ({reason})")
        return {"symbol": symbol, "filled_avg_price": None, "status": "filled_dry", "id": "dry"}

    try:
        order = mrev.alpaca_submit_order(symbol, qty, "sell")
    except Exception as e:
        mrev.err(f"SELL submit failed {symbol}: {e}")
        return None

    if not isinstance(order, dict):
        return None

    if order.get("status") == "filled" and order.get("filled_avg_price"):
        return order

    order_id = order.get("id")
    if not order_id:
        return None
    filled = wait_for_fill(order_id, timeout_s=FILL_TIMEOUT_S)
    if filled:
        return filled

    mrev.warn(f"SELL {symbol} no-fill {FILL_TIMEOUT_S:.0f}s, canceling {str(order_id)[:8]}")
    alpaca_cancel_order(order_id)
    return None


def process_position(conn: sqlite3.Connection, pos_row, alpaca_pos: dict, now: datetime) -> None:
    symbol = pos_row["symbol"]
    entry_price = float(pos_row["entry_price"])
    qty = float(pos_row["qty"])
    try:
        stage = int(pos_row["partial_tp_taken"] or 0)
    except Exception:
        stage = 0
    entry_dt = datetime.fromisoformat(pos_row["entry_dt"]) if pos_row["entry_dt"] else now
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)

    try:
        current_price = float(alpaca_pos.get("current_price", 0))
    except Exception:
        current_price = 0.0
    if current_price <= 0:
        mrev.warn(f"{symbol}: sin precio, skipping")
        return

    prev_high = float(pos_row["highest_since_entry"] or entry_price)
    highest = max(prev_high, current_price)
    if highest > prev_high:
        conn.execute("UPDATE mrev_positions SET highest_since_entry=? WHERE id=?",
                     (highest, pos_row["id"]))
        conn.commit()

    # ── 1. Partial TP ──
    round_qty = make_crypto_round_qty(mrev.CRYPTO_MIN_QTY.get(symbol, 0.0001))
    action = evaluate_partial_tp(
        stage=stage,
        entry_price=entry_price,
        current_price=current_price,
        current_qty=qty,
        tp1_pct=mrev.MREV_TP1_PCT,
        tp2_pct=mrev.MREV_TP2_PCT,
        tp1_ratio=mrev.MREV_TP1_RATIO,
        tp2_ratio=mrev.MREV_TP2_RATIO,
        min_notional=mrev.PARTIAL_MIN_NOTIONAL_USD,
        round_qty=round_qty,
    )
    if action is not None:
        _handle_partial_tp(conn, pos_row, action, current_price)
        return

    # ── 2. check_exit: stop, trailing, time, take_profit ──
    atr = fetch_crypto_atr(symbol)
    if atr is None:
        mrev.info(f"{symbol}: ATR no disponible, skipping exit check")
        return
    row = pd.Series({
        "close": current_price,
        "sma_20": None,  # check_exit usa sma+1.5atr; sin sma el TP no dispara
        "atr_14": atr,
    })
    should_exit, reason = mrev.check_exit(
        row, entry_price, entry_dt, now, highest_since_entry=highest
    )
    if should_exit:
        _handle_full_exit(conn, pos_row, current_price, reason)


def _handle_partial_tp(conn: sqlite3.Connection, pos_row, action: PartialTPAction, price: float) -> None:
    symbol = pos_row["symbol"]
    sell_qty = float(action.sell_qty)
    mrev.ok(f"{symbol}: partial_tp{action.stage} — sell {sell_qty} @ ${price:.2f}")
    order = _execute_sell(conn, symbol, sell_qty, action.reason)
    if not order:
        return
    new_qty = float(pos_row["qty"]) - sell_qty
    new_stop = action.new_stop if action.new_stop is not None else float(pos_row["stop_loss"] or 0)
    conn.execute(
        """UPDATE mrev_positions
           SET qty=?, partial_tp_taken=?, stop_loss=?
           WHERE id=?""",
        (new_qty, action.stage, new_stop, pos_row["id"]),
    )
    conn.commit()


def _handle_full_exit(conn: sqlite3.Connection, pos_row, price: float, reason: str) -> None:
    symbol = pos_row["symbol"]
    qty = float(pos_row["qty"])
    mrev.ok(f"{symbol}: EXIT ({reason}) — sell {qty} @ ${price:.2f}")
    order = _execute_sell(conn, symbol, qty, reason)
    if not order:
        return
    entry_price = float(pos_row["entry_price"])
    fill_px = price
    try:
        if order.get("filled_avg_price"):
            fill_px = float(order["filled_avg_price"])
    except Exception:
        pass
    pnl = (fill_px - entry_price) * qty
    conn.execute(
        """UPDATE mrev_positions
           SET status='CLOSED', qty=0, exit_price=?, pnl=?, exit_reason=?, exit_dt=?
           WHERE id=?""",
        (fill_px, round(pnl, 2), reason,
         datetime.now(tz=timezone.utc).isoformat(), pos_row["id"]),
    )
    conn.commit()

    # Cooldown: solo para exits negativos/neutros (stop/trailing/time)
    if _is_cooldown_reason(reason):
        mrev.record_cooldown(conn, symbol, reason)
        mrev.info(f"{symbol}: cooldown registrado ({reason})")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(tz=timezone.utc)
    mrev.hdr(f"MREV Watchdog — {now.isoformat(timespec='seconds')}")
    mrev.info(f"DRY_RUN={DRY_RUN}")

    # Init schema si el DB es nuevo
    _c = mrev.get_db(); _c.close()

    try:
        report = assert_db_health(
            db_path=str(mrev.DB_PATH),
            required_columns=MREV_REQUIRED_COLUMNS,
            open_run_table="mrev_runs",
            open_run_value="RUNNING",
            stale_run_value="CLOSED",
        )
        if report.get("closed_stale_runs"):
            mrev.warn(f"DB health: closed {report['closed_stale_runs']} stale runs")
    except Exception as e:
        mrev.err(f"DB health check failed: {e}")
        return 3

    if not mrev.ALPACA_API_KEY or not mrev.ALPACA_SECRET_KEY:
        mrev.err("No Alpaca keys")
        return 2

    conn = mrev.get_db()
    run_id = mrev.get_or_create_run(conn)
    mrev.migrate_legacy_etf_positions(conn)
    mrev.sync_with_alpaca(conn, run_id)

    try:
        alpaca_positions_raw = mrev.alpaca_get_positions() or []
    except Exception as e:
        mrev.err(f"alpaca_get_positions: {e}")
        return 2

    def _norm(sym: str) -> str:
        return sym.replace("/", "")

    alpaca_by_sym = {}
    for ap in alpaca_positions_raw:
        sym_raw = ap.get("symbol", "")
        # MREV usa "BTC/USD" pero Alpaca devuelve "BTCUSD" — matchear ambos.
        alpaca_by_sym[sym_raw] = ap
        alpaca_by_sym[_norm(sym_raw)] = ap

    rows = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='OPEN'",
        (run_id,),
    ).fetchall()

    evaluated = 0
    for pos in rows:
        sym = pos["symbol"]
        ap = alpaca_by_sym.get(sym) or alpaca_by_sym.get(_norm(sym))
        if not ap:
            continue
        if sym not in mrev.ALL_SYMBOLS:
            continue  # no es dominio de MREV
        try:
            process_position(conn, pos, ap, now)
            evaluated += 1
        except Exception as e:
            mrev.err(f"watchdog error on {sym}: {e}")

    mrev.ok(f"Watchdog evaluated {evaluated} positions")

    try:
        c = sqlite3.connect(str(mrev.DB_PATH))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
