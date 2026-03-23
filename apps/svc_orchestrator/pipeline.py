"""
apps/svc_orchestrator/pipeline.py
Daily pipeline — pure computation layer.

Takes pre-fetched data (symbols, rows, open positions, portfolio state)
and returns decisions. No DB calls.

Stages
------
  1. scan_symbols      → classify each symbol: ENTER / EXIT / HOLD
  2. evaluate_risk     → apply risk rules to ENTER signals
  3. build_exec_plan   → produce a list of ExecutionIntent (what to do next)

This separation keeps the logic testable without a database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

import pandas as pd

from apps.svc_execution.executor import build_portfolio_snapshot
from apps.svc_risk.engine import EvaluationResult, PortfolioState, evaluate_signal
from apps.svc_strategy.scanner import (
    SignalDecision,
    check_entry_signal,
    check_exit_signal,
    is_regime_bullish,
)
from packages.shared.enums import OrderSide, RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Stage tracking ─────────────────────────────────────────────────────────────

class StageStatus(str, Enum):
    PENDING  = "pending"
    COMPLETE = "complete"
    SKIPPED  = "skipped"
    FAILED   = "failed"


@dataclass
class StageResult:
    """Result of one pipeline stage."""
    name: str
    status: StageStatus = StageStatus.PENDING
    items_processed: int = 0
    items_skipped: int = 0
    error: Optional[str] = None

    def complete(self, processed: int = 0, skipped: int = 0) -> "StageResult":
        self.status = StageStatus.COMPLETE
        self.items_processed = processed
        self.items_skipped = skipped
        return self

    def skip(self, reason: str = "") -> "StageResult":
        self.status = StageStatus.SKIPPED
        self.error = reason
        return self

    def fail(self, error: str) -> "StageResult":
        self.status = StageStatus.FAILED
        self.error = error
        return self


@dataclass
class PipelineRun:
    """Collects the results of each stage for a single pipeline execution."""
    as_of_date: date
    stages: list[StageResult] = field(default_factory=list)

    # Stage outputs (populated as pipeline progresses)
    signals: list[SignalDecision] = field(default_factory=list)
    evaluations: list[tuple[SignalDecision, EvaluationResult]] = field(default_factory=list)
    exec_plan: list["ExecutionIntent"] = field(default_factory=list)

    @property
    def enter_signals(self) -> list[SignalDecision]:
        return [s for s in self.signals if s.signal_type == SignalType.ENTER.value]

    @property
    def exit_signals(self) -> list[SignalDecision]:
        return [s for s in self.signals if s.signal_type == SignalType.EXIT.value]

    @property
    def approved_entries(self) -> list[tuple[SignalDecision, EvaluationResult]]:
        return [
            (sig, ev) for sig, ev in self.evaluations
            if ev.decision == RiskDecision.APPROVED.value
        ]

    @property
    def rejected_entries(self) -> list[tuple[SignalDecision, EvaluationResult]]:
        return [
            (sig, ev) for sig, ev in self.evaluations
            if ev.decision == RiskDecision.REJECTED.value
        ]

    def summary(self) -> dict:
        return {
            "date": str(self.as_of_date),
            "signals_enter": len(self.enter_signals),
            "signals_exit": len(self.exit_signals),
            "approved": len(self.approved_entries),
            "rejected": len(self.rejected_entries),
            "exec_orders": len(self.exec_plan),
            "stages": [
                {"name": s.name, "status": s.status.value, "processed": s.items_processed}
                for s in self.stages
            ],
        }


# ── Execution intent ──────────────────────────────────────────────────────────

@dataclass
class ExecutionIntent:
    """
    What the pipeline wants the execution layer to do.
    One intent per symbol per day.
    """
    symbol: str
    side: str                           # OrderSide.value
    signal: SignalDecision
    evaluation: Optional[EvaluationResult] = None   # None for exits
    position_id: Optional[str] = None               # required for exits

    @property
    def is_entry(self) -> bool:
        return self.side == OrderSide.BUY.value

    @property
    def is_exit(self) -> bool:
        return self.side == OrderSide.SELL.value


# ── Stage 1: Symbol scanning ──────────────────────────────────────────────────

def scan_symbols(
    *,
    symbols: list[str],
    rows: dict[str, pd.Series],
    open_position_symbols: set[str],
    spy_row: Optional[pd.Series],
    as_of_date: date,
) -> tuple[list[SignalDecision], StageResult]:
    """
    Scan every active symbol and generate ENTER / EXIT / HOLD decisions.

    Args:
        symbols:                all active ticker strings
        rows:                   {symbol → latest combined indicator row}
        open_position_symbols:  tickers that have an open position (exit-eligible)
        spy_row:                SPY indicator row for regime filter
        as_of_date:             the date we're evaluating for

    Returns:
        (list[SignalDecision], StageResult)
    """
    stage = StageResult(name="scan_symbols")
    regime_ok = is_regime_bullish(spy_row)

    log.info(
        "scan_start",
        as_of_date=str(as_of_date),
        symbols=len(symbols),
        regime_bullish=regime_ok,
        open_positions=len(open_position_symbols),
    )

    signals: list[SignalDecision] = []
    skipped = 0

    for symbol in symbols:
        row = rows.get(symbol)
        if row is None:
            log.warning("scan_no_row", symbol=symbol)
            skipped += 1
            continue

        if symbol in open_position_symbols:
            # Evaluate for EXIT — need entry_price from the position
            # Caller must provide entry_price inside row if available;
            # fall back to 0 (E3 won't trigger without proper entry price)
            entry_price = float(row.get("entry_price", 0.0) or 0.0)
            decision = check_exit_signal(symbol, row, as_of_date, entry_price=entry_price)
        else:
            decision = check_entry_signal(symbol, row, as_of_date, regime_bullish=regime_ok)

        signals.append(decision)

    enters = sum(1 for s in signals if s.signal_type == SignalType.ENTER.value)
    exits = sum(1 for s in signals if s.signal_type == SignalType.EXIT.value)
    log.info("scan_complete", enters=enters, exits=exits, holds=len(signals)-enters-exits)

    stage.complete(processed=len(signals), skipped=skipped)
    return signals, stage


# ── Stage 2: Risk evaluation ──────────────────────────────────────────────────

def evaluate_risk(
    *,
    signals: list[SignalDecision],
    portfolio: PortfolioState,
) -> tuple[list[tuple[SignalDecision, EvaluationResult]], StageResult]:
    """
    Apply risk rules to all signals.

    EXIT signals are auto-APPROVED.
    HOLD signals are DEFERRED (skipped).
    ENTER signals go through the full rule chain.

    Returns:
        (list[(SignalDecision, EvaluationResult)], StageResult)
    """
    stage = StageResult(name="evaluate_risk")
    evaluations: list[tuple[SignalDecision, EvaluationResult]] = []
    skipped = 0

    for signal in signals:
        if signal.signal_type == SignalType.HOLD.value:
            skipped += 1
            continue

        result = evaluate_signal(signal, portfolio)
        evaluations.append((signal, result))

        log.debug(
            "risk_evaluated",
            symbol=signal.symbol,
            signal=signal.signal_type,
            decision=result.decision,
            rule=result.rule_code,
        )

    approved = sum(1 for _, r in evaluations if r.decision == RiskDecision.APPROVED.value)
    rejected = sum(1 for _, r in evaluations if r.decision == RiskDecision.REJECTED.value)
    log.info("risk_complete", approved=approved, rejected=rejected, skipped=skipped)

    stage.complete(processed=len(evaluations), skipped=skipped)
    return evaluations, stage


# ── Stage 3: Build execution plan ─────────────────────────────────────────────

def build_exec_plan(
    *,
    evaluations: list[tuple[SignalDecision, EvaluationResult]],
    open_positions: dict[str, str],   # symbol → position_id
) -> tuple[list[ExecutionIntent], StageResult]:
    """
    Translate approved risk decisions into concrete ExecutionIntents.

    Exits always come before entries (list is ordered: exits first).

    Args:
        evaluations:    pairs of (SignalDecision, EvaluationResult)
        open_positions: {symbol: position_id} for currently open positions

    Returns:
        (list[ExecutionIntent], StageResult)
    """
    stage = StageResult(name="build_exec_plan")
    exits: list[ExecutionIntent] = []
    entries: list[ExecutionIntent] = []

    for signal, evaluation in evaluations:
        if evaluation.decision != RiskDecision.APPROVED.value:
            continue

        if signal.signal_type == SignalType.EXIT.value:
            position_id = open_positions.get(signal.symbol)
            exits.append(ExecutionIntent(
                symbol=signal.symbol,
                side=OrderSide.SELL.value,
                signal=signal,
                evaluation=evaluation,
                position_id=position_id,
            ))

        elif signal.signal_type == SignalType.ENTER.value:
            entries.append(ExecutionIntent(
                symbol=signal.symbol,
                side=OrderSide.BUY.value,
                signal=signal,
                evaluation=evaluation,
            ))

    plan = exits + entries   # exits always first
    log.info("exec_plan_built", exits=len(exits), entries=len(entries))

    stage.complete(processed=len(plan))
    return plan, stage


# ── Full pipeline (stateless) ─────────────────────────────────────────────────

def run_pipeline(
    *,
    symbols: list[str],
    rows: dict[str, pd.Series],
    open_positions: dict[str, str],    # symbol → position_id
    open_position_entry_prices: dict[str, float],  # symbol → entry_price
    spy_row: Optional[pd.Series],
    portfolio: PortfolioState,
    as_of_date: date,
) -> PipelineRun:
    """
    Execute all three stages in sequence and return a PipelineRun.

    This is the pure-computation entry point: inject all data, get back
    a fully-described plan (no DB, no broker calls).
    """
    result = PipelineRun(as_of_date=as_of_date)

    # Inject entry prices into rows so scan_symbols can use them for stop calc
    enriched_rows: dict[str, pd.Series] = {}
    for sym, row in rows.items():
        if sym in open_position_entry_prices:
            row = row.copy()
            row["entry_price"] = open_position_entry_prices[sym]
        enriched_rows[sym] = row

    # Stage 1: Scan
    signals, stage1 = scan_symbols(
        symbols=symbols,
        rows=enriched_rows,
        open_position_symbols=set(open_positions.keys()),
        spy_row=spy_row,
        as_of_date=as_of_date,
    )
    result.signals = signals
    result.stages.append(stage1)

    # Stage 2: Risk
    evaluations, stage2 = evaluate_risk(signals=signals, portfolio=portfolio)
    result.evaluations = evaluations
    result.stages.append(stage2)

    # Stage 3: Exec plan
    plan, stage3 = build_exec_plan(evaluations=evaluations, open_positions=open_positions)
    result.exec_plan = plan
    result.stages.append(stage3)

    log.info("pipeline_complete", **result.summary())
    return result
