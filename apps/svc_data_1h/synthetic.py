"""
apps/svc_data_1h/synthetic.py
Synthetic 1H OHLCV data generator for development and testing.

Generates realistic-looking 1-hour candles with:
  - Geometric random walk with configurable drift and volatility
  - Realistic high/low spreads relative to close
  - Volume with random variation
  - Mean-reverting patterns injected to test the strategy

No API keys required — perfect for local dev and CI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


def generate_1h_ohlcv(
    symbol: str = "BTC/USD",
    bars: int = 500,
    start_price: float = 60000.0,
    hourly_volatility: float = 0.008,
    drift: float = 0.0001,
    start_datetime: datetime | None = None,
    seed: int | None = None,
    inject_mean_reversion: bool = True,
) -> pd.DataFrame:
    """
    Generate synthetic 1-hour OHLCV data for testing.

    Args:
        symbol:              ticker symbol (used for labeling only)
        bars:                number of hourly bars to generate
        start_price:         starting close price
        hourly_volatility:   per-bar volatility (σ of log returns)
        drift:               per-bar drift (μ of log returns)
        start_datetime:      first bar timestamp (defaults to UTC now - bars hours)
        seed:                numpy random seed for reproducibility
        inject_mean_reversion: if True, injects 3-5 oversold dips for testing

    Returns:
        DataFrame with columns: [datetime, symbol, open, high, low, close, volume]
    """
    if seed is not None:
        np.random.seed(seed)

    if start_datetime is None:
        start_datetime = datetime.now(tz=timezone.utc) - timedelta(hours=bars)

    # Generate log returns
    log_returns = np.random.normal(drift, hourly_volatility, bars)

    # Inject mean-reversion dips (RSI will drop, price hits lower BB)
    if inject_mean_reversion and bars >= 100:
        dip_positions = np.linspace(50, bars - 30, 4).astype(int)
        for pos in dip_positions:
            # Sharp drop over 5-8 bars followed by recovery
            dip_len = np.random.randint(5, 9)
            end = min(pos + dip_len, bars)
            log_returns[pos:end] = np.random.normal(-0.015, 0.005, end - pos)
            # Recovery
            rec_end = min(end + dip_len, bars)
            log_returns[end:rec_end] = np.random.normal(0.008, 0.004, rec_end - end)

    # Build price series from log returns
    cumulative = np.cumsum(log_returns)
    closes = start_price * np.exp(cumulative)

    # Generate OHLV from close
    spread_pct = hourly_volatility * 0.8
    highs = closes * (1 + np.abs(np.random.normal(0, spread_pct, bars)))
    lows = closes * (1 - np.abs(np.random.normal(0, spread_pct, bars)))
    opens = np.roll(closes, 1)
    opens[0] = start_price

    # Ensure high >= max(open, close) and low <= min(open, close)
    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))

    # Volume with variation
    base_volume = 1000 if "USD" in symbol else 1_000_000
    volumes = np.random.lognormal(
        mean=np.log(base_volume), sigma=0.5, size=bars
    ).astype(int)

    # Timestamps
    datetimes = [start_datetime + timedelta(hours=i) for i in range(bars)]

    df = pd.DataFrame({
        "datetime": datetimes,
        "symbol": symbol,
        "open": np.round(opens, 2),
        "high": np.round(highs, 2),
        "low": np.round(lows, 2),
        "close": np.round(closes, 2),
        "volume": volumes,
    })

    return df


def generate_multi_symbol_1h(
    symbols: list[str] | None = None,
    bars: int = 500,
    seed: int = 42,
) -> dict[str, pd.DataFrame]:
    """
    Generate synthetic 1H data for multiple symbols.

    Args:
        symbols: list of ticker symbols; defaults to MREV universe
        bars:    number of bars per symbol
        seed:    base seed (incremented per symbol for variety)

    Returns:
        {symbol: DataFrame} dict
    """
    if symbols is None:
        symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "SPY", "QQQ", "IWM"]

    start_prices = {
        "BTC/USD": 60000.0,
        "ETH/USD": 3200.0,
        "SOL/USD": 150.0,
        "SPY": 520.0,
        "QQQ": 450.0,
        "IWM": 210.0,
    }

    volatilities = {
        "BTC/USD": 0.012,
        "ETH/USD": 0.015,
        "SOL/USD": 0.020,
        "SPY": 0.004,
        "QQQ": 0.005,
        "IWM": 0.006,
    }

    result = {}
    for i, sym in enumerate(symbols):
        result[sym] = generate_1h_ohlcv(
            symbol=sym,
            bars=bars,
            start_price=start_prices.get(sym, 100.0),
            hourly_volatility=volatilities.get(sym, 0.008),
            seed=seed + i,
            inject_mean_reversion=True,
        )

    return result
