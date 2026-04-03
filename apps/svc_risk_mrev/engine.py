"""
apps/svc_risk_mrev/engine.py
Risk Engine for MREV-1H — orchestrates rule evaluation for each signal.

Adapted from the RFTM risk engine with more aggressive parameters:
  - P1: Max drawdown 20% (vs RFTM 15%)
  - P2: Max 4 concurrent positions (vs RFTM 10)
  - P3: Max 25% per position (vs RFTM 10%)
  - P4: Min order $10 (vs RFTM 1 share)

EXIT signals bypass all rules and are auto-approved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apps.svc_risk_mrev.position_sizer import (
    MrevSizingResult,
    calculate_mrev_position_size,
)
from apps.svc_strategy_mrev.constants import MREV_RISK_PARAMS
from apps.svc_strategy_mrev.scanner import MrevSignalDecision
from packages.shared.enums import RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)

PARAMS = MREV_RISK_PARAMS


# ── Portfolio state snapshot ─────────────────────────────────────────────────

@dataclass
class MrevPortfolioState:
    """Lightweight snapshot of MREV portfolio at evaluation time."""
    total_equity: float          # current mark-to-market equity
    peak_equity: float           # highest equity in this run
    open_position_count: int     # number of currently open positions
    cash: float                  # available cash


# ── Evaluation result ────────────────────────────────────────────────────────

@dataclass
class MrevEvaluationResult:
    """Output of evaluate_mrev_signal()."""
    decision: str                              # RiskDecision.value
    rule_code: Optional[str]                   # which rule fired (if rejected)
    rejection_reason: Optional[str]            # human-readable rejection detail
    sizing: Optional[MrevSizingResult] = None  # populated for approved ENTER signals


# ── Rule checks (inline — simpler than RFTM class-based approach) ────────────

def _check_drawdown(peak_equity: float, current_equity: float) -> tuple[bool, str]:
    """P1: Kill switch on excessive drawdown."""
    if peak_equity <= 0:
        return True, ""
    drawdown = (peak_equity - current_equity) / peak_equity
    if drawdown >= PARAMS["max_drawdown_pct"]:
        return False, f"drawdown_{drawdown:.2%}_exceeds_{PARAMS['max_drawdown_pct']:.0%}"
    return True, ""


def _check_max_positions(open_count: int) -> tuple[bool, str]:
    """P2: Max concurrent positions."""
    if open_count >= PARAMS["max_positions"]:
        return False, f"open_{open_count}_at_max_{PARAMS['max_positions']}"
    return True, ""


def _check_position_size(notional: float, portfolio_value: float) -> tuple[bool, str]:
    """P3: Max single position size."""
    if portfolio_value <= 0:
        return False, "portfolio_value_non_positive"
    pct = notional / portfolio_value
    if pct > PARAMS["max_position_pct"]:
        return False, f"position_{pct:.2%}_exceeds_{PARAMS['max_position_pct']:.0%}"
    return True, ""


def _check_min_order(sizing: MrevSizingResult) -> tuple[bool, str]:
    """P4: Minimum viable order."""
    if sizing.qty <= 0 or sizing.notional_value < PARAMS["min_order_usd"]:
        reason = sizing.rejection_reason or "order_too_small"
        return False, reason
    return True, ""


# ── Main evaluation function ─────────────────────────────────────────────────

def evaluate_mrev_signal(
    signal: MrevSignalDecision,
    portfolio: MrevPortfolioState,
) -> MrevEvaluationResult:
    """
    Apply all MREV risk rules to a signal.

    EXIT → always APPROVED.
    HOLD → DEFERRED (no action).
    ENTER → full rule chain P1-P4.
    """
    # EXIT: always approved
    if signal.signal_type == SignalType.EXIT.value:
        log.debug("mrev_risk_auto_approved_exit", symbol=signal.symbol)
        return MrevEvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None,
            rejection_reason=None,
        )

    # HOLD: no action
    if signal.signal_type == SignalType.HOLD.value:
        return MrevEvaluationResult(
            decision=RiskDecision.DEFERRED.value,
            rule_code=None,
            rejection_reason=None,
        )

    # ENTER: full evaluation
    assert signal.signal_type == SignalType.ENTER.value

    # Need ATR for sizing
    if signal.atr_14 is None or signal.atr_14 <= 0:
        return MrevEvaluationResult(
            decision=RiskDecision.REJECTED.value,
            rule_code="P4_MIN_ORDER",
            rejection_reason="atr_14_missing_or_zero",
        )

    # Step 1: Size the position
    sizing = calculate_mrev_position_size(
        portfolio_value=portfolio.total_equity,
        close_price=signal.close_price,
        atr_14=signal.atr_14,
        symbol=signal.symbol,
    )

    # Step 2: P1 — drawdown check
    ok, reason = _check_drawdown(portfolio.peak_equity, portfolio.total_equity)
    if not ok:
        log.warning("mrev_risk_p1_drawdown", symbol=signal.symbol, reason=reason)
        return _rejected("P1_MAX_DRAWDOWN", reason)

    # Step 3: P2 — max positions
    ok, reason = _check_max_positions(portfolio.open_position_count)
    if not ok:
        log.info("mrev_risk_p2_max_positions", symbol=signal.symbol, reason=reason)
        return _rejected("P2_MAX_POSITIONS", reason)

    # Step 4: P3 — position size cap
    ok, reason = _check_position_size(sizing.notional_value, portfolio.total_equity)
    if not ok:
        log.info("mrev_risk_p3_position_size", symbol=signal.symbol, reason=reason)
        return _rejected("P3_MAX_POSITION_SIZE", reason)

    # Step 5: P4 — minimum viable order
    ok, reason = _check_min_order(sizing)
    if not ok:
        log.info("mrev_risk_p4_min_order", symbol=signal.symbol, reason=reason)
        return _rejected("P4_MIN_ORDER", reason)

    # All rules passed
    log.info(
        "mrev_risk_approved",
        symbol=signal.symbol,
        qty=sizing.qty,
        notional=sizing.notional_value,
        stop=sizing.stop_price,
        is_crypto=sizing.is_crypto,
    )
    return MrevEvaluationResult(
        decision=RiskDecision.APPROVED.value,
        rule_code=None,
        rejection_reason=None,
        sizing=sizing,
    )


def _rejected(rule_code: str, reason: str) -> MrevEvaluationResult:
    return MrevEvaluationResult(
        decision=RiskDecision.REJECTED.value,
        rule_code=rule_code,
        rejection_reason=reason,
    )
