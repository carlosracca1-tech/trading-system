"""
System-wide constants.
ETF_UNIVERSE is the definitive list of V1 tradeable assets.
"""
from __future__ import annotations

# ── V1 Universe — 18 ETFs core ───────────────────────────────────────────────
# This list is the whitelist used by the Risk Engine.
# No asset outside this list will ever receive an ENTER signal.
ETF_UNIVERSE: list[dict] = [
    {"symbol": "SPY",  "name": "SPDR S&P 500 ETF",               "sector": "index_us_large"},
    {"symbol": "QQQ",  "name": "Invesco QQQ Trust",               "sector": "index_us_tech"},
    {"symbol": "IWM",  "name": "iShares Russell 2000 ETF",        "sector": "index_us_small"},
    {"symbol": "DIA",  "name": "SPDR Dow Jones Industrial ETF",   "sector": "index_us_large"},
    {"symbol": "GLD",  "name": "SPDR Gold Shares",                "sector": "commodity_gold"},
    {"symbol": "TLT",  "name": "iShares 20+ Year Treasury ETF",   "sector": "fixed_income"},
    {"symbol": "HYG",  "name": "iShares iBoxx High Yield Corp ETF","sector": "fixed_income"},
    {"symbol": "XLE",  "name": "Energy Select Sector SPDR",       "sector": "sector_energy"},
    {"symbol": "XLF",  "name": "Financial Select Sector SPDR",    "sector": "sector_financials"},
    {"symbol": "XLK",  "name": "Technology Select Sector SPDR",   "sector": "sector_tech"},
    {"symbol": "XLV",  "name": "Health Care Select Sector SPDR",  "sector": "sector_health"},
    {"symbol": "XLI",  "name": "Industrial Select Sector SPDR",   "sector": "sector_industrials"},
    {"symbol": "XLC",  "name": "Communication Services SPDR",     "sector": "sector_comms"},
    {"symbol": "XLU",  "name": "Utilities Select Sector SPDR",    "sector": "sector_utilities"},
    {"symbol": "XLB",  "name": "Materials Select Sector SPDR",    "sector": "sector_materials"},
    {"symbol": "XLRE", "name": "Real Estate Select Sector SPDR",  "sector": "sector_realestate"},
    {"symbol": "EEM",  "name": "iShares MSCI Emerging Markets ETF","sector": "index_em"},
    {"symbol": "EFA",  "name": "iShares MSCI EAFE ETF",           "sector": "index_intl"},
]

ETF_SYMBOLS: list[str] = [e["symbol"] for e in ETF_UNIVERSE]

# SPY is used as the market regime benchmark
REGIME_BENCHMARK: str = "SPY"

# ── Strategy parameters (fixed — do not change without re-running backtest) ───
STRATEGY_PARAMS: dict = {
    "ema_short": 50,
    "ema_long": 200,
    "rsi_period": 14,
    "rsi_min_entry": 50,
    "rsi_max_entry": 70,
    "rsi_exit_threshold": 40,
    "breakout_period": 20,
    "volume_multiplier": 1.2,
    "atr_period": 14,
    "atr_rel_min": 0.01,
    "atr_rel_max": 0.05,
    "stop_atr_multiplier": 2.0,
    "cooldown_days": 5,
}

# ── Risk parameters (fixed) ───────────────────────────────────────────────────
RISK_PARAMS: dict = {
    "risk_per_trade": 0.01,          # 1% of NAV
    "max_position_pct": 0.05,        # 5% of NAV cap per position
    "max_positions": 10,
    "max_exposure_pct": 0.50,        # 50% of NAV max total exposure
    "max_drawdown_warning": 0.15,    # 15% → reduce sizing 50%
    "max_drawdown_stop": 0.20,       # 20% → kill switch
    "daily_risk_limit": 0.03,        # 3% of NAV max daily loss
    "weekly_risk_limit": 0.05,       # 5% of NAV max weekly loss
    "loss_streak_limit": 5,          # consecutive losses → pause
    "vix_warning_threshold": 30,
    "vix_critical_threshold": 40,
    "correlation_threshold": 0.75,   # block new position if corr > this
    "earnings_proximity_days": 5,
    "sizing_reduction_factor": 0.50, # 50% sizing in WARNING mode
}

# ── Backtest cost model ───────────────────────────────────────────────────────
BACKTEST_COSTS: dict = {
    "slippage_per_share": 0.01,       # $0.01 per share
    "slippage_pct": 0.0005,           # 0.05% of notional
    "commission_per_share": 0.005,    # $0.005 per share
}

# ── Market hours (NYSE) ───────────────────────────────────────────────────────
NYSE_OPEN_HOUR_ET = 9
NYSE_OPEN_MINUTE_ET = 30
NYSE_CLOSE_HOUR_ET = 16
NYSE_CLOSE_MINUTE_ET = 0
NYSE_TIMEZONE = "America/New_York"

# ── API ───────────────────────────────────────────────────────────────────────
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"
