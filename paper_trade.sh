#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  paper_trade.sh — Daily Paper Trading Runner
#
#  Two modes:
#   • STANDALONE (default, no Docker needed):
#       Uses standalone_paper_trader.py — only requires Python + pandas/numpy
#       Orders go directly to Alpaca paper trading via REST API
#
#   • FULL STACK (--docker):
#       Uses Docker + PostgreSQL + full FastAPI stack
#       More robust, requires Docker Desktop running
#
#  Usage:
#    bash paper_trade.sh              # standalone run (recommended)
#    bash paper_trade.sh --docker     # Docker-based run
#    bash paper_trade.sh --schedule   # schedule daily at 09:35 ET
#    bash paper_trade.sh --status     # show portfolio status
#    bash paper_trade.sh --stop       # stop the active run
#
#  Prerequisites: fill in .env.paper with your Alpaca paper trading keys
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC}  $*"; }
error()   { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="infra/compose/docker-compose.dev.yml"
COMPOSE="docker compose -f $COMPOSE_FILE"

# ── Load RUN_ID ────────────────────────────────────────────────────────────────
load_run_id() {
    if [ -n "${RUN_ID:-}" ]; then
        return 0
    fi
    if [ -f ".paper_run_id" ]; then
        RUN_ID=$(cat .paper_run_id | tr -d '[:space:]')
        if [ -n "$RUN_ID" ]; then
            return 0
        fi
    fi
    error "No RUN_ID found. Run setup_paper.sh first, or set: export RUN_ID=<uuid>"
    exit 1
}

# ── Check Docker ───────────────────────────────────────────────────────────────
check_docker() {
    if ! docker info &>/dev/null; then
        error "Docker is not running. Start Docker Desktop and retry."
        exit 1
    fi
    if ! docker ps --format '{{.Names}}' | grep -q "trading_api_dev"; then
        warn "Containers are not running. Starting them..."
        $COMPOSE up -d
        sleep 5
    fi
}

# ── Single run ─────────────────────────────────────────────────────────────────
run_pipeline() {
    load_run_id
    check_docker

    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  RFTM Paper Trading — $(date '+%Y-%m-%d %H:%M:%S')${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
    echo -e "  RUN_ID: $RUN_ID"
    echo ""

    # Optional: update market data from Polygon
    POLYGON_KEY=$(grep -E "^POLYGON_API_KEY=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "")
    if [ -n "$POLYGON_KEY" ]; then
        info "Updating market data from Polygon.io..."
        $COMPOSE exec -T svc-api python -m apps.svc_data.main --all
        success "Market data updated"
    else
        warn "No POLYGON_API_KEY — skipping data update (using existing DB data)"
    fi

    # Run the full pipeline
    info "Running full daily pipeline..."
    $COMPOSE exec -T -e RUN_ID="$RUN_ID" svc-api python -m apps.svc_orchestrator.main --run-daily

    echo ""
    success "Pipeline complete!"
    echo ""

    # Print portfolio status
    info "Portfolio status:"
    $COMPOSE exec -T -e RUN_ID="$RUN_ID" svc-api python -m apps.svc_orchestrator.main --status 2>/dev/null || true

    echo ""
    echo -e "  ${BOLD}View your paper positions:${NC}  ${YELLOW}https://app.alpaca.markets${NC}"
    echo -e "  ${BOLD}Run again tomorrow:${NC}         ${YELLOW}bash paper_trade.sh${NC}"
    echo ""
}

# ── Status ─────────────────────────────────────────────────────────────────────
show_status() {
    load_run_id
    check_docker
    echo ""
    echo -e "${BOLD}Portfolio Status — RUN_ID: $RUN_ID${NC}"
    echo "$(printf '─%.0s' {1..60})"
    $COMPOSE exec -T -e RUN_ID="$RUN_ID" svc-api python -m apps.svc_orchestrator.main --status
    echo ""
}

# ── Stop run ───────────────────────────────────────────────────────────────────
stop_run() {
    load_run_id
    check_docker
    echo ""
    warn "Stopping run $RUN_ID..."
    $COMPOSE exec -T -e RUN_ID="$RUN_ID" svc-api python -m apps.svc_orchestrator.main --stop
    rm -f .paper_run_id
    success "Run stopped. RUN_ID file removed."
    echo ""
}

# ── Schedule (cron) ────────────────────────────────────────────────────────────
schedule_daily() {
    load_run_id
    SCRIPT_PATH="$(realpath "$SCRIPT_DIR/paper_trade.sh")"
    CRON_CMD="35 9 * * 1-5 cd '$SCRIPT_DIR' && RUN_ID='$RUN_ID' bash '$SCRIPT_PATH' >> '$SCRIPT_DIR/paper_trade.log' 2>&1"

    echo ""
    echo -e "${BOLD}Daily Schedule Setup${NC}"
    echo "$(printf '─%.0s' {1..60})"
    echo ""
    echo "  This will add a cron job to run at 09:35 AM ET on weekdays."
    echo "  (35 minutes after NYSE opens at 09:30 — gives prices time to settle)"
    echo ""
    echo "  Cron entry:"
    echo "  $CRON_CMD"
    echo ""
    read -rp "  Add to crontab? [y/N] " confirm
    if [ "${confirm,,}" != "y" ]; then
        echo "  Cancelled."
        return
    fi

    ( crontab -l 2>/dev/null | grep -v "paper_trade.sh"; echo "$CRON_CMD" ) | crontab -
    success "Cron job added! The bot will run every weekday at 09:35 AM ET."
    echo ""
    echo -e "  ${BOLD}To remove:${NC}  crontab -e  (then delete the paper_trade.sh line)"
    echo -e "  ${BOLD}Logs at:${NC}    $SCRIPT_DIR/paper_trade.log"
    echo ""
}

# ── Standalone runner (no Docker) ─────────────────────────────────────────────
run_standalone() {
    info "Running standalone RFTM paper trader..."
    python3 "$SCRIPT_DIR/standalone_paper_trader.py"
}

standalone_status() {
    python3 "$SCRIPT_DIR/standalone_paper_trader.py" --status
}

standalone_stop() {
    python3 "$SCRIPT_DIR/standalone_paper_trader.py" --reset
    warn "Standalone run reset. Run paper_trade.sh to start fresh."
}

# ── Main ───────────────────────────────────────────────────────────────────────
case "${1:-}" in
    --docker)
        case "${2:-}" in
            --status)   show_status ;;
            --stop)     stop_run ;;
            "")         run_pipeline ;;
            *)          error "Unknown option: $2"; exit 1 ;;
        esac
        ;;
    --status)   standalone_status ;;
    --stop)     standalone_stop ;;
    --schedule) schedule_daily ;;
    --help|-h)
        echo ""
        echo "  Usage: bash paper_trade.sh [OPTION]"
        echo ""
        echo "  Standalone (default, no Docker):"
        echo "    (none)           Run the trading pipeline"
        echo "    --status         Show portfolio + positions"
        echo "    --stop           Reset the current run"
        echo "    --schedule       Add daily cron (09:35 AM ET, Mon-Fri)"
        echo ""
        echo "  Docker-based (full stack):"
        echo "    --docker         Run pipeline via Docker"
        echo "    --docker --status"
        echo "    --docker --stop"
        echo ""
        echo "    --help           Show this help"
        echo ""
        ;;
    "")         run_standalone ;;
    *)
        error "Unknown option: $1"
        echo "  Run with --help for usage."
        exit 1
        ;;
esac
