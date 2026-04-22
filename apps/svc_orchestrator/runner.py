"""
apps/svc_orchestrator/runner.py
DB-aware daily runner.

Fetches all data from the DB, delegates computation to pipeline.py,
then writes results (signals, orders, positions, snapshot) to DB.

Architecture:
  runner.py  — knows about DB + broker
  pipeline.py — pure computation, no DB
  executor.py — pure computation, no DB
  repository.py (execution) — DB writes for orders/positions/snapshots
  repository.py (strategy)  — DB reads/writes for signals
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from apps.svc_execution import executor as exec_mod
from apps.svc_execution import repository as exec_repo
from apps.svc_execution.broker import AbstractBroker, DryRunBroker
from apps.svc_orchestrator.pipeline import (
    ExecutionIntent,
    PipelineRun,
    PortfolioState,
    run_pipeline,
)
from apps.svc_risk.engine import EvaluationResult
from apps.svc_strategy import repository as strat_repo
from apps.svc_strategy.scanner import SignalDecision
from packages.shared.db import db_session
from packages.shared.enums import (
    OrderSide,
    PositionStatus,
    RiskDecision,
    RunStatus,
    RunType,
    SignalType,
    SnapshotType,
)
from packages.shared.logging_config import get_logger
from packages.shared.models.order import Order
from packages.shared.models.trading_run import TradingRun

log = get_logger(__name__)


# ── TradingRun management ──────────────────────────────────────────────────────

def create_run(
    run_type: str = RunType.PAPER.value,
    initial_capital: float = 100_000.0,
    notes: Optional[str] = None,
    config_snapshot: Optional[dict] = None,
) -> str:
    """
    Create and persist a new TradingRun. Returns the new run_id.
    Fails if a RUNNING run already exists for the same type.
    """
    from packages.shared.enums import RunStatus
    with db_session() as session:
        # Guard: only one active run at a time
        from sqlalchemy import select
        existing = session.scalars(
            select(TradingRun).where(
                TradingRun.status == RunStatus.RUNNING.value,
                TradingRun.run_type == run_type,
            ).limit(1)
        ).first()
        if existing:
            raise RuntimeError(
                f"A RUNNING {run_type} run already exists (id={existing.id}). "
                "Stop it before creating a new one."
            )

        run = TradingRun(
            id=str(uuid.uuid4()),
            run_type=run_type,
            status=RunStatus.RUNNING.value,
            started_at=datetime.now(tz=timezone.utc),
            initial_capital=initial_capital,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            notes=notes,
            config_snapshot=json.dumps(config_snapshot) if config_snapshot else None,
        )
        session.add(run)
        session.commit()
        log.info("run_created", run_id=run.id, run_type=run_type, capital=initial_capital)
        return run.id


def stop_run(run_id: str, status: str = RunStatus.STOPPED.value) -> None:
    """Mark a TradingRun as stopped/completed."""
    from sqlalchemy import update
    with db_session() as session:
        session.execute(
            update(TradingRun)
            .where(TradingRun.id == run_id)
            .values(
                status=status,
                ended_at=datetime.now(tz=timezone.utc),
            )
        )
        session.commit()
        log.info("run_stopped", run_id=run_id, status=status)


# ── Portfolio state builder ────────────────────────────────────────────────────

def _build_portfolio_state(session, run_id: str, initial_capital: float) -> PortfolioState:
    """
    Read DB to build the PortfolioState snapshot the risk engine needs.
    """
    open_positions = exec_repo.get_open_positions(session, run_id)
    latest_snapshot = exec_repo.get_latest_snapshot(session, run_id)

    # Use latest snapshot equity if available; fall back to initial capital
    if latest_snapshot:
        total_equity = float(latest_snapshot.total_equity)
        peak_equity = float(latest_snapshot.peak_equity)
        cash = float(latest_snapshot.cash)
    else:
        notional_open = sum(
            float(p.entry_price) * int(p.qty) for p in open_positions
        )
        total_equity = initial_capital
        peak_equity = initial_capital
        cash = initial_capital - notional_open

    return PortfolioState(
        total_equity=total_equity,
        peak_equity=peak_equity,
        open_position_count=len(open_positions),
        cash=cash,
    )


# ── Signal persistence ─────────────────────────────────────────────────────────

def _persist_signals(
    session,
    run_id: str,
    pipeline_result: PipelineRun,
    evaluations_map: dict[str, EvaluationResult],
) -> None:
    """
    Write all signals (ENTER / EXIT / HOLD) + risk decisions to the signals table.
    """
    for signal in pipeline_result.signals:
        ev = evaluations_map.get(signal.symbol)
        sizing = ev.sizing if ev else None

        if ev:
            signal.risk_decision = ev.decision
            if ev.rejection_reason:
                signal.reason = ev.rejection_reason

        strat_repo.write_signal(
            session,
            run_id=run_id,
            decision=signal,
            stop_loss=float(sizing.stop_price) if sizing else None,
            position_size_shares=sizing.shares if sizing else None,
            dry_run=False,
        )


# ── Partial take profit (3% rule) ─────────────────────────────────────────────

PARTIAL_TP_PCT = 0.03          # sell half when position is +3%
PARTIAL_TP_SELL_RATIO = 0.50   # sell 50% of the position


def _execute_partial_take_profits(
    session,
    run_id: str,
    broker: AbstractBroker,
    rows: dict[str, "pd.Series"],
) -> int:
    """
    Pre-pipeline step: check all open positions for +3% unrealized profit.
    If found and partial TP not yet taken, sell 50% of the position.

    Returns the number of partial exits executed.
    """
    open_positions = exec_repo.get_open_positions(session, run_id)
    partial_count = 0

    for position in open_positions:
        # Skip if partial TP already taken
        if getattr(position, "partial_tp_taken", False):
            continue

        entry_price = float(position.entry_price)
        row = rows.get(position.symbol)
        if row is None:
            continue

        current_price = float(row.get("close", 0))
        if current_price <= 0 or entry_price <= 0:
            continue

        unrealized_pct = (current_price - entry_price) / entry_price

        if unrealized_pct >= PARTIAL_TP_PCT:
            current_qty = int(position.qty)
            sell_qty = max(1, int(current_qty * PARTIAL_TP_SELL_RATIO))

            if sell_qty >= current_qty:
                # Don't sell everything as partial — leave at least 1 share
                sell_qty = current_qty - 1
                if sell_qty <= 0:
                    continue

            log.info(
                "partial_tp_triggered",
                symbol=position.symbol,
                unrealized_pct=f"{unrealized_pct:.2%}",
                sell_qty=sell_qty,
                remaining_qty=current_qty - sell_qty,
            )

            # Submit partial sell order
            correlation_id = str(uuid.uuid4())
            try:
                broker_order = broker.submit_order(
                    symbol=position.symbol,
                    side=OrderSide.SELL.value,
                    qty=sell_qty,
                    submitted_price=current_price,
                )
            except Exception as exc:
                log.error("partial_tp_sell_failed", symbol=position.symbol, error=str(exc))
                continue

            if broker_order.is_filled:
                fill_price = broker_order.filled_avg_price or current_price

                # Save the partial sell order to DB
                partial_order = Order(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    symbol=position.symbol,
                    side=OrderSide.SELL.value,
                    qty=sell_qty,
                    order_type="market",
                    submitted_price=current_price,
                    status=broker_order.status,
                    filled_qty=broker_order.filled_qty,
                    filled_price=fill_price,
                    broker_order_id=broker_order.broker_order_id,
                    submitted_at=broker_order.submitted_at,
                    filled_at=broker_order.filled_at,
                    correlation_id=correlation_id,
                )
                session.add(partial_order)

                # Update position: reduce qty, mark partial TP taken
                if not position.initial_qty:
                    position.initial_qty = current_qty
                position.qty = current_qty - sell_qty
                position.partial_tp_taken = True

                log.info(
                    "partial_tp_filled",
                    symbol=position.symbol,
                    sold_qty=sell_qty,
                    fill_price=fill_price,
                    remaining_qty=position.qty,
                    realized_partial_pnl=round((fill_price - entry_price) * sell_qty, 2),
                )
                partial_count += 1

    return partial_count


# ── Order execution ────────────────────────────────────────────────────────────

def _execute_intent(
    session,
    intent: ExecutionIntent,
    broker: AbstractBroker,
    run_id: str,
    open_positions_by_symbol: dict,
) -> bool:
    """
    Execute a single ExecutionIntent (buy or sell).
    Returns True if the order filled successfully.
    """
    signal = intent.signal
    evaluation = intent.evaluation

    correlation_id = str(uuid.uuid4())

    if intent.is_exit:
        position = open_positions_by_symbol.get(signal.symbol)
        if position is None:
            log.warning("exec_no_position_for_exit", symbol=signal.symbol)
            return False

        exit_order = exec_mod.build_exit_order(
            signal=signal,
            position=position,
            run_id=run_id,
            correlation_id=correlation_id,
        )
        exec_repo.save_order(session, exit_order)

        try:
            broker_order = broker.submit_order(
                symbol=exit_order.symbol,
                side=exit_order.side,
                qty=exit_order.qty,
                submitted_price=float(exit_order.submitted_price or 0),
            )
        except Exception as exc:
            log.error("broker_exit_failed", symbol=signal.symbol, error=str(exc))
            return False

        exec_mod.apply_broker_fill(exit_order, broker_order)

        if exit_order.is_filled:
            exec_mod.close_position(
                position=position,
                exit_order=exit_order,
                close_reason=f"signal:{signal.reason or 'exit'}",
            )
            log.info(
                "position_closed",
                symbol=signal.symbol,
                exit_price=broker_order.filled_avg_price,
                qty=broker_order.filled_qty,
                reason=signal.reason,
            )
            return True

    else:  # entry
        if evaluation is None or evaluation.sizing is None:
            log.error("exec_entry_missing_sizing", symbol=signal.symbol)
            return False

        entry_order = exec_mod.build_entry_order(
            signal=signal,
            evaluation=evaluation,
            run_id=run_id,
            correlation_id=correlation_id,
        )
        exec_repo.save_order(session, entry_order)

        # ── Calculate bracket prices for broker-level protection ────────
        # NOTE: TP removed from bracket — handled by partial TP logic
        # (sell 50% at +3%, trail the rest). Only SL stays at broker level.
        stop_loss_price = float(evaluation.sizing.stop_price) if evaluation.sizing.stop_price else None

        try:
            broker_order = broker.submit_order(
                symbol=entry_order.symbol,
                side=entry_order.side,
                qty=entry_order.qty,
                submitted_price=float(entry_order.submitted_price or 0),
                take_profit_price=None,
                stop_loss_price=stop_loss_price,
            )
        except Exception as exc:
            log.error("broker_entry_failed", symbol=signal.symbol, error=str(exc))
            return False

        exec_mod.apply_broker_fill(entry_order, broker_order)

        if entry_order.is_filled:
            position = exec_mod.open_position(order=entry_order, run_id=run_id)
            position.initial_qty = entry_order.filled_qty  # track original qty
            exec_repo.save_position(session, position)
            log.info(
                "position_opened",
                symbol=signal.symbol,
                entry_price=broker_order.filled_avg_price,
                qty=broker_order.filled_qty,
            )
            return True

    return False


# ── Daily runner ──────────────────────────────────────────────────────────────

def run_daily(
    run_id: str,
    broker: AbstractBroker,
    as_of_date: Optional[date] = None,
) -> PipelineRun:
    """
    Execute the full daily pipeline for an existing RUNNING TradingRun.

    Stages:
      1. Fetch data from DB (symbols, indicator rows, open positions)
      2. run_pipeline() — pure computation (scan → risk → exec plan)
      3. Persist signals to DB
      4. Execute orders via broker
      5. Write portfolio snapshot

    Args:
        run_id:       UUID of the active TradingRun
        broker:       instantiated broker (DryRunBroker or AlpacaBroker)
        as_of_date:   evaluation date (defaults to today)

    Returns:
        PipelineRun with full stage results + summary
    """
    as_of_date = as_of_date or date.today()

    log.info("daily_run_start", run_id=run_id, date=str(as_of_date))

    with db_session() as session:
        # ── Load run ──────────────────────────────────────────────────────────
        from sqlalchemy import select
        run = session.get(TradingRun, run_id)
        if run is None:
            raise ValueError(f"TradingRun {run_id} not found")
        if run.status != RunStatus.RUNNING.value:
            raise ValueError(f"TradingRun {run_id} is not RUNNING (status={run.status})")

        initial_capital = float(run.initial_capital)

        # ── Fetch portfolio state ─────────────────────────────────────────────
        portfolio = _build_portfolio_state(session, run_id, initial_capital)
        log.info(
            "portfolio_state",
            equity=portfolio.total_equity,
            peak=portfolio.peak_equity,
            positions=portfolio.open_position_count,
        )

        # ── Kill switch guard — auto-trigger on P1 drawdown breach ────────────
        from apps.svc_risk.kill_switch import check_should_trigger, activate as ks_activate
        ks_check = check_should_trigger(portfolio.peak_equity, portfolio.total_equity)
        if ks_check.should_trigger:
            log.warning(
                "kill_switch_auto_trigger",
                drawdown=ks_check.drawdown_pct,
                reason=ks_check.reason,
            )
            ks_activate(
                session,
                run_id=run_id,
                broker=broker,
                trigger=ks_check.trigger,
                reason=ks_check.reason,
                metrics_snapshot={
                    "equity": portfolio.total_equity,
                    "peak_equity": portfolio.peak_equity,
                    "drawdown_pct": ks_check.drawdown_pct,
                },
            )
            session.commit()
            log.warning("daily_run_aborted_kill_switch", run_id=run_id)
            # Return empty pipeline run to signal abort
            from apps.svc_orchestrator.pipeline import PipelineRun
            return PipelineRun(as_of_date=as_of_date)

        # ── Fetch symbols + rows ──────────────────────────────────────────────
        symbols = strat_repo.get_active_symbols(session)
        if not symbols:
            log.warning("no_active_symbols")

        rows: dict[str, pd.Series] = {}
        for sym in symbols:
            row = strat_repo.get_combined_row(session, sym, as_of_date)
            if row is not None:
                rows[sym] = row

        spy_row = rows.get("SPY")

        # ── Partial take profit check (3% rule) ──────────────────────────────
        # Before the pipeline runs, check if any open positions are +3%.
        # If so, sell 50% to lock in profits and free capital.
        partial_tp_count = _execute_partial_take_profits(
            session=session,
            run_id=run_id,
            broker=broker,
            rows=rows,
        )
        if partial_tp_count > 0:
            log.info("partial_tp_round_complete", count=partial_tp_count)

        # ── Fetch open positions ──────────────────────────────────────────────
        open_positions_list = exec_repo.get_open_positions(session, run_id)
        open_positions_by_symbol = {p.symbol: p for p in open_positions_list}
        open_positions_map = {p.symbol: p.id for p in open_positions_list}
        open_entry_prices = {
            p.symbol: float(p.entry_price) for p in open_positions_list
        }

        # ── Run pipeline (pure computation) ───────────────────────────────────
        pipeline_result = run_pipeline(
            symbols=symbols,
            rows=rows,
            open_positions=open_positions_map,
            open_position_entry_prices=open_entry_prices,
            spy_row=spy_row,
            portfolio=portfolio,
            as_of_date=as_of_date,
        )

        # ── Persist signals ───────────────────────────────────────────────────
        evaluations_map = {sig.symbol: ev for sig, ev in pipeline_result.evaluations}
        _persist_signals(session, run_id, pipeline_result, evaluations_map)

        # ── Execute orders ────────────────────────────────────────────────────
        filled_count = 0
        for intent in pipeline_result.exec_plan:
            ok = _execute_intent(
                session=session,
                intent=intent,
                broker=broker,
                run_id=run_id,
                open_positions_by_symbol=open_positions_by_symbol,
            )
            if ok:
                filled_count += 1

        # ── Portfolio snapshot ────────────────────────────────────────────────
        # Refresh open positions after execution
        refreshed_positions = exec_repo.get_open_positions(session, run_id)
        new_peak = max(
            portfolio.peak_equity,
            portfolio.total_equity,
        )
        snapshot = exec_mod.build_portfolio_snapshot(
            run_id=run_id,
            cash=portfolio.cash,
            open_positions=refreshed_positions,
            initial_capital=initial_capital,
            peak_equity=new_peak,
            snapshot_type=SnapshotType.DAILY_CLOSE.value,
        )
        exec_repo.save_snapshot(session, snapshot)

        session.commit()

        log.info(
            "daily_run_complete",
            run_id=run_id,
            date=str(as_of_date),
            signals=len(pipeline_result.signals),
            orders_filled=filled_count,
            equity=float(snapshot.total_equity),
        )

    return pipeline_result
