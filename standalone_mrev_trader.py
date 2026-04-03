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

# Email schedule: 3 emails per day, each covering 8 hours.
# Default: 09:00, 17:00, 01:00 Argentina time (UTC-3) = 12, 20, 04 UTC
# The bot runs every hour but only sends email at these UTC hours.
EMAIL_HOURS_UTC   = [int(h) for h in os.environ.get("EMAIL_HOURS_UTC", "12,20,4").split(",")]

# MREV-specific config
MREV_CAPITAL      = float(os.environ.get("MREV_INITIAL_CAPITAL", "1000"))
MREV_MAX_POSITIONS = int(os.environ.get("MREV_MAX_POSITIONS", "4"))
MREV_RISK_PER_TRADE = float(os.environ.get("MREV_RISK_PER_TRADE", "0.02"))

# ── Universe ─────────────────────────────────────────────────────────────────
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
ETF_SYMBOLS    = ["SPY", "QQQ", "IWM"]
ALL_SYMBOLS    = CRYPTO_SYMBOLS + ETF_SYMBOLS

CRYPTO_MIN_QTY = {"BTC/USD": 0.0001, "ETH/USD": 0.001, "SOL/USD": 0.01}

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
    df["bb_lower"] = sma_20 - 2.0 * std_20

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

    if rsi > 30.0:
        return False, f"rsi={rsi:.1f} (need ≤30)"
    if close > bb_lower:
        return False, f"close={close:.2f} > bb_lower={bb_lower:.2f}"
    if vol_ma > 0 and volume < vol_ma:
        return False, "low_volume"
    if not (0.003 <= atr_pct <= 0.10):
        return False, f"atr_pct={atr_pct:.4f} out of range"
    return True, ""


def check_exit(row: pd.Series, entry_price: float, entry_dt: datetime, now_dt: datetime) -> tuple[bool, str]:
    """Check MREV exit conditions. Returns (should_exit, reason)."""
    close = float(row["close"])
    sma = row.get("sma_20")
    rsi = row.get("rsi_14")
    atr = row.get("atr_14")

    # X1: Take profit — mean reversion
    if sma is not None and not (isinstance(sma, float) and sma != sma):
        if close >= float(sma):
            return True, f"take_profit (close={close:.2f} ≥ sma={float(sma):.2f})"

    # X2: Stop loss
    if atr is not None and not (isinstance(atr, float) and atr != atr) and float(atr) > 0:
        stop = entry_price - 1.5 * float(atr)
        if close <= stop:
            return True, f"stop_loss (close={close:.2f} ≤ stop={stop:.2f})"

    # X3: RSI normalized
    if rsi is not None and not (isinstance(rsi, float) and rsi != rsi):
        if 40.0 <= float(rsi) <= 60.0:
            return True, f"rsi_normalized ({float(rsi):.1f})"

    # X4: Time stop (24 hours)
    if entry_dt:
        hours_held = (now_dt - entry_dt).total_seconds() / 3600
        if hours_held >= 24:
            return True, f"time_stop ({hours_held:.0f}h)"

    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════════

def size_position(symbol: str, close: float, atr: float, equity: float) -> tuple[float, float]:
    """Calculate position size. Returns (qty, stop_price)."""
    is_crypto = "/" in symbol
    stop_dist = 1.5 * atr
    stop_price = close - stop_dist

    risk_amount = equity * MREV_RISK_PER_TRADE
    qty_risk = risk_amount / stop_dist if stop_dist > 0 else 0

    max_notional = equity * 0.25
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
        exit_price REAL, exit_dt TEXT, pnl REAL, exit_reason TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_signals (
        id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, signal_type TEXT,
        close_price REAL, rsi REAL, reason TEXT, created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrev_snapshots (
        id TEXT PRIMARY KEY, run_id TEXT, equity REAL, cash REAL,
        positions_count INTEGER, peak_equity REAL, created_at TEXT
    )""")
    # Hourly run log — tracks what happened each hour for the 8h summary email
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


def get_last_8h_activity(conn: sqlite3.Connection, run_id: str) -> dict:
    """Gather all trading activity from the last 8 hours for the summary email."""
    now = datetime.now(tz=timezone.utc)
    since = (now - timedelta(hours=8)).isoformat()

    # Hourly runs
    hourly_rows = conn.execute(
        "SELECT * FROM mrev_hourly_log WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # All buys/sells in the last 8h
    all_buys = []
    all_sells = []
    for h in hourly_rows:
        details = json.loads(h["details_json"]) if h["details_json"] else {}
        all_buys.extend(details.get("buys", []))
        all_sells.extend(details.get("sells", []))

    # Closed positions in the last 8h
    closed_8h = conn.execute(
        "SELECT * FROM mrev_positions WHERE run_id=? AND status='CLOSED' AND exit_dt>=? ORDER BY exit_dt",
        (run_id, since)
    ).fetchall()

    # Signals in the last 8h
    signals_8h = conn.execute(
        "SELECT * FROM mrev_signals WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # Equity snapshots for the period
    snapshots = conn.execute(
        "SELECT equity, cash, positions_count, created_at FROM mrev_snapshots WHERE run_id=? AND created_at>=? ORDER BY created_at",
        (run_id, since)
    ).fetchall()

    # Compute period stats
    total_pnl_8h = sum(float(c["pnl"] or 0) for c in closed_8h)
    wins_8h = sum(1 for c in closed_8h if float(c["pnl"] or 0) > 0)
    losses_8h = sum(1 for c in closed_8h if float(c["pnl"] or 0) <= 0)

    equity_start = float(snapshots[0]["equity"]) if snapshots else MREV_CAPITAL
    equity_end = float(snapshots[-1]["equity"]) if snapshots else MREV_CAPITAL

    hours_with_data = len(hourly_rows)
    total_entries = sum(int(h["entries"]) for h in hourly_rows)
    total_exits = sum(int(h["exits"]) for h in hourly_rows)

    return {
        "hours_covered": hours_with_data,
        "period_start": since,
        "period_end": now.isoformat(),
        "all_buys": all_buys,
        "all_sells": all_sells,
        "closed_trades": [dict(c) for c in closed_8h],
        "total_entries": total_entries,
        "total_exits": total_exits,
        "total_pnl": round(total_pnl_8h, 2),
        "wins": wins_8h,
        "losses": losses_8h,
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

    hdr("Portfolio State")
    info(f"Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}  |  Open: {len(open_positions)}")

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
            should_exit, reason = check_exit(row, float(pos["entry_price"]), entry_dt, now)
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
                    if notional <= cash:
                        buys.append({
                            "symbol": sym, "qty": qty,
                            "price": float(row["close"]),
                            "stop": round(stop, 4),
                            "notional": round(notional, 2),
                            "rsi": round(rsi_val, 1),
                        })
                        cash -= notional  # reserve cash
                        print(f"  {C.GREEN}ENTER{C.RESET} {sym:10s}  ${float(row['close']):>10.2f}  qty={qty}  stop=${stop:.2f}  RSI={rsi_val:.1f}")
                    else:
                        info(f"{sym}: entry signal but not enough cash (${notional:.2f} > ${cash:.2f})")
                else:
                    info(f"{sym}: entry signal but qty=0 or max positions reached")
            else:
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

        # Then buys
        for b in buys:
            try:
                if ALPACA_API_KEY:
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

    # ── 10. Send email (only 3x/day — window-based to handle cron delays) ──
    window = get_email_window(now)
    if should_send_email(conn, run_id, now):
        info(f"Email window [{window}] — building 8-hour summary...")
        activity = get_last_8h_activity(conn, run_id)
        result["activity_8h"] = activity
        send_email_report(result)
        record_email_sent(conn, run_id, now)
    else:
        info(f"Email already sent for window [{window}], skipping.")

    conn.close()
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _build_email_report(result: dict) -> tuple[str, str]:
    """Build subject + HTML body for the 8-hour MREV summary email (3x/day)."""
    now = datetime.now(tz=timezone.utc)
    today = now.strftime("%d/%m %H:%M UTC")
    dry_run = result.get("dry_run", False)
    activity = result.get("activity_8h", {})

    total_entries = activity.get("total_entries", 0)
    total_exits = activity.get("total_exits", 0)
    total_trades = total_entries + total_exits
    total_pnl = activity.get("total_pnl", 0)
    wins = activity.get("wins", 0)
    losses = activity.get("losses", 0)
    hours_covered = activity.get("hours_covered", 0)
    eq_change = activity.get("equity_change", 0)
    eq_change_pct = activity.get("equity_change_pct", 0)
    all_buys = activity.get("all_buys", [])
    all_sells = activity.get("all_sells", [])
    closed_trades = activity.get("closed_trades", [])

    # ── Subject line ─────────────────────────────────────────────────────────
    if total_trades > 0:
        parts = []
        if total_entries > 0:
            parts.append(f"{total_entries} compra{'s' if total_entries > 1 else ''}")
        if total_exits > 0:
            parts.append(f"{total_exits} venta{'s' if total_exits > 1 else ''}")
        pnl_str = f" · P&L ${total_pnl:+,.2f}" if closed_trades else ""
        subject = f"MREV 8h {today} — {' + '.join(parts)}{pnl_str}"
    else:
        subject = f"MREV 8h {today} — Sin operaciones ({hours_covered}h escaneadas)"

    if dry_run:
        subject = f"[SIM] {subject}"

    # ── CSS ───────────────────────────────────────────────────────────────────
    css = """<style>
        body { font-family: -apple-system, Helvetica, Arial, sans-serif; background:#f4f4f4; margin:0; padding:16px; color:#222; }
        .wrap { max-width:560px; margin:0 auto; }
        .hero { background:#16213e; color:#fff; border-radius:12px; padding:24px; margin-bottom:12px; }
        .hero h1 { margin:0 0 2px; font-size:17px; color:#fff; }
        .period { color:#8be9fd; font-size:13px; margin-bottom:4px; }
        .date { color:#aaa; font-size:12px; margin-bottom:16px; }
        .big { font-size:30px; font-weight:800; margin:4px 0 0; }
        .lbl { font-size:12px; color:#aaa; margin-bottom:10px; }
        .row3 { display:flex; gap:10px; margin-top:12px; }
        .col3 { flex:1; background:rgba(255,255,255,.08); border-radius:8px; padding:10px 12px; text-align:center; }
        .col3 .val { font-size:17px; font-weight:700; color:#fff; }
        .col3 .lbl { font-size:11px; }
        .stats { display:flex; gap:10px; margin-top:10px; }
        .stat-box { flex:1; background:rgba(255,255,255,.06); border-radius:8px; padding:8px 10px; text-align:center; }
        .stat-box .val { font-size:15px; font-weight:700; color:#fff; }
        .stat-box .lbl { font-size:10px; color:#aaa; }
        .section { background:#fff; border-radius:12px; padding:18px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
        .section h2 { margin:0 0 10px; font-size:14px; color:#333; }
        .green { color:#1b9e4b; } .red { color:#d63031; } .orange { color:#e67e22; } .muted { color:#999; }
        .pill { display:inline-block; padding:2px 8px; border-radius:20px; font-size:11px; font-weight:700; }
        .pill-buy { background:#d4edda; color:#155724; }
        .pill-sell { background:#f8d7da; color:#721c24; }
        .pill-win { background:#d4edda; color:#155724; }
        .pill-loss { background:#f8d7da; color:#721c24; }
        .pill-noop { background:#e9ecef; color:#555; }
        .item { border-bottom:1px solid #f0f0f0; padding:10px 0; }
        .item:last-child { border-bottom:none; }
        .sym { font-size:15px; font-weight:700; }
        .detail { color:#555; font-size:12px; line-height:1.7; margin-top:3px; }
        .time-tag { color:#999; font-size:11px; margin-left:6px; }
        .summary-row { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #f0f0f0; }
        .summary-row:last-child { border-bottom:none; }
        .foot { text-align:center; color:#bbb; font-size:11px; padding:8px 0 0; }
    </style>"""

    # ── Hero section ─────────────────────────────────────────────────────────
    equity = result.get("equity", MREV_CAPITAL)
    return_pct = result.get("return_pct", 0)
    cash = result.get("cash", 0)
    open_count = len(result.get("open_positions", []))
    ret_color = "green" if return_pct >= 0 else "red"
    chg_color = "green" if eq_change >= 0 else "red"

    # Period description
    period_start = activity.get("period_start", "")
    try:
        ps = datetime.fromisoformat(period_start)
        period_str = f"{ps.strftime('%H:%M')} — {now.strftime('%H:%M')} UTC ({hours_covered}h de datos)"
    except Exception:
        period_str = f"Últimas 8 horas ({hours_covered}h de datos)"

    hero = f"""<div class="hero">
        <h1>MREV-1H Bot — Resumen 8 Horas</h1>
        <div class="period">{period_str}</div>
        <div class="date">{today} {'(simulación)' if dry_run else ''}</div>
        <div class="big">${equity:,.2f}</div>
        <div class="lbl">Capital total · Retorno global: <span class="{ret_color}">{return_pct:+.2f}%</span></div>
        <div class="row3">
            <div class="col3"><div class="val {chg_color}">${eq_change:+,.2f}</div><div class="lbl">Cambio 8h</div></div>
            <div class="col3"><div class="val">${cash:,.2f}</div><div class="lbl">Cash</div></div>
            <div class="col3"><div class="val">{open_count}</div><div class="lbl">Posiciones</div></div>
        </div>
        <div class="stats">
            <div class="stat-box"><div class="val">{total_entries}</div><div class="lbl">Compras</div></div>
            <div class="stat-box"><div class="val">{total_exits}</div><div class="lbl">Ventas</div></div>
            <div class="stat-box"><div class="val">{wins}</div><div class="lbl">Wins</div></div>
            <div class="stat-box"><div class="val">{losses}</div><div class="lbl">Losses</div></div>
        </div>
    </div>"""

    # ── Buys section ─────────────────────────────────────────────────────────
    buys_html = ""
    if all_buys:
        items = ""
        for b in all_buys:
            ts = b.get("time", "")
            try:
                ts_short = datetime.fromisoformat(str(ts)).strftime("%H:%M")
            except Exception:
                ts_short = str(ts)[:5] if ts else ""
            items += f"""<div class="item">
                <span class="pill pill-buy">COMPRA</span>
                <span class="sym"> {b.get('symbol', '?')}</span>
                <span class="time-tag">{ts_short} UTC</span>
                <div class="detail">
                    Cantidad: {b.get('qty', '?')} · Precio: ${float(b.get('price', 0)):,.2f} · Inversión: ${float(b.get('notional', 0)):,.2f}<br>
                    Stop loss: ${float(b.get('stop', 0)):,.2f} · RSI: {b.get('rsi', '?')}
                </div>
            </div>"""
        buys_html = f'<div class="section"><h2>Compras en las últimas 8h ({len(all_buys)})</h2>{items}</div>'

    # ── Sells / closed trades section ────────────────────────────────────────
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
            items += f"""<div class="item">
                <span class="pill pill-sell">VENTA</span>
                <span class="sym"> {s.get('symbol', '?')}</span>
                <span class="time-tag">{ts_short} UTC</span>
                <div class="detail">
                    Cantidad: {s.get('qty', '?')} · Entrada: ${float(s.get('entry_price', 0)):,.2f} · Salida: ${float(s.get('exit_price', 0)):,.2f}<br>
                    <span class="{pnl_color}">P&L: ${pnl:+,.2f}</span> · Razón: {s.get('reason', '?')}
                </div>
            </div>"""
        sells_html = f'<div class="section"><h2>Ventas en las últimas 8h ({len(all_sells)})</h2>{items}</div>'

    # ── Closed trades P&L summary ────────────────────────────────────────────
    pnl_html = ""
    if closed_trades:
        rows = ""
        for ct in closed_trades:
            sym = ct.get("symbol", "?")
            pnl = float(ct.get("pnl", 0))
            pnl_color = "green" if pnl >= 0 else "red"
            result_pill = "pill-win" if pnl >= 0 else "pill-loss"
            result_text = "WIN" if pnl >= 0 else "LOSS"
            reason = ct.get("exit_reason", "?")
            rows += f"""<div class="summary-row">
                <span><b>{sym}</b> <span class="pill {result_pill}">{result_text}</span></span>
                <span class="{pnl_color}"><b>${pnl:+,.2f}</b> ({reason})</span>
            </div>"""
        total_color = "green" if total_pnl >= 0 else "red"
        rows += f"""<div class="summary-row" style="border-top:2px solid #ddd; margin-top:6px; padding-top:8px;">
            <span><b>Total P&L 8h</b></span>
            <span class="{total_color}"><b>${total_pnl:+,.2f}</b></span>
        </div>"""
        pnl_html = f'<div class="section"><h2>Resultados de trades cerrados</h2>{rows}</div>'

    # ── No activity fallback ─────────────────────────────────────────────────
    no_activity_html = ""
    if not all_buys and not all_sells:
        closest = result.get("hold_closest", [])
        closest_html = ""
        if closest:
            closest_html = "<br><b>Los más cerca de entrar:</b><br>"
            for c in closest[:3]:
                closest_html += f"· {c['symbol']} — RSI={c['rsi']} ({c['reason']})<br>"

        no_activity_html = f"""<div class="section">
            <span class="pill pill-noop">SIN OPERACIONES (8h)</span>
            <div class="detail" style="margin-top:8px;">
                En las últimas {hours_covered} horas escaneadas, ningún activo cumplió las condiciones
                de entrada (RSI ≤30 + precio bajo Bollinger inferior). El bot sigue monitoreando.
                {closest_html}
            </div>
        </div>"""

    # ── Open positions ───────────────────────────────────────────────────────
    pos_html = ""
    open_pos = result.get("open_positions", [])
    if open_pos:
        items = ""
        for p in open_pos:
            bars_held = int(p.get("bars_held", 0))
            bars_pct = min(100, int(bars_held / 24 * 100))
            items += f"""<div class="item">
                <span class="sym">{p['symbol']}</span>
                <div class="detail">
                    Cantidad: {p['qty']} · Entrada: ${float(p['entry_price']):,.2f} · Stop: ${float(p['stop_loss']):,.2f}<br>
                    Bars sostenido: {bars_held}/24 ({bars_pct}% del time stop)
                </div>
            </div>"""
        pos_html = f'<div class="section"><h2>Posiciones abiertas ({len(open_pos)})</h2>{items}</div>'

    # ── Footer ───────────────────────────────────────────────────────────────
    next_emails = sorted(EMAIL_HOURS_UTC)
    schedule_str = ", ".join(f"{h:02d}:00" for h in next_emails)

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{css}</head>
<body><div class="wrap">
    {hero}
    {buys_html}
    {sells_html}
    {pnl_html}
    {no_activity_html}
    {pos_html}
    <div class="foot">MREV-1H Bot — Resumen cada 8h ({schedule_str} UTC) · Mean Reversion 1H</div>
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
