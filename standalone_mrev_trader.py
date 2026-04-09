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

# ── Universe ─────────────────────────────────────────────────────────────────
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LINK/USD"]
ETF_SYMBOLS    = ["SPY", "QQQ", "IWM", "XLE", "XLF", "GLD", "SLV", "BITO", "ARKK"]
ALL_SYMBOLS    = CRYPTO_SYMBOLS + ETF_SYMBOLS

CRYPTO_MIN_QTY = {
    "BTC/USD": 0.0001, "ETH/USD": 0.001, "SOL/USD": 0.01,
    "AVAX/USD": 0.01, "DOGE/USD": 1.0, "LINK/USD": 0.01,
}

# Account-level config (for unified reporting across both bots)
ACCOUNT_TOTAL_CAPITAL = float(os.environ.get("ACCOUNT_TOTAL_CAPITAL", "100000"))
DAILY_BOT_CAPITAL     = float(os.environ.get("DAILY_BOT_CAPITAL", "75000"))

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


def get_open_positions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='OPEN'", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


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
    """Check if we should send an email now. Returns the email window label if yes, None if no.

    Because GitHub Actions cron can be delayed or skip hours, we use a
    window-based approach: each email hour 'owns' the next few hours until
    the following email hour.  e.g. with EMAIL_HOURS_UTC=[4,12,20]:
      - hour 4 owns 4,5,6,7,8,9,10,11
      - hour 12 owns 12,13,14,15,16,17,18,19
      - hour 20 owns 20,21,22,23,0,1,2,3

    The first run in each window sends the email.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    sorted_hours = sorted(EMAIL_HOURS_UTC)
    current_hour = now.hour

    # Find which email window the current hour belongs to
    owning_hour = None
    for i, eh in enumerate(sorted_hours):
        next_eh = sorted_hours[(i + 1) % len(sorted_hours)]
        if next_eh <= eh:  # wraps around midnight
            if current_hour >= eh or current_hour < next_eh:
                owning_hour = eh
                break
        else:
            if eh <= current_hour < next_eh:
                owning_hour = eh
                break

    if owning_hour is None:
        owning_hour = sorted_hours[-1]  # fallback

    return f"{now.strftime('%Y-%m-%d')}_{owning_hour:02d}"


def should_send_email(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> bool:
    """Check if an email should be sent now (window-based, deduplicated via DB)."""
    if now is None:
        now = datetime.now(tz=timezone.utc)

    window = get_email_window(now)
    if window is None:
        return False

    # Check if we already sent an email for this window
    already = conn.execute(
        "SELECT id FROM mrev_email_log WHERE run_id=? AND email_window=?",
        (run_id, window)
    ).fetchone()

    return already is None


def record_email_sent(conn: sqlite3.Connection, run_id: str, now: datetime | None = None) -> None:
    """Record that an email was sent for the current window."""
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
    already = conn.execute(
        "SELECT id FROM mrev_email_log WHERE run_id=? AND email_window=?",
        (run_id, window)
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


def _build_monthly_email_report(result: dict, monthly_data: dict) -> tuple[str, str]:
    """Build subject + HTML body for the unified monthly summary email (both bots)."""
    now = datetime.now(tz=timezone.utc)
    prev_month = (now.replace(day=1) - timedelta(days=1))
    month_name_map = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
                      7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
    month_name = month_name_map.get(prev_month.month, str(prev_month.month))
    year = prev_month.year
    dry_run = result.get("dry_run", False)
    acct = result.get("account_overview", {})
    acct_available = acct.get("available", False)

    # MREV data
    mrev_return = monthly_data.get("total_return", 0)
    mrev_return_pct = monthly_data.get("total_return_pct", 0)
    mrev_pnl_month = monthly_data.get("pnl_last_month", 0)
    mrev_initial = monthly_data.get("initial_capital", MREV_CAPITAL)
    mrev_equity = monthly_data.get("equity_now", MREV_CAPITAL)
    days_running = monthly_data.get("days_running", 0)
    mrev_closed_all = monthly_data.get("total_closed_all", 0)
    mrev_win_rate_all = monthly_data.get("win_rate_all", 0)
    mrev_closed_month = monthly_data.get("total_closed_month", 0)
    mrev_win_rate_month = monthly_data.get("win_rate_month", 0)
    monthly_returns = monthly_data.get("monthly_returns", [])
    best_trade = monthly_data.get("best_trade")
    worst_trade = monthly_data.get("worst_trade")

    # Account-level data
    acct_equity = acct.get("equity", ACCOUNT_TOTAL_CAPITAL) if acct_available else ACCOUNT_TOTAL_CAPITAL
    acct_return = acct.get("total_return", 0) if acct_available else 0
    acct_return_pct = acct.get("total_return_pct", 0) if acct_available else 0

    # Daily bot estimated equity
    daily_eq_est = acct_equity - mrev_equity if acct_available else DAILY_BOT_CAPITAL
    daily_return_est = round(daily_eq_est - DAILY_BOT_CAPITAL, 2)
    daily_return_pct_est = round(daily_return_est / DAILY_BOT_CAPITAL * 100, 2) if DAILY_BOT_CAPITAL > 0 else 0

    # Comparison with previous month
    prev_month_data = monthly_returns[-1] if monthly_returns else None
    prev_prev_data = monthly_returns[-2] if len(monthly_returns) >= 2 else None
    comparison_text = ""
    if prev_month_data and prev_prev_data:
        prev_name = month_name_map.get(int(prev_prev_data["month"][5:7]), "?")
        if mrev_pnl_month > prev_prev_data["change"]:
            comparison_text = f"Mejor que {prev_name} (${prev_prev_data['change']:+,.2f})."
        elif mrev_pnl_month < prev_prev_data["change"]:
            comparison_text = f"Peor que {prev_name} (${prev_prev_data['change']:+,.2f})."
        else:
            comparison_text = f"Igual que {prev_name}."

    # Subject (account-level)
    subject = f"Reporte Mensual — {month_name} {year}: Cuenta ${acct_equity:,.0f} ({acct_return_pct:+.2f}%)"
    if dry_run:
        subject = f"[SIM] {subject}"

    css = """<style>
        body { font-family: -apple-system, Helvetica, Arial, sans-serif; background:#f4f4f4; margin:0; padding:16px; color:#222; }
        .wrap { max-width:600px; margin:0 auto; }
        .hero { background:linear-gradient(135deg, #0f3460 0%, #16213e 100%); color:#fff; border-radius:14px; padding:28px; margin-bottom:14px; }
        .hero h1 { margin:0 0 4px; font-size:19px; color:#fff; font-weight:600; }
        .subtitle { color:#8be9fd; font-size:13px; margin-bottom:16px; }
        .big { font-size:38px; font-weight:800; margin:4px 0 2px; }
        .lbl { font-size:12px; color:#aaa; margin-bottom:10px; }
        .verdict { font-size:14px; margin:16px 0 0; padding:14px 16px; border-radius:10px; line-height:1.6; }
        .verdict-good { background:rgba(27,158,75,.15); color:#1b9e4b; }
        .verdict-bad { background:rgba(214,48,49,.15); color:#d63031; }
        .verdict-neutral { background:rgba(255,255,255,.08); color:#ccc; }
        .row3 { display:flex; gap:10px; margin-top:14px; }
        .col3 { flex:1; background:rgba(255,255,255,.08); border-radius:8px; padding:12px 10px; text-align:center; }
        .col3 .val { font-size:17px; font-weight:700; color:#fff; }
        .col3 .lbl { font-size:10px; color:#aaa; }
        .section { background:#fff; border-radius:12px; padding:18px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
        .section h2 { margin:0 0 12px; font-size:15px; color:#333; }
        .bot-tag { display:inline-block; padding:3px 10px; border-radius:6px; font-size:11px; font-weight:700; margin-bottom:8px; }
        .bot-mrev { background:#1a1a2e; color:#8be9fd; }
        .bot-daily { background:#1a1a2e; color:#f8b500; }
        .explain { color:#666; font-size:12px; margin-bottom:12px; line-height:1.6; }
        .green { color:#1b9e4b; } .red { color:#d63031; } .muted { color:#999; }
        .kpi-grid { display:flex; gap:10px; margin-top:10px; }
        .kpi { flex:1; background:#f8f9fa; border-radius:10px; padding:12px; text-align:center; }
        .kpi .val { font-size:18px; font-weight:700; }
        .kpi .lbl { font-size:11px; color:#888; margin-top:2px; }
        .month-row { display:flex; justify-content:space-between; padding:8px 12px; border-bottom:1px solid #f0f0f0; align-items:center; }
        .month-row:last-child { border-bottom:none; }
        .bar-container { width:100px; height:14px; background:#f0f0f0; border-radius:7px; overflow:hidden; display:inline-block; vertical-align:middle; margin-left:8px; }
        .bar-fill { height:100%; border-radius:7px; }
        .highlight-box { background:#f8f9fa; border-radius:10px; padding:14px; margin-top:10px; }
        .highlight-box .sym { font-size:14px; font-weight:700; }
        .highlight-box .detail { color:#555; font-size:12px; line-height:1.6; margin-top:4px; }
        .vs-box { display:flex; gap:12px; margin-top:12px; }
        .vs-card { flex:1; border-radius:10px; padding:14px; text-align:center; }
        .vs-card .val { font-size:20px; font-weight:700; }
        .vs-card .lbl { font-size:11px; color:#888; margin-top:4px; }
        .foot { text-align:center; color:#bbb; font-size:11px; padding:10px 0 0; }
    </style>"""

    # ── Hero (ACCOUNT-LEVEL) ─────────────────────────────────────────────────
    ret_color = "green" if acct_return >= 0 else "red"

    try:
        start_str = datetime.fromisoformat(monthly_data["started_at"]).strftime("%d/%m/%Y")
    except Exception:
        start_str = "?"

    # Account verdict
    if acct_return > 0:
        verdict_class = "verdict-good"
        verdict_text = f"Desde que empezaste el {start_str} ({days_running} días), tu cuenta creció <b>${acct_return:+,.2f}</b> (<b>{acct_return_pct:+.2f}%</b>). Empezaste con ${ACCOUNT_TOTAL_CAPITAL:,.0f}."
    elif acct_return < 0:
        verdict_class = "verdict-bad"
        verdict_text = f"Desde que empezaste el {start_str} ({days_running} días), tu cuenta tiene una pérdida de <b>${acct_return:+,.2f}</b> (<b>{acct_return_pct:+.2f}%</b>). Lo importante es la tendencia a largo plazo."
    else:
        verdict_class = "verdict-neutral"
        verdict_text = f"Tu cuenta está igual que al inicio. Operando desde el {start_str} ({days_running} días)."

    hero = f"""<div class="hero">
        <h1>Reporte Mensual — Tu Cuenta de Trading</h1>
        <div class="subtitle">{month_name} {year} · Operando desde {start_str}</div>
        <div class="big">${acct_equity:,.2f}</div>
        <div class="lbl">Capital total · Inicio: ${ACCOUNT_TOTAL_CAPITAL:,.0f} · Retorno: <span class="{ret_color}">{acct_return_pct:+.2f}%</span></div>
        <div class="row3">
            <div class="col3"><div class="val {ret_color}">${acct_return:+,.2f}</div><div class="lbl">Ganancia/Pérdida total</div></div>
            <div class="col3"><div class="val {ret_color}">{acct_return_pct:+.2f}%</div><div class="lbl">Rendimiento total</div></div>
            <div class="col3"><div class="val">{days_running}</div><div class="lbl">Días operando</div></div>
        </div>
        <div class="verdict {verdict_class}">{verdict_text}</div>
    </div>"""

    # ── Bots comparison section ──────────────────────────────────────────────
    mrev_color = "green" if mrev_return >= 0 else "red"
    daily_color = "green" if daily_return_est >= 0 else "red"
    mrev_month_color = "green" if mrev_pnl_month >= 0 else "red"

    bots_html = f"""<div class="section">
        <h2>Rendimiento por robot</h2>
        <div class="explain">Cómo le fue a cada bot este mes y en total desde que arrancó.</div>
        <div class="vs-box">
            <div class="vs-card" style="border-left:3px solid #8be9fd; background:#f0f7ff;">
                <div class="lbl" style="color:#8be9fd; font-weight:700;">MREV · Crypto 1h</div>
                <div class="val {mrev_color}">${mrev_equity:,.2f}</div>
                <div class="lbl">Capital (inicio: ${mrev_initial:,.0f})</div>
                <div class="val {mrev_color}" style="font-size:14px; margin-top:8px;">{mrev_return_pct:+.2f}%</div>
                <div class="lbl">Rendimiento total</div>
                <div class="val {mrev_month_color}" style="font-size:14px; margin-top:8px;">${mrev_pnl_month:+,.2f}</div>
                <div class="lbl">P&L {month_name}</div>
                <div class="lbl" style="margin-top:4px;">{mrev_closed_month} trades · {mrev_win_rate_month}% efectividad</div>
            </div>
            <div class="vs-card" style="border-left:3px solid #f8b500; background:#fffbf0;">
                <div class="lbl" style="color:#f8b500; font-weight:700;">Diario · ETFs</div>
                <div class="val {daily_color}">${daily_eq_est:,.2f}</div>
                <div class="lbl">Capital (inicio: ${DAILY_BOT_CAPITAL:,.0f})</div>
                <div class="val {daily_color}" style="font-size:14px; margin-top:8px;">{daily_return_pct_est:+.2f}%</div>
                <div class="lbl">Rendimiento total</div>
                <div class="lbl" style="margin-top:16px; color:#999;">Datos detallados del bot diario<br>disponibles en su propio reporte</div>
            </div>
        </div>
    </div>"""

    # ── Comparison with previous month ───────────────────────────────────────
    comparison_html = ""
    if prev_prev_data:
        prev_name = month_name_map.get(int(prev_prev_data["month"][5:7]), "?")
        prev_change = prev_prev_data["change"]
        prev_pct = prev_prev_data["change_pct"]
        diff = round(mrev_pnl_month - prev_change, 2)
        diff_color = "green" if diff >= 0 else "red"
        diff_word = "más" if diff >= 0 else "menos"

        comparison_html = f"""<div class="section">
            <h2>Comparación: {month_name} vs {prev_name}</h2>
            <div class="explain">El bot MREV comparado con el mes anterior (solo datos del bot MREV, que es el que tiene historial detallado).</div>
            <div class="kpi-grid">
                <div class="kpi">
                    <div class="lbl" style="font-weight:700;">{month_name}</div>
                    <div class="val {mrev_month_color}">${mrev_pnl_month:+,.2f}</div>
                    <div class="lbl">{mrev_closed_month} trades · {mrev_win_rate_month}%</div>
                </div>
                <div class="kpi">
                    <div class="lbl" style="font-weight:700;">{prev_name}</div>
                    <div class="val {'green' if prev_change >= 0 else 'red'}">${prev_change:+,.2f}</div>
                    <div class="lbl">{prev_pct:+.2f}%</div>
                </div>
                <div class="kpi">
                    <div class="lbl" style="font-weight:700;">Diferencia</div>
                    <div class="val {diff_color}">${diff:+,.2f}</div>
                    <div class="lbl">{diff_word} que {prev_name}</div>
                </div>
            </div>
        </div>"""

    # ── MREV KPIs: This month vs All time ────────────────────────────────────
    kpi_html = f"""<div class="section">
        <h2>MREV: Mes vs. acumulado histórico</h2>
        <div class="explain">Rendimiento del bot MREV este mes comparado con sus números totales desde el inicio.</div>
        <div class="kpi-grid">
            <div class="kpi"><div class="val {mrev_month_color}">${mrev_pnl_month:+,.2f}</div><div class="lbl">P&L {month_name}</div></div>
            <div class="kpi"><div class="val">{mrev_closed_month}</div><div class="lbl">Trades del mes</div></div>
            <div class="kpi"><div class="val">{mrev_win_rate_month}%</div><div class="lbl">Efectividad mes</div></div>
        </div>
        <div class="kpi-grid" style="margin-top:8px;">
            <div class="kpi"><div class="val {mrev_color}">${mrev_return:+,.2f}</div><div class="lbl">P&L acumulado</div></div>
            <div class="kpi"><div class="val">{mrev_closed_all}</div><div class="lbl">Trades totales</div></div>
            <div class="kpi"><div class="val">{mrev_win_rate_all}%</div><div class="lbl">Efectividad total</div></div>
        </div>
    </div>"""

    # ── Monthly returns timeline ─────────────────────────────────────────────
    timeline_html = ""
    if monthly_returns:
        rows = ""
        max_abs = max(abs(m["change"]) for m in monthly_returns) if monthly_returns else 1
        for m in monthly_returns:
            color = "green" if m["change"] >= 0 else "red"
            bar_width = min(100, int(abs(m["change"]) / max_abs * 100)) if max_abs > 0 else 0
            bar_color = "#1b9e4b" if m["change"] >= 0 else "#d63031"
            month_label = month_name_map.get(int(m["month"][5:7]), m["month"][5:7])
            rows += f"""<div class="month-row">
                <span><b>{month_label} {m['month'][:4]}</b></span>
                <span>
                    <span class="{color}"><b>${m['change']:+,.2f}</b> ({m['change_pct']:+.2f}%)</span>
                    <div class="bar-container"><div class="bar-fill" style="width:{bar_width}%; background:{bar_color};"></div></div>
                </span>
            </div>"""
        timeline_html = f"""<div class="section">
            <h2>MREV: Evolución mes a mes</h2>
            <div class="explain">Ganancia o pérdida del bot MREV cada mes desde que arrancó.</div>
            {rows}
        </div>"""

    # ── Best / worst trades ──────────────────────────────────────────────────
    highlights_html = ""
    if best_trade or worst_trade:
        items = ""
        if best_trade:
            pnl_b = float(best_trade.get("pnl", 0))
            items += f"""<div class="highlight-box">
                <span class="sym green">Mejor trade: {best_trade.get('symbol', '?')}</span>
                <div class="detail">
                    ${float(best_trade.get('entry_price', 0)):,.2f} → ${float(best_trade.get('exit_price', 0)):,.2f} · Ganancia: <b class="green">${pnl_b:+,.2f}</b>
                </div>
            </div>"""
        if worst_trade:
            pnl_w = float(worst_trade.get("pnl", 0))
            items += f"""<div class="highlight-box" style="margin-top:8px;">
                <span class="sym red">Peor trade: {worst_trade.get('symbol', '?')}</span>
                <div class="detail">
                    ${float(worst_trade.get('entry_price', 0)):,.2f} → ${float(worst_trade.get('exit_price', 0)):,.2f} · Pérdida: <b class="red">${pnl_w:+,.2f}</b>
                </div>
            </div>"""
        highlights_html = f"""<div class="section">
            <h2>MREV: Trades destacados (histórico)</h2>
            {items}
        </div>"""

    # ── Footer ───────────────────────────────────────────────────────────────
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{css}</head>
<body><div class="wrap">
    {hero}
    {bots_html}
    {comparison_html}
    {kpi_html}
    {timeline_html}
    {highlights_html}
    <div class="foot">Reporte mensual (día {EMAIL_MONTHLY_DAY} de cada mes) · Bot Diario (ETFs) + MREV-1H (Crypto)</div>
</div></body></html>"""

    return subject, body


def send_monthly_email_report(result: dict, monthly_data: dict) -> None:
    """Send the monthly MREV trading report via email."""
    if not EMAIL_ENABLED:
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        return

    try:
        subject, body = _build_monthly_email_report(result, monthly_data)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        ok(f"Email MENSUAL enviado a {EMAIL_TO}")
    except Exception as e:
        err(f"Error enviando email mensual: {e}")


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
    if drawdown >= 0.20:
        err(f"KILL SWITCH: drawdown {drawdown:.1%} ≥ 20%! Stopping all trades.")
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
                    available = min(cash, alpaca_buying_power * 0.90)
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
                if ALPACA_API_KEY:
                    order = alpaca_submit_order(s["symbol"], s["qty"], "sell")
                    ok(f"SOLD {s['symbol']}: qty={s['qty']} → order {order.get('id', '?')[:8]}")
                else:
                    ok(f"SOLD {s['symbol']}: qty={s['qty']} (no Alpaca keys, local only)")

                # Update DB
                conn.execute("""UPDATE mrev_positions SET status='CLOSED', exit_price=?,
                    exit_dt=?, pnl=?, exit_reason=? WHERE id=?""",
                    (s["exit_price"], now.isoformat(), s["pnl"], s["reason"], s["position_id"]))
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
                    if bp_now * 0.90 < b["notional"]:
                        warn(f"Skipping {b['symbol']}: buying power ${bp_now:,.2f} < notional ${b['notional']:,.2f}")
                        continue
                    order = alpaca_submit_order(b["symbol"], b["qty"], "buy")
                    ok(f"BOUGHT {b['symbol']}: qty={b['qty']} @ ${b['price']:.2f} → order {order.get('id', '?')[:8]}")
                else:
                    ok(f"BOUGHT {b['symbol']}: qty={b['qty']} @ ${b['price']:.2f} (no Alpaca keys, local only)")

                # Save position
                conn.execute("INSERT INTO mrev_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8], run_id, b["symbol"], b["qty"], b["price"],
                     b["stop"], now.isoformat(), "OPEN", None, None, None, None))

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
        monthly_data = get_all_time_activity(conn, run_id)
        send_monthly_email_report(result, monthly_data)
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
    """Build subject + HTML body for the unified daily summary email (both bots)."""
    now = datetime.now(tz=timezone.utc)
    today = now.strftime("%d/%m/%Y")
    today_full = now.strftime("%d/%m %H:%M UTC")
    dry_run = result.get("dry_run", False)
    activity = result.get("activity_24h", {})
    acct = result.get("account_overview", {})
    acct_available = acct.get("available", False)

    # MREV data from local DB
    mrev_entries = activity.get("total_entries", 0)
    mrev_exits = activity.get("total_exits", 0)
    mrev_trades = mrev_entries + mrev_exits
    mrev_pnl = activity.get("total_pnl", 0)
    mrev_wins = activity.get("wins", 0)
    mrev_losses = activity.get("losses", 0)
    hours_covered = activity.get("hours_covered", 0)
    mrev_eq_change = activity.get("equity_change", 0)
    mrev_eq_change_pct = activity.get("equity_change_pct", 0)
    all_buys = activity.get("all_buys", [])
    all_sells = activity.get("all_sells", [])
    mrev_win_rate = activity.get("win_rate", 0)
    mrev_avg_win = activity.get("avg_win", 0)
    mrev_avg_loss = activity.get("avg_loss", 0)
    mrev_total_closed = activity.get("total_closed", 0)

    # Account-level data from Alpaca
    acct_equity = acct.get("equity", ACCOUNT_TOTAL_CAPITAL) if acct_available else ACCOUNT_TOTAL_CAPITAL
    acct_cash = acct.get("cash", 0) if acct_available else 0
    acct_return = acct.get("total_return", 0) if acct_available else 0
    acct_return_pct = acct.get("total_return_pct", 0) if acct_available else 0
    daily_positions = acct.get("daily_positions", []) if acct_available else []
    mrev_positions_api = acct.get("mrev_positions", []) if acct_available else []
    daily_invested = acct.get("daily_invested", 0) if acct_available else 0
    mrev_invested = acct.get("mrev_invested", 0) if acct_available else 0
    daily_unrealized = acct.get("daily_unrealized", 0) if acct_available else 0
    mrev_unrealized = acct.get("mrev_unrealized", 0) if acct_available else 0

    # ── Subject line (account-level) ─────────────────────────────────────────
    subject = f"Trading Diario {today} — Cuenta ${acct_equity:,.0f} ({acct_return_pct:+.2f}%)"
    if mrev_trades > 0:
        subject += f" · MREV: {mrev_trades} ops"
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

    # ── Hero section (ACCOUNT-LEVEL) ─────────────────────────────────────────
    ret_color = "green" if acct_return >= 0 else "red"
    total_invested = daily_invested + mrev_invested
    total_positions = len(daily_positions) + len(mrev_positions_api)

    # Account verdict
    if not acct_available:
        verdict_class = "verdict-neutral"
        verdict_text = "No se pudo conectar con Alpaca para obtener el estado de la cuenta. Los datos del bot MREV se muestran abajo."
    elif acct_return > 0:
        verdict_class = "verdict-good"
        verdict_text = f"Tu cuenta tiene una ganancia acumulada de <b>${acct_return:+,.2f}</b> ({acct_return_pct:+.2f}%) desde que empezaste con ${ACCOUNT_TOTAL_CAPITAL:,.0f}."
    elif acct_return < 0:
        verdict_class = "verdict-bad"
        verdict_text = f"Tu cuenta tiene una pérdida acumulada de <b>${acct_return:+,.2f}</b> ({acct_return_pct:+.2f}%) desde que empezaste con ${ACCOUNT_TOTAL_CAPITAL:,.0f}."
    else:
        verdict_class = "verdict-neutral"
        verdict_text = f"Tu cuenta está igual que cuando empezaste (${ACCOUNT_TOTAL_CAPITAL:,.0f})."

    hero = f"""<div class="hero">
        <h1>Resumen Diario — Tu Cuenta de Trading</h1>
        <div class="subtitle">{today_full} {'· SIMULACIÓN' if dry_run else ''}</div>
        <div class="big">${acct_equity:,.2f}</div>
        <div class="lbl">Capital total de la cuenta · Inicio: ${ACCOUNT_TOTAL_CAPITAL:,.0f} · Retorno: <span class="{ret_color}">{acct_return_pct:+.2f}%</span></div>
        <div class="row4">
            <div class="col4"><div class="val {ret_color}">${acct_return:+,.2f}</div><div class="lbl">Ganancia/Pérdida total</div></div>
            <div class="col4"><div class="val">${acct_cash:,.2f}</div><div class="lbl">Efectivo disponible</div></div>
            <div class="col4"><div class="val">${total_invested:,.2f}</div><div class="lbl">Invertido</div></div>
            <div class="col4"><div class="val">{total_positions}</div><div class="lbl">Posiciones</div></div>
        </div>
        <div class="verdict {verdict_class}">{verdict_text}</div>
    </div>"""

    # ── Bots breakdown section ───────────────────────────────────────────────
    mrev_eq = result.get("equity", MREV_CAPITAL)
    mrev_ret = result.get("return_pct", 0)
    daily_eq_est = acct_equity - mrev_eq if acct_available else DAILY_BOT_CAPITAL
    daily_ret_est = round((daily_eq_est - DAILY_BOT_CAPITAL) / DAILY_BOT_CAPITAL * 100, 2) if DAILY_BOT_CAPITAL > 0 else 0
    mrev_color = "green" if mrev_ret >= 0 else "red"
    daily_color = "green" if daily_ret_est >= 0 else "red"

    bots_html = f"""<div class="section">
        <h2>Tus 2 robots</h2>
        <div class="explain">Así se reparte tu capital entre los dos bots que operan en tu cuenta.</div>
        <div class="kpi-grid">
            <div class="kpi" style="border-left:3px solid #8be9fd;">
                <div class="lbl" style="color:#8be9fd; font-weight:700;">MREV · Crypto 1h</div>
                <div class="val">${mrev_eq:,.2f}</div>
                <div class="lbl">Capital asignado: ${MREV_CAPITAL:,.0f} · <span class="{mrev_color}">{mrev_ret:+.2f}%</span></div>
                <div class="lbl">{len(mrev_positions_api)} posiciones · ${mrev_invested:,.2f} invertido</div>
            </div>
            <div class="kpi" style="border-left:3px solid #f8b500;">
                <div class="lbl" style="color:#f8b500; font-weight:700;">Diario · ETFs</div>
                <div class="val">${daily_eq_est:,.2f}</div>
                <div class="lbl">Capital asignado: ${DAILY_BOT_CAPITAL:,.0f} · <span class="{daily_color}">{daily_ret_est:+.2f}%</span></div>
                <div class="lbl">{len(daily_positions)} posiciones · ${daily_invested:,.2f} invertido</div>
            </div>
        </div>
    </div>"""

    # ── Daily bot positions (from Alpaca API) ────────────────────────────────
    daily_pos_html = ""
    if daily_positions:
        items = ""
        for p in daily_positions:
            pnl = p.get("unrealized_pnl", 0)
            pnl_color = "green" if pnl >= 0 else "red"
            pnl_pct = p.get("unrealized_pnl_pct", 0)
            items += f"""<div class="item">
                <span class="pill pill-hold">EN CARTERA</span>
                <span class="sym"> {p['symbol']}</span>
                <div class="detail">
                    Comprado a ${p['entry_price']:,.2f} · Ahora ${p['current_price']:,.2f} · Valor: ${p['market_value']:,.2f}<br>
                    <span class="{pnl_color}">P&L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)</span>
                </div>
            </div>"""
        daily_pos_html = f"""<div class="section">
            <span class="bot-tag bot-daily">BOT DIARIO · ETFs</span>
            <h2>Posiciones abiertas ({len(daily_positions)})</h2>
            <div class="explain">Posiciones del bot diario que opera ETFs. Ganancia/pérdida no realizada (aún no vendió).</div>
            {items}
        </div>"""

    # ── MREV section header ──────────────────────────────────────────────────
    mrev_header = f"""<div class="section" style="background:#f0f7ff; border-left:3px solid #8be9fd;">
        <span class="bot-tag bot-mrev">BOT MREV · Crypto 1h</span>
        <div class="explain">Detalle de las últimas 24 horas del bot que opera criptomonedas cada hora.</div>"""

    # ── MREV KPI section ─────────────────────────────────────────────────────
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

    # ── MREV Open positions ──────────────────────────────────────────────────
    mrev_pos_html = ""
    open_pos = result.get("open_positions", [])
    if open_pos:
        items = ""
        for p in open_pos:
            bars_held = int(p.get("bars_held", 0))
            bars_pct = min(100, int(bars_held / 24 * 100))
            entry_price = float(p['entry_price'])
            stop_price = float(p['stop_loss'])
            risk_pct = round((entry_price - stop_price) / entry_price * 100, 1) if entry_price > 0 else 0
            items += f"""<div class="item">
                <span class="pill pill-hold">EN CARTERA</span>
                <span class="sym"> {p['symbol']}</span>
                <div class="detail">
                    Comprado a ${entry_price:,.2f} · Protección: ${stop_price:,.2f} (riesgo: {risk_pct}%)<br>
                    Tiempo: {bars_held}h de 24h ({bars_pct}%)
                </div>
            </div>"""
        mrev_pos_html = f"""<div class="section">
            <h2>MREV — Posiciones abiertas ({len(open_pos)})</h2>
            {items}
        </div>"""

    # ── Footer ───────────────────────────────────────────────────────────────
    email_hour_utc = sorted(EMAIL_HOURS_UTC)[0]
    email_hour_arg = (email_hour_utc - 3) % 24

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{css}</head>
<body><div class="wrap">
    {hero}
    {bots_html}
    {daily_pos_html}
    {mrev_header}
    {buys_html}
    {sells_html}
    {mrev_pos_html}
    <div class="foot">Resumen diario a las {email_hour_arg:02d}:00 ARG ({email_hour_utc:02d}:00 UTC) · Bot Diario (ETFs) + MREV-1H (Crypto)</div>
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
