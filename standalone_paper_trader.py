#!/usr/bin/env python3
"""
standalone_paper_trader.py
══════════════════════════════════════════════════════════════════════════════
RFTM Strategy — Standalone paper trading runner.

Only requires: pandas, numpy (pre-installed), plus stdlib (sqlite3, urllib, json).
No SQLAlchemy, no Docker, no psycopg2, no alembic needed.

Stores everything in trading_paper.db (SQLite).
Connects to Alpaca paper trading API if keys are set.

Usage:
    python3 standalone_paper_trader.py --dry-run     # scan signals, no orders sent
    python3 standalone_paper_trader.py               # real paper orders to Alpaca
    python3 standalone_paper_trader.py --status      # show portfolio + positions
    python3 standalone_paper_trader.py --reset       # wipe DB and start fresh
    python3 standalone_paper_trader.py --fetch-real  # fetch real Polygon data

Set keys in .env.paper or as environment variables:
    ALPACA_API_KEY, ALPACA_SECRET_KEY
    POLYGON_API_KEY  (optional — uses synthetic data if not set)
    INITIAL_CAPITAL  (default: 100000)
══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
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

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
# Store DB locally (avoids FUSE/network filesystem WAL issues on macOS)
# Falls back to a temp dir if the script dir is a network mount
_local_db_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "rftm_trader"
_local_db_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _local_db_dir / "trading_paper.db"
ENV_PATH = SCRIPT_DIR / ".env.paper"

ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "EFA", "EEM",
    "TLT", "GLD", "SLV", "USO", "DBA",
    "XLF", "XLE", "XLV", "XLK", "XLI",
    "XLP", "XLU", "XLRE",
]

INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "100_000"))
LOOKBACK_DAYS   = 300       # trading days of history to generate/keep
ATR_MULT        = 2.0       # stop = entry - ATR_MULT * ATR14
RISK_PCT        = 0.05      # risk 5% of portfolio per trade (aggressive paper trading)
MAX_POSITIONS   = 10
MAX_POS_PCT     = 0.25      # max 25% of portfolio in one position
MAX_DRAWDOWN    = 0.15      # 15% kill switch
MIN_SHARES      = 1
RSI_ENTRY_LO    = 50
RSI_ENTRY_HI    = 70
RSI_EXIT        = 80
VOL_MULT        = 1.5       # volume must be > 1.5x 20-day avg

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA_URL  = "https://data.alpaca.markets/v2"


# ── Load .env.paper ───────────────────────────────────────────────────────────

def _load_env() -> None:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
POLYGON_API_KEY   = os.environ.get("POLYGON_API_KEY", "")


# ── Colors ────────────────────────────────────────────────────────────────────

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


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_data (
            symbol      TEXT NOT NULL,
            date        TEXT NOT NULL,
            open        REAL, high REAL, low REAL, close REAL, volume INTEGER,
            ema50       REAL, ema200 REAL,
            rsi14       REAL, atr14  REAL,
            vol_ma20    REAL, high20 REAL,
            PRIMARY KEY (symbol, date)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id              TEXT PRIMARY KEY,
            started_at      TEXT,
            status          TEXT DEFAULT 'running',
            initial_capital REAL,
            cash            REAL
        );
        CREATE TABLE IF NOT EXISTS positions (
            id              TEXT PRIMARY KEY,
            run_id          TEXT,
            symbol          TEXT,
            status          TEXT DEFAULT 'open',
            qty             INTEGER,
            entry_price     REAL,
            stop_loss       REAL,
            exit_price      REAL,
            realized_pnl    REAL,
            unrealized_pnl  REAL DEFAULT 0,
            close_reason    TEXT,
            opened_at       TEXT,
            closed_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id              TEXT PRIMARY KEY,
            run_id          TEXT,
            symbol          TEXT,
            side            TEXT,
            qty             INTEGER,
            order_type      TEXT DEFAULT 'market',
            status          TEXT DEFAULT 'pending',
            submitted_price REAL,
            filled_price    REAL,
            broker_order_id TEXT,
            created_at      TEXT,
            filled_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id              TEXT PRIMARY KEY,
            run_id          TEXT,
            snapshot_at     TEXT,
            cash            REAL,
            positions_value REAL,
            total_equity    REAL,
            peak_equity     REAL,
            drawdown_pct    REAL,
            cumul_return_pct REAL,
            open_count      INTEGER
        );
        """)


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute EMA50/200, RSI14, ATR14, VolMA20, 20d-high on a OHLCV DataFrame."""
    df = df.sort_values("date").copy()
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    # EMAs
    df["ema50"]  = close.ewm(span=50,  adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    # RSI-14
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # ATR-14
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(com=13, adjust=False).mean()

    # Volume MA-20
    df["vol_ma20"] = vol.rolling(20).mean()

    # 20-day high (previous 20 days, not including today)
    df["high20"] = close.shift(1).rolling(20).max()

    return df


# ── Signal Scanner ────────────────────────────────────────────────────────────

def check_entry(row: pd.Series) -> bool:
    """RFTM entry: regime bullish + momentum + breakout + volume."""
    try:
        # Regime: EMA50 > EMA200
        if not (row["ema50"] > row["ema200"]):
            return False
        # Momentum: RSI in [50, 70]
        rsi = row["rsi14"]
        if not (RSI_ENTRY_LO <= rsi <= RSI_ENTRY_HI):
            return False
        # Breakout: close above 20-day high
        if not (row["close"] > row["high20"]):
            return False
        # Volume confirmation
        if not (row["volume"] > VOL_MULT * row["vol_ma20"]):
            return False
        return True
    except Exception:
        return False


def check_exit(row: pd.Series, position: Optional[sqlite3.Row]) -> tuple[bool, str]:
    """RFTM exit conditions. Returns (should_exit, reason)."""
    try:
        # E1: Death cross
        if row["ema50"] < row["ema200"]:
            return True, "E1_death_cross"
        # E2: Close below EMA50
        if row["close"] < row["ema50"]:
            return True, "E2_below_ema50"
        # E3: Stop loss
        if position and row["close"] <= float(position["stop_loss"]):
            return True, "E3_stop_loss"
        # E4: RSI overbought
        if row["rsi14"] > RSI_EXIT:
            return True, "E4_rsi_overbought"
        return False, ""
    except Exception:
        return False, ""


# ── Position Sizing ───────────────────────────────────────────────────────────

def size_position(portfolio_value: float, close: float, atr: float) -> int:
    """ATR-based position sizing. Returns number of shares."""
    stop_dist = ATR_MULT * atr
    if stop_dist <= 0:
        return 0
    risk_amount  = portfolio_value * RISK_PCT
    shares_risk  = math.floor(risk_amount / stop_dist)
    shares_cap   = math.floor(portfolio_value * MAX_POS_PCT / close)
    shares       = min(shares_risk, shares_cap)
    return max(shares, 0)


# ── Synthetic Data Generator ──────────────────────────────────────────────────

def generate_synthetic_data(symbol: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Generate realistic random-walk OHLCV data for one symbol."""
    rng = random.Random(hash(symbol) % (2**32))
    end   = date.today()
    start = end - timedelta(days=int(days * 1.5))  # extra for weekends
    cal   = pd.bdate_range(start=start, end=end)[-days:]

    prices = [rng.uniform(50, 400)]
    for _ in range(len(cal) - 1):
        chg = rng.gauss(0.0003, 0.012)
        prices.append(max(prices[-1] * (1 + chg), 1.0))

    rows = []
    for dt, p in zip(cal, prices):
        hi  = p * rng.uniform(1.001, 1.02)
        lo  = p * rng.uniform(0.98, 0.999)
        op  = rng.uniform(lo, hi)
        vol = int(rng.uniform(500_000, 5_000_000))
        rows.append({"date": str(dt.date()), "open": round(op, 2),
                     "high": round(hi, 2), "low": round(lo, 2),
                     "close": round(p, 2), "volume": vol})

    return pd.DataFrame(rows)


# ── Alpaca Data Fetcher (free with paper trading keys) ───────────────────────

def fetch_alpaca_bars(symbol: str, from_date: str, to_date: str) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV from Alpaca Data API.
    Free for all Alpaca accounts — uses the same keys as paper trading.
    IEX feed = free real-time + historical data.
    """
    url = (
        f"{ALPACA_DATA_URL}/stocks/{symbol}/bars"
        f"?timeframe=1Day&start={from_date}&end={to_date}"
        f"&limit=10000&adjustment=all&feed=iex&sort=asc"
    )
    all_bars = []
    while url:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "APCA-API-KEY-ID":     ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                }
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            warn(f"Alpaca data fetch failed for {symbol}: {e}")
            return None

        for bar in data.get("bars", []):
            all_bars.append({
                "date":   bar["t"][:10],   # ISO date string YYYY-MM-DD
                "open":   bar["o"],
                "high":   bar["h"],
                "low":    bar["l"],
                "close":  bar["c"],
                "volume": int(bar["v"]),
            })

        # Pagination
        next_token = data.get("next_page_token")
        if next_token:
            url = (
                f"{ALPACA_DATA_URL}/stocks/{symbol}/bars"
                f"?timeframe=1Day&start={from_date}&end={to_date}"
                f"&limit=10000&adjustment=all&feed=iex&sort=asc"
                f"&page_token={next_token}"
            )
        else:
            url = None

    if not all_bars:
        return None
    return pd.DataFrame(all_bars)


# ── Polygon Data Fetcher ──────────────────────────────────────────────────────

def fetch_polygon(symbol: str, from_date: str, to_date: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Polygon.io REST API (stdlib urllib, no httpx needed)."""
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day"
        f"/{from_date}/{to_date}"
        f"?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "trading-bot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        warn(f"Polygon fetch failed for {symbol}: {e}")
        return None

    if data.get("resultsCount", 0) == 0:
        return None

    rows = []
    for r in data["results"]:
        rows.append({
            "date":   str(date.fromtimestamp(r["t"] / 1000)),
            "open":   r["o"], "high": r["h"], "low": r["l"],
            "close":  r["c"], "volume": int(r["v"]),
        })
    return pd.DataFrame(rows)


# ── Data Layer ────────────────────────────────────────────────────────────────

def load_or_generate_data(symbol: str, use_real: bool) -> pd.DataFrame:
    """Load from DB if available and fresh, else fetch/generate and store."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM market_data WHERE symbol=? ORDER BY date",
            (symbol,)
        ).fetchall()

    has_real_source = bool(ALPACA_API_KEY or POLYGON_API_KEY)

    if rows and len(rows) >= 200 and not use_real:
        # Check if the latest date is today or yesterday (fresh data)
        latest_date = rows[-1]["date"]
        today_str   = str(date.today())
        yesterday   = str(date.today() - timedelta(days=1))
        data_is_fresh = latest_date >= yesterday

        if data_is_fresh or not has_real_source:
            df = pd.DataFrame([dict(r) for r in rows])
            df["date"] = pd.to_datetime(df["date"])
            return df

    # Need to generate/fetch
    today = date.today()
    start = today - timedelta(days=int(LOOKBACK_DAYS * 1.5))

    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        # Alpaca data API is free with any paper/live account — try first
        raw = fetch_alpaca_bars(symbol, str(start), str(today))
        if raw is not None and len(raw) >= 50:
            ok(f"{symbol}: {len(raw)} bars from Alpaca")
        else:
            if POLYGON_API_KEY:
                raw = fetch_polygon(symbol, str(start), str(today))
                if raw is not None:
                    ok(f"{symbol}: {len(raw)} bars from Polygon")
                else:
                    raw = generate_synthetic_data(symbol)
                    warn(f"{symbol}: using synthetic data")
            else:
                raw = generate_synthetic_data(symbol)
                warn(f"{symbol}: Alpaca data unavailable, using synthetic data")
    elif use_real and POLYGON_API_KEY:
        raw = fetch_polygon(symbol, str(start), str(today))
        if raw is None:
            raw = generate_synthetic_data(symbol)
            warn(f"{symbol}: Polygon failed, using synthetic data")
        else:
            ok(f"{symbol}: fetched {len(raw)} bars from Polygon")
    else:
        raw = generate_synthetic_data(symbol)

    df = compute_indicators(raw)
    df = df.dropna()

    # Store in DB
    with get_db() as conn:
        conn.execute("DELETE FROM market_data WHERE symbol=?", (symbol,))
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO market_data
                (symbol, date, open, high, low, close, volume,
                 ema50, ema200, rsi14, atr14, vol_ma20, high20)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol, str(row["date"].date() if hasattr(row["date"], "date") else row["date"]),
                row["open"], row["high"], row["low"], row["close"], int(row["volume"]),
                row.get("ema50"), row.get("ema200"), row.get("rsi14"),
                row.get("atr14"), row.get("vol_ma20"), row.get("high20"),
            ))

    df["date"] = pd.to_datetime(df["date"])
    return df


def get_latest_row(symbol: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    return dict(row) if row else None


# ── Alpaca Broker ─────────────────────────────────────────────────────────────

def _alpaca_request(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    url = f"{ALPACA_PAPER_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type":        "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        err(f"Alpaca {method} {path} → HTTP {e.code}: {body_text}")
        return None
    except Exception as e:
        err(f"Alpaca {method} {path} → {e}")
        return None


def alpaca_get_account() -> Optional[dict]:
    return _alpaca_request("GET", "/account")


def alpaca_submit_order(symbol: str, qty: int, side: str) -> Optional[dict]:
    return _alpaca_request("POST", "/orders", {
        "symbol": symbol, "qty": str(qty),
        "side": side, "type": "market",
        "time_in_force": "day",
    })


def alpaca_get_positions() -> list:
    result = _alpaca_request("GET", "/positions")
    return result if isinstance(result, list) else []


# ── Run Management ────────────────────────────────────────────────────────────

def create_run() -> str:
    run_id = str(uuid.uuid4())
    with get_db() as conn:
        # Check no active run
        existing = conn.execute(
            "SELECT id FROM runs WHERE status='running' LIMIT 1"
        ).fetchone()
        if existing:
            return existing["id"]
        conn.execute(
            "INSERT INTO runs (id, started_at, status, initial_capital, cash) VALUES (?,?,?,?,?)",
            (run_id, datetime.now(tz=timezone.utc).isoformat(),
             "running", INITIAL_CAPITAL, INITIAL_CAPITAL)
        )
    return run_id


def get_active_run() -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE status='running' LIMIT 1"
        ).fetchone()


def get_cash(run_id: str) -> float:
    with get_db() as conn:
        row = conn.execute("SELECT cash FROM runs WHERE id=?", (run_id,)).fetchone()
    return float(row["cash"]) if row else INITIAL_CAPITAL


def set_cash(conn: sqlite3.Connection, run_id: str, cash: float) -> None:
    conn.execute("UPDATE runs SET cash=? WHERE id=?", (cash, run_id))


def get_open_positions(run_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE run_id=? AND status='open'", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_symbols(run_id: str) -> set[str]:
    return {p["symbol"] for p in get_open_positions(run_id)}


# ── Portfolio Metrics ─────────────────────────────────────────────────────────

def compute_portfolio_value(run_id: str) -> tuple[float, float, float]:
    """Returns (cash, positions_value, total_equity)."""
    cash = get_cash(run_id)
    positions = get_open_positions(run_id)
    positions_value = 0.0
    for pos in positions:
        latest = get_latest_row(pos["symbol"])
        price = latest["close"] if latest else pos["entry_price"]
        positions_value += pos["qty"] * price
    return cash, positions_value, cash + positions_value


def get_peak_equity(run_id: str) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(peak_equity) as pk FROM snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
    pk = row["pk"] if row and row["pk"] else None
    return float(pk) if pk else INITIAL_CAPITAL


def write_snapshot(run_id: str, cash: float, positions_value: float) -> None:
    total = cash + positions_value
    peak  = max(get_peak_equity(run_id), total)
    dd    = (peak - total) / peak if peak > 0 else 0.0
    ret   = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL
    positions = get_open_positions(run_id)

    with get_db() as conn:
        conn.execute("""
            INSERT INTO snapshots
            (id, run_id, snapshot_at, cash, positions_value, total_equity,
             peak_equity, drawdown_pct, cumul_return_pct, open_count)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), run_id,
            datetime.now(tz=timezone.utc).isoformat(),
            round(cash, 4), round(positions_value, 4), round(total, 4),
            round(peak, 4), round(dd, 6), round(ret, 6), len(positions),
        ))


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(run_id: str, dry_run: bool, use_real_data: bool) -> dict:
    hdr("RFTM Signal Scanner")

    open_symbols = get_open_symbols(run_id)
    cash         = get_cash(run_id)
    positions    = get_open_positions(run_id)
    portfolio_value = cash + sum(
        p["qty"] * (get_latest_row(p["symbol"]) or {}).get("close", p["entry_price"])
        for p in positions
    )

    # Check kill switch (P1: max drawdown)
    peak = get_peak_equity(run_id)
    drawdown = (peak - portfolio_value) / peak if peak > 0 else 0.0
    if drawdown >= MAX_DRAWDOWN:
        err(f"KILL SWITCH: drawdown={drawdown:.1%} ≥ {MAX_DRAWDOWN:.0%}  — no new entries")
        return {"kill_switch": True}

    signals_enter = []
    signals_exit  = []
    signals_hold  = []

    for symbol in ETF_UNIVERSE:
        df = load_or_generate_data(symbol, use_real_data)
        if df.empty or len(df) < 201:
            continue
        latest = df.iloc[-1]
        row = latest.to_dict()
        row["symbol"] = symbol
        row["volume"] = int(row.get("volume", 0))

        # Check exit for open positions
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if pos:
            should_exit, reason = check_exit(latest, pos)
            if should_exit:
                signals_exit.append((symbol, reason, latest["close"], pos))
            else:
                signals_hold.append(symbol)
            continue

        # Check entry for new positions
        if len(open_symbols) >= MAX_POSITIONS:
            signals_hold.append(symbol)
            continue

        if check_entry(latest):
            atr = latest.get("atr14") or 0
            shares = size_position(portfolio_value, latest["close"], atr)
            cost   = shares * latest["close"]
            if shares >= MIN_SHARES and cost <= cash * MAX_POS_PCT * 2:
                signals_enter.append({
                    "symbol": symbol,
                    "close":  round(latest["close"], 2),
                    "ema50":  round(latest["ema50"], 2),
                    "ema200": round(latest["ema200"], 2),
                    "rsi14":  round(latest["rsi14"], 1),
                    "atr14":  round(atr, 2),
                    "shares": shares,
                    "cost":   round(cost, 2),
                    "stop":   round(latest["close"] - ATR_MULT * atr, 2),
                })

    # ── Print full watchlist status ───────────────────────────────────────────
    print(f"\n  {'Symbol':<8} {'Signal':<8} {'Close':>8} {'EMA50':>8} {'RSI':>6} "
          f"{'Vol×':>6}  Conditions")
    print(f"  {'─'*78}")

    # Build per-symbol rows for ALL ETFs (not just signals)
    all_rows = {}
    for symbol in ETF_UNIVERSE:
        latest_row = get_latest_row(symbol)
        if latest_row:
            all_rows[symbol] = latest_row

    # Entered / exited symbols get colored rows
    enter_syms = {e["symbol"] for e in signals_enter}
    exit_syms  = {s for s, _, _, _ in signals_exit}

    for symbol in ETF_UNIVERSE:
        r = all_rows.get(symbol)
        if not r:
            continue
        try:
            regime  = "✓" if r["ema50"] and r["ema200"] and r["ema50"] > r["ema200"] else "✗"
            rsi_v   = r["rsi14"] or 0
            rsi_s   = "✓" if 50 <= rsi_v <= 70 else "✗"
            brkout  = "✓" if r["close"] and r["high20"] and r["close"] > r["high20"] else "✗"
            vol_v   = r["volume"] / r["vol_ma20"] if r["vol_ma20"] and r["vol_ma20"] > 0 else 0
            vol_s   = "✓" if vol_v > VOL_MULT else "✗"
            conds   = f"regime={regime} rsi={rsi_s}({rsi_v:.0f}) brkout={brkout} vol={vol_s}({vol_v:.1f}x)"
        except Exception:
            conds = "data error"

        if symbol in enter_syms:
            e = next(e for e in signals_enter if e["symbol"] == symbol)
            print(f"  {C.GREEN}{symbol:<8}{C.RESET} {'ENTER':<8} "
                  f"{r['close']:>8.2f} {r['ema50']:>8.2f} {rsi_v:>6.1f} {vol_v:>6.1f}x  {conds}  "
                  f"→ {e['shares']} shares @ ${e['close']:.2f}")
        elif symbol in exit_syms:
            _, reason, close, _ = next(x for x in signals_exit if x[0] == symbol)
            print(f"  {C.RED}{symbol:<8}{C.RESET} {'EXIT':<8} "
                  f"{r['close']:>8.2f} {r['ema50']:>8.2f} {rsi_v:>6.1f} {vol_v:>6.1f}x  [{reason}]")
        elif symbol in open_symbols:
            print(f"  {C.YELLOW}{symbol:<8}{C.RESET} {'HOLD':<8} "
                  f"{r['close']:>8.2f} {r['ema50']:>8.2f} {rsi_v:>6.1f} {vol_v:>6.1f}x  {conds}")
        else:
            # Show dimmed with condition breakdown
            all_ok = all(c == "✓" for c in [regime, rsi_s, brkout, vol_s])
            color  = C.GRAY
            sig    = "watch"
            print(f"  {color}{symbol:<8}{C.RESET} {sig:<8} "
                  f"{r['close']:>8.2f} {r['ema50']:>8.2f} {rsi_v:>6.1f} {vol_v:>6.1f}x  {conds}")

    # Recent signals history (last 30 trading days)
    print(f"\n  {C.BOLD}Recent signals — last 30 trading days:{C.RESET}")
    print(f"  {'Date':<12} {'Symbol':<8} {'Signal':<8} {'Close':>8} {'RSI':>6}")
    print(f"  {'─'*50}")
    recent_signals = []
    for symbol in ETF_UNIVERSE:
        r = all_rows.get(symbol)
        if not r:
            continue
        with get_db() as conn:
            hist = conn.execute(
                "SELECT * FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 35",
                (symbol,)
            ).fetchall()
        hist = [dict(h) for h in hist]
        hist.reverse()
        for i, day in enumerate(hist):
            try:
                pos_today = None
                entry = (
                    day["ema50"] and day["ema200"] and day["ema50"] > day["ema200"] and
                    50 <= (day["rsi14"] or 0) <= 70 and
                    day["close"] and day["high20"] and day["close"] > day["high20"] and
                    day["volume"] and day["vol_ma20"] and day["volume"] > VOL_MULT * day["vol_ma20"]
                )
                if entry:
                    recent_signals.append((day["date"], symbol, "ENTER", day["close"], day["rsi14"]))
                else:
                    # Check exit (approximate — no position tracking in history)
                    e1 = day["ema50"] and day["ema200"] and day["ema50"] < day["ema200"]
                    e4 = (day["rsi14"] or 0) > RSI_EXIT
                    if e1:
                        recent_signals.append((day["date"], symbol, "EXIT:death_cross", day["close"], day["rsi14"]))
            except Exception:
                pass

    recent_signals.sort(key=lambda x: x[0], reverse=True)
    shown = 0
    for sig_date, sym, sig_type, close, rsi in recent_signals[:20]:
        color = C.GREEN if "ENTER" in sig_type else C.RED
        print(f"  {sig_date:<12} {color}{sym:<8}{C.RESET} {sig_type:<8} {close:>8.2f} {rsi:>6.1f}")
        shown += 1
    if shown == 0:
        print(f"  {C.GRAY}  No signals in last 30 days (synthetic data — run with real Polygon data for live signals){C.RESET}")

    # ── Execute ───────────────────────────────────────────────────────────────
    hdr("Execution")

    orders_placed = 0

    if dry_run:
        warn("DRY RUN — no orders sent to Alpaca")
        if signals_enter:
            for e in signals_enter:
                info(f"Would BUY  {e['shares']:4d} × {e['symbol']:<6}  @ ${e['close']:.2f}  cost=${e['cost']:,.0f}")
        if signals_exit:
            for symbol, reason, close, pos in signals_exit:
                pnl = (close - pos["entry_price"]) * pos["qty"]
                info(f"Would SELL {pos['qty']:4d} × {symbol:<6}  @ ${close:.2f}  P&L=${pnl:+,.0f}  ({reason})")
    else:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            err("No Alpaca keys in .env.paper — cannot place orders")
            err("Edit .env.paper and set ALPACA_API_KEY + ALPACA_SECRET_KEY")
        else:
            # Process exits first (free up cash)
            for symbol, reason, close, pos in signals_exit:
                result = alpaca_submit_order(symbol, pos["qty"], "sell")
                if result:
                    filled_price = float(result.get("filled_avg_price") or close)
                    pnl = (filled_price - pos["entry_price"]) * pos["qty"]
                    released_cash = filled_price * pos["qty"]
                    with get_db() as conn:
                        conn.execute("""
                            UPDATE positions SET status='closed', exit_price=?,
                            realized_pnl=?, close_reason=?,
                            closed_at=? WHERE id=?
                        """, (filled_price, round(pnl, 4), reason,
                              datetime.now(tz=timezone.utc).isoformat(), pos["id"]))
                        new_cash = get_cash(run_id) + released_cash
                        set_cash(conn, run_id, new_cash)
                    ok(f"SOLD   {pos['qty']:4d} × {symbol:<6}  @ ${filled_price:.2f}  P&L=${pnl:+,.0f}  [{reason}]")
                    orders_placed += 1
                    cash = get_cash(run_id)

            # Process entries
            for e in signals_enter:
                if e["cost"] > cash * 1.05:
                    warn(f"Insufficient cash for {e['symbol']} (need ${e['cost']:,.0f}, have ${cash:,.0f})")
                    continue
                result = alpaca_submit_order(e["symbol"], e["shares"], "buy")
                if result:
                    filled_price = float(result.get("filled_avg_price") or e["close"])
                    actual_cost  = filled_price * e["shares"]
                    stop         = filled_price - ATR_MULT * e["atr14"]
                    with get_db() as conn:
                        pos_id = str(uuid.uuid4())
                        conn.execute("""
                            INSERT INTO positions
                            (id, run_id, symbol, status, qty, entry_price, stop_loss,
                             unrealized_pnl, opened_at)
                            VALUES (?,?,?,?,?,?,?,0,?)
                        """, (pos_id, run_id, e["symbol"], "open",
                              e["shares"], filled_price, round(stop, 4),
                              datetime.now(tz=timezone.utc).isoformat()))
                        new_cash = get_cash(run_id) - actual_cost
                        set_cash(conn, run_id, new_cash)
                    ok(f"BOUGHT {e['shares']:4d} × {e['symbol']:<6}  @ ${filled_price:.2f}  stop=${stop:.2f}")
                    orders_placed += 1
                    cash = get_cash(run_id)

    # ── Snapshot ──────────────────────────────────────────────────────────────
    cash_now, pos_val, total = compute_portfolio_value(run_id)
    write_snapshot(run_id, cash_now, pos_val)

    return {
        "enter": len(signals_enter),
        "exit":  len(signals_exit),
        "hold":  len(signals_hold),
        "orders_placed": orders_placed,
        "total_equity":  round(total, 2),
        "cash":          round(cash_now, 2),
        "positions_val": round(pos_val, 2),
        "drawdown":      round(drawdown, 4),
    }


# ── Status ────────────────────────────────────────────────────────────────────

def show_status() -> None:
    run = get_active_run()
    if not run:
        warn("No active run. Run without --status to start one.")
        return

    run_id = run["id"]
    cash, pos_val, total = compute_portfolio_value(run_id)
    peak     = get_peak_equity(run_id)
    drawdown = (peak - total) / peak if peak > 0 else 0.0
    ret      = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL

    hdr("Portfolio Status")
    print(f"  {'Run ID:':<20} {run_id}")
    print(f"  {'Started:':<20} {run['started_at'][:19]}")
    print(f"  {'Initial capital:':<20} ${INITIAL_CAPITAL:>12,.2f}")
    print(f"  {'Total equity:':<20} {C.GREEN if total >= INITIAL_CAPITAL else C.RED}"
          f"${total:>12,.2f}{C.RESET}")
    print(f"  {'Cash:':<20} ${cash:>12,.2f}")
    print(f"  {'Positions value:':<20} ${pos_val:>12,.2f}")
    print(f"  {'Cumul. return:':<20} {C.GREEN if ret >= 0 else C.RED}{ret:>+11.2%}{C.RESET}")
    print(f"  {'Drawdown:':<20} {C.RED if drawdown > 0.05 else ''}{drawdown:>11.2%}{C.RESET}")

    positions = get_open_positions(run_id)
    if positions:
        hdr(f"Open Positions ({len(positions)})")
        print(f"  {'Symbol':<8} {'Qty':>6} {'Entry':>9} {'Current':>9} "
              f"{'Stop':>9} {'Unrlzd P&L':>12}")
        print(f"  {'─'*60}")
        for pos in positions:
            latest = get_latest_row(pos["symbol"])
            curr   = latest["close"] if latest else pos["entry_price"]
            upnl   = (curr - pos["entry_price"]) * pos["qty"]
            color  = C.GREEN if upnl >= 0 else C.RED
            print(f"  {pos['symbol']:<8} {pos['qty']:>6d} "
                  f"{pos['entry_price']:>9.2f} {curr:>9.2f} "
                  f"{pos['stop_loss']:>9.2f} "
                  f"{color}{upnl:>+11,.0f}{C.RESET}")
    else:
        print(f"\n  No open positions.")

    # Alpaca positions
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        hdr("Alpaca Paper Account")
        acct = alpaca_get_account()
        if acct:
            print(f"  {'Buying power:':<20} ${float(acct.get('buying_power',0)):>12,.2f}")
            print(f"  {'Portfolio value:':<20} ${float(acct.get('portfolio_value',0)):>12,.2f}")
            print(f"  {'Equity:':<20} ${float(acct.get('equity',0)):>12,.2f}")
        ap = alpaca_get_positions()
        if ap:
            print(f"\n  Alpaca positions ({len(ap)}):")
            for p in ap:
                print(f"    {p['symbol']:<8} {p['qty']:>6}  "
                      f"avg=${float(p['avg_entry_price']):.2f}  "
                      f"mkt=${float(p.get('market_value',0)):,.0f}  "
                      f"P&L=${float(p.get('unrealized_pl',0)):+,.0f}")


# ── Reset ─────────────────────────────────────────────────────────────────────

def reset_db() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
        ok(f"Database deleted: {DB_PATH.name}")
    init_db()
    ok("Fresh database created")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="RFTM Standalone Paper Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run",    action="store_true", help="Scan signals only, no orders")
    parser.add_argument("--status",     action="store_true", help="Show portfolio status")
    parser.add_argument("--reset",      action="store_true", help="Wipe DB and start fresh")
    parser.add_argument("--fetch-real", action="store_true", help="Fetch real data from Polygon")
    args = parser.parse_args()

    print(f"\n{C.BOLD}{'═'*56}{C.RESET}")
    print(f"{C.BOLD}  RFTM Paper Trader — {date.today()}{C.RESET}")
    print(f"{C.BOLD}{'═'*56}{C.RESET}")

    init_db()

    if args.reset:
        reset_db()
        return 0

    if args.status:
        show_status()
        return 0

    dry_run  = args.dry_run or (not ALPACA_API_KEY)
    use_real = args.fetch_real  # hint to force-refresh even if DB has data

    if not ALPACA_API_KEY:
        warn("No ALPACA_API_KEY set — running in DRY-RUN mode")
        warn("Edit .env.paper to add your Alpaca paper trading keys")
    elif dry_run:
        info("DRY RUN mode  (remove --dry-run to send real paper orders)")
    else:
        ok("PAPER TRADING mode — orders will be sent to Alpaca")

    if ALPACA_API_KEY:
        info("Market data: Alpaca Data API (free, real prices)")
    elif POLYGON_API_KEY:
        info("Market data: Polygon.io")
    else:
        warn("Market data: synthetic (add Alpaca keys for real prices)")

    # Create or get run
    run_id = create_run()
    info(f"Run ID: {run_id}")

    hdr("Loading Market Data")
    for symbol in ETF_UNIVERSE:
        load_or_generate_data(symbol, use_real)

    result = run_pipeline(run_id, dry_run, use_real)

    if result.get("kill_switch"):
        err("Kill switch active — pipeline halted")
        return 1

    hdr("Summary")
    dd_color = C.RED if result["drawdown"] > 0.05 else C.GREEN
    print(f"  ENTER signals:     {C.GREEN}{result['enter']}{C.RESET}")
    print(f"  EXIT  signals:     {C.RED}{result['exit']}{C.RESET}")
    print(f"  HOLD:              {result['hold']}")
    print(f"  Orders placed:     {result['orders_placed']}")
    print(f"  Total equity:      ${result['total_equity']:>12,.2f}")
    print(f"  Cash:              ${result['cash']:>12,.2f}")
    print(f"  Positions value:   ${result['positions_val']:>12,.2f}")
    print(f"  Drawdown:          {dd_color}{result['drawdown']:.2%}{C.RESET}")
    print()

    if dry_run and (result["enter"] > 0 or result["exit"] > 0):
        print(f"  {C.YELLOW}To place real paper orders on Alpaca:{C.RESET}")
        print(f"  1. Edit .env.paper — add ALPACA_API_KEY + ALPACA_SECRET_KEY")
        print(f"  2. Run:  python3 standalone_paper_trader.py")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
