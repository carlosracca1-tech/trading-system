"""
apps/svc_orchestrator_mrev/pipeline.py
MREV-1H Pipeline — pure computation layer (no DB, no broker).

Stages:
  1. scan_symbols   → classify each symbol: ENTER / EXIT / HOLD
  2. evaluate_risk  → apply MREV risk rules to ENTER signals
  3. build_exec_plan → produce ExecutionIntents

Same architecture as the daily RFTM pipeline but adapted for:
  - 1H candle data
  - MrevSignalDecision dataclass
  - MrevEvaluationResult with fractional crypto sizing
  - No regime filter (mean reversion works in any regime)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd

from apps.svc_risk_mrev.engine import (
    MrevEvaluationResult,
    MrevPortfolioState,
    evaluate_mrev_signal,
)
from apps.svc_strategy_mrev.scanner import (
    MrevSignalDecision,
    check_mrev_entry_signal,
    check_mrev_exit_signal,
)
from packages.shared.enums import OrderSide, RiskDecision, SignalType
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Stage tracking ───────────────────────────────────────────────────────────

class StageStatus(str, Enum):
    PENDING  = "pending"
    COMPLETE = "complete"
    SKIPPED  = "skipped"
    FAILED   = "failed"


@dataclass
class StageResult:
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

    def fail(self, error: str) -> "StageResult":
        self.status = StageStatus.FAILED
        self.error = error
        return self


# ── Execution intent ─────────────────────────────────────────────────────────

@dataclass
class MrevExecutionIntent:
    """What the pipeline wants the execution layer to do."""
    symbol: str
    side: str                                          # OrderSide.value
    signal: MrevSignalDecision
    evaluation: Optional[MrevEvaluationResult] = None  # None for exits
    position_id: Optional[str] = None                  # required for exits

    @property
    def is_entry(self) -> bool:
        return self.side == OrderSide.BUY.value

    @property
    def is_exit(self) -> bool:
        return self.side == OrderSide.SELL.value


# ── Pipeline run result ──────────────────────────────────────────────────────

@dataclass
class MrevPipelineRun:
    """Collects results of each stage for a single 1H pipeline execution."""
    as_of_datetime: datetime
    stages: list[StageResult] = field(default_factory=list)
    signals: list[MrevSignalDecision] = field(default_factory=list)
    evaluations: list[tuple[MrevSignalDecision, MrevEvaluationResult]] = field(default_factory=list)
    exec_plan: list[MrevExecutionIntent] = field(default_factory=list)

    @property
    def enter_signals(self) -> list[MrevSignalDecision]:
        return [s for s in self.signals if s.signal_type == SignalType.ENTER.value]

    @property
    def exit_signals(self) -> list[MrevSignalDecision]:
        return [s for s in self.signals if s.signal_type == SignalType.EXIT.value]

    @property
    def approved_entries(self) -> list[tuple[MrevSignalDecision, MrevEvaluationResult]]:
        return [
            (sig, ev) for sig, ev in self.evaluations
            if ev.decision == RiskDecision.APPROVED.value
        ]

    def summary(self) -> dict:
        return {
            "datetime": str(self.as_of_datetime),
            "signals_enter": len(self.enter_signals),
            "signals_exit": len(self.exit_signals),
            "approved": len(self.approved_entries),
            "exec_orders": len(self.exec_plan),
            "stages": [
                {"name": s.name, "status": s.status.value, "processed": s.items_processed}
                for s in self.stages
            ],
        }


# ── Stage 1: Symbol scanning ────────────────────────────────────────────────

def scan_symbols(
    *,
    symbols: list[str],
    rows: dict[str, pd.Series],
    open_positions: dict[str, dict],  # symbol → {"position_id": str, "entry_price": float, "entry_datetime": datetime}
    as_of_datetime: datetime,
) -> tuple[list[MrevSignalDecision], StageResult]:
    """
    Scan every active symbol and generate ENTER / EXIT / HOLD decisions.

    No regime filter for mean reversion — works in bull and bear markets.
    """
    stage = StageResult(name="mrev_scan_symbols")

    log.info(
        "mrev_scan_start",
        as_of_datetime=str(as_of_datetime),
        symbols=len(symbols),
        open_positions=len(open_positions),
    )

    signals: list[MrevSignalDecision] = []
    skipped = 0

    for symbol in symbols:
        row = rows.get(symbol)
        if row is None:
            log.warning("mrev_scan_no_row", symbol=symbol)
            skipped += 1
            continue

        if symbol in open_positions:
            pos_info = open_positions[symbol]
            decision = check_mrev_exit_signal(
                symbol=symbol,
                row=row,
                signal_datetime=as_of_datetime,
                entry_price=float(pos_info["entry_price"]),
                entry_datetime=pos_info["entry_datetime"],
            )
        else:
            decision = check_mrev_entry_signal(
                symbol=symbol,
                row=row,
                signal_datetime=as_of_datetime,
            )

        signals.append(decision)

    enters = sum(1 for s in signals if s.signal_type == SignalType.ENTER.value)
    exits = sum(1 for s in signals if s.signal_type == SignalType.EXIT.value)
    log.info("mrev_scan_complete", enters=enters, exits=exits, holds=len(signals) - enters - exits)

    stage.complete(processed=len(signals), skipped=skipped)
    return signals, stage


# ── Stage 2: Risk evaluation ────────────────────────────────────────────────

def evaluate_risk(
    *,
    signals: list[MrevSignalDecision],
    portfolio: MrevPortfolioState,
) -> tuple[list[tuple[MrevSignalDecision, MrevEvaluationResult]], StageResult]:
    """Apply MREV risk rules to all signals."""
    stage = StageResult(name="mrev_evaluate_risk")
    evaluations: list[tuple[MrevSignalDecision, MrevEvaluationResult]] = []
    skipped = 0

    for signal in signals:
        if signal.signal_type == SignalType.HOLD.value:
            skipped += 1
            continue

        result = evaluate_mrev_signal(signal, portfolio)
        evaluations.append((signal, result))

    approved = sum(1 for _, r in evaluations if r.decision == RiskDecision.APPROVED.value)
    rejected = sum(1 for _, r in evaluations if r.decision == RiskDecision.REJECTED.value)
    log.info("mrev_risk_complete", approved=approved, rejected=rejected, skipped=skipped)

    stage.complete(processed=len(evaluations), skipped=skipped)
    return evaluations, stage


# ── Stage 3: Build execution plan ───────────────────────────────────────────

def build_exec_plan(
    *,
    evaluations: list[tuple[MrevSignalDecision, MrevEvaluationResult]],
    open_positions: dict[str, dict],  # symbol → {"position_id": str, ...}
) -> tuple[list[MrevExecutionIntent], StageResult]:
    """Translate approved decisions into ExecutionIntents. Exits first."""
    stage = StageResult(name="mrev_build_exec_plan")
    exits: list[MrevExecutionIntent] = []
    entries: list[MrevExecutionIntent] = []

    for signal, evaluation in evaluations:
        if evaluation.decision != RiskDecision.APPROVED.value:
            continue

        if signal.signal_type == SignalType.EXIT.value:
            pos_info = open_positions.get(signal.symbol, {})
            exits.append(MrevExecutionIntent(
                symbol=signal.symbol,
                side=OrderSide.SELL.value,
                signal=signal,
                evaluation=evaluation,
                position_id=pos_info.get("position_id"),
            ))
        elif signal.signal_type == SignalType.ENTER.value:
            entries.append(MrevExecutionIntent(
                symbol=signal.symbol,
                side=OrderSide.BUY.value,
                signal=signal,
                evaluation=evaluation,
            ))

    plan = exits + entries
    log.info("mrev_exec_plan_built", exits=len(exits), entries=len(entries))
    stage.complete(processed=len(plan))
    return plan, stage


# ── Full pipeline (stateless) ────────────────────────────────────────────────

def run_mrev_pipeline(
    *,
    symbols: list[str],
    rows: dict[str, pd.Series],
    open_positions: dict[str, dict],  # symbol → {"position_id", "entry_price", "entry_datetime"}
    portfolio: MrevPortfolioState,
    as_of_datetime: datetime,
) -> MrevPipelineRun:
    """
    Execute all three MREV stages and return a MrevPipelineRun.

    Pure computation — no DB, no broker.
    """
    result = MrevPipelineRun(as_of_datetime=as_of_datetime)

    # Stage 1: Scan
    signals, stage1 = scan_symbols(
        symbols=symbols,
        rows=rows,
        open_positions=open_positions,
        as_of_datetime=as_of_datetime,
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

    log.info("mrev_pipeline_complete", **result.summary())
    return result
