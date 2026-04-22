# Trading System V1 — Operational Runbook

## Overview

RFTM Strategy (Regime-Filtered Trend Momentum) across 18 ETFs.
Paper trading via Alpaca · Dev mode uses synthetic data (no API key required).

---

## Scheduling (standalone bots)

**Único scheduler activo: GitHub Actions.** El launchd local está **deprecated** desde 2026-04-22.

| Bot | Workflow | Cron (UTC) | Notas |
|-----|----------|-----------|-------|
| RFTM (ETFs, diario) | `.github/workflows/daily_trade.yml` | `35 13 * * 1-5` | 13:35 UTC ≈ 9:35 ET |
| MREV (cripto+ETFs, 1h) | `.github/workflows/mrev_hourly.yml` | `5 * * * *` | minuto 5 cada hora, `concurrency: mrev-hourly` |

**No correr launchd local** (`com.rftm.trader.plist` + `setup_autorun.sh`): causa doble ejecución
y doble-compra. Los archivos se mantienen como referencia histórica.

Para desinstalar un agente launchd ya cargado:
```bash
launchctl unload ~/Library/LaunchAgents/com.rftm.trader.plist
rm ~/Library/LaunchAgents/com.rftm.trader.plist
```

```
Architecture:
  svc_data      → ingest OHLCV + compute indicators
  svc_strategy  → scan signals (ENTER / EXIT / HOLD)
  svc_risk      → evaluate signals (position sizing + risk rules)
  svc_execution → submit orders (DryRunBroker | AlpacaBroker)
  svc_api       → REST API (FastAPI)
  svc_orchestrator → daily runner (coordinates the above)
```

---

## Quick Start (Dev / Synthetic Data)

```bash
# 1. Start containers
make up

# 2. Run migrations
make migrate

# 3. Seed ETF symbols (18 symbols)
make seed

# 4. Seed synthetic market data (3 years, no Polygon required)
make seed-data

# 5. Verify coverage
make seed-data-show

# 6. Create a trading run
make create-run
# → prints: Created TradingRun: <RUN_ID>

# 7. Run the full daily pipeline
make run-daily RUN_ID=<RUN_ID>

# 8. Check system health
make api-health

# 9. Run smoke tests
make smoke-test
```

---

## Environment Variables

| Variable | Dev Default | Description |
|---|---|---|
| `TRADING_MODE` | `dev` | `dev` / `paper` / `live` |
| `DRY_RUN` | `true` | `true` → no real orders |
| `API_KEY` | `dev-api-key-change-me` | X-API-KEY header |
| `DATABASE_URL` | `postgresql://trading:trading_dev_pass@localhost:5433/trading_dev` | |
| `ALPACA_API_KEY` | — | Required for paper/live |
| `ALPACA_SECRET_KEY` | — | Required for paper/live |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Paper endpoint |
| `INITIAL_CAPITAL` | `100000` | Starting cash |
| `LOG_FORMAT` | `console` | `json` in production |

Configuration lives in `.env` (copy from `.env.example`).
Docker Compose injects vars from `infra/compose/docker-compose.dev.yml`.

---

## Docker Operations

```bash
make up                  # Start PostgreSQL + API
make down                # Stop all containers
make restart             # Restart all containers
make build               # Rebuild images (no cache)
make logs                # Tail all logs
make logs-api            # Tail API logs only
```

**Container names:**
- `trading_postgres_dev` — PostgreSQL 15 + TimescaleDB
- `trading_api_dev`       — FastAPI (port 8000)

**Ports:**
- `127.0.0.1:5433` → PostgreSQL
- `127.0.0.1:8000` → API

---

## Database Operations

```bash
make migrate             # Apply all pending migrations
make migrate-dry         # Preview SQL without applying
make shell-db            # Open psql shell in trading_dev
make reset-db            # DANGER: drop + recreate (wipes all data)
```

Manual psql access:
```bash
docker exec -it trading_postgres_dev psql -U trading -d trading_dev
```

---

## Seeds

```bash
make seed                # Seed 18 ETF symbols into symbols table (idempotent)
make seed-dry            # Preview only
make seed-show           # Show current symbols

make seed-data           # Generate synthetic OHLCV + indicators (756 trading days)
make seed-data-dry       # Preview counts only
make seed-data-show      # Show current data coverage per symbol

# One symbol only:
docker exec trading_api_dev python scripts/seed_market_data.py --symbol SPY
docker exec trading_api_dev python scripts/seed_market_data.py --days 300
```

**Note:** Synthetic data uses a geometric random-walk with upward drift in the
last 40% of history to ensure ENTER signals are generated during dev/testing.
It is **NOT** real market data. Never use for live trading.

---

## Manual Pipeline (Step-by-Step)

Use this to run and inspect each stage individually.

### Step 1: Create a TradingRun
```bash
make create-run
# → Created TradingRun (PAPER): abc123-...
# Copy the run_id for subsequent steps.
```

Or via API:
```bash
curl -s -X POST http://localhost:8000/api/v1/runs \
  -H "X-API-KEY: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"run_type":"PAPER","initial_capital":100000}' | python3 -m json.tool
```

### Step 2: List all runs
```bash
make list-runs
# Or:
curl -s -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/runs | python3 -m json.tool
```

### Step 3: Scan signals
```bash
make scan-signals-manual RUN_ID=<RUN_ID>
# Equivalent:
docker exec trading_api_dev python -m apps.svc_strategy.main --run-id <RUN_ID>
```

### Step 4: Evaluate risk
```bash
make evaluate-risk-manual RUN_ID=<RUN_ID>
# Equivalent:
docker exec trading_api_dev python -m apps.svc_risk.main --run-id <RUN_ID>
```

### Step 5: Submit paper orders
```bash
make submit-paper-orders RUN_ID=<RUN_ID>
# Equivalent:
docker exec trading_api_dev python -m apps.svc_execution.main --execute --dry-run
```

### Step 6: Confirm fills (reconcile)
```bash
make confirm-fills RUN_ID=<RUN_ID>
# Equivalent:
docker exec trading_api_dev python -m apps.svc_execution.main --reconcile
```

### Step 7: Take portfolio snapshot
```bash
make snapshot
# Equivalent:
docker exec trading_api_dev python -m apps.svc_execution.main --snapshot
```

### Full daily run (all stages in one command)
```bash
make run-daily RUN_ID=<RUN_ID>
# Equivalent:
docker exec trading_api_dev python -m apps.svc_orchestrator.runner --run-id <RUN_ID>
```

---

## API Reference

Base URL: `http://localhost:8000/api/v1`
Auth: `X-API-KEY: dev-api-key-change-me` header required on all endpoints except `/health`.

### Health
```bash
# Liveness (public)
curl http://localhost:8000/api/v1/health

# Detailed (authenticated)
curl -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/health/detailed
```

### Runs
```bash
# List runs
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/runs?limit=10"

# Get one run
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/runs/<RUN_ID>"

# Create run
curl -X POST -H "X-API-KEY: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"run_type":"PAPER","initial_capital":100000}' \
  http://localhost:8000/api/v1/runs

# Stop run
curl -X DELETE -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/runs/<RUN_ID>"
```

### Portfolio
```bash
# Current portfolio (snapshot + open positions)
curl -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/portfolio

# Equity curve
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/portfolio/snapshots?limit=30"
```

### Positions
```bash
# List all positions (open + closed)
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/positions"

# Open positions only
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/positions?status=OPEN"
```

### Signals
```bash
# List signals
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/signals?signal_type=ENTER&limit=20"
```

### Orders
```bash
# List orders
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/orders"
```

### System
```bash
# System status
curl -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/system/status

# Risk events
curl -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/system/risk-events?limit=20"

# Trigger reconcile
curl -X POST -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/system/reconcile
```

---

## Kill Switch

The kill switch is the P0 emergency stop. It immediately:
1. Submits SELL orders for all open positions
2. Sets TradingRun.status = STOPPED
3. Writes a P0_KILL_SWITCH RiskEvent to the DB

**Automatic trigger:** drawdown ≥ 15% of peak equity.

### Activate (manual)
```bash
make kill-switch RUN_ID=<RUN_ID>

# Or via API:
curl -X POST -H "X-API-KEY: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"run_id":"<RUN_ID>","reason":"manual_stop"}' \
  http://localhost:8000/api/v1/system/kill-switch

# Or via Makefile (uses current active run):
make kill-switch
```

### Check status
```bash
make risk-status
# Or:
curl -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/system/status
# Look for: "kill_switch_active": true / "status": "kill_switch_active"
```

### Resolve (re-enable trading)
```bash
make resolve-kill-switch RUN_ID=<RUN_ID>

# Or via API:
curl -X DELETE -H "X-API-KEY: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"run_id":"<RUN_ID>","resolved_by":"operator"}' \
  http://localhost:8000/api/v1/system/kill-switch
```

**Important:** Resolving the kill switch sets the run back to RUNNING.
It does **NOT** reopen closed positions. You start fresh with the remaining cash.

---

## Tests

```bash
# Run all unit tests (no Docker required)
make test

# Run with coverage report
make test-cov

# Run specific test file
pytest tests/test_smoke.py -v
pytest tests/test_api.py -v
pytest tests/test_kill_switch.py -v
pytest tests/test_execution.py -v

# Shell E2E smoke test (requires running Docker)
make smoke-test
# Or directly:
bash scripts/smoke_test.sh --verbose
```

**All tests except `smoke_test.sh` run without Docker or PostgreSQL.**
They use SQLite in-memory or mock objects.

---

## Data Pipeline (Real Data via Polygon)

For real market data (paper/live trading), set:
```bash
POLYGON_API_KEY=your_key_here
```

Then:
```bash
make fetch-data              # Fetch all 18 ETFs (last stored date → today)
make ingest-full             # Force full re-fetch
make ingest-symbol SYMBOL=SPY  # Single symbol
make compute-indicators      # Recompute all indicators
make coverage                # Show coverage per symbol
```

---

## Monitoring

```bash
# API health
make api-health

# System status via API
curl -s -H "X-API-KEY: dev-api-key-change-me" \
  http://localhost:8000/api/v1/system/status | python3 -m json.tool

# Recent risk events (last 20)
curl -s -H "X-API-KEY: dev-api-key-change-me" \
  "http://localhost:8000/api/v1/system/risk-events?limit=20" | python3 -m json.tool

# Live container logs
make logs-api

# Postgres queries for debugging:
docker exec -it trading_postgres_dev psql -U trading -d trading_dev -c \
  "SELECT id, run_type, status, started_at, total_trades FROM trading_runs ORDER BY started_at DESC LIMIT 5;"

docker exec -it trading_postgres_dev psql -U trading -d trading_dev -c \
  "SELECT symbol, signal_type, risk_decision, signal_date FROM signals ORDER BY created_at DESC LIMIT 20;"

docker exec -it trading_postgres_dev psql -U trading -d trading_dev -c \
  "SELECT symbol, status, qty, entry_price, unrealized_pnl FROM positions WHERE status='OPEN';"
```

---

## Switching to Paper Trading (Alpaca)

1. Set env vars:
```bash
# In .env:
TRADING_MODE=paper
DRY_RUN=false
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

2. Update docker-compose or export env vars before starting.

3. The `AlpacaBroker` is automatically selected when `DRY_RUN=false`.

4. In paper mode, `submit_order()` calls `alpaca_py.TradingClient.submit_order_request()`.

**Never set `DRY_RUN=false` and `TRADING_MODE=live` at the same time unless
you intend to place real orders.**

---

## Common Issues & Solutions

### API returns 500 on startup
Check DB connectivity:
```bash
make logs-api | grep "startup_db"
# If "startup_db_unreachable": run `make migrate` first
```

### "No active RUNNING TradingRun found"
```bash
make create-run
# Then use the printed RUN_ID for subsequent commands
```

### Seeds fail with "not in symbols table"
Run `make seed` (seeds the symbols) before `make seed-data` (seeds the OHLCV).

### scan-signals produces no ENTER signals
- Check data coverage: `make seed-data-show`
- Synthetic data generates trend signals in the last 40% of history
- Verify SPY EMA200 is computed: the scanner requires 200+ bars

### Kill switch can't be resolved
Only a STOPPED run with a P0_KILL_SWITCH RiskEvent can be resolved.
Check with `make risk-status` first.

### Port 5433 already in use
```bash
lsof -i :5433   # find the process
kill -9 <PID>
make up
```

### Container logs show `ImportError`
```bash
make build      # Rebuild the image to pick up code changes
make restart
```

---

## Directory Structure

```
trading-system/
├── apps/
│   ├── api/            FastAPI app + routers + schemas
│   ├── svc_data/       Market data ingest + indicators
│   ├── svc_strategy/   Signal scanner (RFTM)
│   ├── svc_risk/       Risk engine + kill switch
│   ├── svc_execution/  Order execution + broker adapters
│   └── svc_orchestrator/ Daily runner + pipeline
├── packages/
│   └── shared/         Models, enums, DB, logging
├── config/             Settings (pydantic-settings)
├── migrations/         Alembic versions
├── tests/              All test files
├── scripts/            seed_market_data.py, smoke_test.sh
├── infra/              Docker Compose + Dockerfiles
├── Makefile            All operational commands
├── .env                Local env (gitignored)
└── .env.example        Template
```

---

## Risk Rules Summary

| Code | Priority | Rule | Threshold |
|---|---|---|---|
| P0_KILL_SWITCH | P0 | Manual or auto drawdown stop | Triggered by P1 or manual |
| P1_MAX_DRAWDOWN | P1 | Max portfolio drawdown | 15% from peak |
| P2_MAX_POSITIONS | P2 | Max open positions | 5 concurrent |
| P3_MAX_POSITION_SIZE | P3 | Max notional per position | 10% of portfolio |
| P4_MIN_SHARES | P4 | Minimum viable position | ≥ 1 share |

Rules are evaluated in priority order. The first rejection wins.

---

## RFTM Strategy Entry Conditions

All conditions must hold simultaneously:

1. **Regime filter**: SPY close > SPY EMA200 (bullish market)
2. **Trend alignment**: close > EMA50 > EMA200
3. **Momentum zone**: 50 ≤ RSI14 ≤ 70
4. **20-day breakout**: close ≥ 20-day high
5. **Volume confirmation**: volume ≥ volume_ma_20 × 1.2
6. **Volatility filter**: 0.01 ≤ ATR14/close ≤ 0.05

## Exit Conditions (priority order)

| Code | Condition |
|---|---|
| E1 | Death cross: EMA50 < EMA200 |
| E2 | Trend broken: close < EMA50 |
| E3 | Stop loss: close ≤ entry_price − 2 × ATR14 |
| E4 | Overbought: RSI14 > 80 |

---

*Last updated: see git log*
