"""
apps/svc_risk/main.py
Risk Service CLI — manual risk evaluation entry point.

Usage:
  python -m apps.svc_risk.main --evaluate
  python -m apps.svc_risk.main --evaluate --date 2024-06-15
  python -m apps.svc_risk.main --kill-switch          (manual activation)
  python -m apps.svc_risk.main --resolve-kill-switch  (manual resolution)
  python -m apps.svc_risk.main --status               (show drawdown + ks state)

Requires RUN_ID environment variable.

Pipeline (--evaluate):
  1. Load portfolio state from DB (equity, positions, peak)
  2. Check kill switch guard (auto-trigger on drawdown breach)
  3. For each PENDING signal from today → evaluate_signal()
  4. Update signal.risk_decision + write RiskEvent for rejections
  5. Print summary
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone

from packages.shared.logging_config import get_logger

log = get_logger(__name__)


def _require_run_id() -> str:
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        print("ERROR: RUN_ID environment variable is required.")
        sys.exit(1)
    return run_id


def _build_broker():
    from apps.svc_execution.broker import DryRunBroker, AlpacaBroker
    dry = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
    if dry:
        return DryRunBroker(float(os.environ.get("INITIAL_CAPITAL", "100000")))
    return AlpacaBroker(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        base_url=os.environ["ALPACA_BASE_URL"],
    )


def run_evaluate(run_id: str, as_of_date: date) -> dict:
    """
    Evaluate risk for all PENDING signals on as_of_date.

    Returns summary dict.
    """
    from sqlalchemy import select, update
    from packages.shared.db import db_session
    from packages.shared.enums import RiskDecision, SignalType
    from packages.shared.models.signal import Signal
    from packages.shared.models.trading_run import TradingRun
    from apps.svc_execution.repository import (
        get_open_positions,
        get_latest_snapshot,
    )
    from apps.svc_risk.engine import evaluate_signal, PortfolioState
    from apps.svc_risk.kill_switch import check_should_trigger
    from apps.svc_risk.position_sizer import SizingResult
    from apps.svc_strategy.scanner import SignalDecision

    summary = {"approved": 0, "rejected": 0, "skipped": 0, "ks_triggered": False}

    with db_session() as session:
        run = session.get(TradingRun, run_id)
        if run is None:
            raise ValueError(f"TradingRun {run_id} not found")

        initial_capital = float(run.initial_capital)
        open_positions = get_open_positions(session, run_id)
        latest_snap = get_latest_snapshot(session, run_id)

        if latest_snap:
            total_equity = float(latest_snap.total_equity)
            peak_equity = float(latest_snap.peak_equity)
            cash = float(latest_snap.cash)
        else:
            notional = sum(float(p.entry_price) * int(p.qty) for p in open_positions)
            total_equity = initial_capital
            peak_equity = initial_capital
            cash = initial_capital - notional

        portfolio = PortfolioState(
            total_equity=total_equity,
            peak_equity=peak_equity,
            open_position_count=len(open_positions),
            cash=cash,
        )

        # Kill switch guard
        ks_check = check_should_trigger(peak_equity, total_equity)
        if ks_check.should_trigger:
            broker = _build_broker()
            from apps.svc_risk.kill_switch import activate as ks_activate
            ks_activate(
                session,
                run_id=run_id,
                broker=broker,
                trigger=ks_check.trigger,
                reason=ks_check.reason,
                metrics_snapshot={"equity": total_equity, "peak_equity": peak_equity},
            )
            session.commit()
            print(f"KILL SWITCH ACTIVATED: {ks_check.reason}")
            summary["ks_triggered"] = True
            return summary

        # Load today's PENDING signals
        stmt = (
            select(Signal)
            .where(
                Signal.run_id == run_id,
                Signal.signal_date == as_of_date,
                Signal.risk_decision == RiskDecision.PENDING.value,
                Signal.signal_type.in_([SignalType.ENTER.value, SignalType.EXIT.value]),
            )
        )
        signals = list(session.scalars(stmt).all())

        for sig in signals:
            sd = SignalDecision(
                symbol=sig.symbol,
                signal_date=sig.signal_date,
                signal_type=sig.signal_type,
                close_price=float(sig.close_price),
                atr_14=float(sig.atr_14) if sig.atr_14 else None,
                ema_50=float(sig.ema_50) if sig.ema_50 else None,
                ema_200=float(sig.ema_200) if sig.ema_200 else None,
                rsi_14=float(sig.rsi_14) if sig.rsi_14 else None,
                regime_ok=bool(sig.regime_ok),
            )

            result = evaluate_signal(sd, portfolio)

            # Update signal in DB
            update_vals: dict = {"risk_decision": result.decision}
            if result.rejection_reason:
                update_vals["risk_rejection_reason"] = result.rejection_reason
            if result.sizing:
                update_vals["stop_loss"] = result.sizing.stop_price
                update_vals["position_size_shares"] = str(result.sizing.shares)

            session.execute(
                update(Signal).where(Signal.id == sig.id).values(**update_vals)
            )

            if result.decision == RiskDecision.APPROVED.value:
                summary["approved"] += 1
                # Increment portfolio counter for subsequent evaluations
                if sig.signal_type == SignalType.ENTER.value:
                    portfolio = PortfolioState(
                        total_equity=portfolio.total_equity,
                        peak_equity=portfolio.peak_equity,
                        open_position_count=portfolio.open_position_count + 1,
                        cash=portfolio.cash,
                    )
            elif result.decision == RiskDecision.REJECTED.value:
                summary["rejected"] += 1
                # Write RiskEvent for audit trail
                from packages.shared.models.risk_event import RiskEvent
                risk_event = RiskEvent.rejected(
                    rule_code=result.rule_code or "UNKNOWN",
                    rule_priority=result.rule_code[:2] if result.rule_code else "P0",
                    correlation_id=str(uuid.uuid4()),
                    rejection_reason=result.rejection_reason or "",
                    run_id=run_id,
                    symbol=sig.symbol,
                )
                session.add(risk_event)
            else:
                summary["skipped"] += 1

        session.commit()
        log.info("risk_evaluation_complete", **summary)

    return summary


def run_kill_switch_manual(run_id: str) -> None:
    """Activate the kill switch manually."""
    from packages.shared.db import db_session
    from apps.svc_risk.kill_switch import activate as ks_activate
    from packages.shared.enums import KillSwitchTrigger

    broker = _build_broker()
    with db_session() as session:
        ks_activate(
            session,
            run_id=run_id,
            broker=broker,
            trigger=KillSwitchTrigger.MANUAL.value,
            reason="manual_cli_activation",
        )
        session.commit()
    print(f"Kill switch ACTIVATED for run {run_id}. All positions closed.")


def run_resolve_kill_switch(run_id: str) -> None:
    """Resolve the kill switch (re-enable the run)."""
    from packages.shared.db import db_session
    from apps.svc_risk.kill_switch import resolve as ks_resolve

    with db_session() as session:
        ks_resolve(session, run_id=run_id, resolved_by="cli")
        session.commit()
    print(f"Kill switch RESOLVED for run {run_id}. Run is now RUNNING.")


def run_status(run_id: str) -> None:
    """Show kill switch state and drawdown."""
    from packages.shared.db import db_session
    from packages.shared.models.trading_run import TradingRun
    from apps.svc_execution.repository import get_latest_snapshot
    from apps.svc_risk.kill_switch import check_should_trigger, is_active

    with db_session() as session:
        run = session.get(TradingRun, run_id)
        if run is None:
            print(f"ERROR: TradingRun {run_id} not found")
            return

        snap = get_latest_snapshot(session, run_id)
        ks = is_active(session, run_id)

        print(f"\n── Risk Status ──────────────────────────────────────────")
        print(f"  Run ID:      {run_id}")
        print(f"  Run status:  {run.status}")
        print(f"  Kill switch: {'ACTIVE ⚠' if ks else 'inactive'}")

        if snap:
            dd = float(snap.drawdown_pct)
            ks_check = check_should_trigger(float(snap.peak_equity), float(snap.total_equity))
            print(f"  Equity:      ${float(snap.total_equity):>12,.2f}")
            print(f"  Peak equity: ${float(snap.peak_equity):>12,.2f}")
            print(f"  Drawdown:    {dd:.2%}  (threshold: {15:.0%})")
            threshold_pct = (1 - float(snap.total_equity) / float(snap.peak_equity)) * 100
            bar = "█" * int(threshold_pct) + "░" * (20 - int(min(threshold_pct, 20)))
            print(f"  DD gauge:    [{bar}] {threshold_pct:.1f}%")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Risk Service CLI")
    parser.add_argument("--evaluate", action="store_true",
                        help="Evaluate risk for today's pending signals")
    parser.add_argument("--kill-switch", action="store_true",
                        help="Manually activate kill switch (closes all positions)")
    parser.add_argument("--resolve-kill-switch", action="store_true",
                        help="Resolve kill switch (re-enable trading)")
    parser.add_argument("--status", action="store_true",
                        help="Show drawdown and kill switch state")
    parser.add_argument("--date", type=str, default=None,
                        help="Override evaluation date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    run_id = _require_run_id()

    try:
        if args.evaluate:
            as_of = date.fromisoformat(args.date) if args.date else date.today()
            print(f"Evaluating risk: run_id={run_id}  date={as_of}")
            summary = run_evaluate(run_id=run_id, as_of_date=as_of)
            print("\n── Risk Evaluation Summary ───────────────────────────────")
            print(f"  APPROVED: {summary['approved']}")
            print(f"  REJECTED: {summary['rejected']}")
            print(f"  SKIPPED:  {summary['skipped']}")
            if summary["ks_triggered"]:
                print("  ⚠  KILL SWITCH WAS TRIGGERED")
            return 0

        if args.kill_switch:
            confirm = input("Activate kill switch? This will close ALL positions. [yes/NO] ")
            if confirm.strip().lower() != "yes":
                print("Cancelled.")
                return 0
            run_kill_switch_manual(run_id)
            return 0

        if args.resolve_kill_switch:
            run_resolve_kill_switch(run_id)
            return 0

        if args.status:
            run_status(run_id)
            return 0

        parser.print_help()
        return 1

    except Exception as exc:
        log.exception("risk_cli_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
