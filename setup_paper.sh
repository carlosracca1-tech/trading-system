#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  setup_paper.sh — One-click Paper Trading Setup
#
#  What this script does:
#    1. Validates your .env.paper configuration
#    2. Copies it to .env (used by Docker)
#    3. Starts PostgreSQL + API containers (Docker)
#    4. Runs database migrations
#    5. Seeds 18 ETF symbols into the DB
#    6. Seeds 3 years of synthetic OHLCV + indicators (no Polygon key needed)
#       OR fetches real data from Polygon if POLYGON_API_KEY is set
#    7. Creates a paper trading run
#    8. Saves the RUN_ID to .paper_run_id for use by paper_trade.sh
#
#  Prerequisites:
#    - Docker Desktop running
#    - .env.paper filled with your Alpaca paper keys
#
#  Usage:
#    bash setup_paper.sh
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
header()  { echo -e "\n${BOLD}$*${NC}"; echo "$(printf '─%.0s' {1..60})"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Step 1: Validate .env.paper ───────────────────────────────────────────────
header "Step 1/7 — Validate configuration"

if [ ! -f ".env.paper" ]; then
    error ".env.paper not found in $(pwd)"
    error "Run: cp .env.example .env.paper  then fill in your Alpaca keys"
    exit 1
fi

ALPACA_KEY=$(grep -E "^ALPACA_API_KEY=" .env.paper | cut -d= -f2 | tr -d ' ')
ALPACA_SECRET=$(grep -E "^ALPACA_SECRET_KEY=" .env.paper | cut -d= -f2 | tr -d ' ')
POLYGON_KEY=$(grep -E "^POLYGON_API_KEY=" .env.paper | cut -d= -f2 | tr -d ' ')

if [ -z "$ALPACA_KEY" ] || [ "$ALPACA_KEY" = "YOUR_ALPACA_PAPER_API_KEY_HERE" ]; then
    error "ALPACA_API_KEY is not set in .env.paper"
    error ""
    error "  1. Go to https://app.alpaca.markets"
    error "  2. Switch to Paper Trading (toggle in the top-right)"
    error "  3. Go to Overview → API Keys → Generate New Key"
    error "  4. Paste the key into .env.paper"
    exit 1
fi

if [ -z "$ALPACA_SECRET" ] || [ "$ALPACA_SECRET" = "YOUR_ALPACA_PAPER_SECRET_KEY_HERE" ]; then
    error "ALPACA_SECRET_KEY is not set in .env.paper"
    exit 1
fi

success "Alpaca paper keys found"

if [ -z "$POLYGON_KEY" ]; then
    warn "POLYGON_API_KEY not set — will use synthetic market data"
    USE_REAL_DATA=false
else
    info "POLYGON_API_KEY found — will fetch real market data"
    USE_REAL_DATA=true
fi

# ── Step 2: Copy to .env ─────────────────────────────────────────────────────
header "Step 2/7 — Apply configuration"

cp .env.paper .env
success ".env.paper → .env"

# ── Step 3: Start Docker ─────────────────────────────────────────────────────
header "Step 3/7 — Start containers (Docker)"

if ! command -v docker &>/dev/null; then
    error "Docker is not installed or not in PATH"
    error "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    exit 1
fi

if ! docker info &>/dev/null; then
    error "Docker daemon is not running. Please start Docker Desktop and retry."
    exit 1
fi

success "Docker is running"

COMPOSE_FILE="infra/compose/docker-compose.dev.yml"
info "Starting containers..."
docker compose -f "$COMPOSE_FILE" up -d --build

# Wait for PostgreSQL
info "Waiting for PostgreSQL to be ready..."
DB_CONTAINER="trading_postgres_dev"
for i in $(seq 1 30); do
    if docker exec "$DB_CONTAINER" pg_isready -U trading -d trading_dev &>/dev/null; then
        success "PostgreSQL is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "PostgreSQL did not become ready after 60s"
        docker compose -f "$COMPOSE_FILE" logs postgres
        exit 1
    fi
    echo -n "."
    sleep 2
done

# ── Step 4: Migrations ────────────────────────────────────────────────────────
header "Step 4/7 — Database migrations"

info "Running Alembic migrations..."
docker compose -f "$COMPOSE_FILE" exec -T svc-api alembic upgrade head
success "Migrations complete"

# ── Step 5: Seed symbols ──────────────────────────────────────────────────────
header "Step 5/7 — Seed ETF symbols"

info "Seeding 18 RFTM ETF symbols..."
docker compose -f "$COMPOSE_FILE" exec -T svc-api python scripts/seed_symbols.py
success "Symbols seeded"

# ── Step 6: Market data ────────────────────────────────────────────────────────
header "Step 6/7 — Market data"

if [ "$USE_REAL_DATA" = true ]; then
    info "Fetching real OHLCV data from Polygon.io (this may take ~5 min on free tier)..."
    docker compose -f "$COMPOSE_FILE" exec -T svc-api python -m apps.svc_data.main --all
    success "Real market data ingested"
else
    info "Seeding 3 years of synthetic OHLCV + indicators (fast, no API needed)..."
    docker compose -f "$COMPOSE_FILE" exec -T svc-api python scripts/seed_market_data.py
    success "Synthetic market data seeded"
fi

# ── Step 7: Create run ────────────────────────────────────────────────────────
header "Step 7/7 — Create paper trading run"

info "Creating new paper trading run..."
RUN_OUTPUT=$(docker compose -f "$COMPOSE_FILE" exec -T svc-api python -m apps.svc_orchestrator.main --create-run 2>&1)
echo "$RUN_OUTPUT"

# Extract UUID from output
RUN_ID=$(echo "$RUN_OUTPUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)

if [ -z "$RUN_ID" ]; then
    error "Could not extract RUN_ID from output. Check the output above."
    error "You can get the RUN_ID manually with: make list-runs"
    exit 1
fi

echo "$RUN_ID" > .paper_run_id
success "Run created: $RUN_ID"
success "RUN_ID saved to .paper_run_id"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  Paper Trading Setup Complete!${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}RUN_ID:${NC}     $RUN_ID"
echo -e "  ${BOLD}Mode:${NC}       Paper Trading (Alpaca)"
echo -e "  ${BOLD}Orders:${NC}     REAL paper orders (no real money)"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "    Run the bot now:    ${YELLOW}bash paper_trade.sh${NC}"
echo -e "    Schedule daily:     ${YELLOW}bash paper_trade.sh --schedule${NC}"
echo -e "    Check positions:    ${YELLOW}make status-run RUN_ID=$RUN_ID${NC}"
echo -e "    View API docs:      ${YELLOW}http://localhost:8000/docs${NC}"
echo -e "    View Alpaca paper:  ${YELLOW}https://app.alpaca.markets${NC}"
echo ""
echo -e "  ${BOLD}Stop everything:${NC}  make down"
echo ""
