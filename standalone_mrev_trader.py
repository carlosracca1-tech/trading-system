#!/usr/bin/env python3
"""
standalone_mrev_trader.py
══════════════════════════════════════════════════════════════════════════════
MREV-1H Strategy — Standalone paper trading runner for hourly execution.

Mean Reversion strategy on 1-hour candles.
Trades crypto (BTC, ETH, SOL) 24/7 and liquid ETFs (SPY, QQQ, IWM) during market hours.

Only requires: pandas, numpy (pre-installed), plus stdlib.
Stores state in mrev_paper.db (SQLite).
Connects to Alpaca paper trading for real data + orders.
Sends email report after each run.

Usage:
    python3 standalone_mrev_trader.py                # full scan + trade + email
    python3 standalone_mrev_trader.py --dry-run      # scan only, no orders
    python3 standalone_mrev_trader.py --status        # show portfolio + positions
    python3 standalone_mrev_trader.py --reset         # wipe DB and start fresh
    python3 standalone_mrev_trader.py --backtest      # run backtest on synthetic data

Set keys in .env.paper or as environment variables:
    ALPACA_API_KEY, ALPACA_SECRET_KEY
    MREV_INITIAL_CAPITAL (default: 1000)
══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import json
import math
import os
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from _email_helpers import build_css, send_smtp, send_stage_event_email


# ── Load .env.paper ──────────────────────────────────────────────────────────
def _load_env():
    for name in (".env.paper", ".env"):
        p = Path(name)
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# Email config
EMAIL_ENABLED     = os.environ.get("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_SMTP_SERVER = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT   = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO          = os.environ.get("EMAIL_TO", "")

# Email schedule: 1 email per day covering 24 hours.
# Default: 09:00 Argentina time (UTC-3) = 12 UTC
# The bot runs every hour but only sends email at this UTC hour.
EMAIL_HOURS_UTC   = [int(h) for h in os.environ.get("EMAIL_HOURS_UTC", "12").split(",")]

# Monthly report: sent on the 1st of each month at the same hour as the daily email.
EMAIL_MONTHLY_ENABLED = os.environ.get("EMAIL_MONTHLY_ENABLED", "true").lower() == "true"
EMAIL_MONTHLY_DAY = int(os.environ.get("EMAIL_MONTHLY_DAY", "1"))  # day of month

# MREV-specific config
MREV_CAPITAL      = float(os.environ.get("MREV_INITIAL_CAPITAL", "25000"))
MREV_MAX_POSITIONS = int(os.environ.get("MREV_MAX_POSITIONS", "6"))
MREV_RISK_PER_TRADE = float(os.environ.get("MREV_RISK_PER_TRADE", "0.05"))

# Partial TP en dos etapas para MREV (espejo de RFTM):
#   Stage 0 → +5% vende 50%  → pasa a stage 1 y sube SL al breakeven
#   Stage 1 → +7.5% vende 50% del remanente → pasa a stage 2
#   Stage 2 → el 25% final queda corriendo hasta el TP dinámico (SMA20+1.5×ATR)
MREV_TP1_PCT   = float(os.environ.get("MREV_PARTIAL_TP1_PCT",
                                      os.environ.get("MREV_PARTIAL_TP_PCT", "0.05")))
MREV_TP1_RATIO = float(os.environ.get("MREV_PARTIAL_TP1_SELL_RATIO",
                                      os.environ.get("MREV_PARTIAL_TP_SELL_RATIO", "0.50")))
MREV_TP2_PCT   = float(os.environ.get("MREV_PARTIAL_TP2_PCT",        "0.075"))
MREV_TP2_RATIO = float(os.environ.get("MREV_PARTIAL_TP2_SELL_RATIO", "0.50"))

# Mínimo notional (USD) para que un parcial dispare. Coincide con el mínimo
# de Alpaca para órdenes de cripto ($10). Si el 50% calculado queda por
# debajo, se skipea el parcial y se espera al próximo trigger o al exit final.
PARTIAL_MIN_NOTIONAL_USD = float(os.environ.get("PARTIAL_MIN_NOTIONAL_USD", "10.0"))

# ── Universe ─────────────────────────────────────────────────────────────────
# MREV = SOLO cripto. RFTM = SOLO ETFs. Decisión de arquitectura 2026-04-22
# para evitar que un bot venda posiciones que abrió el otro (bug que causó que
# MREV disparara un TP1 sobre SPY que había comprado RFTM).
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LINK/USD"]
ETF_SYMBOLS    = []   # DEPRECATED: MREV ya no opera ETFs. Los ETFs son dominio de RFTM.
ALL_SYMBOLS    = CRYPTO_SYMBOLS + ETF_SYMBOLS   # == CRYPTO_SYMBOLS

CRYPTO_MIN_QTY = {
    "BTC/USD": 0.0001, "ETH/USD": 0.001, "SOL/USD": 0.01,
    "AVAX/USD": 0.01, "DOGE/USD": 1.0, "LINK/USD": 0.01,
}

# Account-level config (for unified reporting across both bots)
ACCOUNT_TOTAL_CAPITAL = float(os.environ.get("ACCOUNT_TOTAL_CAPITAL", "100000"))
DAILY_BOT_CAPITAL     = float(os.environ.get("DAILY_BOT_CAPITAL", "75000"))

# Safety margin sobre el buying power Alpaca (ver standalone_paper_trader.py).
# Usamos como máximo este % del BP antes de cada compra para protegernos de
# slippage y latencia. Default 0.90 = 90% del BP reportado.
ALPACA_BP_SAFETY = float(os.environ.get("ALPACA_BP_SAFETY", "0.90"))

# Kill switch. Si el equity cae más de este % desde su peak histórico, MREV
# deja de abrir posiciones nuevas (no cierra las abiertas — sólo frena entradas).
# Default 0.20 = 20%.
MAX_DRAWDOWN = float(os.environ.get("MAX_DRAWDOWN", "0.20"))

# ── Colors ───────────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[32m"
    RED    = "\033[31m"
    YELLOW = "\033[33m"
    BLUE   = "\033[34m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"

def ok(msg):   print(f"{C.GREEN}  ✓{C.RESET}  {msg}")
def err(msg):  print(f"{C.RED}  ✗{C.RESET}  {msg}")
def warn(msg): print(f"{C.YELLOW}  ⚠{C.RESET}  {msg}")
def info(msg): print(f"{C.BLUE}  →{C.RESET}  {msg}")
def hdr(msg):  print(f"\n{C.BOLD}{msg}{C.RESET}\n{'─'*56}")


# ══════════════════════════════════════════════════════════════════════════════
#  ALPACA API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def alpaca_request(path: str, method: str = "GET", body: dict | None = None, base: str = "") -> dict | list:
    """Make authenticated request to Alpaca API."""
    if not base:
        base = "https://paper-api.alpaca.markets/v2"
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Alpaca {method} {path} → {e.code}: {err_body}") from e


def alpaca_get_account() -> dict:
    return alpaca_request("/account")


def alpaca_get_positions() -> list:
    """Get all open positions from Alpaca (across all bots)."""
    try:
        return alpaca_request("/positions")
    except Exception:
        return []


def alpaca_get_portfolio_history(period: str = "1M", timeframe: str = "1D") -> dict:
    """Get portfolio equity history from Alpaca. period: 1D, 1W, 1M, 3M, 1A, all."""
    try:
        return alpaca_request(f"/account/portfolio/history?period={period}&timeframe={timeframe}")
    except Exception:
        return {}


def alpaca_get_1h_bars(symbol: str, hours_back: int = 250) -> pd.DataFrame:
    """Fetch 1-hour bars from Alpaca market data API."""
    is_crypto = "/" in symbol
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=hours_back)

    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    if is_crypto:
        # Alpaca v1beta3 crypto expects symbols WITH slash: BTC/USD
        alpaca_sym = symbol  # keep BTC/USD as-is
        encoded_sym = urllib.parse.quote(symbol, safe="")  # BTC%2FUSD for URL
        path = f"/v1beta3/crypto/us/bars?symbols={encoded_sym}&timeframe=1Hour&start={start_str}&end={end_str}&limit=10000&sort=asc"
        data = alpaca_request(path, base="https://data.alpaca.markets")
        bars_raw = data.get("bars", {}).get(alpaca_sym, [])
    else:
        path = f"/v2/stocks/{symbol}/bars?timeframe=1Hour&start={start_str}&end={end_str}&limit=10000&adjustment=split&feed=iex&sort=asc"
        data = alpaca_request(path, base="https://data.alpaca.markets")
        bars_raw = data.get("bars", [])

    if not bars_raw:
        return pd.DataFrame(columns=["datetime", "symbol", "open", "high", "low", "close", "volume"])

    rows = []
    for b in bars_raw:
        try:
            dt_str = b["t"]
            if dt_str.endswith("Z"):
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(dt_str)
            rows.append({
                "datetime": dt,
                "symbol": symbol,
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": float(b["v"]),
            })
        except (KeyError, ValueError):
            continue

    return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)


def alpaca_submit_order(symbol: str, qty: float, side: str) -> dict:
    """Submit order to Alpaca paper trading."""
    is_crypto = "/" in symbol
    order_body = {
        "symbol": symbol.replace("/", "") if is_crypto else symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc" if is_crypto else "day",
    }
    return alpaca_request("/orders", method="POST", body=order_body)


def is_market_open_for(symbol: str) -> bool:
    """Check if the market is currently open for a symbol."""
    if "/" in symbol:
        return True  # crypto trades 24/7
    try:
        clock = alpaca_request("/clock")
        return clock.get("is_open", False)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS (1H — Bollinger Bands, RSI, ATR)
# ══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute MREV-1H indicators: Bollinger Bands, RSI, ATR, Volume MA."""
    if df.empty:
        return df.copy()

    df = df.copy().sort_values("datetime").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # Bollinger Bands (20-period SMA ± 2σ)
    sma_20 = close.rolling(20, min_periods=20).mean()
    std_20 = close.rolling(20, min_periods=20).std()
    df["sma_20"] = sma_20
    df["bb_upper"] = sma_20 + 2.0 * std_20
    df["bb_lower"] = sma_20 - 1.8 * std_20

    # RSI 14 (Wilder's)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = np.where(np.isnan(avg_gain), np.nan, rsi)
    df["rsi_14"] = pd.Series(rsi, index=df.index)

    # ATR 14 (Wilder's)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr_14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    df["atr_14_pct"] = df["atr_14"] / close

    # Volume MA 20
    df["volume_ma_20"] = volume.rolling(20, min_periods=20).mean()

    return df


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def check_entry(symbol: str, row: pd.Series) -> tuple[bool, str]:
    """Check MREV entry conditions. Returns (should_enter, reason)."""
    for col in ("sma_20", "bb_lower", "rsi_14", "atr_14_pct", "volume_ma_20"):
        v = row.get(col)
        if v is None or (isinstance(v, float) and v != v):
            return False, "indicators_not_ready"

    close = float(row["close"])
    rsi = float(row["rsi_14"])
    bb_lower = float(row["bb_lower"])
    volume = float(row["volume"])
    vol_ma = float(row["volume_ma_20"])
    atr_pct = float(row["atr_14_pct"])

    if rsi > 45.0:
        return False, f"rsi={rsi:.1f} (need ≤45)"
    if close > bb_lower:
        return False, f"close={close:.2f} > bb_lower={bb_lower:.2f}"
    if vol_ma > 0 and volume < vol_ma * 0.5:
        return False, "low_volume"
    if not (0.002 <= atr_pct <= 0.15):
        return False, f"atr_pct={atr_pct:.4f} out of range"
    return True, ""


def check_exit(row: pd.Series, entry_price: float, entry_dt: datetime, now_dt: datetime,
               highest_since_entry: float = 0.0) -> tuple[bool, str]:
    """Check MREV exit conditions. Returns (should_exit, reason)."""
    close = float(row["close"])
    sma = row.get("sma_20")
    atr = row.get("atr_14")

    atr_val = float(atr) if (atr is not None and not (isinstance(atr, float) and atr != atr) and float(atr) > 0) else 0.0

    # X1: Take profit — mean reversion to SMA(20) + 1.5×ATR (let winners run more)
    if sma is not None and not (isinstance(sma, float) and sma != sma) and atr_val > 0:
        tp_level = float(sma) + 1.5 * atr_val
        if close >= tp_level:
            return True, f"take_profit (close={close:.2f} ≥ sma+1.5atr={tp_level:.2f})"

    # X2: Stop loss — 2.0×ATR (keeps 1:1.5 risk/reward minimum)
    if atr_val > 0:
        stop = entry_price - 2.0 * atr_val
        if close <= stop:
            return True, f"stop_loss (close={close:.2f} ≤ stop={stop:.2f})"

    # X3: RSI normalized — REMOVED (was killing profitable trades prematurely)

    # X4: Time stop (120 hours = 5 days — more room for mean reversion to play out)
    if entry_dt:
        hours_held = (now_dt - entry_dt).total_seconds() / 3600
        if hours_held >= 120:
            return True, f"time_stop ({hours_held:.0f}h)"

    # X5: Trailing stop — 1.0×ATR from highest (wider trail, let profits run)
    if atr_val > 0 and highest_since_entry > entry_price:
        trail_stop = highest_since_entry - 1.0 * atr_val
        if close <= trail_stop:
            return True, f"trailing_stop (close={close:.2f} ≤ trail={trail_stop:.2f})"

    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════════

def size_position(symbol: str, close: float, atr: float, equity: float) -> tuple[float, float]:
    """Calculate position size. Returns (qty, stop_price)."""
    is_crypto = "/" in symbol
    stop_dist = 2.0 * atr
    stop_price = close - stop_dist

    risk_amount = equity * MREV_RISK_PER_TRADE
    qty_risk = risk_amount / stop_dist if stop_dist > 0 else 0

    max_notional = equity * 0.40
    qty_cap = max_notional / close if close > 0 else 0

    raw_qty = min(qty_risk, qty_cap)

    if is_crypto:
        min_q = CRYPTO_MIN_QTY.get(symbol, 0.0001)
        precision = len(str(min_q).rstrip("0").split(".")[-1])
        qty = round(math.floor(raw_qty / min_q) * min_q, precision)
    else:
        qty = math.floor(raw_qty)

    if qty * close < 10.0:  # min $10 order
        qty = 0

    return qty, stop_price


# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE DATABASE
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = Path(os.environ.get("MREV_DB_PATH", Path(__file__).parent / "mrev_paper.db"))

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_runs (
        id TEXT PRIMARY KEY, started_at TEXT, initial_capital REAL, status TEXT DEFAULT 'RUNNING'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_positions (
        id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL, entry_price REAL,
        stop_loss REAL, entry_dt TEXT, status TEXT DEFAULT 'OPEN',
        exit_price REAL, exit_dt TEXT, pnl REAL, exit_reason TEXT,
        highest_since_entry REAL DEFAULT 0.0
    )""")
    # Migration: add highest_since_entry if table already exists without it
    try:
        conn.execute("ALTER TABLE mrev_positions ADD COLUMN highest_since_entry REAL DEFAULT 0.0")
    except Exception:
        pass  # column already exists
    # Partial TP bookkeeping: vender 50% cuando unrealized >= +3%
    for _stmt in [
        "ALTER TABLE mrev_positions ADD COLUMN partial_tp_taken INTEGER DEFAULT 0",
        "ALTER TABLE mrev_positions ADD COLUMN initial_qty REAL",
    ]:
        try:
            conn.execute(_stmt)
        except Exception:
            pass
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_signals (
        id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, signal_type TEXT,
        close_price REAL, rsi REAL, reason TEXT, created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_snapshots (
        id TEXT PRIMARY KEY, run_id TEXT, equity REAL, cash REAL,
        positions_count INTEGER, peak_equity REAL, created_at TEXT
    )""")
    # Hourly run log — tracks what happened each hour for the daily summary email
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_hourly_log (
        id TEXT PRIMARY KEY, run_id TEXT, hour_utc TEXT,
        symbols_scanned INTEGER DEFAULT 0, entries INTEGER DEFAULT 0,
        exits INTEGER DEFAULT 0, equity REAL, cash REAL,
        details_json TEXT, created_at TEXT
    )""")
    # Email send log — tracks when emails were sent to avoid duplicates
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_email_log (
        id TEXT PRIMARY KEY, run_id TEXT, email_window TEXT,
        sent_at TEXT
    )""")
    conn.commit()
    return conn


def get_or_create_run(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM mrev_runs WHERE status='RUNNING' LIMIT 1").fetchone()
    if row:
        return row["id"]
    run_id = str(uuid.uuid4())[:8]
    conn.execute("INSERT INTO mrev_runs VALUES (?,?,?,?)",
                 (run_id, datetime.now(tz=timezone.utc).isoformat(), MREV_CAPITAL, "RUNNING"))
    conn.commit()
    ok(f"New MREV run created: {run_id} with ${MREV_CAPITAL:,.2f}")
    return run_id


def migrate_legacy_etf_positions(conn: sqlite3.Connection) -> int:
    """One-shot migración al dividir los bots: MREV = cripto, RFTM = ETFs.

    Cierra cualquier posición OPEN en mrev_positions cuyo symbol NO sea cripto
    (no tiene '/' y no empieza con un root cripto conocido). Estas son ETFs que
    MREV había reclamado del Alpaca shared account bajo la versión anterior del
    universo. Ahora pertenecen exclusivamente a RFTM.

    Idempotente: si no hay ETFs en mrev_positions, no hace nada.
    """
    crypto_roots = ("BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK", "DOT", "ADA", "MATIC", "XRP")

    def _is_crypto(sym: str) -> bool:
        if "/" in sym:
            return True
        return any(sym.startswith(c) for c in crypto_roots) and sym.endswith(("USD", "USDT", "USDC"))

    rows = list(conn.execute(
        "SELECT id, symbol FROM mrev_positions WHERE status='OPEN'"
    ))
    closed = 0
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    for r in rows:
        if _is_crypto(r["symbol"]):
            continue
        conn.execute(
            """UPDATE mrev_positions
               SET status='CLOSED', exit_reason='migrated_etf_out_of_mrev', exit_dt=?
               WHERE id=?""",
            (now_iso, r["id"])
        )
        warn(f"MIGRATION: cerrando ETF {r['symbol']} de mrev_positions — ahora es dominio de RFTM")
        closed += 1
    if closed:
        conn.commit()
        ok(f"MIGRATION: {closed} posición(es) ETF cerrada(s) en mrev_positions")
    return closed


def get_open_positions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='OPEN'", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def sync_with_alpaca(conn: sqlite3.Connection, run_id: str) -> None:
    """Reconcile mrev_positions con las posiciones REALES de Alpaca.

    - Si Alpaca tiene una cripto / símbolo MREV que la DB no tiene → la inserta
      con stage=0 e initial_qty = qty actual, para que los partial TP funcionen.
    - Si la DB tiene una posición OPEN que Alpaca ya no tiene → la cierra.
    - Fija el entry_price al avg_entry_price de Alpaca si difieren.

    MREV cubre SOLO cripto (desde el split 2026-04-22). El bot
    RFTM maneja el resto de ETFs. Para evitar que los dos bots reclamen la misma
    posición, MREV solo reclama símbolos que pertenecen a ALL_SYMBOLS.
    """
    try:
        alpaca_positions = alpaca_get_positions() or []
    except Exception as e:
        warn(f"SYNC MREV: no se pudo consultar Alpaca ({e}) — salteando")
        return

    # Normalizar símbolo de Alpaca (cripto puede venir como 'AVAXUSD' o 'AVAX/USD')
    def _norm(sym: str) -> str:
        if "/" in sym:
            return sym
        # Alpaca devuelve cripto como "AVAXUSD" — convertirlo a "AVAX/USD"
        for c in ("USD", "USDT", "USDC"):
            if sym.endswith(c) and len(sym) > len(c) and sym not in ETF_SYMBOLS:
                return f"{sym[:-len(c)]}/{c}"
        return sym

    alpaca_by_sym: dict[str, dict] = {}
    for p in alpaca_positions:
        alpaca_by_sym[_norm(p.get("symbol", ""))] = p

    open_positions = get_open_positions(conn, run_id)
    open_by_sym = {p["symbol"]: p for p in open_positions}

    # 1. Cerrar posiciones locales que Alpaca ya no tiene
    for sym, lp in open_by_sym.items():
        if sym in alpaca_by_sym or _norm(sym) in alpaca_by_sym:
            continue
        conn.execute(
            """UPDATE mrev_positions SET status='CLOSED', exit_reason='synced_from_alpaca',
               exit_dt=? WHERE id=?""",
            (datetime.now(tz=timezone.utc).isoformat(), lp["id"])
        )
        ok(f"SYNC MREV: cerrada {sym} (ya no está en Alpaca)")

    # 2. Insertar posiciones que Alpaca tiene y la DB no
    for sym, ap in alpaca_by_sym.items():
        if sym in open_by_sym:
            # Arreglar entry_price si difiere
            real_entry = float(ap.get("avg_entry_price", 0))
            lp_entry = float(open_by_sym[sym].get("entry_price", 0))
            if real_entry > 0 and abs(lp_entry - real_entry) / max(real_entry, 0.0001) > 0.001:
                conn.execute("UPDATE mrev_positions SET entry_price=? WHERE id=?",
                             (real_entry, open_by_sym[sym]["id"]))
                info(f"SYNC MREV: {sym} entry fix ${lp_entry:.4f} → ${real_entry:.4f}")
            continue
        if sym not in ALL_SYMBOLS:
            continue  # no es dominio de MREV — lo maneja RFTM
        qty = float(ap.get("qty", 0))
        entry = float(ap.get("avg_entry_price", 0))
        if qty <= 0 or entry <= 0:
            continue
        pos_id = str(uuid.uuid4())[:8]
        conn.execute(
            """INSERT INTO mrev_positions
               (id, run_id, symbol, qty, entry_price, stop_loss, entry_dt,
                status, highest_since_entry, partial_tp_taken, initial_qty)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pos_id, run_id, sym, qty, entry,
             round(entry * 0.95, 6),
             datetime.now(tz=timezone.utc).isoformat(),
             "OPEN", entry, 0, qty)
        )
        ok(f"SYNC MREV: insertada {sym} qty={qty} @ ${entry:.4f} (stage=0, initial_qty={qty})")
    conn.commit()


def get_cash(conn: sqlite3.Connection, run_id: str) -> float:
    """Calculate available cash from initial capital minus open positions."""
    initial = conn.execute("SELECT initial_capital FROM mrev_runs WHERE id=?", (run_id,)).fetchone()
    if not initial:
        return MREV_CAPITAL
    capital = float(initial["initial_capital"])

    open_pos = get_open_positions(conn, run_id)
    invested = sum(float(p["qty"]) * float(p["entry_price"]) for p in open_pos)

    closed = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM mrev_positions WHERE run_id=? AND status='CLOSED'",
        (run_id,)
    ).fetchone()
    total_pnl = float(closed["total_pnl"]) if closed else 0.0

    return capital + total_pnl - invested


def get_equity(conn: sqlite3.Connection, run_id: str, current_prices: dict[str, float]) -> float:
    """Calculate total equity (cash + market value of open positions)."""
    cash = get_cash(conn, run_id)
    open_pos = get_open_positions(conn, run_id)
    pos_value = sum(
        float(p["qty"]) * current_prices.get(p["symbol"], float(p["entry_price"]))
        for p in open_pos
    )
    return cash + pos_value


def get_peak_equity(conn: sqlite3.Connection, run_id: str) -> float:
    row = conn.execute(
        "SELECT MAX(peak_equity) as peak FROM mrev_snapshots WHERE run_id=?", (run_id,)
    ).fetchone()
    peak = float(row["peak"]) if row and row["peak"] else MREV_CAPITAL
    return max(peak, MREV_CAPITAL)


def log_hourly_run(conn: sqlite3.Connection, run_id: str, buys: list, sells: list,
                   equity: float, cash: float, symbols_scanned: int) -> None:
    """Log this hourly run for the 8-hour summary email."""
    now = datetime.now(tz=timezone.utc)
    details = {
        "buys": buys,
        "sells": sells,
    }
    conn.execute("INSERT INTO mrev_hourly_log VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4())[:8], run_id, now.strftime("%Y-%m-%dT%H:00"),
         symbols_scanned, len(buys), len(sells), equity, cash,
         json.dumps(details, default=str), now.isoformat()))
    conn.commit()


def get_last_24h_activity(conn: sqlite3.Connection, run_id: str) -> dict:
    """Gather all trading activity from the last 24 hours for the daily summary email."""
    now = datetime.now(tz=timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    # Hourly runs
    hourly_rows = conn.execute(
        "SELECT * FROM mrev_hourly_log WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # All buys/sells in the last 24h
    all_buys = []
    all_sells = []
    for h in hourly_rows:
        details = json.loads(h["details_json"]) if h["details_json"] else {}
        all_buys.extend(details.get("buys", []))
        all_sells.extend(details.get("sells", []))

    # Closed positions in the last 24h
    closed_24h = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='CLOSED' AND exit_dt>=? ORDER BY exit_dt",
        (run_id, since)
    ).fetchall()

    # Signals in the last 24h
    signals_24h = conn.execute(
        "SELECT * FROM mrev_signals WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # Equity snapshots for the period
    snapshots = conn.execute(
        "SELECT equity, cash, positions_count, created_at FROM mrev_snapshots WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # Compute period stats
    total_pnl_24h = sum(float(c["pnl"] or 0) for c in closed_24h)
    wins_24h = sum(1 for c in closed_24h if float(c["pnl"] or 0) > 0)
    losses_24h = sum(1 for c in closed_24h if float(c["pnl"] or 0) <= 0)

    equity_start = float(snapshots[0]["equity"]) if snapshots else MREV_CAPITAL
    equity_end = float(snapshots[-1]["equity"]) if snapshots else MREV_CAPITAL

    hours_with_data = len(hourly_rows)
    total_entries = sum(int(h["entries"]) for h in hourly_rows)
    total_exits = sum(int(h["exits"]) for h in hourly_rows)

    # Win rate and average P&L for the summary
    total_closed = len(closed_24h)
    win_rate = round(wins_24h / total_closed * 100, 1) if total_closed > 0 else 0
    avg_win = round(sum(float(c["pnl"]) for c in closed_24h if float(c["pnl"] or 0) > 0) / wins_24h, 2) if wins_24h > 0 else 0
    avg_loss = round(sum(float(c["pnl"]) for c in closed_24h if float(c["pnl"] or 0) <= 0) / losses_24h, 2) if losses_24h > 0 else 0

    return {
        "hours_covered": hours_with_data,
        "period_start": since,
        "period_end": now.isoformat(),
        "all_buys": all_buys,
        "all_sells": all_sells,
        "closed_trades": [dict(c) for c in closed_24h],
        "total_entries": total_entries,
        "total_exits": total_exits,
        "total_pnl": round(total_pnl_24h, 2),
        "wins": wins_24h,
        "losses": losses_24h,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "total_closed": total_closed,
        "equity_start": equity_start,
        "equity_end": equity_end,
        "equity_change": round(equity_end - equity_start, 2),
        "equity_change_pct": round((equity_end - equity_start) / equity_start * 100, 2) if equity_start > 0 else 0,
        "snapshots": [dict(s) for s in snapshots],
    }


def get_email_window(now: datetime | None = None) -> str | None:
    """Returns a daily email window label like '2026-04-09_DAILY'.

    Only returns a window if the current hour is within the email window
    (email hour + 3h buffer for GitHub Actions delays). Otherwise returns None.
    One email per calendar day — no more, no less.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    email_hour = sorted(EMAIL_HOURS_UTC)[0]  # use first configured hour
    # Allow a 3-hour window after the target hour for GH Actions delays
    if email_hour <= now.hour < email_hour + 4:
        return f"{now.strftime('%Y-%m-%d')}_DAILY"
    return None


def should_send_email(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> bool:
    """Check if daily email should be sent. Deduplicates by DATE (not run_id)
    so even if the DB cache is lost and run_id changes, it won't resend."""
    if now is None:
        now = datetime.now(tz=timezone.utc)

    window = get_email_window(now)
    if window is None:
        return False

    # Deduplicate by window label ONLY (ignore run_id — survives cache misses)
    already = conn.execute(
        "SELECT id FROM mrev_email_log WHERE email_window=?",
        (window,)
    ).fetchone()

    return already is None


def record_email_sent(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> None:
    """Record that the daily email was sent."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    window = get_email_window(now)
    conn.execute("INSERT INTO mrev_email_log VALUES (?,?,?,?)",
        (str(uuid.uuid4())[:8], run_id, window, now.isoformat()))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  MONTHLY REPORT
# ══════════════════════════════════════════════════════════════════════════════

def should_send_monthly_email(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> bool:
    """Check if we should send the monthly report (1st of month, deduplicated)."""
    if not EMAIL_MONTHLY_ENABLED:
        return False
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if now.day != EMAIL_MONTHLY_DAY:
        return False

    window = f"MONTHLY_{now.strftime('%Y-%m')}"
    # Deduplicate by window ONLY (survives run_id changes from cache misses)
    already = conn.execute(
        "SELECT id FROM mrev_email_log WHERE email_window=?",
        (window,)
    ).fetchone()
    return already is None


def record_monthly_email_sent(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(tz=timezone.utc)
    window = f"MONTHLY_{now.strftime('%Y-%m')}"
    conn.execute("INSERT INTO mrev_email_log VALUES (?,?,?,?)",
        (str(uuid.uuid4())[:8], run_id, window, now.isoformat()))
    conn.commit()


def get_all_time_activity(conn: sqlite3.Connection, run_id: str) -> dict:
    """Gather all trading activity since the bot started for the monthly report."""
    now = datetime.now(tz=timezone.utc)

    # Run start date
    run_row = conn.execute("SELECT started_at, initial_capital FROM mrev_runs WHERE id=?", (run_id,)).fetchone()
    started_at = run_row["started_at"] if run_row else now.isoformat()
    initial_capital = float(run_row["initial_capital"]) if run_row else MREV_CAPITAL

    # All closed positions ever
    all_closed = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='CLOSED' ORDER BY exit_dt",
        (run_id,)
    ).fetchall()

    # Closed positions this month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
    closed_last_month = [c for c in all_closed
        if prev_month_start.isoformat() <= (c["exit_dt"] or "") < month_start.isoformat()]

    # All-time stats
    total_pnl_all = sum(float(c["pnl"] or 0) for c in all_closed)
    wins_all = sum(1 for c in all_closed if float(c["pnl"] or 0) > 0)
    losses_all = sum(1 for c in all_closed if float(c["pnl"] or 0) <= 0)
    total_closed_all = len(all_closed)

    # Last month stats
    pnl_last_month = sum(float(c["pnl"] or 0) for c in closed_last_month)
    wins_month = sum(1 for c in closed_last_month if float(c["pnl"] or 0) > 0)
    losses_month = sum(1 for c in closed_last_month if float(c["pnl"] or 0) <= 0)
    total_closed_month = len(closed_last_month)

    # Equity snapshots — first ever vs latest
    first_snap = conn.execute(
        "SELECT equity, created_at FROM mrev_snapshots WHERE run_id=? ORDER BY created_at ASC LIMIT 1",
        (run_id,)
    ).fetchone()
    last_snap = conn.execute(
        "SELECT equity, created_at FROM mrev_snapshots WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
        (run_id,)
    ).fetchone()

    equity_first = float(first_snap["equity"]) if first_snap else initial_capital
    equity_now = float(last_snap["equity"]) if last_snap else initial_capital

    # Monthly equity snapshots (first of each month) for the chart data
    monthly_snapshots = conn.execute(
        "SELECT equity, created_at FROM mrev_snapshots WHERE run_id=? ORDER BY created_at",
        (run_id,)
    ).fetchall()

    # Group by month
    months_data = {}
    for s in monthly_snapshots:
        month_key = s["created_at"][:7]  # "YYYY-MM"
        if month_key not in months_data:
            months_data[month_key] = {"first": float(s["equity"]), "last": float(s["equity"])}
        months_data[month_key]["last"] = float(s["equity"])

    # Compute monthly returns
    monthly_returns = []
    prev_equity = initial_capital
    for month_key in sorted(months_data.keys()):
        m = months_data[month_key]
        change = m["last"] - prev_equity
        change_pct = round(change / prev_equity * 100, 2) if prev_equity > 0 else 0
        monthly_returns.append({
            "month": month_key,
            "equity_end": m["last"],
            "change": round(change, 2),
            "change_pct": change_pct,
        })
        prev_equity = m["last"]

    # Best and worst trade all time
    best_trade = max(all_closed, key=lambda c: float(c["pnl"] or 0)) if all_closed else None
    worst_trade = min(all_closed, key=lambda c: float(c["pnl"] or 0)) if all_closed else None

    # Days running
    try:
        start_dt = datetime.fromisoformat(started_at)
        days_running = (now - start_dt).days
    except Exception:
        days_running = 0

    return {
        "started_at": started_at,
        "days_running": days_running,
        "initial_capital": initial_capital,
        "equity_now": equity_now,
        "total_return": round(equity_now - initial_capital, 2),
        "total_return_pct": round((equity_now - initial_capital) / initial_capital * 100, 2) if initial_capital > 0 else 0,
        "total_pnl_all": round(total_pnl_all, 2),
        "total_closed_all": total_closed_all,
        "wins_all": wins_all,
        "losses_all": losses_all,
        "win_rate_all": round(wins_all / total_closed_all * 100, 1) if total_closed_all > 0 else 0,
        "pnl_last_month": round(pnl_last_month, 2),
        "total_closed_month": total_closed_month,
        "wins_month": wins_month,
        "losses_month": losses_month,
        "win_rate_month": round(wins_month / total_closed_month * 100, 1) if total_closed_month > 0 else 0,
        "monthly_returns": monthly_returns,
        "best_trade": dict(best_trade) if best_trade else None,
        "worst_trade": dict(worst_trade) if worst_trade else None,
    }


def get_mrev_monthly_stats(conn: sqlite3.Connection, run_id: str, now: datetime) -> dict:
    """
    Compute extended stats for the previous calendar month.

    Returns dict with keys:
        month_label, year, month_start, month_end,
        equity_start, equity_end, equity_peak_month, max_drawdown_pct,
        closed (list of rows), count, wins, losses, win_rate_pct,
        total_pnl, profit_factor, avg_hold_hours, avg_return_pct,
        exit_breakdown (dict of reason_kind -> count),
        per_symbol (list of {symbol, count, pnl, avg_pct}),
        top_winners (up to 5), top_losers (up to 5),
        equity_sparkline (list of floats, one per day),
        open_at_eom (list of open positions at month end),
        alpaca_rejected (int, from mrev_hourly_log if tracked).
    """
    month_name_map = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
                      5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
                      9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}

    # Previous calendar month window
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = this_month_start
    month_start = (this_month_start - timedelta(days=1)).replace(day=1)
    month_label = month_name_map.get(month_start.month, str(month_start.month))

    # Closed trades in month
    closed = conn.execute(
        """SELECT symbol, qty, entry_price, entry_dt, exit_price, exit_dt,
                  pnl, exit_reason, initial_qty, partial_tp_taken
           FROM mrev_positions
           WHERE run_id=? AND status='CLOSED'
             AND exit_dt >= ? AND exit_dt < ?
           ORDER BY exit_dt""",
        (run_id, month_start.isoformat(), month_end.isoformat())
    ).fetchall()
    closed_dicts = [dict(r) for r in closed]

    count = len(closed_dicts)
    wins = [c for c in closed_dicts if float(c.get("pnl") or 0) > 0]
    losses = [c for c in closed_dicts if float(c.get("pnl") or 0) <= 0]
    total_pnl = sum(float(c.get("pnl") or 0) for c in closed_dicts)
    win_rate = round(len(wins) / count * 100, 1) if count > 0 else 0.0

    sum_wins = sum(float(c["pnl"]) for c in wins) if wins else 0.0
    sum_losses = abs(sum(float(c["pnl"]) for c in losses)) if losses else 0.0
    profit_factor = round(sum_wins / sum_losses, 2) if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0.0)

    # Avg hold time + avg return pct per closed trade
    hold_hours: list[float] = []
    return_pcts: list[float] = []
    for c in closed_dicts:
        try:
            e_dt = datetime.fromisoformat(c["entry_dt"])
            x_dt = datetime.fromisoformat(c["exit_dt"])
            hold_hours.append(max(0.0, (x_dt - e_dt).total_seconds() / 3600))
        except Exception:
            pass
        try:
            entry = float(c["entry_price"])
            exit_ = float(c["exit_price"])
            if entry > 0:
                return_pcts.append((exit_ - entry) / entry * 100)
        except Exception:
            pass
    avg_hold_hours = round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else 0.0
    avg_return_pct = round(sum(return_pcts) / len(return_pcts), 2) if return_pcts else 0.0

    # Exit reason breakdown — bucket raw reasons into kinds.
    exit_breakdown = {"take_profit": 0, "stop_loss": 0, "trailing": 0, "time_stop": 0,
                      "partial_tp": 0, "other": 0}
    for c in closed_dicts:
        reason = (c.get("exit_reason") or "").lower()
        if reason.startswith("take_profit"):
            exit_breakdown["take_profit"] += 1
        elif reason.startswith("stop_loss"):
            exit_breakdown["stop_loss"] += 1
        elif reason.startswith("trailing"):
            exit_breakdown["trailing"] += 1
        elif reason.startswith("time_stop"):
            exit_breakdown["time_stop"] += 1
        elif reason.startswith("partial_tp"):
            exit_breakdown["partial_tp"] += 1
        else:
            exit_breakdown["other"] += 1

    # Per-symbol breakdown
    by_sym: dict[str, dict] = {}
    for c in closed_dicts:
        sym = c.get("symbol", "?")
        b = by_sym.setdefault(sym, {"symbol": sym, "count": 0, "pnl": 0.0, "pcts": []})
        b["count"] += 1
        b["pnl"] += float(c.get("pnl") or 0)
        try:
            entry = float(c["entry_price"]); exit_ = float(c["exit_price"])
            if entry > 0:
                b["pcts"].append((exit_ - entry) / entry * 100)
        except Exception:
            pass
    per_symbol: list[dict] = []
    for sym, b in by_sym.items():
        avg_pct = round(sum(b["pcts"]) / len(b["pcts"]), 2) if b["pcts"] else 0.0
        per_symbol.append({"symbol": sym, "count": b["count"],
                           "pnl": round(b["pnl"], 2), "avg_pct": avg_pct})
    per_symbol.sort(key=lambda r: r["pnl"], reverse=True)

    # Top winners / losers
    enriched = []
    for c in closed_dicts:
        try:
            entry = float(c["entry_price"]); exit_ = float(c["exit_price"])
            ret = ((exit_ - entry) / entry * 100) if entry > 0 else 0.0
        except Exception:
            ret = 0.0
        enriched.append({"symbol": c.get("symbol"), "pnl": float(c.get("pnl") or 0),
                         "ret_pct": round(ret, 2),
                         "entry": float(c.get("entry_price") or 0),
                         "exit": float(c.get("exit_price") or 0)})
    top_winners = sorted([e for e in enriched if e["pnl"] > 0], key=lambda e: e["pnl"], reverse=True)[:5]
    top_losers = sorted([e for e in enriched if e["pnl"] <= 0], key=lambda e: e["pnl"])[:5]

    # Equity within month + max drawdown
    snaps = conn.execute(
        """SELECT equity, created_at FROM mrev_snapshots
           WHERE run_id=? AND created_at >= ? AND created_at < ?
           ORDER BY created_at""",
        (run_id, month_start.isoformat(), month_end.isoformat())
    ).fetchall()
    equities = [float(s["equity"]) for s in snaps]
    equity_start = equities[0] if equities else MREV_CAPITAL
    equity_end = equities[-1] if equities else equity_start
    # Max drawdown during the month (running peak → lowest subsequent dip)
    peak = -1.0
    max_dd_pct = 0.0
    equity_peak_month = equity_start
    for e in equities:
        if e > peak:
            peak = e
            equity_peak_month = max(equity_peak_month, e)
        if peak > 0:
            dd = (peak - e) / peak * 100
            if dd > max_dd_pct:
                max_dd_pct = dd
    max_dd_pct = round(max_dd_pct, 2)

    # Sparkline — one equity value per day (last snapshot of each day)
    per_day: dict[str, float] = {}
    for s in snaps:
        day = s["created_at"][:10]
        per_day[day] = float(s["equity"])
    sparkline = [per_day[k] for k in sorted(per_day.keys())]

    # Open positions at end-of-month (from current open, since we're reporting at month flip)
    open_rows = conn.execute(
        "SELECT symbol, qty, entry_price, entry_dt, stop_loss, partial_tp_taken "
        "FROM mrev_positions WHERE run_id=? AND status='OPEN' ORDER BY entry_dt",
        (run_id,)
    ).fetchall()
    open_at_eom = [dict(r) for r in open_rows]

    return {
        "month_label": month_label,
        "year": month_start.year,
        "month_start": month_start,
        "month_end": month_end,
        "equity_start": round(equity_start, 2),
        "equity_end": round(equity_end, 2),
        "equity_peak_month": round(equity_peak_month, 2),
        "max_drawdown_pct": max_dd_pct,
        "count": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "total_pnl": round(total_pnl, 2),
        "profit_factor": profit_factor,
        "avg_hold_hours": avg_hold_hours,
        "avg_return_pct": avg_return_pct,
        "exit_breakdown": exit_breakdown,
        "per_symbol": per_symbol,
        "top_winners": top_winners,
        "top_losers": top_losers,
        "equity_sparkline": sparkline,
        "open_at_eom": open_at_eom,
    }


def _render_sparkline(values: list[float]) -> str:
    """Tiny unicode sparkline. Returns fixed-width spark + direction label."""
    if not values or len(values) < 2:
        return "—"
    chars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    out = "".join(chars[min(7, int((v - lo) / span * 7))] for v in values)
    return out


def _build_monthly_email_report(monthly_data: dict) -> tuple[str, str]:
    """Build subject + HTML body for the MREV monthly summary email.

    100% MREV: no references to ACCOUNT_TOTAL_CAPITAL or DAILY_BOT_CAPITAL.
    Data comes from mrev_paper.db only.
    """
    d = monthly_data
    month_label = d["month_label"]
    year = d["year"]
    equity_start = d["equity_start"]
    equity_end = d["equity_end"]
    pnl = round(equity_end - equity_start, 2)
    pnl_pct = round((pnl / equity_start * 100), 2) if equity_start > 0 else 0.0
    max_dd = d["max_drawdown_pct"]

    css = f"<style>{build_css()}</style>"
    is_profit = pnl >= 0

    subject = (f"[MREV Mensual] {month_label} {year} · "
               f"{pnl_pct:+.2f}% · ${equity_end:,.0f}")

    # ── Hero ─────────────────────────────────────────────────────────────────
    hero_cls = "hero" if is_profit else "hero loss"
    hero = f"""<div class="{hero_cls}">
        <div class="label">MREV · Mean Reversion 1H · {month_label} {year}</div>
        <div class="metric">${equity_end:,.2f}</div>
        <p style="margin:6px 0 0; font-size:13px; opacity:.9;">
            Equity al cierre del mes · {'+' if pnl >= 0 else ''}${pnl:,.2f} vs inicio de mes
            (<b>{pnl_pct:+.2f}%</b>) · Max drawdown intra-mes: <b>{max_dd:.2f}%</b>
        </p>
    </div>"""

    # ── KPIs card ────────────────────────────────────────────────────────────
    pf = d["profit_factor"]
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
    breakdown = d["exit_breakdown"]
    total_exits = sum(breakdown.values()) or 1

    def _pct_of(kind: str) -> str:
        return f"{breakdown[kind] / total_exits * 100:.0f}%"

    kpis = f"""<div class="card">
        <h2>Resumen del mes</h2>
        <div class="kpis">
            <div class="kpi"><div class="v">{d['count']}</div><div class="l">Trades cerrados</div></div>
            <div class="kpi"><div class="v">{d['win_rate_pct']:.0f}%</div><div class="l">Win rate</div></div>
            <div class="kpi"><div class="v">{pf_str}</div><div class="l">Profit factor</div></div>
            <div class="kpi"><div class="v">{d['avg_hold_hours']:.0f}h</div><div class="l">Hold promedio</div></div>
            <div class="kpi"><div class="v">{d['avg_return_pct']:+.2f}%</div><div class="l">Retorno promedio/trade</div></div>
        </div>
        <p style="margin-top:14px;"><small>
            Dónde salieron las posiciones:
            <b>{breakdown['take_profit']}</b> TP ({_pct_of('take_profit')}) ·
            <b>{breakdown['stop_loss']}</b> stop ({_pct_of('stop_loss')}) ·
            <b>{breakdown['trailing']}</b> trailing ({_pct_of('trailing')}) ·
            <b>{breakdown['time_stop']}</b> time stop ({_pct_of('time_stop')}) ·
            <b>{breakdown['partial_tp']}</b> parcial ({_pct_of('partial_tp')})
        </small></p>
    </div>"""

    # ── Top winners / losers ─────────────────────────────────────────────────
    def _rows_from(items: list[dict]) -> str:
        if not items:
            return '<tr><td colspan="4" class="muted">— sin trades</td></tr>'
        return "".join(
            f"<tr><td><b>{r['symbol']}</b></td>"
            f"<td>${r['entry']:,.2f} → ${r['exit']:,.2f}</td>"
            f"<td class=\"{'pos' if r['pnl'] >= 0 else 'neg'}\">{r['ret_pct']:+.2f}%</td>"
            f"<td class=\"{'pos' if r['pnl'] >= 0 else 'neg'}\">${r['pnl']:+,.2f}</td></tr>"
            for r in items
        )

    tops_html = f"""<div class="card">
        <h2>Top winners / losers del mes</h2>
        <h3 style="margin-top:8px;">5 mejores</h3>
        <table class="data">
            <thead><tr><th>Símbolo</th><th>Entry → Exit</th><th>Retorno</th><th>P&L</th></tr></thead>
            <tbody>{_rows_from(d['top_winners'])}</tbody>
        </table>
        <h3 style="margin-top:14px;">5 peores</h3>
        <table class="data">
            <thead><tr><th>Símbolo</th><th>Entry → Exit</th><th>Retorno</th><th>P&L</th></tr></thead>
            <tbody>{_rows_from(d['top_losers'])}</tbody>
        </table>
    </div>"""

    # ── Per-symbol breakdown ─────────────────────────────────────────────────
    if d["per_symbol"]:
        sym_rows = "".join(
            f"<tr><td><b>{r['symbol']}</b></td>"
            f"<td>{r['count']}</td>"
            f"<td class=\"{'pos' if r['pnl'] >= 0 else 'neg'}\">${r['pnl']:+,.2f}</td>"
            f"<td>{r['avg_pct']:+.2f}%</td></tr>"
            for r in d["per_symbol"]
        )
        by_sym_html = f"""<div class="card">
            <h2>Por símbolo</h2>
            <table class="data">
                <thead><tr><th>Símbolo</th><th>Trades</th><th>P&L total</th><th>Retorno promedio</th></tr></thead>
                <tbody>{sym_rows}</tbody>
            </table>
        </div>"""
    else:
        by_sym_html = ""

    # ── Equity sparkline ─────────────────────────────────────────────────────
    spark = _render_sparkline(d["equity_sparkline"])
    spark_html = f"""<div class="card">
        <h2>Equity del mes</h2>
        <p><span class="spark">{spark}</span></p>
        <p><small>
            Inicio: <b>${equity_start:,.2f}</b> ·
            Pico: <b>${d['equity_peak_month']:,.2f}</b> ·
            Cierre: <b>${equity_end:,.2f}</b> ·
            Drawdown máximo: <b>{max_dd:.2f}%</b>
        </small></p>
    </div>"""

    # ── System behaviour ─────────────────────────────────────────────────────
    behaviour_html = f"""<div class="card">
        <h2>Comportamiento del sistema</h2>
        <p><b>Partial TP stats:</b> {breakdown['partial_tp']} parciales ejecutados en el mes.</p>
        <p><b>Exits finales:</b>
            take-profit {breakdown['take_profit']} ·
            stop-loss {breakdown['stop_loss']} ·
            trailing {breakdown['trailing']} ·
            time-stop {breakdown['time_stop']}.</p>
        <p><b>Kill switch (MAX_DRAWDOWN={MAX_DRAWDOWN:.0%}):</b>
            drawdown máximo observado {max_dd:.2f}% — {'ACTIVADO' if max_dd >= MAX_DRAWDOWN*100 else 'no activado'}.</p>
    </div>"""

    # ── Open at EOM ──────────────────────────────────────────────────────────
    open_rows_html = ""
    if d["open_at_eom"]:
        rows = "".join(
            f"<tr><td><b>{r['symbol']}</b></td>"
            f"<td>{float(r['qty']):g}</td>"
            f"<td>${float(r['entry_price']):,.2f}</td>"
            f"<td>${float(r['stop_loss'] or 0):,.2f}</td>"
            f"<td>{int(r['partial_tp_taken'] or 0)}</td></tr>"
            for r in d["open_at_eom"]
        )
        open_rows_html = f"""<div class="card">
            <h2>Próximo mes — posiciones abiertas</h2>
            <table class="data">
                <thead><tr><th>Símbolo</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Stage</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""
    else:
        open_rows_html = """<div class="card">
            <h2>Próximo mes — posiciones abiertas</h2>
            <p class="muted">Sin posiciones abiertas al cierre de mes.</p>
        </div>"""

    # ── Assemble body ────────────────────────────────────────────────────────
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{css}
</head>
<body><div class="wrap">
    {hero}
    {kpis}
    {spark_html}
    {tops_html}
    {by_sym_html}
    {behaviour_html}
    {open_rows_html}
    <div class="foot">MREV · Mean Reversion 1H · reporte mensual generado el día {EMAIL_MONTHLY_DAY} de cada mes</div>
</div></body></html>"""

    return subject, body



def send_monthly_email_report(monthly_data: dict, dry_run: bool = False) -> None:
    """Send (or preview) the monthly MREV trading report.

    In dry_run mode, writes mrev_monthly_preview.html and does not send.
    """
    subject, body = _build_monthly_email_report(monthly_data)

    if dry_run:
        preview_path = Path(__file__).resolve().parent / "mrev_monthly_preview.html"
        try:
            preview_path.write_text(body, encoding="utf-8")
            ok(f"[DRY] Monthly preview written to {preview_path}")
        except Exception as e:
            warn(f"Could not write monthly preview: {e}")
        info(f"[DRY] Subject: {subject}")
        return

    if not EMAIL_ENABLED:
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        return

    if send_smtp(subject, body):
        ok(f"Email MENSUAL enviado a {EMAIL_TO}")
    else:
        warn("Falló el envío del email mensual")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(dry_run: bool = False) -> dict:
    """Run the full MREV-1H pipeline: fetch → indicators → scan → trade → email."""
    hdr("MREV-1H Mean Reversion Bot")
    now = datetime.now(tz=timezone.utc)
    info(f"Run time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    conn = get_db()
    run_id = get_or_create_run(conn)

    # Migración de posiciones ETF legacy (si quedaron atrapadas en mrev_positions
    # antes del split RFTM/MREV). Idempotente — solo actúa si encuentra ETFs.
    migrate_legacy_etf_positions(conn)

    # ── 0. Sync local DB with Alpaca ─────────────────────────────────────────
    # Garantiza que toda posición de Alpaca (solo cripto en MREV) esté registrada
    # en mrev_paper.db antes de evaluar señales. Sin esto, posiciones compradas
    # manualmente (o perdidas en otra corrida) nunca dispararían partial TPs.
    sync_with_alpaca(conn, run_id)

    # ── 1. Determine tradeable symbols ───────────────────────────────────────
    hdr("Fetching 1H Market Data")
    tradeable = []
    for sym in ALL_SYMBOLS:
        if is_market_open_for(sym):
            tradeable.append(sym)
        else:
            info(f"{sym}: market closed, skipping")

    if not tradeable:
        warn("No markets open right now. Nothing to trade.")
        return {"status": "no_markets", "buys": [], "sells": [], "run_id": run_id}

    ok(f"Trading {len(tradeable)} symbols: {', '.join(tradeable)}")

    # ── 2. Fetch real 1H data from Alpaca ────────────────────────────────────
    all_data: dict[str, pd.DataFrame] = {}
    for sym in tradeable:
        try:
            df = alpaca_get_1h_bars(sym, hours_back=250)
            if len(df) >= 25:
                all_data[sym] = compute_indicators(df)
                ok(f"{sym}: {len(df)} bars fetched + indicators computed")
            else:
                warn(f"{sym}: only {len(df)} bars (need ≥25), skipping")
        except Exception as e:
            err(f"{sym}: failed to fetch data — {e}")

    if not all_data:
        err("No data available for any symbol")
        return {"status": "no_data", "buys": [], "sells": [], "run_id": run_id}

    # ── 3. Get current prices ────────────────────────────────────────────────
    current_prices: dict[str, float] = {}
    for sym, df in all_data.items():
        current_prices[sym] = float(df.iloc[-1]["close"])

    # ── 4. Portfolio state ───────────────────────────────────────────────────
    open_positions = get_open_positions(conn, run_id)
    cash = get_cash(conn, run_id)
    equity = get_equity(conn, run_id, current_prices)
    peak = get_peak_equity(conn, run_id)
    open_symbols = {p["symbol"] for p in open_positions}

    # Query Alpaca's REAL buying power (account is shared with RFTM)
    try:
        acct = alpaca_get_account()
        alpaca_buying_power = float(acct.get("buying_power", cash))
    except Exception:
        alpaca_buying_power = cash

    hdr("Portfolio State")
    info(f"Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}  |  Open: {len(open_positions)}")
    info(f"Alpaca buying power: ${alpaca_buying_power:,.2f}")

    # Drawdown check
    drawdown = (peak - equity) / peak if peak > 0 else 0
    if drawdown >= MAX_DRAWDOWN:
        err(f"KILL SWITCH: drawdown {drawdown:.1%} ≥ {MAX_DRAWDOWN:.0%}! Stopping all trades.")
        return {"status": "kill_switch", "buys": [], "sells": [], "run_id": run_id,
                "equity": equity, "drawdown": drawdown}

    # ── 5. Scan for signals ──────────────────────────────────────────────────
    hdr("Signal Scanner")
    buys: list[dict] = []
    sells: list[dict] = []
    hold_closest: list[dict] = []

    for sym in tradeable:
        if sym not in all_data:
            continue
        df = all_data[sym]
        row = df.iloc[-1]

        if sym in open_symbols:
            # Check exit
            pos = next(p for p in open_positions if p["symbol"] == sym)
            entry_dt = datetime.fromisoformat(pos["entry_dt"]) if pos["entry_dt"] else now - timedelta(hours=1)
            # Track highest price since entry for trailing stop
            cur_close = float(row["close"])
            prev_high = float(pos.get("highest_since_entry") or pos["entry_price"])
            highest = max(prev_high, cur_close)
            if highest > prev_high:
                conn.execute("UPDATE mrev_positions SET highest_since_entry=? WHERE id=?",
                             (highest, pos["id"]))
                conn.commit()
            # ── Partial Take Profit en DOS ETAPAS (5% y 7.5%) ───────────────
            #   Stage 0 → a +5%  vende 50% (50% del original).
            #   Stage 1 → a +7.5% vende 50% del remanente (= 25% del original).
            #   Stage 2 → no se dispara ningún parcial más; queda el 25% corriendo.
            try:
                tp_stage = int(pos["partial_tp_taken"] or 0)
            except Exception:
                tp_stage = 0

            entry_px = float(pos["entry_price"])
            qty_full = float(pos["qty"])
            unrealized_pct = (cur_close - entry_px) / entry_px if entry_px > 0 else 0.0
            is_crypto = "/" in sym

            def _round_qty(q: float) -> float:
                if is_crypto:
                    min_q = CRYPTO_MIN_QTY.get(sym, 0.0001)
                    precision = len(str(min_q).rstrip("0").split(".")[-1])
                    return round(math.floor(q / min_q) * min_q, precision)
                return float(math.floor(q))

            tp_trigger_pct, tp_ratio, next_stage = None, None, None
            if tp_stage == 0 and unrealized_pct >= MREV_TP1_PCT:
                tp_trigger_pct, tp_ratio, next_stage = MREV_TP1_PCT, MREV_TP1_RATIO, 1
            elif tp_stage == 1 and unrealized_pct >= MREV_TP2_PCT:
                tp_trigger_pct, tp_ratio, next_stage = MREV_TP2_PCT, MREV_TP2_RATIO, 2

            if tp_trigger_pct is not None and qty_full > 0:
                sell_qty = _round_qty(qty_full * tp_ratio)
                notional = sell_qty * cur_close
                if sell_qty > 0 and sell_qty < qty_full and notional < PARTIAL_MIN_NOTIONAL_USD:
                    info(f"PARTIAL_TP{next_stage} skipped {sym}: notional ${notional:.2f} < ${PARTIAL_MIN_NOTIONAL_USD:.2f}")
                elif sell_qty > 0 and sell_qty < qty_full:
                    pnl_p = (cur_close - entry_px) * sell_qty
                    sells.append({
                        "symbol": sym, "qty": sell_qty,
                        "entry_price": entry_px,
                        "exit_price": cur_close,
                        "pnl": round(pnl_p, 2),
                        "reason": f"partial_tp{next_stage}_{tp_trigger_pct*100:.1f}pct:{unrealized_pct:.2%}",
                        "position_id": pos["id"],
                        "partial": True,
                        "remaining_qty": qty_full - sell_qty,
                        "new_stage": next_stage,
                        "prev_stage": tp_stage,
                        "old_stop": float(pos["stop_loss"] or 0),
                        "trigger_pct": tp_trigger_pct,
                        "sma20": float(row.get("sma_20") or 0) or None,
                        "atr14": float(row.get("atr_14") or 0) or None,
                    })
                    print(f"  {C.GREEN}PTP{next_stage}{C.RESET} {sym:10s}  ${cur_close:>10.2f}  {tp_ratio*100:.0f}%={sell_qty}  (+{unrealized_pct:.2%})")
                    conn.execute("INSERT INTO mrev_signals VALUES (?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4())[:8], run_id, sym, f"PARTIAL_TP{next_stage}", cur_close,
                         float(row.get("rsi_14", 0) or 0),
                         f"partial_tp{next_stage}:{unrealized_pct:.2%}", now.isoformat()))
                    conn.commit()
                    continue

            should_exit, reason = check_exit(row, float(pos["entry_price"]), entry_dt, now,
                                             highest_since_entry=highest)
            if should_exit:
                pnl = (float(row["close"]) - float(pos["entry_price"])) * float(pos["qty"])
                sells.append({
                    "symbol": sym, "qty": float(pos["qty"]),
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": float(row["close"]),
                    "pnl": round(pnl, 2), "reason": reason,
                    "position_id": pos["id"],
                    "prev_stage": tp_stage,
                })
                icon = C.GREEN if pnl >= 0 else C.RED
                print(f"  {icon}EXIT{C.RESET}  {sym:10s}  ${float(row['close']):>10.2f}  P&L: ${pnl:+.2f}  ({reason})")

                # Log signal
                conn.execute("INSERT INTO mrev_signals VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8], run_id, sym, "EXIT", float(row["close"]),
                     float(row.get("rsi_14", 0) or 0), reason, now.isoformat()))
        else:
            # Check entry
            should_enter, reason = check_entry(sym, row)
            rsi_val = float(row.get("rsi_14", 50) or 50)

            if should_enter:
                qty, stop = size_position(sym, float(row["close"]), float(row["atr_14"]), equity)
                if qty > 0 and len(open_positions) + len(buys) < MREV_MAX_POSITIONS:
                    notional = qty * float(row["close"])
                    # Use the lesser of local cash and Alpaca buying power
                    available = min(cash, alpaca_buying_power * ALPACA_BP_SAFETY)
                    if notional > available and float(row["close"]) > 0:
                        # Re-cap qty to fit within available funds
                        is_crypto = "/" in sym
                        if is_crypto:
                            min_q = CRYPTO_MIN_QTY.get(sym, 0.0001)
                            precision = len(str(min_q).rstrip("0").split(".")[-1])
                            qty = round(math.floor((available / float(row["close"])) / min_q) * min_q, precision)
                        else:
                            qty = math.floor(available / float(row["close"]))
                        notional = qty * float(row["close"])
                    if notional <= available and notional >= 10.0:
                        buys.append({
                            "symbol": sym, "qty": qty,
                            "price": float(row["close"]),
                            "stop": round(stop, 4),
                            "notional": round(notional, 2),
                            "rsi": round(rsi_val, 1),
                        })
                        cash -= notional  # reserve local cash
                        alpaca_buying_power -= notional  # reserve Alpaca BP too
                        print(f"  {C.GREEN}ENTER{C.RESET} {sym:10s}  ${float(row['close']):>10.2f}  qty={qty}  stop=${stop:.2f}  RSI={rsi_val:.1f}")
                    else:
                        info(f"{sym}: entry signal but not enough buying power (${notional:.2f} > ${available:.2f})")
                else:
                    info(f"{sym}: entry signal but qty=0 or max positions reached")
            else:
                # Debug: show why each symbol was rejected
                print(f"    DBG  {sym:10s}  rejected: {reason}")
                # Track closest to entry for email report
                hold_closest.append({"symbol": sym, "rsi": round(rsi_val, 1), "reason": reason})

                # Log signal
                conn.execute("INSERT INTO mrev_signals VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8], run_id, sym, "HOLD", float(row["close"]),
                     rsi_val, reason, now.isoformat()))

    if not buys and not sells:
        info("No signals this hour. All holdings maintained.")

    # ── 6. Execute orders ────────────────────────────────────────────────────
    if not dry_run:
        hdr("Executing Orders")

        # Sells first
        for s in sells:
            try:
                is_partial = bool(s.get("partial"))
                if ALPACA_API_KEY:
                    order = alpaca_submit_order(s["symbol"], s["qty"], "sell")
                    tag = "PTP " if is_partial else "SOLD"
                    ok(f"{tag} {s['symbol']}: qty={s['qty']} → order {order.get('id', '?')[:8]}")
                else:
                    ok(f"SOLD {s['symbol']}: qty={s['qty']} (no Alpaca keys, local only)")

                if is_partial:
                    # Keep position open, avanzar stage, reducir qty
                    stage_to_write = int(s.get("new_stage") or 1)
                    prev_stage = int(s.get("prev_stage") or 0)
                    entry_px = float(s.get("entry_price") or 0)
                    old_stop = float(s.get("old_stop") or 0)
                    # F1: cuando pasa 0→1 (TP1) subir stop al breakeven.
                    # Regla: nunca bajar el stop — sólo subir.
                    if prev_stage == 0 and stage_to_write >= 1 and entry_px > 0:
                        new_stop = max(old_stop, entry_px)
                        if new_stop > old_stop:
                            info(f"E3 raised to breakeven for {s['symbol']}: ${old_stop:.2f} → ${new_stop:.2f}")
                        conn.execute("""UPDATE mrev_positions
                            SET qty=?, partial_tp_taken=?,
                                initial_qty=COALESCE(initial_qty, ?),
                                pnl=COALESCE(pnl,0)+?,
                                stop_loss=?
                            WHERE id=?""",
                            (s["remaining_qty"], stage_to_write,
                             s["qty"] + s["remaining_qty"] if stage_to_write == 1 else s["qty"] + s["remaining_qty"],
                             s["pnl"], round(new_stop, 4), s["position_id"]))
                    else:
                        conn.execute("""UPDATE mrev_positions
                            SET qty=?, partial_tp_taken=?,
                                initial_qty=COALESCE(initial_qty, ?),
                                pnl=COALESCE(pnl,0)+?
                            WHERE id=?""",
                            (s["remaining_qty"], stage_to_write,
                             s["qty"] + s["remaining_qty"] if stage_to_write == 1 else s["qty"] + s["remaining_qty"],
                             s["pnl"], s["position_id"]))
                else:
                    conn.execute("""UPDATE mrev_positions SET status='CLOSED', exit_price=?,
                        exit_dt=?, pnl=COALESCE(pnl,0)+?, exit_reason=? WHERE id=?""",
                        (s["exit_price"], now.isoformat(), s["pnl"], s["reason"], s["position_id"]))

                # F2: email inmediato en eventos de stage (TP1, TP2, TP final)
                try:
                    entry_px_ev = float(s.get("entry_price") or 0)
                    sell_price = float(s["exit_price"])
                    if is_partial:
                        stage_now = int(s.get("new_stage") or 1)
                        remaining = float(s["remaining_qty"])
                        if stage_now == 1:
                            next_target = round(entry_px_ev * (1.0 + MREV_TP2_PCT), 4)
                            next_label = "TP2"
                            event_tag = "TP1"
                        elif stage_now == 2:
                            # TP final dinámico = SMA20 + 1.5 × ATR (X1 de MREV)
                            sma = s.get("sma20")
                            atr = s.get("atr14")
                            if sma and atr:
                                next_target = round(float(sma) + 1.5 * float(atr), 4)
                            else:
                                next_target = round(entry_px_ev * 1.10, 4)
                            next_label = "TP final (SMA20 + 1.5×ATR)"
                            event_tag = "TP2"
                        else:
                            next_target = None
                            next_label = ""
                            event_tag = f"TP{stage_now}"
                        # Stop post-evento: en TP1 subió a breakeven, en otros
                        # stages el stop no se toca en este flujo.
                        old_stop_ev = float(s.get("old_stop") or 0)
                        new_stop_ev = (max(old_stop_ev, entry_px_ev)
                                       if stage_now >= 1 and entry_px_ev > 0
                                       else old_stop_ev)
                        send_stage_event_email(
                            bot_tag="MREV",
                            event=event_tag,
                            symbol=s["symbol"],
                            entry_price=entry_px_ev,
                            sell_price=sell_price,
                            sell_qty=float(s["qty"]),
                            realized_pnl=float(s["pnl"]),
                            remaining_qty=remaining,
                            new_stage=stage_now,
                            next_target=next_target,
                            next_target_label=next_label,
                            current_price=sell_price,
                            dry_run=dry_run,
                            old_stop_loss=old_stop_ev,
                            new_stop_loss=new_stop_ev,
                        )
                    else:
                        # E7 equivalente en MREV = X1 take_profit después de stage 2
                        prev_stage_full = int(s.get("prev_stage") or 0)
                        reason_full = str(s.get("reason") or "")
                        if prev_stage_full >= 2 and reason_full.startswith("take_profit"):
                            send_stage_event_email(
                                bot_tag="MREV",
                                event="TP_FINAL",
                                symbol=s["symbol"],
                                entry_price=entry_px_ev,
                                sell_price=sell_price,
                                sell_qty=float(s["qty"]),
                                realized_pnl=float(s["pnl"]),
                                remaining_qty=0,
                                new_stage=3,
                                next_target=None,
                                next_target_label="",
                                current_price=sell_price,
                                dry_run=dry_run,
                            )
                except Exception as _e_email:
                    warn(f"Falló email de stage para {s.get('symbol')}: {_e_email}")
            except Exception as e:
                err(f"Failed to sell {s['symbol']}: {e}")

        # Then buys — re-check buying power before each
        for b in buys:
            try:
                if ALPACA_API_KEY:
                    # Re-query Alpaca BP (changes after each fill)
                    try:
                        acct_now = alpaca_get_account()
                        bp_now = float(acct_now.get("buying_power", 0))
                    except Exception:
                        bp_now = b["notional"] + 1  # allow through on error
                    if bp_now * ALPACA_BP_SAFETY < b["notional"]:
                        warn(f"Skipping {b['symbol']}: buying power ${bp_now:,.2f} < notional ${b['notional']:,.2f}")
                        continue
                    order = alpaca_submit_order(b["symbol"], b["qty"], "buy")
                    ok(f"BOUGHT {b['symbol']}: qty={b['qty']} @ ${b['price']:.2f} → order {order.get('id', '?')[:8]}")
                else:
                    ok(f"BOUGHT {b['symbol']}: qty={b['qty']} @ ${b['price']:.2f} (no Alpaca keys, local only)")

                # Save position — columnas explícitas (la tabla tiene 15, no 12)
                conn.execute(
                    """INSERT INTO mrev_positions
                       (id, run_id, symbol, qty, entry_price, stop_loss, entry_dt,
                        status, highest_since_entry, partial_tp_taken, initial_qty)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4())[:8], run_id, b["symbol"], b["qty"], b["price"],
                     b["stop"], now.isoformat(), "OPEN", b["price"], 0, b["qty"]))

                # Log signal
                conn.execute("INSERT INTO mrev_signals VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8], run_id, b["symbol"], "ENTER", b["price"],
                     b["rsi"], "", now.isoformat()))
            except Exception as e:
                err(f"Failed to buy {b['symbol']}: {e}")

        conn.commit()
    else:
        hdr("DRY RUN — No orders sent")
        for b in buys:
            info(f"Would BUY  {b['symbol']}: qty={b['qty']} @ ${b['price']:.2f}")
        for s in sells:
            info(f"Would SELL {s['symbol']}: qty={s['qty']} @ ${s['exit_price']:.2f}")

    # ── 7. Save snapshot ─────────────────────────────────────────────────────
    equity_now = get_equity(conn, run_id, current_prices)
    new_peak = max(peak, equity_now)
    conn.execute("INSERT INTO mrev_snapshots VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4())[:8], run_id, equity_now, get_cash(conn, run_id),
         len(get_open_positions(conn, run_id)), new_peak, now.isoformat()))
    conn.commit()

    # ── 8. Log this hourly run ───────────────────────────────────────────────
    log_hourly_run(conn, run_id, buys, sells, equity_now, get_cash(conn, run_id), len(tradeable))

    # ── 9. Summary ───────────────────────────────────────────────────────────
    hdr("Summary")
    return_pct = (equity_now - MREV_CAPITAL) / MREV_CAPITAL * 100
    ok(f"Equity: ${equity_now:,.2f} ({return_pct:+.2f}%)")
    ok(f"Buys: {len(buys)}  |  Sells: {len(sells)}  |  Open: {len(get_open_positions(conn, run_id))}")

    # Indicadores por símbolo para el email (para TP dinámico SMA20+1.5×ATR)
    per_symbol_ind: dict[str, dict] = {}
    for sym, df in all_data.items():
        try:
            last = df.iloc[-1]
            per_symbol_ind[sym] = {
                "close": float(last.get("close") or 0),
                "sma_20": float(last.get("sma_20") or 0),
                "atr_14": float(last.get("atr_14") or 0),
                "rsi_14": float(last.get("rsi_14") or 0),
            }
        except Exception:
            pass

    result = {
        "status": "ok",
        "run_id": run_id,
        "buys": buys,
        "sells": sells,
        "equity": equity_now,
        "cash": get_cash(conn, run_id),
        "return_pct": return_pct,
        "peak": new_peak,
        "open_positions": get_open_positions(conn, run_id),
        "hold_closest": sorted(hold_closest, key=lambda x: x["rsi"])[:3],
        "dry_run": dry_run,
        "per_symbol_ind": per_symbol_ind,
        "current_prices": current_prices,
    }

    # ── 10. Get unified account overview (for all emails) ──────────────────
    acct_overview = get_account_overview()
    result["account_overview"] = acct_overview

    # ── 11. Send daily email (1x/day — window-based to handle cron delays) ──
    window = get_email_window(now)
    if should_send_email(conn, run_id, now):
        info(f"Email window [{window}] — building unified daily summary...")
        activity = get_last_24h_activity(conn, run_id)
        result["activity_24h"] = activity
        send_email_report(result)
        record_email_sent(conn, run_id, now)
    else:
        info(f"Email already sent for window [{window}], skipping.")

    # ── 12. Send monthly email (1st of each month, same email hour) ──────────
    if should_send_monthly_email(conn, run_id, now):
        info("Building MONTHLY summary report...")
        monthly_data = get_mrev_monthly_stats(conn, run_id, now)
        send_monthly_email_report(monthly_data, dry_run=dry_run)
        record_monthly_email_sent(conn, run_id, now)

    conn.close()
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  UNIFIED ACCOUNT DATA (both bots)
# ══════════════════════════════════════════════════════════════════════════════

def get_account_overview() -> dict:
    """Get unified account data from Alpaca API (covers both bots)."""
    try:
        acct = alpaca_get_account()
        positions = alpaca_get_positions()
    except Exception:
        return {"available": False}

    equity = float(acct.get("equity", 0))
    cash = float(acct.get("cash", 0))
    long_value = float(acct.get("long_market_value", 0))

    # Classify positions by bot: MREV trades crypto (has "/"), daily trades ETFs
    mrev_positions = []
    daily_positions = []
    for p in positions:
        sym = p.get("symbol", "")
        pos_data = {
            "symbol": sym,
            "qty": float(p.get("qty", 0)),
            "entry_price": float(p.get("avg_entry_price", 0)),
            "current_price": float(p.get("current_price", 0)),
            "market_value": float(p.get("market_value", 0)),
            "unrealized_pnl": float(p.get("unrealized_pl", 0)),
            "unrealized_pnl_pct": float(p.get("unrealized_plpc", 0)) * 100,
        }
        # Crypto symbols in Alpaca don't have "/" but MREV bot trades crypto
        # Also check against known MREV symbols
        if any(c in sym for c in ["BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "DOT", "ADA"]):
            mrev_positions.append(pos_data)
        else:
            daily_positions.append(pos_data)

    mrev_invested = sum(p["market_value"] for p in mrev_positions)
    daily_invested = sum(p["market_value"] for p in daily_positions)
    mrev_unrealized = sum(p["unrealized_pnl"] for p in mrev_positions)
    daily_unrealized = sum(p["unrealized_pnl"] for p in daily_positions)

    return {
        "available": True,
        "equity": equity,
        "cash": cash,
        "long_value": long_value,
        "total_return": round(equity - ACCOUNT_TOTAL_CAPITAL, 2),
        "total_return_pct": round((equity - ACCOUNT_TOTAL_CAPITAL) / ACCOUNT_TOTAL_CAPITAL * 100, 2) if ACCOUNT_TOTAL_CAPITAL > 0 else 0,
        "mrev_positions": mrev_positions,
        "daily_positions": daily_positions,
        "mrev_invested": round(mrev_invested, 2),
        "daily_invested": round(daily_invested, 2),
        "mrev_unrealized": round(mrev_unrealized, 2),
        "daily_unrealized": round(daily_unrealized, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _build_email_report(result: dict) -> tuple[str, str]:
    """Build subject + HTML body for the MREV 1h daily summary email.
    Espejo del email del bot RFTM pero con datos EXCLUSIVAMENTE de MREV:
    - Hero con equity MREV vs MREV_CAPITAL.
    - Lo que tengo en cartera: posiciones de mrev_paper.db con cuadrados SL/Precio/TP dinámico.
    - Línea "Stage X · próximo: TPY a $Z (faltan W%)".
    - Actividad últimas 24h (buys/sells cerrados).
    """
    now = datetime.now(tz=timezone.utc)
    today = now.strftime("%d/%m/%Y")
    today_full = now.strftime("%d/%m %H:%M UTC")
    dry_run = result.get("dry_run", False)
    activity = result.get("activity_24h", {})

    # MREV 24h data from local DB
    mrev_entries = activity.get("total_entries", 0)
    mrev_exits = activity.get("total_exits", 0)
    mrev_trades = mrev_entries + mrev_exits
    mrev_pnl = activity.get("total_pnl", 0)
    all_buys = activity.get("all_buys", [])
    all_sells = activity.get("all_sells", [])
    mrev_win_rate = activity.get("win_rate", 0)
    mrev_total_closed = activity.get("total_closed", 0)

    # MREV bot-only equity (no el total de la cuenta)
    mrev_eq = float(result.get("equity", MREV_CAPITAL))
    mrev_ret_pct = (mrev_eq - MREV_CAPITAL) / MREV_CAPITAL * 100 if MREV_CAPITAL else 0
    mrev_ret_abs = mrev_eq - MREV_CAPITAL
    mrev_cash = float(result.get("cash", 0))
    open_pos = result.get("open_positions", []) or []
    per_symbol_ind = result.get("per_symbol_ind", {}) or {}
    current_prices = result.get("current_prices", {}) or {}
    mrev_invested = sum(
        float(p["qty"]) * float(current_prices.get(p["symbol"], p["entry_price"]))
        for p in open_pos
    )

    # ── Subject line — MREV-only ───────────────────────────────────────────────
    subject = f"[MREV Crypto 1h] Resumen 24h — {today} — Bot ${mrev_eq:,.0f} ({mrev_ret_pct:+.2f}%)"
    if mrev_trades > 0:
        subject += f" · {mrev_trades} operaciones"
    if dry_run:
        subject = f"[SIM] {subject}"

    # ── CSS ───────────────────────────────────────────────────────────────────
    css = """<style>
        body { font-family: -apple-system, Helvetica, Arial, sans-serif; background:#f4f4f4; margin:0; padding:16px; color:#222; }
        .wrap { max-width:600px; margin:0 auto; }
        .hero { background:linear-gradient(135deg, #16213e 0%, #1a1a2e 100%); color:#fff; border-radius:14px; padding:28px; margin-bottom:14px; }
        .hero h1 { margin:0 0 2px; font-size:18px; color:#fff; font-weight:600; }
        .subtitle { color:#8be9fd; font-size:13px; margin-bottom:2px; }
        .date { color:#aaa; font-size:12px; margin-bottom:20px; }
        .big { font-size:36px; font-weight:800; margin:4px 0 0; }
        .lbl { font-size:12px; color:#aaa; margin-bottom:10px; }
        .verdict { font-size:14px; margin:14px 0 0; padding:12px 16px; border-radius:10px; line-height:1.5; }
        .verdict-good { background:rgba(27,158,75,.15); color:#1b9e4b; }
        .verdict-bad { background:rgba(214,48,49,.15); color:#d63031; }
        .verdict-neutral { background:rgba(255,255,255,.08); color:#ccc; }
        .row3 { display:flex; gap:10px; margin-top:14px; }
        .col3 { flex:1; background:rgba(255,255,255,.08); border-radius:8px; padding:10px 10px; text-align:center; }
        .col3 .val { font-size:17px; font-weight:700; color:#fff; }
        .col3 .lbl { font-size:10px; color:#aaa; }
        .row4 { display:flex; gap:8px; margin-top:14px; }
        .col4 { flex:1; background:rgba(255,255,255,.08); border-radius:8px; padding:10px 8px; text-align:center; }
        .col4 .val { font-size:16px; font-weight:700; color:#fff; }
        .col4 .lbl { font-size:10px; color:#aaa; }
        .section { background:#fff; border-radius:12px; padding:18px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
        .section h2 { margin:0 0 12px; font-size:15px; color:#333; }
        .bot-tag { display:inline-block; padding:3px 10px; border-radius:6px; font-size:11px; font-weight:700; margin-bottom:10px; }
        .bot-mrev { background:#1a1a2e; color:#8be9fd; }
        .bot-daily { background:#1a1a2e; color:#f8b500; }
        .explain { color:#666; font-size:12px; margin-bottom:12px; line-height:1.6; }
        .green { color:#1b9e4b; } .red { color:#d63031; } .orange { color:#e67e22; } .muted { color:#999; }
        .pill { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:700; }
        .pill-buy { background:#d4edda; color:#155724; }
        .pill-sell { background:#f8d7da; color:#721c24; }
        .pill-win { background:#d4edda; color:#155724; }
        .pill-loss { background:#f8d7da; color:#721c24; }
        .pill-noop { background:#e9ecef; color:#555; }
        .pill-hold { background:#fff3cd; color:#856404; }
        .item { border-bottom:1px solid #f0f0f0; padding:10px 0; }
        .item:last-child { border-bottom:none; }
        .sym { font-size:15px; font-weight:700; }
        .detail { color:#555; font-size:12px; line-height:1.7; margin-top:3px; }
        .time-tag { color:#999; font-size:11px; margin-left:6px; }
        .kpi-grid { display:flex; gap:10px; margin-top:10px; }
        .kpi { flex:1; background:#f8f9fa; border-radius:10px; padding:12px; text-align:center; }
        .kpi .val { font-size:18px; font-weight:700; }
        .kpi .lbl { font-size:11px; color:#888; margin-top:2px; }
        .divider { border:none; border-top:2px solid #e9ecef; margin:4px 0; }
        .foot { text-align:center; color:#bbb; font-size:11px; padding:10px 0 0; }
    </style>"""

    # ── Hero section (MREV-only) ─────────────────────────────────────────────
    ret_color = "green" if mrev_ret_abs >= 0 else "red"

    if mrev_ret_abs > 0:
        verdict_class = "verdict-good"
        verdict_text = f"El bot MREV tiene ganancia acumulada de <b>${mrev_ret_abs:+,.2f}</b> ({mrev_ret_pct:+.2f}%) desde los ${MREV_CAPITAL:,.0f} iniciales."
    elif mrev_ret_abs < 0:
        verdict_class = "verdict-bad"
        verdict_text = f"El bot MREV tiene pérdida acumulada de <b>${mrev_ret_abs:+,.2f}</b> ({mrev_ret_pct:+.2f}%) desde los ${MREV_CAPITAL:,.0f} iniciales."
    else:
        verdict_class = "verdict-neutral"
        verdict_text = f"El bot MREV está igual que cuando empezó (${MREV_CAPITAL:,.0f})."

    hero = f"""<div class="hero">
        <h1>BOT MREV (Crypto 1h) — Resumen diario de las ultimas 24hs</h1>
        <div class="subtitle">{today_full} {'· SIMULACION' if dry_run else ''} · Este mail se envia 1 vez por dia a las 09:00 ARG</div>
        <div class="big">${mrev_eq:,.2f}</div>
        <div class="lbl">Capital del bot MREV · Inicio: ${MREV_CAPITAL:,.0f} · Retorno: <span class="{ret_color}">{mrev_ret_pct:+.2f}%</span></div>
        <div class="row4">
            <div class="col4"><div class="val {ret_color}">${mrev_ret_abs:+,.2f}</div><div class="lbl">Ganancia/Pérdida total</div></div>
            <div class="col4"><div class="val">${mrev_cash:,.2f}</div><div class="lbl">Efectivo MREV</div></div>
            <div class="col4"><div class="val">${mrev_invested:,.2f}</div><div class="lbl">Invertido</div></div>
            <div class="col4"><div class="val">{len(open_pos)}</div><div class="lbl">Posiciones</div></div>
        </div>
        <div class="verdict {verdict_class}">{verdict_text}</div>
    </div>"""

    # ── MREV section header + KPIs ──────────────────────────────────────────
    mrev_header = f"""<div class="section" style="background:#f0f7ff; border-left:3px solid #8be9fd;">
        <span class="bot-tag bot-mrev">BOT MREV · Crypto 1h</span>
        <div class="explain">Detalle de las últimas 24 horas del bot MREV (cripto 24/7).</div>"""

    mrev_kpi_html = ""
    if mrev_total_closed > 0:
        mrev_chg_color = "green" if mrev_pnl >= 0 else "red"
        mrev_kpi_html = f"""
            <div class="kpi-grid">
                <div class="kpi"><div class="val {mrev_chg_color}">${mrev_pnl:+,.2f}</div><div class="lbl">P&L del día</div></div>
                <div class="kpi"><div class="val">{mrev_total_closed}</div><div class="lbl">Trades cerrados</div></div>
                <div class="kpi"><div class="val">{mrev_win_rate}%</div><div class="lbl">Efectividad</div></div>
            </div>"""
    elif mrev_trades == 0:
        mrev_kpi_html = """<div style="padding:8px 0;"><span class="pill pill-noop">Sin operaciones hoy</span></div>"""

    mrev_header += mrev_kpi_html + "</div>"

    # ── MREV Buys section ────────────────────────────────────────────────────
    buys_html = ""
    if all_buys:
        items = ""
        for b in all_buys:
            ts = b.get("time", "")
            try:
                ts_short = datetime.fromisoformat(str(ts)).strftime("%H:%M")
            except Exception:
                ts_short = str(ts)[:5] if ts else ""
            notional = float(b.get('notional', 0))
            items += f"""<div class="item">
                <span class="pill pill-buy">COMPRA</span>
                <span class="sym"> {b.get('symbol', '?')}</span>
                <span class="time-tag">{ts_short} UTC</span>
                <div class="detail">
                    Invertido: ${notional:,.2f} · Precio: ${float(b.get('price', 0)):,.2f}<br>
                    Protección: ${float(b.get('stop', 0)):,.2f} · RSI: {b.get('rsi', '?')}
                </div>
            </div>"""
        buys_html = f"""<div class="section">
            <h2>MREV — Compras hoy ({len(all_buys)})</h2>
            {items}
        </div>"""

    # ── MREV Sells section ───────────────────────────────────────────────────
    sells_html = ""
    if all_sells:
        items = ""
        for s in all_sells:
            pnl = float(s.get("pnl", 0))
            pnl_color = "green" if pnl >= 0 else "red"
            ts = s.get("time", "")
            try:
                ts_short = datetime.fromisoformat(str(ts)).strftime("%H:%M")
            except Exception:
                ts_short = str(ts)[:5] if ts else ""
            reason_raw = s.get('reason', '?')
            reason_map = {"stop_loss": "Protección activada", "time_stop": "Tiempo máximo (24h)", "target": "Objetivo alcanzado", "signal": "Señal de salida"}
            reason_text = reason_map.get(reason_raw, reason_raw)
            items += f"""<div class="item">
                <span class="pill {'pill-win' if pnl >= 0 else 'pill-loss'}">{'GANANCIA' if pnl >= 0 else 'PÉRDIDA'}</span>
                <span class="sym"> {s.get('symbol', '?')}</span>
                <span class="time-tag">{ts_short} UTC</span>
                <div class="detail">
                    ${float(s.get('entry_price', 0)):,.2f} → ${float(s.get('exit_price', 0)):,.2f} ·
                    <span class="{pnl_color}"><b>${pnl:+,.2f}</b></span> · {reason_text}
                </div>
            </div>"""
        sells_html = f"""<div class="section">
            <h2>MREV — Ventas hoy ({len(all_sells)})</h2>
            {items}
        </div>"""

    # ── MREV Open positions — Lo que tengo en cartera (espejo de RFTM) ──────
    # Para cada posición: cuadrados SL / Precio actual / TP dinámico (SMA20+1.5×ATR)
    # + línea "Stage X · próximo: TPY a $Z (faltan W%)".
    mrev_pos_html = ""
    if open_pos:
        items = ""
        for p in open_pos:
            sym = p["symbol"]
            entry_price = float(p["entry_price"])
            stop_price = float(p["stop_loss"] or 0)
            qty_p = float(p["qty"])
            ind = per_symbol_ind.get(sym, {})
            curr = float(current_prices.get(sym) or ind.get("close") or entry_price)
            sma20 = float(ind.get("sma_20") or 0)
            atr14 = float(ind.get("atr_14") or 0)
            # TP dinámico MREV: SMA20 + 1.5 × ATR (idéntico a check_exit X1)
            if sma20 > 0 and atr14 > 0:
                tp_dyn = round(sma20 + 1.5 * atr14, 4)
            else:
                tp_dyn = round(entry_price * 1.05, 4)  # fallback

            upnl = (curr - entry_price) * qty_p
            pnl_cls = "green" if upnl >= 0 else "red"
            pnl_word = "Ganando" if upnl >= 0 else "Perdiendo"
            change_pct = ((curr - entry_price) / entry_price * 100) if entry_price else 0
            dist_to_sl = ((curr - stop_price) / curr * 100) if curr else 0
            dist_to_tp = ((tp_dyn - curr) / curr * 100) if curr else 0

            # Feature 3 en MREV: distancia al próximo stage
            try:
                stage = int(p.get("partial_tp_taken") or 0)
            except Exception:
                stage = 0
            if stage == 0:
                next_target = round(entry_price * (1.0 + MREV_TP1_PCT), 4)
                next_label = f"TP1 a <b>${next_target:,.2f}</b>"
            elif stage == 1:
                next_target = round(entry_price * (1.0 + MREV_TP2_PCT), 4)
                next_label = f"TP2 a <b>${next_target:,.2f}</b>"
            else:
                next_target = tp_dyn
                next_label = f"TP final (SMA20+1.5×ATR) a <b>${next_target:,.2f}</b>"
            if curr > 0:
                delta_next = (next_target - curr) / curr * 100
            else:
                delta_next = 0.0
            if delta_next < 0:
                next_dist_txt = "ya superado — dispara en la próxima corrida"
            else:
                next_dist_txt = f"faltan <b>{delta_next:.1f}%</b>"
            stage_line = (
                f'<div style="color:#999;font-size:11px;margin-top:6px;">'
                f'Stage {stage} · próximo: {next_label} ({next_dist_txt})'
                f'</div>'
            )

            items += f"""
            <div class="item">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="sym">{sym}</span>
                    <span class="{pnl_cls}" style="font-size:18px;font-weight:700;">${upnl:+,.2f}</span>
                </div>
                <div class="detail">
                    {qty_p:g} · Compré a ${entry_price:,.2f} · Ahora a ${curr:,.2f}
                    (<span class="{pnl_cls}">{change_pct:+.1f}%</span>)<br>
                    {pnl_word} ${abs(upnl):,.2f}
                </div>
                <div style="display:flex;gap:8px;margin-top:8px;">
                    <div style="flex:1;background:#fdecea;border-radius:8px;padding:8px 10px;text-align:center;font-size:12px;">
                        <div style="font-size:15px;font-weight:700;color:#d63031;">${stop_price:,.2f}</div>
                        Stop Loss<br><span style="font-size:11px;">a {dist_to_sl:.1f}% de distancia</span>
                    </div>
                    <div style="flex:1;background:#e8f4fd;border-radius:8px;padding:8px 10px;text-align:center;font-size:12px;">
                        <div style="font-size:15px;font-weight:700;color:#2980b9;">${curr:,.2f}</div>
                        Precio actual
                    </div>
                    <div style="flex:1;background:#e8f5e9;border-radius:8px;padding:8px 10px;text-align:center;font-size:12px;">
                        <div style="font-size:15px;font-weight:700;color:#1b9e4b;">${tp_dyn:,.2f}</div>
                        Take Profit<br><span style="font-size:11px;">a {dist_to_tp:.1f}% de distancia</span>
                    </div>
                </div>
                {stage_line}
            </div>"""
        mrev_pos_html = f"""<div class="section">
            <h2>Lo que tengo en cartera ({len(open_pos)})</h2>
            {items}
        </div>"""

    # ── Footer ───────────────────────────────────────────────────────────────
    email_hour_utc = sorted(EMAIL_HOURS_UTC)[0]
    email_hour_arg = (email_hour_utc - 3) % 24

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{css}</head>
<body><div class="wrap">
    {hero}
    {mrev_header}
    {mrev_pos_html}
    {buys_html}
    {sells_html}
    <div class="foot">BOT MREV (Crypto 1h) · Resumen diario a las {email_hour_arg:02d}:00 ARG · Se envia 1 sola vez por dia · El bot escanea cada hora pero solo envia este mail una vez.</div>
</div></body></html>"""

    return subject, body


def send_email_report(result: dict) -> None:
    """Send the MREV trading report via email."""
    if not EMAIL_ENABLED:
        info("Email disabled (EMAIL_ENABLED=false)")
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        warn("Email not configured — set EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO")
        return

    try:
        subject, body = _build_email_report(result)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        ok(f"Email enviado a {EMAIL_TO}")
    except Exception as e:
        err(f"Error enviando email: {e}")


# ── Immediate TP/E7 notification (Feature 2) ─────────────────────────────────
# send_stage_event_email lives in _email_helpers.py (shared with RFTM).


# ══════════════════════════════════════════════════════════════════════════════
#  STATUS COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def show_status():
    """Show current MREV portfolio status."""
    conn = get_db()
    run_row = conn.execute("SELECT * FROM mrev_runs WHERE status='RUNNING' LIMIT 1").fetchone()
    if not run_row:
        warn("No active MREV run. Start one with: python3 standalone_mrev_trader.py")
        return

    run_id = run_row["id"]
    open_pos = get_open_positions(conn, run_id)
    cash = get_cash(conn, run_id)

    closed = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='CLOSED' ORDER BY exit_dt DESC LIMIT 10",
        (run_id,)
    ).fetchall()

    hdr(f"MREV-1H Status — Run {run_id}")
    info(f"Started: {run_row['started_at']}")
    info(f"Initial Capital: ${float(run_row['initial_capital']):,.2f}")
    info(f"Cash: ${cash:,.2f}")
    info(f"Open Positions: {len(open_pos)}")

    if open_pos:
        print()
        for p in open_pos:
            print(f"  {p['symbol']:10s}  qty={p['qty']}  entry=${float(p['entry_price']):,.2f}  stop=${float(p['stop_loss']):,.2f}  since {p['entry_dt'][:16]}")

    if closed:
        print(f"\n  Last {len(closed)} closed trades:")
        for c in closed:
            pnl = float(c["pnl"] or 0)
            icon = C.GREEN if pnl >= 0 else C.RED
            print(f"  {icon}{c['symbol']:10s}{C.RESET}  ${pnl:+.2f}  ({c['exit_reason']})")

    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM mrev_positions WHERE run_id=? AND status='CLOSED'",
        (run_id,)
    ).fetchone()[0]
    total_trades = conn.execute(
        "SELECT COUNT(*) FROM mrev_positions WHERE run_id=? AND status='CLOSED'",
        (run_id,)
    ).fetchone()[0]
    wins = conn.execute(
        "SELECT COUNT(*) FROM mrev_positions WHERE run_id=? AND status='CLOSED' AND pnl > 0",
        (run_id,)
    ).fetchone()[0]

    print(f"\n  Total P&L: ${float(total_pnl):+,.2f}  |  Trades: {total_trades}  |  Win rate: {wins}/{total_trades}")
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MREV-1H Mean Reversion Paper Trader")
    parser.add_argument("--dry-run", action="store_true", help="Scan signals only, no orders")
    parser.add_argument("--status", action="store_true", help="Show portfolio status")
    parser.add_argument("--reset", action="store_true", help="Wipe DB and start fresh")
    args = parser.parse_args()

    print(f"\n{C.BOLD}{'═'*56}{C.RESET}")
    print(f"{C.BOLD}  MREV-1H Mean Reversion Bot{C.RESET}")
    print(f"{C.BOLD}{'═'*56}{C.RESET}")

    if args.status:
        show_status()
        return

    if args.reset:
        if DB_PATH.exists():
            DB_PATH.unlink()
            ok("Database wiped. Fresh start.")
        return

    result = run_pipeline(dry_run=args.dry_run)
    return result


if __name__ == "__main__":
    main()
