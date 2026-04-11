"""
apps/svc_risk/position_sizer.py
ATR-based position sizing for the RFTM strategy.

Methodology
-----------
Risk per trade = RISK_PCT_PER_TRADE × portfolio_value
Stop distance  = ATR_STOP_MULTIPLIER × atr_14
Shares (risk)  = risk_per_trade / stop_distance
Shares (cap)   = (MAX_POSITION_PCT × portfolio_value) / close_price
Final shares   = floor(min(shares_risk, shares_cap))

The floor ensures whole-share quantities only.
A result of 0 shares means the trade is not executable at current prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# ── Default risk parameters (AGGRESSIVE — 8/10 risk profile) ─────────────────
RISK_PARAMS = {
    "RISK_PCT_PER_TRADE": 0.03,     # 3 % of portfolio risked per trade (was 1%)
    "ATR_STOP_MULTIPLIER": 1.5,     # stop = entry_price - 1.5 × ATR14 (was 2.0)
    "MAX_POSITION_PCT": 0.25,       # single position ≤ 25 % of portfolio (was 10%)
    "MIN_SHARES": 1,                # minimum viable order size
}


@dataclass
class SizingResult:
    """Output of the position sizer."""
    shares: int                       # whole shares to buy
    stop_price: float                 # stop-loss price level
    risk_amount: float                # $ risked (shares × stop_distance)
    notional_value: float             # shares × close_price
    pct_of_portfolio: float           # notional / portfolio_value
    rejection_reason: Optional[str]   # None = accepted, else the blocking reason


def calculate_position_size(
    portfolio_value: float,
    close_price: float,
    atr_14: float,
    *,
    risk_pct_per_trade: float = RISK_PARAMS["RISK_PCT_PER_TRADE"],
    atr_stop_multiplier: float = RISK_PARAMS["ATR_STOP_MULTIPLIER"],
    max_position_pct: float = RISK_PARAMS["MAX_POSITION_PCT"],
    fundamental_multiplier: float = 1.0,
) -> SizingResult:
    """
    Compute whole-share position size using ATR-based risk management.

    Args:
        portfolio_value:       current total portfolio equity in USD
        close_price:           current close price of the ETF
        atr_14:                14-period ATR for the ETF
        risk_pct_per_trade:    fraction of portfolio to risk per trade (default 3 %)
        atr_stop_multiplier:   ATR multiplier for the hard stop (default 1.5)
        max_position_pct:      max single position as fraction of portfolio (default 25 %)
        fundamental_multiplier: 0.5-1.0 multiplier from FundamentalChecker sentiment

    Returns:
        SizingResult with shares=0 and rejection_reason set if trade is not viable.
    """
    # ── Guard rails ──────────────────────────────────────────────────────────
    if portfolio_value <= 0:
        return _rejected(0.0, "portfolio_value_non_positive")
    if close_price <= 0:
        return _rejected(0.0, "close_price_non_positive")
    if atr_14 <= 0:
        return _rejected(close_price, "atr_non_positive")

    # ── Core calculation ─────────────────────────────────────────────────────
    stop_distance = atr_stop_multiplier * atr_14
    stop_price = close_price - stop_distance

    risk_amount = portfolio_value * risk_pct_per_trade
    shares_risk_based = risk_amount / stop_distance

    max_notional = portfolio_value * max_position_pct
    shares_capped = max_notional / close_price

    raw_shares = min(shares_risk_based, shares_capped)
    # Apply fundamental sentiment multiplier (reduces size in fearful markets)
    raw_shares *= fundamental_multiplier
    shares = math.floor(raw_shares)

    if shares < RISK_PARAMS["MIN_SHARES"]:
        return SizingResult(
            shares=0,
            stop_price=stop_price,
            risk_amount=0.0,
            notional_value=0.0,
            pct_of_portfolio=0.0,
            rejection_reason="position_size_rounds_to_zero",
        )

    notional = shares * close_price
    return SizingResult(
        shares=shares,
        stop_price=round(stop_price, 4),
        risk_amount=round(shares * stop_distance, 4),
        notional_value=round(notional, 4),
        pct_of_portfolio=round(notional / portfolio_value, 6),
        rejection_reason=None,
    )


def _rejected(stop_price: float, reason: str) -> SizingResult:
    return SizingResult(
        shares=0,
        stop_price=stop_price,
        risk_amount=0.0,
        notional_value=0.0,
        pct_of_portfolio=0.0,
        rejection_reason=reason,
    )
