"""
apps/svc_execution/main.py
Execution Service CLI entry point.

Usage:
  python -m apps.svc_execution.main --execute
  python -m apps.svc_execution.main --snapshot
  python -m apps.svc_execution.main --reconcile

Modes:
  --execute    Submit pending orders for all approved signals + exits
  --snapshot   Write a portfolio snapshot to DB
  --reconcile  Poll broker for order status and update positions
  --dry-run    Force DRY_RUN mode regardless of env (safe override)
"""
from __future__ import annotations

import argparse
import os
import sys

from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Mode detection ─────────────────────────────────────────────────────────────

def _is_dry_run(force: bool = False) -> bool:
    """Return True if system should simulate orders instead of hitting broker."""
    if force:
        return True
    return os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")


def _build_broker(dry_run: bool):
    """Instantiate the correct broker based on mode."""
    from apps.svc_execution.broker import AlpacaBroker, DryRunBroker
    if dry_run:
        log.info("broker_mode", mode="dry_run")
        return DryRunBroker(initial_cash=float(os.environ.get("INITIAL_CAPITAL", "100000")))

    api_key = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]
    base_url = os.environ["ALPACA_BASE_URL"]
    log.info("broker_mode", mode="alpaca", base_url=base_url)
    return AlpacaBroker(api_key=api_key, secret_key=secret_key, base_url=base_url)


# ── Execute pipeline ──────────────────────────────────────────────────────────

def run_execute(dry_run: bool) -> None:
    """
    Main execution loop:
    1. Load the active TradingRun + portfolio state from DB
    2. For each approved ENTER signal → build entry order → submit to broker → open position
    3. For each approved EXIT signal  → build exit order  → submit to broker → close position
    """
    from packages.shared.db import db_session
    from apps.svc_execution import broker as broker_mod
    from apps.svc_execution import executor, repository

    broker = _build_broker(dry_run)

    with db_session() as session:
        # ── Get active run ────────────────────────────────────────────────────
        run = repository.get_active_run(session)
        if run is None:
            log.warning("no_active_run")
            print("No active RUNNING TradingRun found — nothing to execute.")
            return

        log.info("execution_start", run_id=run.id, run_type=run.run_type)

        # ── Process EXIT signals first (always before new entries) ────────────
        exit_signals = repository.get_exit_signals(session, run.id)
        for sig in exit_signals:
            position = repository.get_open_position_by_symbol(session, run.id, sig.symbol)
            if position is None:
                log.warning("exit_signal_no_position", symbol=sig.symbol)
                continue

            exit_order = executor.build_exit_order(
                signal=_signal_model_to_decision(sig),
                position=position,
                run_id=run.id,
            )
            repository.save_order(session, exit_order)

            try:
                broker_order = broker.submit_order(
                    symbol=exit_order.symbol,
                    side=exit_order.side,
                    qty=exit_order.qty,
                    submitted_price=float(exit_order.submitted_price or 0),
                )
            except Exception as exc:
                log.error("broker_exit_failed", symbol=sig.symbol, error=str(exc))
                continue

            executor.apply_broker_fill(exit_order, broker_order)

            if exit_order.is_filled:
                executor.close_position(
                    position=position,
                    exit_order=exit_order,
                    close_reason=f"signal_exit:{sig.risk_rejection_reason or 'strategy'}",
                )
                log.info(
                    "position_closed",
                    symbol=sig.symbol,
                    exit_price=broker_order.filled_avg_price,
                )

        # ── Process ENTER signals ─────────────────────────────────────────────
        enter_signals = repository.get_approved_signals(session, run.id)
        for sig in enter_signals:
            # Skip if we already have an open position in this symbol
            if repository.get_open_position_by_symbol(session, run.id, sig.symbol):
                log.debug("enter_signal_already_open", symbol=sig.symbol)
                continue

            # Skip if we already submitted an order for this signal today
            if repository.has_order_for_signal(session, run.id, sig.symbol, sig.signal_date):
                log.debug("enter_signal_already_ordered", symbol=sig.symbol)
                continue

            # Reconstruct a minimal EvaluationResult for the executor
            from apps.svc_risk.position_sizer import SizingResult
            from apps.svc_risk.engine import EvaluationResult
            from packages.shared.enums import RiskDecision

            if sig.position_size_shares is None or sig.stop_loss is None:
                log.warning("enter_signal_missing_sizing", symbol=sig.symbol)
                continue

            sizing = SizingResult(
                shares=int(sig.position_size_shares),
                stop_price=float(sig.stop_loss),
                risk_amount=0.0,
                notional_value=float(sig.entry_price or sig.close_price) * int(sig.position_size_shares),
                pct_of_portfolio=0.0,
            )
            evaluation = EvaluationResult(
                decision=RiskDecision.APPROVED.value,
                rule_code=None,
                rejection_reason=None,
                sizing=sizing,
            )

            entry_order = executor.build_entry_order(
                signal=_signal_model_to_decision(sig),
                evaluation=evaluation,
                run_id=run.id,
                signal_id=sig.id,
            )
            repository.save_order(session, entry_order)

            try:
                broker_order = broker.submit_order(
                    symbol=entry_order.symbol,
                    side=entry_order.side,
                    qty=entry_order.qty,
                    submitted_price=float(entry_order.submitted_price or 0),
                )
            except Exception as exc:
                log.error("broker_entry_failed", symbol=sig.symbol, error=str(exc))
                continue

            executor.apply_broker_fill(entry_order, broker_order)

            if entry_order.is_filled:
                position = executor.open_position(order=entry_order, run_id=run.id)
                repository.save_position(session, position)
                log.info(
                    "position_opened",
                    symbol=sig.symbol,
                    entry_price=broker_order.filled_avg_price,
                    qty=broker_order.filled_qty,
                )

        session.commit()
        log.info("execution_complete", run_id=run.id)


# ── Snapshot pipeline ─────────────────────────────────────────────────────────

def run_snapshot() -> None:
    """Write a point-in-time portfolio snapshot to the DB."""
    from packages.shared.db import db_session
    from apps.svc_execution import executor, repository

    with db_session() as session:
        run = repository.get_active_run(session)
        if run is None:
            log.warning("no_active_run_for_snapshot")
            return

        open_positions = repository.get_open_positions(session, run.id)
        latest = repository.get_latest_snapshot(session, run.id)

        peak_equity = float(run.initial_capital)
        if latest is not None:
            peak_equity = max(peak_equity, float(latest.peak_equity), float(latest.total_equity))

        # Estimate cash (initial_capital minus notional of open positions)
        notional_open = sum(
            float(p.entry_price) * int(p.qty) for p in open_positions
        )
        cash = float(run.initial_capital) - notional_open

        snapshot = executor.build_portfolio_snapshot(
            run_id=run.id,
            cash=cash,
            open_positions=open_positions,
            initial_capital=float(run.initial_capital),
            peak_equity=peak_equity,
        )
        repository.save_snapshot(session, snapshot)
        session.commit()
        log.info(
            "snapshot_written",
            run_id=run.id,
            equity=snapshot.total_equity,
            drawdown=float(snapshot.drawdown_pct),
        )
        print(
            f"Snapshot: equity={snapshot.total_equity:.2f}  "
            f"drawdown={float(snapshot.drawdown_pct):.2%}  "
            f"positions={snapshot.open_positions_count}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal_model_to_decision(sig) -> "SignalDecision":
    """Convert a Signal ORM object to a SignalDecision dataclass."""
    from apps.svc_strategy.scanner import SignalDecision
    return SignalDecision(
        symbol=sig.symbol,
        signal_date=sig.signal_date,
        signal_type=sig.signal_type,
        close_price=float(sig.close_price),
        atr_14=float(sig.atr_14) if sig.atr_14 else None,
        ema_50=float(sig.ema_50) if sig.ema_50 else None,
        ema_200=float(sig.ema_200) if sig.ema_200 else None,
        rsi_14=float(sig.rsi_14) if sig.rsi_14 else None,
        volume_ratio=float(sig.volume_ratio) if sig.volume_ratio else None,
        regime_ok=bool(sig.regime_ok),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execution Service")
    parser.add_argument("--execute", action="store_true", help="Submit pending orders")
    parser.add_argument("--snapshot", action="store_true", help="Write portfolio snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    args = parser.parse_args(argv)

    dry = _is_dry_run(force=args.dry_run)

    if not any([args.execute, args.snapshot]):
        parser.print_help()
        return 1

    try:
        if args.execute:
            run_execute(dry_run=dry)
        if args.snapshot:
            run_snapshot()
        return 0
    except Exception as exc:
        log.exception("execution_service_error", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
