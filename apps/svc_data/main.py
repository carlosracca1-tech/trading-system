"""
apps/svc_data/main.py
Data Service CLI entry point.

Usage (inside Docker):
    python -m apps.svc_data.main --all           # ingest all symbols
    python -m apps.svc_data.main --symbol SPY    # ingest one symbol
    python -m apps.svc_data.main --all --force   # full re-fetch (ignore last date)
    python -m apps.svc_data.main --coverage      # print data coverage table

Can also be invoked from the Makefile:
    make ingest
    make ingest-symbol SYMBOL=SPY
"""
from __future__ import annotations

import argparse
import sys

from config.settings import get_settings
from packages.shared.logging_config import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = get_settings()

    if not settings.polygon_api_key:
        logger.error("data.no_polygon_key", msg="POLYGON_API_KEY is not set")
        print("ERROR: POLYGON_API_KEY is not set in .env", file=sys.stderr)
        return 1

    from apps.svc_data.ingestion import DataIngestionService

    paid_tier = getattr(settings, "polygon_paid_tier", False)

    with DataIngestionService(
        polygon_api_key=settings.polygon_api_key,
        paid_tier=paid_tier,
    ) as svc:
        if args.symbol:
            result = svc.run_symbol(args.symbol, force_full=args.force)
            if result.error:
                print(f"FAILED  {result.symbol}: {result.error}", file=sys.stderr)
                return 1
            elif result.skipped:
                print(f"SKIPPED {result.symbol}: {result.skip_reason}")
            else:
                print(
                    f"OK      {result.symbol}: "
                    f"{result.bars_upserted} bars, "
                    f"{result.indicators_upserted} indicators, "
                    f"{result.duration_sec:.1f}s"
                )
        else:
            report = svc.run_all(force_full=args.force)
            print()
            print(report.summary())
            print()
            for r in report.results:
                status = "OK   " if r.success else ("SKIP " if r.skipped else "FAIL ")
                detail = r.error or r.skip_reason or f"{r.bars_upserted} bars, {r.indicators_upserted} ind"
                print(f"  {status} {r.symbol:<6} {detail}")
            print()
            return 0 if report.failed == 0 else 1

    return 0


def cmd_coverage(_args: argparse.Namespace) -> int:
    from packages.shared.db import db_session
    from apps.svc_data.repository import get_data_coverage

    with db_session() as session:
        rows = get_data_coverage(session)

    if not rows:
        print("No data in market_data_daily.")
        return 0

    print(f"\n{'Symbol':<8} {'Bars':>6} {'First':>12} {'Last':>12} {'Stale':>6}")
    print("-" * 50)
    for r in rows:
        stale = "YES" if r["is_stale"] else "no"
        print(
            f"{r['symbol']:<8} {r['total_bars']:>6} "
            f"{str(r['first_date']):>12} {str(r['last_date']):>12} {stale:>6}"
        )
    print()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trading System — Data Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Fetch and store market data")
    ingest_parser.add_argument("--symbol", "-s", help="Single symbol (default: all active)")
    ingest_parser.add_argument(
        "--force", "-f", action="store_true",
        help=f"Re-fetch full history (ignore last stored date)",
    )

    # coverage command
    subparsers.add_parser("coverage", help="Show data coverage per symbol")

    # Legacy flat args for backwards compat
    parser.add_argument("--all", action="store_true", help="Ingest all symbols")
    parser.add_argument("--symbol", help="Ingest one symbol")
    parser.add_argument("--force", action="store_true", help="Force full re-fetch")
    parser.add_argument("--coverage", action="store_true", help="Show coverage table")

    args = parser.parse_args()

    if args.coverage or (hasattr(args, "command") and args.command == "coverage"):
        sys.exit(cmd_coverage(args))
    else:
        sys.exit(cmd_ingest(args))


if __name__ == "__main__":
    main()
