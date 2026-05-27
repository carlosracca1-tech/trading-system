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
from _exit_logic import (
    ExitAction,
    PartialTPAction,
    evaluate_final_tp,
    evaluate_partial_tp,
    make_crypto_round_qty,
)


DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
FILL_TIMEOUT_S = float(os.environ.get("WATCHDOG_FILL_TIMEOUT_S", "10"))

# Razones de exit que deben disparar cooldown (la lista viene del bot: stop_loss,
# trailing_stop, time_stop; NO take_profit ni partial_tp*).
COOLDOWN_REASONS = ("stop_loss", "trailing_stop", "time_stop")

# F3.2: HealthReport singleton (idem patrón RFTM)
_CURRENT_HEALTH = None


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
    """Fetch 4H bars de cripto y calcular ATR14. None si no hay data."""
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=hours_back)
    encoded = urllib.parse.quote(symbol, safe="")
    path = (
        f"/v1beta3/crypto/us/bars?symbols={encoded}&timeframe=4Hour"
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
    if _CURRENT_HEALTH is not None:
        _CURRENT_HEALTH.sell_attempts += 1
    if DRY_RUN:
        mrev.info(f"[DRY] SELL {qty} {symbol} ({reason})")
        return {"symbol": symbol, "filled_avg_price": None, "status": "filled_dry", "id": "dry"}

    try:
        order = mrev.alpaca_submit_order(symbol, qty, "sell")
    except Exception as e:
        mrev.err(f"SELL submit failed {symbol}: {e}")
        if _CURRENT_HEALTH is not None:
            _CURRENT_HEALTH.sell_failures += 1
            _CURRENT_HEALTH.add_event("warn", "sell_submit_fail",
                                       f"{symbol} qty={qty} ({reason}): {e}")
        return None

    if not isinstance(order, dict):
        if _CURRENT_HEALTH is not None:
            _CURRENT_HEALTH.sell_failures += 1
        return None

    if order.get("status") == "filled" and order.get("filled_avg_price"):
        return order

    order_id = order.get("id")
    if not order_id:
        if _CURRENT_HEALTH is not None:
            _CURRENT_HEALTH.sell_failures += 1
        return None
    filled = wait_for_fill(order_id, timeout_s=FILL_TIMEOUT_S)
    if filled:
        return filled

    mrev.warn(f"SELL {symbol} no-fill {FILL_TIMEOUT_S:.0f}s, canceling {str(order_id)[:8]}")
    alpaca_cancel_order(order_id)
    if _CURRENT_HEALTH is not None:
        _CURRENT_HEALTH.sell_timeouts += 1
        _CURRENT_HEALTH.add_event("info", "sell_timeout",
                                   f"{symbol} qty={qty} ({reason}) — canceled after {FILL_TIMEOUT_S:.0f}s")
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

    # ── 1a. Hard final TP (+FINAL_TP_PCT) — preempta la cascada ──
    # Si el unrealized supera el umbral, vende TODO el remanente sin importar
    # el stage. Pensado para cortar runners en super-profit.
    final_action = evaluate_final_tp(
        entry_price=entry_price,
        current_price=current_price,
        current_qty=qty,
        final_tp_pct=mrev.FINAL_TP_PCT,
        min_notional=mrev.PARTIAL_MIN_NOTIONAL_USD,
    )
    if final_action is not None:
        _handle_full_exit(conn, pos_row, current_price, final_action.reason)
        return

    # ── 1b. Partial TP ──
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

    # Log de evento de trade (JSONL local + Sheets best effort)
    # F5.1: enriquecido. MREV no guarda market_data en SQLite igual que
    # RFTM (recomputa indicadores a partir de bars cada run), por eso
    # solo pasamos close + régimen + execution.
    try:
        from _trade_logger import log_trade_event, make_trade_id, make_event_id
        from _kaizen_enrichment import build_enriched_extra
        fill_px = float(order.get("filled_avg_price") or price)
        entry_px = float(pos_row["entry_price"])
        trade_id = make_trade_id("MREV", pos_row["id"])
        side = f"SELL_TP{action.stage}"
        enriched = build_enriched_extra(
            bot="MREV",
            market_row={"close": price},
            close=price,
            fill_price=fill_px,
            target_price=price,
            entry_dt_iso=pos_row["entry_dt"] if "entry_dt" in pos_row.keys() else None,
            alpaca_request_fn=mrev.alpaca_request,
            include_regime=True,
        )
        log_trade_event(
            bot="MREV",
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
            source="mrev_watchdog",
            extra=enriched,
        )
    except Exception as _e:
        mrev.warn(f"trade log failed (non-fatal): {_e}")


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

    # Cooldown: solo para exits negativos/neutros (stop/trailing/time).
    # F1: pasamos fill_px para que se grabe last_exit_price y el cooldown
    # de precio pueda evaluarse en futuras entries.
    if _is_cooldown_reason(reason):
        mrev.record_cooldown(conn, symbol, fill_px, reason)
        mrev.info(f"{symbol}: cooldown registrado ({reason} @ ${fill_px:.2f})")

    # Log de evento de trade (JSONL local + Sheets best effort)
    # F5.1: enriquecido
    try:
        from _trade_logger import log_trade_event, make_trade_id, make_event_id
        from _kaizen_enrichment import build_enriched_extra
        if reason.startswith("final_tp"):
            side = "SELL_FINAL_TP"
        elif reason.startswith("stop_loss"):
            side = "SELL_STOP"
        elif reason.startswith("trailing_stop"):
            side = "SELL_TRAIL"
        elif reason.startswith("time_stop"):
            side = "SELL_TIME"
        elif reason.startswith("take_profit"):
            side = "SELL_FINAL_TP"
        else:
            side = "SELL_FINAL_TP"
        trade_id = make_trade_id("MREV", pos_row["id"])
        enriched = build_enriched_extra(
            bot="MREV",
            market_row={"close": price},
            close=price,
            fill_price=fill_px,
            target_price=price,
            entry_dt_iso=pos_row["entry_dt"] if "entry_dt" in pos_row.keys() else None,
            alpaca_request_fn=mrev.alpaca_request,
            include_regime=True,
        )
        log_trade_event(
            bot="MREV",
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
            realized_pnl_event=pnl,
            reason=reason,
            broker_order_id=str(order.get("id") or ""),
            source="mrev_watchdog",
            extra=enriched,
        )
    except Exception as _e:
        mrev.warn(f"trade log failed (non-fatal): {_e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    # F3.2: HealthReport singleton
    global _CURRENT_HEALTH
    from _watchdog_health import HealthReport, finalize_report, SEV_WARN, SEV_ERROR
    now = datetime.now(tz=timezone.utc)
    started_at = now.isoformat(timespec='seconds')
    health = HealthReport(bot="MREV")
    health.extra = {"dry_run": DRY_RUN}
    _CURRENT_HEALTH = health

    mrev.hdr(f"MREV Watchdog — {started_at}")
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
            health.add_event(SEV_WARN, "stale_runs_closed",
                             f"{report['closed_stale_runs']} stale runs")
    except Exception as e:
        mrev.err(f"DB health check failed: {e}")
        health.db_health_ok = False
        health.add_event(SEV_ERROR, "db_health_fail", str(e))
        finalize_report(health, started_at_iso=started_at)
        return 3

    if not mrev.ALPACA_API_KEY or not mrev.ALPACA_SECRET_KEY:
        mrev.err("No Alpaca keys")
        health.add_event(SEV_ERROR, "no_alpaca_keys")
        finalize_report(health, started_at_iso=started_at)
        return 2

    conn = mrev.get_db()
    run_id = mrev.get_or_create_run(conn)
    mrev.migrate_legacy_etf_positions(conn)
    mrev.sync_with_alpaca(conn, run_id)

    try:
        alpaca_positions_raw = mrev.alpaca_get_positions() or []
    except Exception as e:
        mrev.err(f"alpaca_get_positions: {e}")
        health.add_event(SEV_ERROR, "alpaca_positions_fail", str(e))
        finalize_report(health, started_at_iso=started_at)
        return 2

    def _norm(sym: str) -> str:
        return sym.replace("/", "")

    alpaca_by_sym = {}
    for ap in alpaca_positions_raw:
        sym_raw = ap.get("symbol", "")
        # MREV usa "BTC/USD" pero Alpaca devuelve "BTCUSD" — matchear ambos.
        alpaca_by_sym[sym_raw] = ap
        alpaca_by_sym[_norm(sym_raw)] = ap

    # F1.1 fix (2026-05-24): NO filtrar por run_id. El bot hourly usa otro
    # run_id (assert_db_health cierra runs viejos entre invocations) y antes
    # el watchdog no veía la posición recién comprada — quedaban posiciones
    # sin proteger. Ahora vemos todas las OPEN; si hay duplicadas por symbol
    # (legacy del bug pre-fix), nos quedamos con la más reciente y cerramos
    # las viejas marcándolas con exit_reason='dedup_run_id_fix'.
    _all_open = conn.execute(
        "SELECT * FROM mrev_positions WHERE status='OPEN' ORDER BY entry_dt DESC"
    ).fetchall()
    rows = []
    _seen_syms = set()
    _now_iso = now.isoformat()
    _dedup_count = 0
    for pos in _all_open:
        sym = pos["symbol"]
        if sym in _seen_syms:
            conn.execute(
                "UPDATE mrev_positions SET status='CLOSED', "
                "exit_reason='dedup_run_id_fix', exit_dt=? WHERE id=?",
                (_now_iso, pos["id"])
            )
            mrev.warn(f"DEDUP MREV: cerrada {sym} duplicada "
                      f"(id={pos['id']}, run_id={pos['run_id']})")
            _dedup_count += 1
            continue
        _seen_syms.add(sym)
        rows.append(pos)
    if _dedup_count:
        conn.commit()

    # Expected = posiciones que la DB conoce Y Alpaca tiene Y son dominio MREV
    expected = 0
    for pos in rows:
        sym = pos["symbol"]
        ap = alpaca_by_sym.get(sym) or alpaca_by_sym.get(_norm(sym))
        if ap and sym in mrev.ALL_SYMBOLS:
            expected += 1
    health.expected_count = expected

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
            health.add_event(SEV_ERROR, "position_eval_error", f"{sym}: {e}")

    health.evaluated_count = evaluated
    mrev.ok(f"Watchdog evaluated {evaluated} positions (expected {expected})")

    try:
        c = sqlite3.connect(str(mrev.DB_PATH))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass

    conn.close()
    finalize_report(health, started_at_iso=started_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
