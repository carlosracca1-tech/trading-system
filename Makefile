# =============================================================================
# Makefile — Trading System V1 (RFTM Strategy)
# =============================================================================

.DEFAULT_GOAL := help
.PHONY: help up down restart build logs logs-api \
        migrate migrate-dry shell-db reset-db \
        seed seed-dry seed-show seed-data seed-data-dry seed-data-show \
        fetch-data ingest ingest-full ingest-symbol compute-indicators coverage \
        scan-signals scan-signals-manual evaluate-risk evaluate-risk-manual \
        submit-paper-orders execute-orders confirm-fills \
        take-snapshot snapshot reconcile \
        create-run list-runs run-daily status-run stop-run \
        kill-switch resolve-kill-switch risk-status \
        api-health \
        test test-unit test-cov smoke-test \
        lint format typecheck clean env-check

# ── Config ────────────────────────────────────────────────────────────────────

COMPOSE_FILE  := infra/compose/docker-compose.dev.yml
COMPOSE       := docker compose -f $(COMPOSE_FILE)
API_CONTAINER := trading_api_dev
DB_CONTAINER  := trading_postgres_dev

PYTHON  := python3
PYTEST  := pytest
RUFF    := ruff
MYPY    := mypy

SRC_DIRS := apps packages config migrations

# Default API key (matches docker-compose.dev.yml)
API_KEY ?= dev-api-key-change-me
API_URL ?= http://localhost:8000/api/v1

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  Trading System V1 — Available Commands"
	@echo "  ════════════════════════════════════════════════════════════════"
	@echo ""
	@echo "  Dev Environment:"
	@echo "    make up                  Start PostgreSQL + API (Docker)"
	@echo "    make down                Stop all containers"
	@echo "    make restart             Restart all containers"
	@echo "    make build               Rebuild Docker images (no cache)"
	@echo "    make logs                Tail all container logs"
	@echo "    make logs-api            Tail svc-api logs only"
	@echo ""
	@echo "  Database:"
	@echo "    make migrate             Run Alembic migrations (upgrade head)"
	@echo "    make migrate-dry         Preview SQL without applying"
	@echo "    make shell-db            Open psql shell in trading_dev"
	@echo "    make reset-db            DANGER: drop + recreate DB volume"
	@echo ""
	@echo "  Seeds:"
	@echo "    make seed                Seed 18 ETF symbols (idempotent)"
	@echo "    make seed-dry            Dry run: show what would be seeded"
	@echo "    make seed-show           Show current symbols in DB"
	@echo "    make seed-data           Seed synthetic OHLCV + indicators (no Polygon needed)"
	@echo "    make seed-data-dry       Dry run for seed-data"
	@echo "    make seed-data-show      Show current data coverage"
	@echo ""
	@echo "  Data Pipeline:"
	@echo "    make fetch-data          Fetch real market data from Polygon (all symbols)"
	@echo "    make ingest              (alias for fetch-data)"
	@echo "    make ingest-full         Force full re-fetch (ignores last stored date)"
	@echo "    make ingest-symbol       Fetch one symbol: make ingest-symbol SYMBOL=SPY"
	@echo "    make compute-indicators  Recompute indicators for all symbols"
	@echo "    make coverage            Show data coverage per symbol"
	@echo ""
	@echo "  Manual Pipeline (step-by-step):"
	@echo "    make create-run          Create a new TradingRun (prints run_id)"
	@echo "    make list-runs           List all TradingRuns"
	@echo "    make scan-signals-manual Scan signals        (RUN_ID=<uuid>)"
	@echo "    make evaluate-risk-manual Evaluate risk      (RUN_ID=<uuid>)"
	@echo "    make submit-paper-orders Submit paper orders  (RUN_ID=<uuid>)"
	@echo "    make confirm-fills       Poll broker + update fills (RUN_ID=<uuid>)"
	@echo "    make snapshot            Write portfolio snapshot   (RUN_ID=<uuid>)"
	@echo "    make reconcile           Trigger reconciliation pass"
	@echo ""
	@echo "  Orchestrator (full daily run):"
	@echo "    make run-daily           Run the full daily pipeline (RUN_ID=<uuid>)"
	@echo "    make status-run          Print portfolio status     (RUN_ID=<uuid>)"
	@echo "    make stop-run            Stop the active run        (RUN_ID=<uuid>)"
	@echo ""
	@echo "  Risk & Kill Switch:"
	@echo "    make kill-switch         Manually activate kill switch  (RUN_ID=<uuid>)"
	@echo "    make resolve-kill-switch Resolve kill switch            (RUN_ID=<uuid>)"
	@echo "    make risk-status         Show drawdown + kill switch    (RUN_ID=<uuid>)"
	@echo ""
	@echo "  API:"
	@echo "    make api-health          Check API health endpoint"
	@echo ""
	@echo "  Testing:"
	@echo "    make test                Run all unit tests inside container"
	@echo "    make test-unit           Unit tests only (no integration, no e2e)"
	@echo "    make test-cov            Tests + HTML coverage report"
	@echo "    make smoke-test          E2E smoke test (full flow, inside container)"
	@echo ""
	@echo "  Code Quality:"
	@echo "    make lint                Run ruff linter (check only)"
	@echo "    make format              Auto-fix with ruff"
	@echo "    make typecheck           Run mypy"
	@echo ""
	@echo "  Utilities:"
	@echo "    make env-check           Validate .env file exists"
	@echo "    make clean               Remove build/cache artifacts"
	@echo ""

# ── Dev Environment ───────────────────────────────────────────────────────────

up: env-check
	@echo "Starting dev environment..."
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  API:      http://localhost:8000"
	@echo "  Health:   http://localhost:8000/api/v1/health"
	@echo "  Docs:     http://localhost:8000/docs"
	@echo "  DB:       localhost:5433 (trading / trading_dev)"
	@echo ""
	@echo "Waiting for PostgreSQL to be healthy..."
	@i=0; until docker exec $(DB_CONTAINER) pg_isready -U trading -d trading_dev > /dev/null 2>&1; do \
		i=$$((i+1)); if [ $$i -ge 30 ]; then echo "  ERROR: PostgreSQL did not become healthy after 60s" >&2; exit 1; fi; \
		echo "  ... waiting ($$i/30)"; sleep 2; \
	done
	@echo "  PostgreSQL is healthy"
	@echo ""
	@echo "Running migrations..."
	$(COMPOSE) exec -T svc-api alembic upgrade head
	@echo "  Migrations complete"
	@echo ""
	@echo "  Dev environment ready!"

down:
	@echo "Stopping containers..."
	$(COMPOSE) down
	@echo "  Containers stopped"

restart:
	@echo "Restarting containers..."
	$(COMPOSE) restart
	@echo "  Containers restarted"

build:
	@echo "Rebuilding images (no cache)..."
	$(COMPOSE) build --no-cache
	@echo "  Build complete"

logs:
	$(COMPOSE) logs -f

logs-api:
	$(COMPOSE) logs -f svc-api

# ── Database ──────────────────────────────────────────────────────────────────

migrate: env-check
	@echo "Running migrations..."
	$(COMPOSE) exec -T svc-api alembic upgrade head
	@echo "  Migrations complete"

migrate-dry: env-check
	@echo "Preview migration SQL (dry run)..."
	$(COMPOSE) exec -T svc-api alembic upgrade head --sql

shell-db:
	@echo "Opening psql shell — type \q to exit"
	$(COMPOSE) exec postgres psql -U trading -d trading_dev

reset-db:
	@echo ""
	@echo "  WARNING: This will DESTROY all data in trading-system_pgdata_dev"
	@read -p "  Type 'DESTROY' to confirm: " c; \
	if [ "$$c" != "DESTROY" ]; then echo "Cancelled."; exit 1; fi
	$(COMPOSE) down -v
	@echo "  Volume destroyed. Run 'make up' to recreate."

# ── Seeds ─────────────────────────────────────────────────────────────────────

seed:
	@echo "Seeding 18 ETF symbols into DB..."
	$(COMPOSE) exec -T svc-api python scripts/seed_symbols.py
	@echo "  Done."

seed-dry:
	$(COMPOSE) exec -T svc-api python scripts/seed_symbols.py --dry-run

seed-show:
	$(COMPOSE) exec -T svc-api python scripts/seed_symbols.py --show

seed-data:
	@echo "Seeding synthetic OHLCV + indicators (3 years per symbol)..."
	$(COMPOSE) exec -T svc-api python scripts/seed_market_data.py
	@echo "  Done."

seed-data-dry:
	$(COMPOSE) exec -T svc-api python scripts/seed_market_data.py --dry-run

seed-data-show:
	$(COMPOSE) exec -T svc-api python scripts/seed_market_data.py --show

# ── Data Pipeline (real Polygon data) ─────────────────────────────────────────

fetch-data: ingest

ingest:
	@echo "Ingesting real market data from Polygon (all symbols)..."
	$(COMPOSE) exec -T svc-api python -m apps.svc_data.main --all

ingest-full:
	@echo "Full re-fetch for all symbols (ignores last stored date)..."
	$(COMPOSE) exec -T svc-api python -m apps.svc_data.main --all --force

ingest-symbol:
	@if [ -z "$(SYMBOL)" ]; then echo "Usage: make ingest-symbol SYMBOL=SPY"; exit 1; fi
	$(COMPOSE) exec -T svc-api python -m apps.svc_data.main --symbol $(SYMBOL)

compute-indicators:
	@echo "Recomputing indicators for all symbols (full re-fetch)..."
	$(COMPOSE) exec -T svc-api python -m apps.svc_data.main --all --force

coverage:
	$(COMPOSE) exec -T svc-api python -m apps.svc_data.main --coverage

# ── Orchestrator ───────────────────────────────────────────────────────────────

create-run:
	@echo "Creating a new TradingRun..."
	$(COMPOSE) exec -T svc-api python -m apps.svc_orchestrator.main --create-run
	@echo ""
	@echo "  Copy the UUID above and run:  export RUN_ID=<uuid>"

list-runs:
	@echo "Listing TradingRuns (newest first)..."
	$(COMPOSE) exec -T svc-api python -c "\
from packages.shared.db import db_session; \
from sqlalchemy import select; \
from packages.shared.models.trading_run import TradingRun; \
with db_session() as s: \
    runs = list(s.scalars(select(TradingRun).order_by(TradingRun.started_at.desc()).limit(10)).all()); \
    print(f'\n  {\"ID\":36}  {\"Type\":8}  {\"Status\":10}  Started'); \
    print('  ' + '-'*80); \
    [print(f'  {r.id}  {r.run_type:8}  {r.status:10}  {str(r.started_at)[:19]}') for r in runs]; \
    print(f'\n  {len(runs)} run(s)') \
"

run-daily:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make run-daily RUN_ID=<uuid>"; exit 1; fi
	@echo "Running full daily pipeline for RUN_ID=$(RUN_ID)..."
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_orchestrator.main --run-daily

status-run:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make status-run RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_orchestrator.main --status

stop-run:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make stop-run RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_orchestrator.main --stop

# ── Manual pipeline steps ──────────────────────────────────────────────────────

scan-signals: scan-signals-manual

scan-signals-manual:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make scan-signals-manual RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_strategy.main --scan

evaluate-risk: evaluate-risk-manual

evaluate-risk-manual:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make evaluate-risk-manual RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_risk.main --evaluate

submit-paper-orders: execute-orders

execute-orders:
	@if [ -z "$(RUN_ID)" ]; then \
		echo "Running execute-orders (active run)..."; \
		$(COMPOSE) exec -T svc-api python -m apps.svc_execution.main --execute; \
	else \
		echo "Running execute-orders for RUN_ID=$(RUN_ID)..."; \
		$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_execution.main --execute; \
	fi

confirm-fills: reconcile

snapshot: take-snapshot

take-snapshot:
	@if [ -z "$(RUN_ID)" ]; then \
		echo "Writing snapshot (active run)..."; \
		$(COMPOSE) exec -T svc-api python -m apps.svc_execution.main --snapshot; \
	else \
		echo "Writing snapshot for RUN_ID=$(RUN_ID)..."; \
		$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_execution.main --snapshot; \
	fi

reconcile:
	@echo "Triggering reconciliation via API..."
	@if [ -z "$(RUN_ID)" ]; then \
		curl -sf -X POST "$(API_URL)/system/reconcile" \
			-H "X-API-KEY: $(API_KEY)" | python3 -m json.tool 2>/dev/null || echo "  No active run or API not reachable"; \
	else \
		curl -sf -X POST "$(API_URL)/system/reconcile?run_id=$(RUN_ID)" \
			-H "X-API-KEY: $(API_KEY)" | python3 -m json.tool 2>/dev/null || echo "  Error: check API is up"; \
	fi

# ── Risk & Kill Switch ─────────────────────────────────────────────────────────

kill-switch:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make kill-switch RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -it -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_risk.main --kill-switch

resolve-kill-switch:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make resolve-kill-switch RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_risk.main --resolve-kill-switch

risk-status:
	@if [ -z "$(RUN_ID)" ]; then echo "Usage: make risk-status RUN_ID=<uuid>"; exit 1; fi
	$(COMPOSE) exec -T -e RUN_ID=$(RUN_ID) svc-api python -m apps.svc_risk.main --status

# ── API ────────────────────────────────────────────────────────────────────────

api-health:
	@echo "=== Public health check ==="
	@curl -sf "$(API_URL)/health" | python3 -m json.tool || echo "  API not reachable at $(API_URL)"
	@echo ""
	@echo "=== Detailed health check ==="
	@curl -sf "$(API_URL)/health/detailed" -H "X-API-KEY: $(API_KEY)" | python3 -m json.tool || echo "  API not reachable"

# ── Testing ────────────────────────────────────────────────────────────────────

test:
	@echo "Running all tests inside svc-api container..."
	$(COMPOSE) exec -T svc-api pytest tests/ -v --tb=short

test-unit:
	@echo "Running unit tests inside svc-api container..."
	$(COMPOSE) exec -T svc-api pytest tests/ -v --tb=short -m "not integration and not e2e"

test-cov:
	@echo "Running tests with coverage inside svc-api container..."
	$(COMPOSE) exec -T svc-api pytest tests/ -v --tb=short \
		--cov=apps --cov=packages \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@echo ""
	@echo "  Coverage report: htmlcov/index.html"

smoke-test:
	@echo "Running E2E smoke test inside svc-api container..."
	$(COMPOSE) exec -T svc-api pytest tests/test_smoke.py -v --tb=short -s
	@echo ""
	@echo "  Tip: for Docker-level E2E test, run:  bash scripts/smoke_test.sh"

# ── Code Quality ──────────────────────────────────────────────────────────────

lint:
	$(RUFF) check $(SRC_DIRS) tests

format:
	$(RUFF) check --fix $(SRC_DIRS) tests
	$(RUFF) format $(SRC_DIRS) tests

typecheck:
	$(MYPY) $(SRC_DIRS) --ignore-missing-imports

# ── Utilities ──────────────────────────────────────────────────────────────────

env-check:
	@if [ ! -f ".env" ]; then \
		echo "  .env not found — copying from .env.example"; \
		cp .env.example .env; \
		echo "  .env created — review and set API_KEY before use"; \
	fi

clean:
	@echo "Cleaning build artifacts..."
	@find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@rm -rf htmlcov/ .coverage coverage.xml 2>/dev/null || true
	@echo "  Clean complete"

# ── F2: DB sync con branch state/db ──────────────────────────────────────────

sync-db:
	@bash scripts/sync_db.sh

sync-db-force:
	@bash scripts/sync_db.sh --force
