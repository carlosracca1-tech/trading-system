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

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
# Store DB locally (avoids FUSE/network filesystem WAL issues on macOS)
# Falls back to a temp dir if the script dir is a network mount
_local_db_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "rftm_trader"
_local_db_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _local_db_dir / "trading_paper.db"
ENV_PATH = SCRIPT_DIR / ".env.paper"

ETF_UNIVERSE = [
    # ── EE.UU. — Índices principales ─────────────────────────────────
    "SPY",    # S&P 500
    "QQQ",    # Nasdaq 100
    "IWM",    # Russell 2000 (small caps)
    "DIA",    # Dow Jones 30
    "MDY",    # S&P MidCap 400

    # ── EE.UU. — Sectores ────────────────────────────────────────────
    "XLK",    # Tecnología
    "XLF",    # Financiero
    "XLE",    # Energía
    "XLV",    # Salud
    "XLI",    # Industrial
    "XLP",    # Consumo básico
    "XLY",    # Consumo discrecional
    "XLU",    # Utilities
    "XLRE",   # Real estate
    "XLC",    # Comunicaciones
    "XLB",    # Materiales

    # ── Tecnología & Innovación ──────────────────────────────────────
    "ARKK",   # ARK Innovation (disruptivas)
    "SOXX",   # Semiconductores
    "SKYY",   # Cloud computing
    "BOTZ",   # Robótica & AI
    "TAN",    # Energía solar
    "LIT",    # Litio & baterías
    "HACK",   # Ciberseguridad

    # ── LATAM ────────────────────────────────────────────────────────
    "EWZ",    # Brasil (iShares MSCI Brazil)
    "EWW",    # México (iShares MSCI Mexico)
    "ECH",    # Chile (iShares MSCI Chile)
    "ARGT",   # Argentina (Global X MSCI Argentina)
    "ILF",    # LatAm 40 (las 40 más grandes de LATAM)
    "FLBR",   # Franklin FTSE Brazil
    "GXG",    # Colombia (Global X MSCI Colombia)
    "EPU",    # Perú (iShares MSCI Peru)

    # ── Internacional — Asia & Emergentes ────────────────────────────
    "FXI",    # China large-cap
    "KWEB",   # China tech/internet
    "INDA",   # India
    "EWT",    # Taiwan (TSMC, semiconductores)
    "EWY",    # Corea del Sur (Samsung, etc)
    "VWO",    # Todos los emergentes

    # ── Internacional — Europa & Desarrollados ───────────────────────
    "EFA",    # EAFE (Europa, Australia, Japón)
    "EWG",    # Alemania
    "EWU",    # Reino Unido
    "EWJ",    # Japón

    # ── Commodities & Recursos ───────────────────────────────────────
    "GLD",    # Oro
    "SLV",    # Plata
    "USO",    # Petróleo
    "DBA",    # Agricultura
    "UNG",    # Gas natural
    "COPX",   # Cobre (mineras)
    "WEAT",   # Trigo
    "CPER",   # Cobre físico

    # ── Renta Fija & Defensivos ──────────────────────────────────────
    "TLT",    # Bonos largos US Treasury
    "HYG",    # High yield bonds (junk bonds)
    "EMB",    # Bonos emergentes

    # ── Temáticos trending ───────────────────────────────────────────
    "BITO",   # Bitcoin futures ETF
    "BLOK",   # Blockchain companies
    "JETS",   # Aerolíneas
    "XHB",    # Homebuilders (construcción)
    "PAVE",   # Infraestructura
    "URA",    # Uranio (energía nuclear)
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

# ── Email notification config ─────────────────────────────────────────────────
EMAIL_ENABLED     = os.environ.get("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_SMTP_SERVER = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT   = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD", "")  # Gmail App Password
EMAIL_TO          = os.environ.get("EMAIL_TO", "")


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
                    # NEVER use synthetic data when we have API keys —
                    # synthetic prices cause the bot to make wrong decisions
                    err(f"{symbol}: no real data available — SKIPPING (not using fake data)")
                    return pd.DataFrame()
            else:
                err(f"{symbol}: Alpaca data unavailable — SKIPPING (not using fake data)")
                return pd.DataFrame()
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
    result = _alpaca_request("POST", "/orders", {
        "symbol": symbol, "qty": str(qty),
        "side": side, "type": "market",
        "time_in_force": "day",
    })
    if not result:
        return None

    # Market orders fill almost instantly — poll briefly for real fill price
    order_id = result.get("id")
    if order_id and not result.get("filled_avg_price"):
        for _ in range(5):
            time.sleep(1)
            updated = _alpaca_request("GET", f"/orders/{order_id}")
            if updated and updated.get("filled_avg_price"):
                return updated
            if updated and updated.get("status") in ("filled", "partially_filled"):
                return updated
    return result


def alpaca_get_positions() -> list:
    result = _alpaca_request("GET", "/positions")
    return result if isinstance(result, list) else []


def alpaca_get_orders_today() -> list:
    """Get all filled orders from today via Alpaca API."""
    today_str = date.today().isoformat()
    result = _alpaca_request(
        "GET",
        f"/orders?status=filled&after={today_str}T00:00:00Z&direction=desc&limit=50"
    )
    return result if isinstance(result, list) else []


def alpaca_get_portfolio_history(days: int = 30) -> Optional[dict]:
    """Get portfolio equity history for the last N days."""
    return _alpaca_request(
        "GET",
        f"/account/portfolio/history?period={days}D&timeframe=1D"
    )


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


def sync_with_alpaca(run_id: str) -> None:
    """Reconcile local DB positions/cash with Alpaca's real state.

    This fixes mismatches caused by stale synthetic data, missed fills, etc.
    Alpaca is the source of truth — our local DB adapts to match.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return

    acct = alpaca_get_account()
    if not acct:
        warn("Could not reach Alpaca to sync — skipping")
        return

    alpaca_positions = alpaca_get_positions() or []
    alpaca_syms = {p["symbol"]: p for p in alpaca_positions}
    local_positions = get_open_positions(run_id)
    local_syms = {p["symbol"]: p for p in local_positions}

    changed = False
    with get_db() as conn:
        # 1. Close local positions that Alpaca no longer has (already sold)
        for lp in local_positions:
            sym = lp["symbol"]
            if sym not in alpaca_syms:
                # Alpaca doesn't have this position — it was sold
                # Try to get the real sell price from recent orders
                orders = alpaca_get_orders_today()
                sell_order = None
                for o in (orders or []):
                    if o.get("symbol") == sym and o.get("side") == "sell" and o.get("filled_avg_price"):
                        sell_order = o
                        break

                if sell_order:
                    exit_price = float(sell_order["filled_avg_price"])
                else:
                    # Use latest market data as approximation
                    latest = get_latest_row(sym)
                    exit_price = latest["close"] if latest else lp["entry_price"]

                # Fix entry price if it was synthetic (wildly different from exit)
                entry = lp["entry_price"]
                if abs(entry - exit_price) / max(exit_price, 0.01) > 2.0:
                    # Entry is way off from reality — try to find real entry from Alpaca orders
                    warn(f"SYNC: {sym} entry ${entry:.2f} looks wrong vs exit ${exit_price:.2f} — fixing")
                    entry = exit_price  # conservative: assume ~breakeven if we can't find real entry

                pnl = (exit_price - entry) * lp["qty"]
                conn.execute("""
                    UPDATE positions SET status='closed', exit_price=?,
                    realized_pnl=?, close_reason='synced_from_alpaca',
                    entry_price=?, closed_at=?
                    WHERE id=?
                """, (exit_price, round(pnl, 4), entry,
                      datetime.now(tz=timezone.utc).isoformat(), lp["id"]))
                ok(f"SYNC: closed {sym} (no longer on Alpaca) exit=${exit_price:.2f} P&L=${pnl:+,.0f}")
                changed = True

        # 2. Fix entry prices on positions that still exist on Alpaca
        for lp in local_positions:
            sym = lp["symbol"]
            if sym in alpaca_syms:
                ap = alpaca_syms[sym]
                real_entry = float(ap.get("avg_entry_price", 0))
                real_qty = int(float(ap.get("qty", 0)))
                if real_entry > 0 and abs(lp["entry_price"] - real_entry) > 0.01:
                    warn(f"SYNC: {sym} fixing entry ${lp['entry_price']:.2f} → ${real_entry:.2f}")
                    conn.execute(
                        "UPDATE positions SET entry_price=?, qty=? WHERE id=?",
                        (real_entry, real_qty, lp["id"])
                    )
                    changed = True

        # 3. Add positions that Alpaca has but we don't track locally
        for sym, ap in alpaca_syms.items():
            if sym not in local_syms:
                real_entry = float(ap.get("avg_entry_price", 0))
                real_qty = int(float(ap.get("qty", 0)))
                if real_qty > 0 and real_entry > 0:
                    pos_id = str(uuid.uuid4())
                    conn.execute("""
                        INSERT INTO positions
                        (id, run_id, symbol, status, qty, entry_price, stop_loss,
                         unrealized_pnl, opened_at)
                        VALUES (?,?,?,?,?,?,?,0,?)
                    """, (pos_id, run_id, sym, "open", real_qty, real_entry,
                          round(real_entry * 0.95, 4),  # 5% default stop if unknown
                          datetime.now(tz=timezone.utc).isoformat()))
                    ok(f"SYNC: added missing position {sym} {real_qty}x @ ${real_entry:.2f}")
                    changed = True

        # 4. Sync cash with Alpaca account
        real_cash = float(acct.get("cash", 0))
        conn.execute("UPDATE runs SET cash=? WHERE id=?", (real_cash, run_id))

    if changed:
        ok("SYNC: local DB reconciled with Alpaca")
    else:
        ok("SYNC: local DB matches Alpaca — all good")


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


# ── Email Report ──────────────────────────────────────────────────────────────

def _build_email_report(
    run_id: str,
    result: dict,
    signals_enter: list,
    signals_exit: list,
    watchlist_rows: list[dict],
    dry_run: bool,
) -> tuple[str, str]:
    """Build subject + HTML body for the daily trading email report."""
    today = date.today().strftime("%d/%m/%Y")
    buys  = len(signals_enter)
    sells = len(signals_exit)

    # ── Pull REAL data from Alpaca if available ──────────────────────────────
    alpaca_acct = None
    alpaca_positions = []
    alpaca_orders_today = []
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            alpaca_acct = alpaca_get_account()
            alpaca_positions = alpaca_get_positions() or []
            alpaca_orders_today = alpaca_get_orders_today() or []
        except Exception:
            pass

    # Build lookups by symbol for real prices
    alpaca_by_sym = {}
    for ap in alpaca_positions:
        alpaca_by_sym[ap["symbol"]] = ap

    # Build lookup of today's filled orders by symbol+side
    alpaca_buys_today = {}   # symbol -> order
    alpaca_sells_today = {}  # symbol -> order
    for ao in alpaca_orders_today:
        sym = ao.get("symbol", "")
        if ao.get("side") == "buy":
            alpaca_buys_today[sym] = ao
        elif ao.get("side") == "sell":
            alpaca_sells_today[sym] = ao

    # ── Subject — super corto ────────────────────────────────────────────────
    if buys > 0 and sells > 0:
        subject = f"Bot {date.today()} — Compré {buys} + Vendí {sells}"
    elif buys > 0:
        subject = f"Bot {date.today()} — Compré {buys} ETF{'s' if buys > 1 else ''}"
    elif sells > 0:
        subject = f"Bot {date.today()} — Vendí {sells} ETF{'s' if sells > 1 else ''}"
    else:
        subject = f"Bot {date.today()} — Hoy no operé"

    if dry_run:
        subject = f"[SIMULACIÓN] {subject}"

    # ── CSS ──────────────────────────────────────────────────────────────────
    css = """<style>
        body { font-family: -apple-system, Helvetica, Arial, sans-serif; background:#f4f4f4; margin:0; padding:16px; color:#222; }
        .wrap { max-width:540px; margin:0 auto; }
        .hero { background:#1a1a2e; color:#fff; border-radius:12px; padding:24px; margin-bottom:12px; }
        .hero h1 { margin:0 0 2px; font-size:18px; color:#fff; }
        .date { color:#aaa; font-size:13px; margin-bottom:18px; }
        .big { font-size:32px; font-weight:800; margin:4px 0 0; }
        .lbl { font-size:12px; color:#aaa; margin-bottom:12px; }
        .hero .lbl { color:#999; }
        .row2 { display:flex; gap:16px; margin-top:12px; }
        .col2 { flex:1; background:rgba(255,255,255,.08); border-radius:8px; padding:10px 14px; }
        .col2 .val { font-size:18px; font-weight:700; color:#fff; }
        .section { background:#fff; border-radius:12px; padding:20px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
        .section h2 { margin:0 0 12px; font-size:15px; }
        .green { color:#1b9e4b; } .red { color:#d63031; } .orange { color:#e67e22; }
        .pill { display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; }
        .pill-buy { background:#d4edda; color:#155724; }
        .pill-sell { background:#f8d7da; color:#721c24; }
        .pill-noop { background:#e9ecef; color:#555; }
        .item { border-bottom:1px solid #f0f0f0; padding:12px 0; }
        .item:last-child { border-bottom:none; }
        .sym { font-size:16px; font-weight:700; }
        .detail { color:#555; font-size:13px; line-height:1.8; margin-top:4px; }
        .levels { display:flex; gap:8px; margin-top:8px; }
        .lvl { flex:1; border-radius:8px; padding:8px 10px; text-align:center; font-size:12px; }
        .lvl .lvl-val { font-size:15px; font-weight:700; margin-bottom:2px; }
        .lvl-sl { background:#fdecea; }
        .lvl-entry { background:#e8f4fd; }
        .lvl-tp { background:#e8f5e9; }
        .noop-box { background:#fff8e1; border-radius:8px; padding:14px 16px; margin-top:8px; font-size:13px; line-height:1.6; }
        .closest { margin-top:12px; }
        .bar-bg { background:#eee; border-radius:4px; height:7px; margin:3px 0 2px; }
        .bar { height:7px; border-radius:4px; }
        .foot { text-align:center; color:#bbb; font-size:11px; padding:8px 0 0; }
        .divider { border:none; border-top:1px solid #eee; margin:8px 0; }
    </style>"""

    # ── Real equity from Alpaca, fallback to local DB ────────────────────────
    if alpaca_acct:
        equity  = float(alpaca_acct.get("equity", 0))
        cash    = float(alpaca_acct.get("cash", 0))
        pos_val = float(alpaca_acct.get("long_market_value", 0))
        initial = float(alpaca_acct.get("last_equity", equity))  # yesterday's close
    else:
        equity  = result.get("total_equity", 0)
        cash    = result.get("cash", 0)
        pos_val = result.get("positions_val", 0)

    ret_pct = (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL if INITIAL_CAPITAL else 0
    ret_cls = "green" if ret_pct >= 0 else "red"
    ret_sign = "+" if ret_pct >= 0 else ""

    # ── Cómo venimos — run info ──────────────────────────────────────────────
    run_info = ""
    run = get_active_run()
    if run:
        started = run["started_at"][:10]
        try:
            start_date = datetime.fromisoformat(started).date()
            days_running = (date.today() - start_date).days
            run_info = f"Día {days_running} del bot"
        except Exception:
            run_info = ""

    # ── Hero section ─────────────────────────────────────────────────────────
    hero = f"""
    <div class="hero">
        <h1>Reporte del Bot</h1>
        <div class="date">{today}{' · SIMULACIÓN' if dry_run else ''}{' · ' + run_info if run_info else ''}</div>
        <div class="big" style="color:{'#2ecc71' if ret_pct >= 0 else '#e74c3c'};">${equity:,.2f}</div>
        <div class="lbl">Tu portafolio hoy ({ret_sign}{ret_pct:.1%} desde los ${INITIAL_CAPITAL:,.0f} iniciales)</div>
        <div class="row2">
            <div class="col2"><div class="val">${cash:,.0f}</div><div class="lbl">Disponible para comprar</div></div>
            <div class="col2"><div class="val">${pos_val:,.0f}</div><div class="lbl">Invertido en ETFs</div></div>
        </div>
    </div>"""

    # ── Helper: take profit calc (2:1 risk/reward) ───────────────────────────
    def _calc_take_profit(entry: float, stop: float) -> float:
        """TP at 2:1 risk/reward ratio."""
        risk = entry - stop
        return round(entry + 2 * risk, 2)

    # ── Qué hizo hoy ────────────────────────────────────────────────────────
    action_html = ""

    # COMPRAS
    if signals_enter:
        items = ""
        for e in signals_enter:
            sym = e["symbol"]
            # Use REAL Alpaca fill price if available
            a_order = alpaca_buys_today.get(sym)
            a_pos = alpaca_by_sym.get(sym)
            if a_order and a_order.get("filled_avg_price"):
                real_price = float(a_order["filled_avg_price"])
                real_qty = int(a_order.get("filled_qty", e["shares"]))
            elif a_pos:
                real_price = float(a_pos.get("avg_entry_price", e["close"]))
                real_qty = int(float(a_pos.get("qty", e["shares"])))
            else:
                real_price = e["close"]
                real_qty = e["shares"]

            real_cost = real_price * real_qty
            # Recalculate stop based on real price (same ATR distance)
            atr_dist = e["close"] - e["stop"]  # original ATR distance
            real_stop = round(real_price - atr_dist, 2)
            real_tp = _calc_take_profit(real_price, real_stop)

            risk = real_price - real_stop
            risk_pct = (risk / real_price) * 100
            tp_gain = real_tp - real_price
            tp_pct = (tp_gain / real_price) * 100

            items += f"""
            <div class="item">
                <span class="pill pill-buy">COMPRA</span>
                <span class="sym" style="margin-left:8px;">{sym}</span>
                <div class="detail">
                    Compré <strong>{real_qty} acciones</strong> a <strong>${real_price:.2f}</strong> c/u<br>
                    Invertí en total: <strong>${real_cost:,.0f}</strong>
                </div>
                <div class="levels">
                    <div class="lvl lvl-sl">
                        <div class="lvl-val red">${real_stop:.2f}</div>
                        Stop Loss<br><span style="font-size:11px;">(-{risk_pct:.1f}%)</span>
                    </div>
                    <div class="lvl lvl-entry">
                        <div class="lvl-val">${real_price:.2f}</div>
                        Compra
                    </div>
                    <div class="lvl lvl-tp">
                        <div class="lvl-val green">${real_tp:.2f}</div>
                        Take Profit<br><span style="font-size:11px;">(+{tp_pct:.1f}%)</span>
                    </div>
                </div>
                <div style="font-size:11px;color:#999;margin-top:6px;text-align:center;">
                    Arriesgo ${risk * real_qty:,.0f} para ganar ${tp_gain * real_qty:,.0f} (ratio 2:1)
                </div>
            </div>"""
        action_html += f"""<div class="section"><h2>Compras de hoy</h2>{items}</div>"""

    # VENTAS
    if signals_exit:
        items = ""
        for symbol, reason, close, pos in signals_exit:
            # Use REAL Alpaca order data if available
            a_order = alpaca_sells_today.get(symbol)
            if a_order and a_order.get("filled_avg_price"):
                actual_close = float(a_order["filled_avg_price"])
                actual_qty = int(a_order.get("filled_qty", pos["qty"]))
            else:
                actual_close = close
                actual_qty = pos["qty"]

            # Use real entry price from Alpaca position history if possible
            # (the local DB might have synthetic prices)
            a_pos = alpaca_by_sym.get(symbol)
            if a_pos and a_pos.get("avg_entry_price"):
                actual_entry = float(a_pos["avg_entry_price"])
            else:
                # Check today's sell order — if we sold everything, position is gone
                # In that case, compute from account change or use local
                actual_entry = pos["entry_price"]

            pnl = (actual_close - actual_entry) * actual_qty
            pnl_cls = "green" if pnl >= 0 else "red"
            reason_map = {
                "E1_death_cross": "La tendencia se dio vuelta (la media de corto plazo cruzó abajo de la de largo plazo)",
                "E2_below_ema50": "El precio cayó por debajo de su tendencia de 50 días",
                "E3_stop_loss": "Tocó el Stop Loss (el límite de pérdida que habíamos puesto al comprar)",
                "E4_rsi_overbought": "Estaba sobrecomprado (subió demasiado rápido, mejor asegurar)",
            }
            reason_nice = reason_map.get(reason, reason)
            word = "Gané" if pnl >= 0 else "Perdí"
            change_pct = ((actual_close - actual_entry) / actual_entry) * 100 if actual_entry else 0
            items += f"""
            <div class="item">
                <span class="pill pill-sell">VENTA</span>
                <span class="sym" style="margin-left:8px;">{symbol}</span>
                <div class="detail">
                    Vendí <strong>{actual_qty} acciones</strong> a <strong>${actual_close:.2f}</strong><br>
                    Las había comprado a <strong>${actual_entry:.2f}</strong> ({change_pct:+.1f}%)<br>
                    Resultado: <strong class="{pnl_cls}">{word} ${abs(pnl):,.0f}</strong>
                </div>
                <hr class="divider">
                <div style="font-size:12px;color:#666;">
                    <strong>¿Por qué vendí?</strong> {reason_nice}
                </div>
            </div>"""
        action_html += f"""<div class="section"><h2>Ventas de hoy</h2>{items}</div>"""

    # NO OPERÓ
    if buys == 0 and sells == 0:
        closest_html = ""
        if watchlist_rows:
            scored = []
            for w in watchlist_rows:
                if w.get("is_held"):
                    continue
                score = sum([w.get("regime_ok", False), w.get("rsi_ok", False),
                             w.get("breakout_ok", False), w.get("volume_ok", False)])
                scored.append((w, score))
            scored.sort(key=lambda x: -x[1])
            top = scored[:3]
            if top:
                closest_html = '<div class="closest"><strong>Los que estuvieron más cerca de dar señal:</strong>'
                for w, sc in top:
                    pct = sc * 25
                    color = "#1b9e4b" if sc >= 3 else "#f39c12" if sc >= 2 else "#ddd"
                    missing = []
                    if not w.get("regime_ok"):
                        missing.append("tendencia bajista")
                    if not w.get("rsi_ok"):
                        missing.append("momentum fuera de rango")
                    if not w.get("breakout_ok"):
                        missing.append("no rompió máximos")
                    if not w.get("volume_ok"):
                        missing.append("poco volumen")
                    missing_str = ", ".join(missing) if missing else "—"
                    closest_html += f"""
                    <div style="margin-top:8px;">
                        <strong>{w['symbol']}</strong> — {sc} de 4 condiciones
                        <div class="bar-bg"><div class="bar" style="width:{pct}%;background:{color};"></div></div>
                        <span style="font-size:12px;color:#999;">Le falta: {missing_str}</span>
                    </div>"""
                closest_html += "</div>"

        action_html += f"""
        <div class="section">
            <h2>Hoy no operé</h2>
            <span class="pill pill-noop">SIN MOVIMIENTOS</span>
            <div class="noop-box">
                Para comprar, necesito que un ETF cumpla <strong>4 condiciones juntas</strong>:<br><br>
                1. Tendencia general alcista<br>
                2. Buen impulso (ni muy frío ni muy caliente)<br>
                3. Que esté rompiendo máximos recientes<br>
                4. Volumen alto (mucha gente operando)<br><br>
                Hoy ninguno las cumplió todas.
            </div>
            {closest_html}
        </div>"""

    # ── Posiciones abiertas — con precios REALES de Alpaca ───────────────────
    positions = get_open_positions(run_id)
    pos_html = ""
    if positions:
        items = ""
        for pos in positions:
            # Prefer real Alpaca price
            a_pos = alpaca_by_sym.get(pos["symbol"])
            if a_pos:
                curr = float(a_pos.get("current_price", 0))
                upnl = float(a_pos.get("unrealized_pl", 0))
            else:
                latest = get_latest_row(pos["symbol"])
                curr = latest["close"] if latest else pos["entry_price"]
                upnl = (curr - pos["entry_price"]) * pos["qty"]

            pnl_cls = "green" if upnl >= 0 else "red"
            pnl_word = "Ganando" if upnl >= 0 else "Perdiendo"
            change_pct = ((curr - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0

            # Take profit & stop loss levels
            tp = _calc_take_profit(pos["entry_price"], pos["stop_loss"])
            risk = pos["entry_price"] - pos["stop_loss"]
            reward = tp - pos["entry_price"]

            # Distance from current price to SL and TP
            dist_to_sl = ((curr - pos["stop_loss"]) / curr) * 100 if curr else 0
            dist_to_tp = ((tp - curr) / curr) * 100 if curr else 0

            items += f"""
            <div class="item">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="sym">{pos['symbol']}</span>
                    <span class="{pnl_cls}" style="font-size:18px;font-weight:700;">${upnl:+,.0f}</span>
                </div>
                <div class="detail">
                    {pos['qty']} acciones · Compré a ${pos['entry_price']:.2f} · Ahora a ${curr:.2f}
                    (<span class="{pnl_cls}">{change_pct:+.1f}%</span>)<br>
                    {pnl_word} ${abs(upnl):,.0f}
                </div>
                <div class="levels">
                    <div class="lvl lvl-sl">
                        <div class="lvl-val red">${pos['stop_loss']:.2f}</div>
                        Stop Loss<br><span style="font-size:11px;">a {dist_to_sl:.1f}% de distancia</span>
                    </div>
                    <div class="lvl lvl-entry">
                        <div class="lvl-val" style="color:#2980b9;">${curr:.2f}</div>
                        Precio actual
                    </div>
                    <div class="lvl lvl-tp">
                        <div class="lvl-val green">${tp:.2f}</div>
                        Take Profit<br><span style="font-size:11px;">a {dist_to_tp:.1f}% de distancia</span>
                    </div>
                </div>
            </div>"""
        pos_html = f"""<div class="section"><h2>Lo que tengo en cartera</h2>{items}</div>"""

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{css}</head>
<body><div class="wrap">
    {hero}
    {action_html}
    {pos_html}
    <div class="foot">RFTM Bot — reporte automático</div>
</div></body></html>"""

    return subject, body


def send_email_report(
    run_id: str,
    result: dict,
    signals_enter: list,
    signals_exit: list,
    watchlist_rows: list[dict],
    dry_run: bool,
) -> None:
    """Send the daily trading report via email."""
    if not EMAIL_ENABLED:
        info("Email notifications disabled (EMAIL_ENABLED=false)")
        return
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        warn("Email not configured — set EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO in .env.paper")
        return

    try:
        subject, body = _build_email_report(
            run_id, result, signals_enter, signals_exit, watchlist_rows, dry_run
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        ok(f"Email report sent to {EMAIL_TO}")
    except Exception as e:
        err(f"Failed to send email: {e}")


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

    watchlist_rows = []  # collect per-symbol data for email report

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

        # Collect watchlist data for email (inside the per-symbol loop)
        try:
            regime_ok  = bool(r["ema50"] and r["ema200"] and r["ema50"] > r["ema200"])
            rsi_val    = r["rsi14"] or 0
            rsi_ok     = 50 <= rsi_val <= 70
            breakout_ok = bool(r["close"] and r["high20"] and r["close"] > r["high20"])
            vol_ratio  = r["volume"] / r["vol_ma20"] if r["vol_ma20"] and r["vol_ma20"] > 0 else 0
            volume_ok  = vol_ratio > VOL_MULT
            watchlist_rows.append({
                "symbol": symbol, "close": r["close"] or 0,
                "regime_ok": regime_ok, "rsi_ok": rsi_ok, "rsi": rsi_val,
                "breakout_ok": breakout_ok, "volume_ok": volume_ok, "vol_ratio": vol_ratio,
                "is_held": symbol in open_symbols,
            })
        except Exception:
            pass

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

    result = {
        "enter": len(signals_enter),
        "exit":  len(signals_exit),
        "hold":  len(signals_hold),
        "orders_placed": orders_placed,
        "total_equity":  round(total, 2),
        "cash":          round(cash_now, 2),
        "positions_val": round(pos_val, 2),
        "drawdown":      round(drawdown, 4),
    }

    # ── Email report ─────────────────────────────────────────────────────────
    send_email_report(run_id, result, signals_enter, signals_exit, watchlist_rows, dry_run)

    return result


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

    # Sync local DB with Alpaca reality BEFORE doing anything else
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        hdr("Syncing with Alpaca")
        sync_with_alpaca(run_id)

    hdr("Loading Market Data")
    # Force refresh from Alpaca on first run with real keys to purge any stale
    # synthetic data that may have been cached from a previous keyless run
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        with get_db() as conn:
            # Check if we have obviously synthetic data (prices way off from reality)
            # by looking for any market_data row — if data exists but was never fetched
            # from Alpaca, force a refresh
            stale = conn.execute(
                "SELECT 1 FROM market_data LIMIT 1"
            ).fetchone()
            if stale and use_real:
                warn("Force-refreshing market data from Alpaca (purging stale cache)...")
                conn.execute("DELETE FROM market_data")

    for symbol in ETF_UNIVERSE:
        load_or_generate_data(symbol, use_real)

    result = run_pipeline(run_id, dry_run, use_real)

    if result.get("kill_switch"):
        err("Kill switch active — pipeline halted")
        return 1

    hdr("Summary")

    # Use real Alpaca data for summary if available
    summary_equity = result["total_equity"]
    summary_cash = result["cash"]
    summary_posval = result["positions_val"]
    summary_dd = result["drawdown"]
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        acct = alpaca_get_account()
        if acct:
            summary_equity = float(acct.get("equity", summary_equity))
            summary_cash = float(acct.get("cash", summary_cash))
            summary_posval = float(acct.get("long_market_value", summary_posval))
            peak = max(summary_equity, INITIAL_CAPITAL)
            summary_dd = (peak - summary_equity) / peak if peak > 0 else 0.0

    ret_pct = (summary_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL
    ret_color = C.GREEN if ret_pct >= 0 else C.RED
    dd_color = C.RED if summary_dd > 0.05 else C.GREEN

    print(f"  ENTER signals:     {C.GREEN}{result['enter']}{C.RESET}")
    print(f"  EXIT  signals:     {C.RED}{result['exit']}{C.RESET}")
    print(f"  HOLD:              {result['hold']}")
    print(f"  Orders placed:     {result['orders_placed']}")
    print(f"  Total equity:      {ret_color}${summary_equity:>12,.2f}  ({ret_pct:+.2%}){C.RESET}")
    print(f"  Cash:              ${summary_cash:>12,.2f}")
    print(f"  Positions value:   ${summary_posval:>12,.2f}")
    print(f"  Drawdown:          {dd_color}{summary_dd:.2%}{C.RESET}")
    print()

    if dry_run and (result["enter"] > 0 or result["exit"] > 0):
        print(f"  {C.YELLOW}To place real paper orders on Alpaca:{C.RESET}")
        print(f"  1. Edit .env.paper — add ALPACA_API_KEY + ALPACA_SECRET_KEY")
        print(f"  2. Run:  python3 standalone_paper_trader.py")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
