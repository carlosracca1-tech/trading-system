"""
scripts/seed_market_data.py
Synthetic market data seeder for dev/testing — no Polygon API key required.

Generates realistic OHLCV bars + computed indicators for all 18 ETFs
covering the last 3 years (enough to warm up EMA200).

Usage:
  python scripts/seed_market_data.py                  # seed all symbols
  python scripts/seed_market_data.py --symbol SPY     # seed one symbol
  python scripts/seed_market_data.py --days 300       # fewer bars (faster)
  python scripts/seed_market_data.py --dry-run        # validate only
  python scripts/seed_market_data.py --show           # show current coverage

This script is IDEMPOTENT — safe to re-run. Uses upsert (ON CONFLICT DO UPDATE).

WARNING: Data is synthetic / random-walk — NOT real prices. For dev/testing ONLY.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import uuid
from datetime import date, timedelta
from typing import Optional

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from packages.shared.constants import ETF_UNIVERSE, ETF_SYMBOLS
from packages.shared.logging_config import get_logger

log = get_logger(__name__)

# Seed prices for realistic starting points
_BASE_PRICES: dict[str, float] = {
    "SPY": 480.0,
    "QQQ": 420.0,
    "IWM": 200.0,
    "DIA": 380.0,
    "GLD": 185.0,
    "TLT": 95.0,
    "HYG": 77.0,
    "XLE": 90.0,
    "XLF": 39.0,
    "XLK": 195.0,
    "XLV": 140.0,
    "XLI": 115.0,
    "XLC": 73.0,
    "XLU": 65.0,
    "XLB": 88.0,
    "XLRE": 41.0,
    "EEM": 42.0,
    "EFA": 78.0,
}

# Annualised volatility estimates per symbol (for random-walk calibration)
_ANNUAL_VOL: dict[str, float] = {
    "SPY": 0.15,  "QQQ": 0.20,  "IWM": 0.22,  "DIA": 0.14,
    "GLD": 0.13,  "TLT": 0.12,  "HYG": 0.07,  "XLE": 0.30,
    "XLF": 0.20,  "XLK": 0.22,  "XLV": 0.14,  "XLI": 0.16,
    "XLC": 0.18,  "XLU": 0.14,  "XLB": 0.18,  "XLRE": 0.19,
    "EEM": 0.22,  "EFA": 0.16,
}


def _trading_days(n_days: int) -> list[date]:
    """Return the last n_days of trading dates (Mon–Fri, no weekend)."""
    today = date.today()
    days: list[date] = []
    d = today - timedelta(days=1)  # start from yesterday
    while len(days) < n_days:
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _generate_ohlcv(
    symbol: str,
    dates: list[date],
    *,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Simulate OHLCV bars using a geometric random-walk.

    The last half of the bars trend upward (bullish regime for strategy testing).
    Volume follows a lognormal distribution around 30M/day for broad ETFs.
    """
    rng = random.Random(seed + hash(symbol) % 1000)
    np_rng = np.random.default_rng(seed + hash(symbol) % 1000)

    base_price = _BASE_PRICES.get(symbol, 100.0)
    annual_vol = _ANNUAL_VOL.get(symbol, 0.18)
    daily_vol = annual_vol / math.sqrt(252)

    # Slight upward drift so strategy can find ENTER signals
    drift = 0.0003  # ~0.03% per day ≈ 7% annualised

    closes: list[float] = []
    price = base_price
    n = len(dates)

    for i in range(n):
        # Extra drift in last 40% of history to guarantee trend signals
        trend_drift = drift * 2 if i > int(n * 0.6) else drift
        ret = trend_drift + np_rng.normal(0, daily_vol)
        price *= (1 + ret)
        price = max(price, 1.0)
        closes.append(round(price, 4))

    rows = []
    for i, (d, close) in enumerate(zip(dates, closes)):
        daily_range = close * daily_vol * rng.uniform(0.5, 2.0)
        open_ = round(close * rng.uniform(1 - daily_vol, 1 + daily_vol), 4)
        high = round(max(open_, close) + daily_range * rng.uniform(0, 0.8), 4)
        low = round(min(open_, close) - daily_range * rng.uniform(0, 0.8), 4)
        low = max(low, 0.01)
        base_vol = 30_000_000 if symbol in ("SPY", "QQQ", "IWM", "DIA") else 8_000_000
        volume = int(abs(np_rng.lognormal(math.log(base_vol), 0.4)))

        rows.append({
            "date": d,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "vwap": round((open_ + high + low + close) / 4, 4),
            "num_trades": int(volume / 200),
        })

    return pd.DataFrame(rows)


def seed_symbol(
    symbol: str,
    n_days: int = 756,  # ~3 years
    dry_run: bool = False,
) -> dict:
    """
    Generate and insert synthetic OHLCV + indicators for one symbol.
    Returns {"bars": int, "indicators": int, "skipped": bool}.
    """
    from sqlalchemy import select
    from packages.shared.db import db_session
    from packages.shared.models.symbol import Symbol
    from apps.svc_data.indicators import compute_indicators
    from apps.svc_data.repository import (
        get_symbol_by_ticker,
        upsert_daily_bars,
        upsert_indicators,
    )

    with db_session() as session:
        sym_record = get_symbol_by_ticker(session, symbol)
        if sym_record is None:
            return {"bars": 0, "indicators": 0, "skipped": True, "reason": "not_in_symbols_table"}

        if dry_run:
            return {"bars": n_days, "indicators": n_days, "skipped": False}

        dates = _trading_days(n_days)
        df = _generate_ohlcv(symbol, dates)

        # Upsert bars
        bar_dicts = df.to_dict(orient="records")
        bars_upserted = upsert_daily_bars(
            session, sym_record.id, symbol, bar_dicts
        )

        # Compute + upsert indicators
        df_ind = compute_indicators(df)
        indicators_upserted = upsert_indicators(
            session, sym_record.id, symbol, df_ind
        )

        session.commit()

    return {"bars": bars_upserted, "indicators": indicators_upserted, "skipped": False}


def seed_all(n_days: int = 756, dry_run: bool = False) -> dict:
    """Seed all 18 ETFs. Returns summary dict."""
    results = {"ok": 0, "skipped": 0, "failed": 0, "total_bars": 0}
    for etf in ETF_UNIVERSE:
        symbol = etf["symbol"]
        try:
            r = seed_symbol(symbol, n_days=n_days, dry_run=dry_run)
            if r["skipped"]:
                results["skipped"] += 1
                if dry_run:
                    print(f"  SKIP  {symbol:<6}  {r.get('reason', '')}")
                else:
                    print(f"  SKIP  {symbol:<6}  not in symbols table — run 'make seed' first")
            else:
                results["ok"] += 1
                results["total_bars"] += r["bars"]
                marker = "(dry)" if dry_run else "ok"
                print(f"  {marker.upper():<6} {symbol:<6}  {r['bars']} bars, {r['indicators']} indicators")
        except Exception as exc:
            results["failed"] += 1
            print(f"  FAIL  {symbol:<6}  {exc}", file=sys.stderr)
    return results


def show_coverage() -> None:
    """Print current data coverage per symbol."""
    from packages.shared.db import db_session
    from apps.svc_data.repository import get_data_coverage

    with db_session() as session:
        rows = get_data_coverage(session)

    if not rows:
        print("  No data in market_data_daily.")
        return

    print(f"\n  {'Symbol':<8} {'Bars':>6} {'First':>12} {'Last':>12} {'Stale':>6}")
    print("  " + "─" * 50)
    for r in rows:
        stale = "YES" if r["is_stale"] else "no"
        print(
            f"  {r['symbol']:<8} {r['total_bars']:>6} "
            f"{str(r['first_date']):>12} {str(r['last_date']):>12} {stale:>6}"
        )
    print(f"\n  {len(rows)} symbols with data")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed synthetic OHLCV + indicator data for dev (no Polygon required)"
    )
    parser.add_argument("--symbol", "-s", help="Seed a single symbol (default: all 18)")
    parser.add_argument("--days", "-d", type=int, default=756,
                        help="Number of trading days to generate (default: 756 ≈ 3 years)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate only — no DB writes")
    parser.add_argument("--show", action="store_true",
                        help="Show current data coverage and exit")
    args = parser.parse_args(argv)

    if args.show:
        show_coverage()
        return 0

    if args.dry_run:
        print(f"\nDRY RUN — would generate {args.days} synthetic bars per symbol\n")

    target = args.symbol.upper() if args.symbol else None

    if target:
        if target not in ETF_SYMBOLS:
            print(f"ERROR: {target} not in ETF_UNIVERSE. Valid: {', '.join(ETF_SYMBOLS)}")
            return 1
        r = seed_symbol(target, n_days=args.days, dry_run=args.dry_run)
        if r["skipped"]:
            print(f"SKIP {target}: {r.get('reason', '')} — run 'make seed' first")
        else:
            action = "Would insert" if args.dry_run else "Inserted"
            print(f"{action} {r['bars']} bars + {r['indicators']} indicators for {target}")
        return 0

    # All symbols
    print(f"\nSeeding synthetic market data ({args.days} days per symbol)...\n")
    r = seed_all(n_days=args.days, dry_run=args.dry_run)
    print(f"\n  OK: {r['ok']}  Skipped: {r['skipped']}  Failed: {r['failed']}  Total bars: {r['total_bars']}")
    return 0 if r["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
