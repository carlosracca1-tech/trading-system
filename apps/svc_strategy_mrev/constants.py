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
    {"symbol": "BTC/USD",   "name": "Bitcoin",          "asset_type": "crypto", "sector": "crypto_major"},
    {"symbol": "ETH/USD",   "name": "Ethereum",         "asset_type": "crypto", "sector": "crypto_major"},
    {"symbol": "SOL/USD",   "name": "Solana",           "asset_type": "crypto", "sector": "crypto_alt"},
    {"symbol": "AVAX/USD",  "name": "Avalanche",        "asset_type": "crypto", "sector": "crypto_alt"},
    {"symbol": "DOGE/USD",  "name": "Dogecoin",         "asset_type": "crypto", "sector": "crypto_meme"},
    {"symbol": "LINK/USD",  "name": "Chainlink",        "asset_type": "crypto", "sector": "crypto_defi"},
    # Liquid ETFs — market hours only
    {"symbol": "SPY",       "name": "S&P 500 ETF",      "asset_type": "etf",    "sector": "index_us_large"},
    {"symbol": "QQQ",       "name": "Nasdaq 100 ETF",   "asset_type": "etf",    "sector": "index_us_tech"},
    {"symbol": "IWM",       "name": "Russell 2000 ETF", "asset_type": "etf",    "sector": "index_us_small"},
    {"symbol": "XLE",       "name": "Energy Select",    "asset_type": "etf",    "sector": "sector_energy"},
    {"symbol": "XLF",       "name": "Financial Select", "asset_type": "etf",    "sector": "sector_financial"},
    {"symbol": "GLD",       "name": "Gold ETF",         "asset_type": "etf",    "sector": "commodity_gold"},
    {"symbol": "SLV",       "name": "Silver ETF",       "asset_type": "etf",    "sector": "commodity_silver"},
    {"symbol": "BITO",      "name": "Bitcoin Strategy",  "asset_type": "etf",    "sector": "crypto_etf"},
    {"symbol": "ARKK",      "name": "ARK Innovation",   "asset_type": "etf",    "sector": "thematic_disruptive"},
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
    "rsi_oversold": 35,         # BUY when RSI ≤ this (was 30 — wider entry window)
    "rsi_overbought": 70,       # SHORT when RSI ≥ this (v2 — long-only for now)

    # ATR for stops and volatility filter
    "atr_period": 14,
    "atr_rel_min": 0.002,       # min ATR/close — accept calmer assets (was 0.003)
    "atr_rel_max": 0.15,        # max ATR/close — accept higher vol (was 0.10)

    # Volume confirmation
    "volume_ma_period": 20,
    "volume_multiplier": 0.7,   # more relaxed (was 1.0) — don't miss entries

    # Exit parameters
    "take_profit_target": "sma_plus_atr",  # exit at SMA(20) + 1×ATR (was just SMA)
    "stop_atr_multiplier": 2.0,   # wider stop for more room (was 1.5)
    "max_hold_bars": 96,          # time stop: 96 hourly bars ≈ 4 days (was 24)
    "trailing_distance_atr": 0.75,  # NEW: trailing stop at 0.75×ATR once profitable

    # Cooldown
    "cooldown_bars": 2,           # faster re-entry (was 3)
}

# ── Risk parameters (more aggressive than RFTM) ─────────────────────────────
MREV_RISK_PARAMS: dict = {
    "risk_per_trade": 0.04,       # 4% of allocated capital per trade (was 2%)
    "max_position_pct": 0.35,     # 35% of capital per position (was 25%)
    "max_positions": 6,           # max 6 concurrent positions (was 4 — wider universe)
    "max_drawdown_pct": 0.25,     # 25% drawdown → kill switch (was 20%)
    "stop_atr_multiplier": 2.0,   # ATR-based stop multiplier (was 1.5 — more room)
    "min_order_usd": 10.0,        # minimum order size $10 (for small capital)
}

# ── Crypto fractional sizing ─────────────────────────────────────────────────
# Unlike ETFs (whole shares), crypto supports fractional quantities.
CRYPTO_MIN_QTY: dict = {
    "BTC/USD":  0.0001,   # ~$6 at $60k BTC
    "ETH/USD":  0.001,    # ~$3 at $3k ETH
    "SOL/USD":  0.01,     # ~$1.5 at $150 SOL
    "AVAX/USD": 0.01,     # ~$0.35 at $35 AVAX
    "DOGE/USD": 1.0,      # ~$0.15 at $0.15 DOGE
    "LINK/USD": 0.01,     # ~$0.15 at $15 LINK
}

# ── Cost model for 1H trading ────────────────────────────────────────────────
MREV_COSTS: dict = {
    "crypto_maker_fee": 0.0015,   # 0.15% Alpaca crypto fee
    "crypto_taker_fee": 0.0025,   # 0.25% Alpaca crypto fee
    "etf_slippage_pct": 0.0005,   # 0.05% slippage for ETFs
    "etf_commission": 0.0,        # Alpaca is commission-free for ETFs
}
