"""
apps/svc_orchestrator/main.py
Orchestrator CLI.

Commands:
  --create-run          Create a new TradingRun and print the run_id
  --run-daily           Execute the full daily pipeline for the active run
  --status              Print current portfolio status
  --stop                Stop the active running run

Environment variables:
  RUN_ID                UUID of TradingRun (required for --run-daily / --status / --stop)
  DRY_RUN               "true"/"false" — default true (safe)
  TRADING_MODE          "paper" / "live"
  INITIAL_CAPITAL       Starting capital in USD (default 100000)
  ALPACA_API_KEY        (required when DRY_RUN=false)
  ALPACA_SECRET_KEY
  ALPACA_BASE_URL
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from packages.shared.logging_config import get_logger

log = get_logger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")


def _build_broker(dry_run: bool):
    from apps.svc_execution.broker import AlpacaBroker, DryRunBroker
    if dry_run:
        initial_cash = float(os.environ.get("INITIAL_CAPITAL", "100000"))
        log.info("broker_dry_run", initial_cash=initial_cash)
        return DryRunBroker(initial_cash=initial_cash)

    api_key = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]
    base_url = os.environ["ALPACA_BASE_URL"]
    return AlpacaBroker(api_key=api_key, secret_key=secret_key, base_url=base_url)


def _require_run_id() -> str:
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        print("ERROR: RUN_ID environment variable is required for this command.")
        sys.exit(1)
    return run_id


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_create_run(args) -> int:
    from apps.svc_orchestrator.runner import create_run
    from packages.shared.enums import RunType

    run_type_map = {
        "paper": RunType.PAPER.value,
        "live": RunType.LIVE.value,
        "backtest": RunType.BACKTEST.value,
    }
    run_type = run_type_map.get(
        os.environ.get("TRADING_MODE", "paper").lower(),
        RunType.PAPER.value,
    )
    initial_capital = float(os.environ.get("INITIAL_CAPITAL", "100000"))

    from apps.svc_strategy.scanner import STRATEGY_PARAMS
    from apps.svc_risk.position_sizer import RISK_PARAMS
    config = {"strategy": STRATEGY_PARAMS, "risk": RISK_PARAMS}

    try:
        run_id = create_run(
            run_type=run_type,
            initial_capital=initial_capital,
            notes=args.notes,
            config_snapshot=config,
        )
        print(f"Run created: {run_id}")
        print(f"  type={run_type}  capital={initial_capital:,.0f}")
        print(f"\nExport to shell:  export RUN_ID={run_id}")
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1


def cmd_run_daily(args) -> int:
    from apps.svc_orchestrator.runner import run_daily

    run_id = _require_run_id()
    dry_run = _is_dry_run()
    broker = _build_broker(dry_run)

    as_of = date.fromisoformat(args.date) if args.date else date.today()

    print(f"Running daily pipeline: run_id={run_id}  date={as_of}  dry_run={dry_run}")

    try:
        result = run_daily(run_id=run_id, broker=broker, as_of_date=as_of)
        summary = result.summary()

        print("\n── Pipeline Summary ──────────────────────────────────────")
        print(f"  Date:        {summary['date']}")
        print(f"  ENTER sigs:  {summary['signals_enter']}")
        print(f"  EXIT  sigs:  {summary['signals_exit']}")
        print(f"  Approved:    {summary['approved']}")
        print(f"  Rejected:    {summary['rejected']}")
        print(f"  Orders sent: {summary['exec_orders']}")

        for stage in summary["stages"]:
            icon = "✓" if stage["status"] == "complete" else "✗"
            print(f"  [{icon}] {stage['name']:20s}  processed={stage['processed']}")

        return 0
    except Exception as exc:
        log.exception("run_daily_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_status(args) -> int:
    from packages.shared.db import db_session
    from apps.svc_execution.repository import get_latest_snapshot, get_open_positions

    run_id = _require_run_id()

    with db_session() as session:
        from sqlalchemy import select
        from packages.shared.models.trading_run import TradingRun

        run = session.get(TradingRun, run_id)
        if run is None:
            print(f"ERROR: TradingRun {run_id} not found")
            return 1

        snapshot = get_latest_snapshot(session, run_id)
        positions = get_open_positions(session, run_id)

        print(f"\n── Trading Run Status ────────────────────────────────────")
        print(f"  Run ID:      {run_id}")
        print(f"  Type:        {run.run_type}")
        print(f"  Status:      {run.status}")
        print(f"  Started:     {run.started_at.date() if run.started_at else 'N/A'}")
        print(f"  Capital:     ${float(run.initial_capital):>12,.2f}")

        if snapshot:
            dd = float(snapshot.drawdown_pct)
            ret = float(snapshot.cumulative_return_pct)
            print(f"\n── Latest Snapshot ({snapshot.snapshot_at.date()}) ──────────────")
            print(f"  Total Equity:  ${float(snapshot.total_equity):>12,.2f}")
            print(f"  Cash:          ${float(snapshot.cash):>12,.2f}")
            print(f"  Positions val: ${float(snapshot.positions_value):>12,.2f}")
            print(f"  Peak equity:   ${float(snapshot.peak_equity):>12,.2f}")
            print(f"  Drawdown:      {dd:>8.2%}")
            print(f"  Cumul return:  {ret:>8.2%}")
        else:
            print("\n  No snapshots yet.")

        if positions:
            print(f"\n── Open Positions ({len(positions)}) ──────────────────────────────")
            for p in positions:
                upnl = float(p.unrealized_pnl or 0)
                print(
                    f"  {p.symbol:<6}  qty={int(p.qty):<5}  "
                    f"entry=${float(p.entry_price):<8.2f}  "
                    f"stop=${float(p.stop_loss):<8.2f}  "
                    f"unrlzd_pnl=${upnl:+.2f}"
                )
        else:
            print("\n  No open positions.")

    return 0


def cmd_stop(args) -> int:
    from apps.svc_orchestrator.runner import stop_run
    from packages.shared.enums import RunStatus

    run_id = _require_run_id()
    try:
        stop_run(run_id, status=RunStatus.STOPPED.value)
        print(f"Run {run_id} stopped.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RFTM Orchestrator — ties together data, strategy, risk, and execution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--create-run", action="store_true", help="Create a new TradingRun")
    parser.add_argument("--run-daily", action="store_true", help="Run the full daily pipeline")
    parser.add_argument("--status", action="store_true", help="Print current portfolio status")
    parser.add_argument("--stop", action="store_true", help="Stop the active TradingRun")
    parser.add_argument("--date", type=str, default=None,
                        help="Override date (YYYY-MM-DD, for --run-daily)")
    parser.add_argument("--notes", type=str, default=None,
                        help="Optional notes for the run (for --create-run)")

    args = parser.parse_args(argv)

    if args.create_run:
        return cmd_create_run(args)
    if args.run_daily:
        return cmd_run_daily(args)
    if args.status:
        return cmd_status(args)
    if args.stop:
        return cmd_stop(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
