#!/usr/bin/env bash
# migrate.sh — Run Alembic migrations against the target database.
#
# Usage:
#   ./scripts/migrate.sh             → runs against DATABASE_URL in .env
#   ./scripts/migrate.sh --dry-run   → shows SQL without applying
#   DATABASE_URL=postgresql://... ./scripts/migrate.sh
#
# The script must be run from the project root directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# ── Load .env if present ──────────────────────────────────────────────────────
if [ -f ".env" ]; then
    echo "Loading .env..."
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

# ── Safety check for live mode ────────────────────────────────────────────────
if [ "${TRADING_MODE:-dev}" = "live" ]; then
    echo ""
    echo "⚠  WARNING: TRADING_MODE=live"
    echo "   You are about to run migrations on the PRODUCTION database."
    echo "   DATABASE_URL = ${DATABASE_URL:-NOT SET}"
    echo ""
    read -p "Type 'CONFIRM_LIVE_MIGRATION' to proceed: " confirmation
    if [ "$confirmation" != "CONFIRM_LIVE_MIGRATION" ]; then
        echo "Migration cancelled."
        exit 1
    fi
fi

# ── Show current revision ─────────────────────────────────────────────────────
echo ""
echo "Current migration state:"
alembic current

# ── Dry run mode ──────────────────────────────────────────────────────────────
if [ "${1:-}" = "--dry-run" ]; then
    echo ""
    echo "Dry run — showing SQL that would be executed:"
    alembic upgrade head --sql
    exit 0
fi

# ── Run migrations ────────────────────────────────────────────────────────────
echo ""
echo "Running migrations: alembic upgrade head"
alembic upgrade head

echo ""
echo "Migration complete. Final state:"
alembic current
