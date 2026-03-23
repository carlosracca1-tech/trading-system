"""
scripts/seed_symbols.py
Idempotent seeder for the 18-ETF V1 universe.

Usage:
  python scripts/seed_symbols.py            # seed all 18 ETFs
  python scripts/seed_symbols.py --dry-run  # print what would be inserted
  python scripts/seed_symbols.py --show     # show current DB state

Behaviour:
  - INSERT OR IGNORE on (symbol) unique constraint → fully idempotent
  - Re-running never duplicates or overwrites existing records
  - Exits 0 on success, 1 on error

Database:
  Reads DATABASE_URL from environment (set in docker-compose.dev.yml).
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running from the project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packages.shared.constants import ETF_UNIVERSE
from packages.shared.logging_config import get_logger

log = get_logger(__name__)

# Asset type for all V1 symbols
ASSET_TYPE = "etf"


def seed(dry_run: bool = False) -> dict:
    """
    Upsert all 18 ETFs into the symbols table.

    Returns:
        {inserted: int, existing: int, total: int}
    """
    from sqlalchemy import select
    from packages.shared.db import db_session
    from packages.shared.models.symbol import Symbol

    result = {"inserted": 0, "existing": 0, "total": len(ETF_UNIVERSE)}

    with db_session() as session:
        for etf in ETF_UNIVERSE:
            # Check if already present
            existing = session.scalars(
                select(Symbol).where(Symbol.symbol == etf["symbol"])
            ).first()

            if existing:
                result["existing"] += 1
                if dry_run:
                    print(f"  [SKIP]    {etf['symbol']:<6}  already in DB")
                else:
                    log.debug("seed_symbol_exists", symbol=etf["symbol"])
                continue

            if dry_run:
                print(f"  [INSERT]  {etf['symbol']:<6}  {etf['name']}")
                result["inserted"] += 1
                continue

            sym = Symbol(
                symbol=etf["symbol"],
                name=etf["name"],
                sector=etf.get("sector"),
                asset_type=ASSET_TYPE,
                is_active=True,
            )
            session.add(sym)
            result["inserted"] += 1
            log.info("seed_symbol_inserted", symbol=etf["symbol"], name=etf["name"])

        if not dry_run:
            session.commit()

    return result


def show_current() -> None:
    """Print the current state of the symbols table."""
    from sqlalchemy import select
    from packages.shared.db import db_session
    from packages.shared.models.symbol import Symbol

    with db_session() as session:
        symbols = list(
            session.scalars(select(Symbol).order_by(Symbol.symbol)).all()
        )

    if not symbols:
        print("  No symbols in DB. Run: python scripts/seed_symbols.py")
        return

    print(f"\n{'Symbol':<8}  {'Active':<8}  {'Asset':<8}  {'Sector':<25}  Name")
    print("─" * 80)
    for s in symbols:
        active = "✓" if s.is_active else "✗"
        print(
            f"  {s.symbol:<6}  {active:<8}  {s.asset_type or '':<8}  "
            f"{s.sector or '':<25}  {s.name}"
        )
    print(f"\n  Total: {len(symbols)} symbols")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the 18-ETF V1 universe into the symbols table"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be inserted without writing to DB")
    parser.add_argument("--show", action="store_true",
                        help="Show current symbols in DB (no writes)")
    args = parser.parse_args(argv)

    if args.show:
        show_current()
        return 0

    if args.dry_run:
        print(f"\nDRY RUN — {len(ETF_UNIVERSE)} symbols to seed:\n")

    try:
        result = seed(dry_run=args.dry_run)

        if not args.dry_run:
            print(f"\nSeeding complete:")
            print(f"  Inserted: {result['inserted']}")
            print(f"  Existing: {result['existing']}")
            print(f"  Total:    {result['total']}")

            if result["inserted"] > 0:
                print(f"\n  ✓ {result['inserted']} new symbol(s) added.")
            else:
                print("\n  All symbols already present — nothing to do.")
        else:
            print(f"\n  Would insert: {result['inserted']}")
            print(f"  Would skip:   {result['existing']}")

        return 0
    except Exception as exc:
        log.exception("seed_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
