#!/usr/bin/env bash
# start_dev.sh — Full dev environment startup script.
#
# What it does:
#   1. Verifies Docker is running
#   2. Copies .env.example to .env if .env doesn't exist
#   3. Brings up PostgreSQL + API containers
#   4. Waits for DB to be healthy
#   5. Runs Alembic migrations
#   6. Prints status
#
# Usage (from project root):
#   chmod +x scripts/start_dev.sh
#   ./scripts/start_dev.sh

set -euo pipefail

COMPOSE_FILE="infra/compose/docker-compose.dev.yml"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$PROJECT_ROOT"

echo "═══════════════════════════════════════════════════════"
echo "  Trading System — Dev Environment Startup"
echo "═══════════════════════════════════════════════════════"

# ── 1. Check Docker ───────────────────────────────────────────────────────────
if ! docker info > /dev/null 2>&1; then
    echo "❌  Docker is not running. Please start Docker Desktop and retry."
    exit 1
fi
echo "✓  Docker is running"

# ── 2. Setup .env ─────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "  .env not found — copying from .env.example"
    cp .env.example .env
    echo "✓  .env created from template"
    echo "  ⚠  Review .env before running in paper/live mode"
else
    echo "✓  .env already exists"
fi

# ── 3. Build & start containers ───────────────────────────────────────────────
echo ""
echo "Starting containers..."
docker compose -f "$COMPOSE_FILE" up -d --build

# ── 4. Wait for PostgreSQL ────────────────────────────────────────────────────
echo ""
echo "Waiting for PostgreSQL to be healthy..."
MAX_WAIT=60
WAITED=0
until docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_isready -U trading -d trading_dev > /dev/null 2>&1; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "❌  PostgreSQL did not become healthy within ${MAX_WAIT}s"
        docker compose -f "$COMPOSE_FILE" logs postgres
        exit 1
    fi
    sleep 2
    WAITED=$((WAITED + 2))
    echo "  ... waiting (${WAITED}s)"
done
echo "✓  PostgreSQL is healthy"

# ── 5. Run migrations ─────────────────────────────────────────────────────────
echo ""
echo "Running database migrations..."
# Run inside the API container (has alembic installed)
docker compose -f "$COMPOSE_FILE" exec -T svc-api \
    alembic upgrade head
echo "✓  Migrations complete"

# ── 6. Final status ───────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✓  Dev environment is ready!"
echo ""
echo "  API:           http://localhost:8000"
echo "  Health:        http://localhost:8000/api/v1/health"
echo "  Docs (Swagger): http://localhost:8000/docs"
echo "  PostgreSQL:    localhost:5433  (user: trading)"
echo ""
echo "  Logs:     docker compose -f ${COMPOSE_FILE} logs -f"
echo "  Stop:     docker compose -f ${COMPOSE_FILE} down"
echo "  DB shell: docker compose -f ${COMPOSE_FILE} exec postgres psql -U trading -d trading_dev"
echo "═══════════════════════════════════════════════════════"
