"""
apps/svc_risk_mrev/position_sizer.py
ATR-based position sizing for the MREV-1H strategy.

Key differences from RFTM position sizer:
  - 2% risk per trade (vs 1%)
  - 25% max position size (vs 10%)
  - Supports fractional quantities for crypto
  - Tighter stop: 1.5 × ATR (vs 2.0)
  - Minimum order size in USD (for small capital)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from apps.svc_strategy_mrev.constants import (
    CRYPTO_MIN_QTY,
    MREV_RISK_PARAMS,
)

PARAMS = MREV_RISK_PARAMS


@dataclass
class MrevSizingResult:
    """Output of the MREV position sizer."""
    qty: float                        # quantity to buy (fractional for crypto)
    stop_price: float                 # stop-loss price level
    risk_amount: float                # $ risked (qty × stop_distance)
    notional_value: float             # qty × close_price
    pct_of_portfolio: float           # notional / portfolio_value
    rejection_reason: Optional[str]   # None = accepted, else the blocking reason
    is_crypto: bool = False           # True if fractional sizing was used


def calculate_mrev_position_size(
    portfolio_value: float,
    close_price: float,
    atr_14: float,
    symbol: str = "",
    *,
    risk_pct_per_trade: float = PARAMS["risk_per_trade"],
    atr_stop_multiplier: float = PARAMS["stop_atr_multiplier"],
    max_position_pct: float = PARAMS["max_position_pct"],
    min_order_usd: float = PARAMS["min_order_usd"],
) -> MrevSizingResult:
    """
    Compute position size for MREV-1H using ATR-based risk management.

    Supports both whole shares (ETFs) and fractional quantities (crypto).

    Args:
        portfolio_value:    current total allocated equity in USD
        close_price:        current close price
        atr_14:             14-period ATR
        symbol:             ticker symbol (used to detect crypto for fractional sizing)
        risk_pct_per_trade: fraction of portfolio to risk per trade (default 2%)
        atr_stop_multiplier: ATR multiplier for the hard stop (default 1.5)
        max_position_pct:   max single position as fraction of portfolio (default 25%)
        min_order_usd:      minimum order size in USD (default $10)

    Returns:
        MrevSizingResult with qty=0 and rejection_reason set if trade is not viable.
    """
    is_crypto = "/" in symbol  # BTC/USD, ETH/USD, SOL/USD

    # ── Guard rails ──────────────────────────────────────────────────────────
    if portfolio_value <= 0:
        return _rejected(0.0, "portfolio_value_non_positive", is_crypto)
    if close_price <= 0:
        return _rejected(0.0, "close_price_non_positive", is_crypto)
    if atr_14 <= 0:
        return _rejected(close_price, "atr_non_positive", is_crypto)

    # ── Core calculation ─────────────────────────────────────────────────────
    stop_distance = atr_stop_multiplier * atr_14
    stop_price = close_price - stop_distance

    risk_amount = portfolio_value * risk_pct_per_trade
    qty_risk_based = risk_amount / stop_distance

    max_notional = portfolio_value * max_position_pct
    qty_capped = max_notional / close_price

    raw_qty = min(qty_risk_based, qty_capped)

    # ── Round based on asset type ────────────────────────────────────────────
    if is_crypto:
        min_qty = CRYPTO_MIN_QTY.get(symbol, 0.0001)
        # Round down to min_qty precision
        precision = len(str(min_qty).rstrip("0").split(".")[-1])
        qty = round(math.floor(raw_qty / min_qty) * min_qty, precision)
    else:
        qty = math.floor(raw_qty)  # whole shares for ETFs

    # ── Minimum viable check ─────────────────────────────────────────────────
    notional = qty * close_price
    if notional < min_order_usd:
        return MrevSizingResult(
            qty=0,
            stop_price=stop_price,
            risk_amount=0.0,
            notional_value=0.0,
            pct_of_portfolio=0.0,
            rejection_reason=f"order_below_minimum_usd:{notional:.2f}<{min_order_usd}",
            is_crypto=is_crypto,
        )

    if qty <= 0:
        return MrevSizingResult(
            qty=0,
            stop_price=stop_price,
            risk_amount=0.0,
            notional_value=0.0,
            pct_of_portfolio=0.0,
            rejection_reason="position_size_rounds_to_zero",
            is_crypto=is_crypto,
        )

    return MrevSizingResult(
        qty=qty,
        stop_price=round(stop_price, 6),
        risk_amount=round(qty * stop_distance, 4),
        notional_value=round(notional, 4),
        pct_of_portfolio=round(notional / portfolio_value, 6),
        rejection_reason=None,
        is_crypto=is_crypto,
    )


def _rejected(stop_price: float, reason: str, is_crypto: bool) -> MrevSizingResult:
    return MrevSizingResult(
        qty=0,
        stop_price=stop_price,
        risk_amount=0.0,
        notional_value=0.0,
        pct_of_portfolio=0.0,
        rejection_reason=reason,
        is_crypto=is_crypto,
    )
