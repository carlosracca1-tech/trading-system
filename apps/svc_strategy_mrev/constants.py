"""
Mean Reversion 1H Strategy — constants and universe.

Strategy: MREV-1H (Mean Reversion on 1-Hour candles)
=====================================================
A higher-frequency, more aggressive strategy that trades mean-reversion
setups on 1-hour candles across crypto (24/7) and liquid ETFs (market hours).

Designed to run IN PARALLEL with the daily RFTM strategy on separate capital.
"""
from __future__ import annotations

# ── MREV Universe — crypto + liquid ETFs ─────────────────────────────────────
# Crypto trades 24/7 (ideal for 1H candles), ETFs only during market hours.

MREV_UNIVERSE: list[dict] = [
    # Crypto — 24/7 markets, high volatility
    {"symbol": "BTC/USD",  "name": "Bitcoin",          "asset_type": "crypto", "sector": "crypto_major"},
    {"symbol": "ETH/USD",  "name": "Ethereum",         "asset_type": "crypto", "sector": "crypto_major"},
    {"symbol": "SOL/USD",  "name": "Solana",           "asset_type": "crypto", "sector": "crypto_alt"},
    # Liquid ETFs — market hours only
    {"symbol": "SPY",      "name": "S&P 500 ETF",      "asset_type": "etf",    "sector": "index_us_large"},
    {"symbol": "QQQ",      "name": "Nasdaq 100 ETF",   "asset_type": "etf",    "sector": "index_us_tech"},
    {"symbol": "IWM",      "name": "Russell 2000 ETF", "asset_type": "etf",    "sector": "index_us_small"},
]

MREV_SYMBOLS: list[str] = [e["symbol"] for e in MREV_UNIVERSE]
MREV_CRYPTO_SYMBOLS: list[str] = [e["symbol"] for e in MREV_UNIVERSE if e["asset_type"] == "crypto"]
MREV_ETF_SYMBOLS: list[str] = [e["symbol"] for e in MREV_UNIVERSE if e["asset_type"] == "etf"]

# ── Strategy parameters (1H Mean Reversion) ─────────────────────────────────
# Bollinger Bands + RSI oversold/overbought detection.
# More aggressive than RFTM: wider RSI thresholds, tighter stops.

MREV_STRATEGY_PARAMS: dict = {
    # Bollinger Bands
    "bb_period": 20,            # SMA period for middle band
    "bb_std_dev": 2.0,          # standard deviations for upper/lower bands

    # RSI
    "rsi_period": 14,
    "rsi_oversold": 30,         # BUY when RSI ≤ this
    "rsi_overbought": 70,       # SHORT when RSI ≥ this (v2 — long-only for now)

    # ATR for stops and volatility filter
    "atr_period": 14,
    "atr_rel_min": 0.003,       # min ATR/close — filter out dead assets
    "atr_rel_max": 0.10,        # max ATR/close — filter out extreme volatility

    # Volume confirmation
    "volume_ma_period": 20,
    "volume_multiplier": 1.0,   # relaxed vs RFTM's 1.2

    # Exit parameters
    "take_profit_target": "sma",  # exit when price crosses back to SMA(20) middle band
    "stop_atr_multiplier": 1.5,   # tighter stop than RFTM's 2.0 (more aggressive)
    "rsi_exit_normalized_min": 40,  # exit when RSI normalizes to 40-60
    "rsi_exit_normalized_max": 60,
    "max_hold_bars": 24,          # time stop: max 24 hourly bars (≈1 day)

    # Cooldown
    "cooldown_bars": 3,           # wait 3 bars after closing before re-entering same symbol
}

# ── Risk parameters (more aggressive than RFTM) ─────────────────────────────
MREV_RISK_PARAMS: dict = {
    "risk_per_trade": 0.02,       # 2% of allocated capital per trade (vs RFTM's 1%)
    "max_position_pct": 0.25,     # 25% of capital per position (vs RFTM's 10%)
    "max_positions": 4,           # max 4 concurrent positions (vs RFTM's 10)
    "max_drawdown_pct": 0.20,     # 20% drawdown → kill switch
    "stop_atr_multiplier": 1.5,   # ATR-based stop multiplier
    "min_order_usd": 10.0,        # minimum order size $10 (for small capital)
}

# ── Crypto fractional sizing ─────────────────────────────────────────────────
# Unlike ETFs (whole shares), crypto supports fractional quantities.
CRYPTO_MIN_QTY: dict = {
    "BTC/USD": 0.0001,   # ~$6 at $60k BTC
    "ETH/USD": 0.001,    # ~$3 at $3k ETH
    "SOL/USD": 0.01,     # ~$1.5 at $150 SOL
}

# ── Cost model for 1H trading ────────────────────────────────────────────────
MREV_COSTS: dict = {
    "crypto_maker_fee": 0.0015,   # 0.15% Alpaca crypto fee
    "crypto_taker_fee": 0.0025,   # 0.25% Alpaca crypto fee
    "etf_slippage_pct": 0.0005,   # 0.05% slippage for ETFs
    "etf_commission": 0.0,        # Alpaca is commission-free for ETFs
}
