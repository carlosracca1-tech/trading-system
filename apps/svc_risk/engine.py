"""
apps/svc_risk/engine.py
Risk Engine — orchestrates rule evaluation for each signal.

Workflow for an ENTER signal
----------------------------
1. Size the position (position_sizer.calculate_position_size)
2. Run P1 → P2 → P3 → P4 in order; first failure → REJECTED
3. If all pass → APPROVED, return sizing data

EXIT signals bypass all rules and are auto-approved (never block a closing trade).
HOLD signals are passed through unchanged (no risk evaluation needed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apps.svc_fundamental.checker import FundamentalChecker
from apps.svc_risk.position_sizer import (
    RISK_PARAMS,
    SizingResult,
    calculate_position_size,
)
from apps.svc_risk.rules import (
    P1MaxDrawdown,
    P2MaxPositions,
    P3MaxPositionSize,
    P4MinShares,
    RuleResult,
)
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.enums import RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Portfolio state snapshot (passed in by the caller) ────────────────────────

@dataclass
class PortfolioState:
    """
    Lightweight snapshot of portfolio state at evaluation time.
    Caller fetches this from the DB; the engine only reads it.
    """
    total_equity: float          # current mark-to-market equity
    peak_equity: float           # highest equity in this run (for drawdown calc)
    open_position_count: int     # number of currently open positions
    cash: float                  # available cash


# ── Evaluation result ─────────────────────────────────────────────────────────

@dataclass
class EvaluationResult:
    """Output of evaluate_signal()."""
    decision: str                        # RiskDecision.value
    rule_code: Optional[str]             # which rule fired (if rejected)
    rejection_reason: Optional[str]      # human-readable rejection detail
    # Sizing (only populated for approved ENTER signals)
    sizing: Optional[SizingResult] = None


# ── Risk engine instances (singleton-style; rules are stateless) ──────────────
_p1 = P1MaxDrawdown()
_p2 = P2MaxPositions()
_p3 = P3MaxPositionSize()
_p4 = P4MinShares()
_fundamental = FundamentalChecker()


def evaluate_signal(
    signal: SignalDecision,
    portfolio: PortfolioState,
) -> EvaluationResult:
    """
    Apply all risk rules to a signal and return an EvaluationResult.

    EXIT signals → always APPROVED (never block a closing trade).
    HOLD signals → passed through as DEFERRED (no action needed).
    ENTER signals → full rule evaluation + position sizing.
    """
    # ── EXIT: always approved — closing positions is always safe ─────────────
    if signal.signal_type == SignalType.EXIT.value:
        log.debug("risk_auto_approved_exit", symbol=signal.symbol)
        return EvaluationResult(
            decision=RiskDecision.APPROVED.value,
            rule_code=None,
            rejection_reason=None,
        )

    # ── HOLD: no action required ─────────────────────────────────────────────
    if signal.signal_type == SignalType.HOLD.value:
        return EvaluationResult(
            decision=RiskDecision.DEFERRED.value,
            rule_code=None,
            rejection_reason=None,
        )

    # ── ENTER: full evaluation pipeline ──────────────────────────────────────
    assert signal.signal_type == SignalType.ENTER.value

    # Step 0.5: Fundamental check (redundant with pipeline Stage 0, but
    # serves as safety net if engine is called outside the pipeline)
    can_trade, fund_reason = _fundamental.can_trade(signal.symbol, deep_check=True)
    if not can_trade:
        log.info("risk_f1_fundamental_blocked", symbol=signal.symbol, reason=fund_reason)
        return EvaluationResult(
            decision=RiskDecision.REJECTED.value,
            rule_code="F1_FUNDAMENTAL",
            rejection_reason=fund_reason,
        )

    # Get fundamental sentiment multiplier (reduces position in fearful markets)
    fund_multiplier = _fundamental.should_reduce_size()

    # Step 1: Position sizing (needed before P3 / P4 checks)
    if signal.atr_14 is None or signal.atr_14 <= 0:
        return EvaluationResult(
            decision=RiskDecision.REJECTED.value,
            rule_code="P4_MIN_SHARES",
            rejection_reason="atr_14_missing_or_zero",
        )

    sizing = calculate_position_size(
        portfolio_value=portfolio.total_equity,
        close_price=signal.close_price,
        atr_14=signal.atr_14,
        fundamental_multiplier=fund_multiplier,
    )

    # Step 2: P1 — portfolio drawdown kill switch
    r1 = _p1.check(portfolio.peak_equity, portfolio.total_equity)
    if not r1.passed:
        log.warning("risk_p1_triggered", symbol=signal.symbol, reason=r1.reason)
        return _rejected(r1)

    # Step 3: P2 — max concurrent positions
    r2 = _p2.check(portfolio.open_position_count)
    if not r2.passed:
        log.info("risk_p2_triggered", symbol=signal.symbol, reason=r2.reason)
        return _rejected(r2)

    # Step 4: P3 — max single position size
    r3 = _p3.check(sizing.notional_value, portfolio.total_equity)
    if not r3.passed:
        log.info("risk_p3_triggered", symbol=signal.symbol, reason=r3.reason)
        return _rejected(r3)

    # Step 5: P4 — minimum viable shares
    r4 = _p4.check(sizing.shares, sizing.rejection_reason)
    if not r4.passed:
        log.info("risk_p4_triggered", symbol=signal.symbol, reason=r4.reason)
        return _rejected(r4)

    # All rules passed — APPROVED
    log.info(
        "risk_approved",
        symbol=signal.symbol,
        shares=sizing.shares,
        notional=sizing.notional_value,
        stop=sizing.stop_price,
    )
    return EvaluationResult(
        decision=RiskDecision.APPROVED.value,
        rule_code=None,
        rejection_reason=None,
        sizing=sizing,
    )


def _rejected(result: RuleResult) -> EvaluationResult:
    return EvaluationResult(
        decision=RiskDecision.REJECTED.value,
        rule_code=result.rule_code,
        rejection_reason=result.reason,
    )
