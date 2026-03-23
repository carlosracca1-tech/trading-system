"""
apps/svc_strategy/main.py
Strategy Service CLI — manual signal scanning entry point.

Usage:
  python -m apps.svc_strategy.main --scan
  python -m apps.svc_strategy.main --scan --date 2024-06-15
  python -m apps.svc_strategy.main --scan --dry-run   (skip DB writes)

Requires RUN_ID environment variable (the active TradingRun UUID).

Pipeline:
  1. Load active symbols from DB
  2. Fetch latest indicator rows for each symbol
  3. Get SPY row for regime filter
  4. For each open position: check_exit_signal()
  5. For each non-open symbol:  check_entry_signal()
  6. Write all signals to DB (unless --dry-run)
  7. Print summary table
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from packages.shared.logging_config import get_logger

log = get_logger(__name__)


def _require_run_id() -> str:
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        print("ERROR: RUN_ID environment variable is required.")
        sys.exit(1)
    return run_id


def run_scan(
    run_id: str,
    as_of_date: date,
    dry_run: bool = False,
) -> dict:
    """
    Execute the strategy scan and write signals to DB.

    Returns a summary dict with counts.
    """
    from packages.shared.db import db_session
    from apps.svc_strategy import repository as strat_repo
    from apps.svc_strategy.scanner import (
        check_entry_signal,
        check_exit_signal,
        is_regime_bullish,
    )
    from packages.shared.enums import PositionStatus, SignalType

    summary = {"enters": 0, "exits": 0, "holds": 0, "skipped": 0, "written": 0}

    with db_session() as session:
        # Load all active symbols
        symbols = strat_repo.get_active_symbols(session)
        if not symbols:
            log.warning("scan_no_active_symbols")
            print("WARNING: No active symbols found. Run the seeder first.")
            return summary

        # Load open positions
        open_positions = strat_repo.get_open_positions(session, run_id)
        open_position_map = {p.symbol: float(p.entry_price) for p in open_positions}
        open_symbol_set = set(open_position_map.keys())

        # Load SPY row for regime filter
        spy_row = strat_repo.get_combined_row(session, "SPY", as_of_date)
        regime_ok = is_regime_bullish(spy_row)
        log.info("regime_filter", bullish=regime_ok, date=str(as_of_date))

        decisions = []
        for symbol in symbols:
            row = strat_repo.get_combined_row(session, symbol, as_of_date)
            if row is None:
                summary["skipped"] += 1
                log.debug("scan_no_data", symbol=symbol)
                continue

            if symbol in open_symbol_set:
                entry_price = open_position_map[symbol]
                import pandas as pd
                row = row.copy()
                row["entry_price"] = entry_price
                decision = check_exit_signal(symbol, row, as_of_date, entry_price=entry_price)
            else:
                decision = check_entry_signal(symbol, row, as_of_date, regime_bullish=regime_ok)

            decisions.append(decision)

            if decision.signal_type == SignalType.ENTER.value:
                summary["enters"] += 1
            elif decision.signal_type == SignalType.EXIT.value:
                summary["exits"] += 1
            else:
                summary["holds"] += 1

        # Write to DB
        if not dry_run:
            for decision in decisions:
                strat_repo.write_signal(
                    session,
                    run_id=run_id,
                    decision=decision,
                    dry_run=False,
                )
                summary["written"] += 1
            session.commit()
            log.info("scan_signals_written", count=summary["written"])
        else:
            log.info("scan_dry_run_skip_write", count=len(decisions))

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy Service — signal scanner")
    parser.add_argument("--scan", action="store_true", help="Run signal scan for all symbols")
    parser.add_argument("--date", type=str, default=None,
                        help="Override evaluation date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes (print only)")
    args = parser.parse_args(argv)

    if not args.scan:
        parser.print_help()
        return 1

    run_id = _require_run_id()
    as_of = date.fromisoformat(args.date) if args.date else date.today()
    dry = args.dry_run or os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")

    print(f"Scanning signals: run_id={run_id}  date={as_of}  dry_run={dry}")

    try:
        summary = run_scan(run_id=run_id, as_of_date=as_of, dry_run=dry)
        print("\n── Signal Scan Summary ──────────────────────────────────")
        print(f"  ENTER:   {summary['enters']}")
        print(f"  EXIT:    {summary['exits']}")
        print(f"  HOLD:    {summary['holds']}")
        print(f"  SKIPPED: {summary['skipped']} (no data)")
        print(f"  WRITTEN: {summary['written']} signals to DB")
        return 0
    except Exception as exc:
        log.exception("scan_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
