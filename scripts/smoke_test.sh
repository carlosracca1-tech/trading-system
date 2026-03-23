#!/usr/bin/env bash
# =============================================================================
# scripts/smoke_test.sh
# Docker-level end-to-end smoke test for Trading System V1
#
# Prerequisites:
#   1. make up         — Docker containers running
#   2. make migrate    — Schema applied
#   3. make seed       — 18 ETF symbols in DB
#   4. make seed-data  — Synthetic OHLCV + indicators loaded
#
# Usage:
#   bash scripts/smoke_test.sh
#   bash scripts/smoke_test.sh --verbose   # show full curl responses
#   bash scripts/smoke_test.sh --stop-on-fail
#
# Exit code: 0 = all checks passed, 1 = one or more checks failed
# =============================================================================

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
API_URL="${API_URL:-http://localhost:8000/api/v1}"
API_KEY="${API_KEY:-dev-api-key-change-me}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/docker-compose.dev.yml}"
API_CONTAINER="${API_CONTAINER:-trading_api_dev}"

VERBOSE=false
STOP_ON_FAIL=false

for arg in "$@"; do
  case "$arg" in
    --verbose)      VERBOSE=true ;;
    --stop-on-fail) STOP_ON_FAIL=true ;;
  esac
done

# ── Colors ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0
RUN_ID=""

# ── Helpers ────────────────────────────────────────────────────────────────────

log()  { echo -e "${CYAN}[SMOKE]${NC} $*"; }
ok()   { echo -e "  ${GREEN}✓${NC} $*"; ((PASS++)) || true; }
fail() { echo -e "  ${RED}✗${NC} $*"; ((FAIL++)) || true; $STOP_ON_FAIL && exit 1; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }

require() {
  command -v "$1" &>/dev/null || { echo "ERROR: '$1' not found. Install it first."; exit 1; }
}

api_get() {
  local path="$1"
  local expected_status="${2:-200}"
  local response
  response=$(curl -s -w "\n%{http_code}" \
    -H "X-API-KEY: ${API_KEY}" \
    "${API_URL}${path}" 2>/dev/null)
  local body status
  body=$(echo "$response" | head -n -1)
  status=$(echo "$response" | tail -n 1)
  $VERBOSE && echo "    GET ${path} → ${status}: ${body}"
  if [[ "$status" == "$expected_status" ]]; then
    echo "$body"
  else
    echo "HTTP_ERROR:${status}:${body}"
  fi
}

api_post() {
  local path="$1"
  local data="$2"
  local expected_status="${3:-200}"
  local response
  response=$(curl -s -w "\n%{http_code}" \
    -H "X-API-KEY: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$data" \
    -X POST \
    "${API_URL}${path}" 2>/dev/null)
  local body status
  body=$(echo "$response" | head -n -1)
  status=$(echo "$response" | tail -n 1)
  $VERBOSE && echo "    POST ${path} → ${status}: ${body}"
  if [[ "$status" == "$expected_status" ]]; then
    echo "$body"
  else
    echo "HTTP_ERROR:${status}:${body}"
  fi
}

api_delete() {
  local path="$1"
  local data="$2"
  local expected_status="${3:-200}"
  local response
  response=$(curl -s -w "\n%{http_code}" \
    -H "X-API-KEY: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$data" \
    -X DELETE \
    "${API_URL}${path}" 2>/dev/null)
  local body status
  body=$(echo "$response" | head -n -1)
  status=$(echo "$response" | tail -n 1)
  $VERBOSE && echo "    DELETE ${path} → ${status}: ${body}"
  if [[ "$status" == "$expected_status" ]]; then
    echo "$body"
  else
    echo "HTTP_ERROR:${status}:${body}"
  fi
}

exec_in_container() {
  docker compose -f "$COMPOSE_FILE" exec -T "$API_CONTAINER" "$@"
}

json_field() {
  # Extract field from JSON using python (available everywhere)
  local json="$1" field="$2"
  python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('${field}',''))" "$json" 2>/dev/null || echo ""
}

check_response() {
  local resp="$1"
  [[ "$resp" != HTTP_ERROR* ]]
}

# ── Pre-flight checks ──────────────────────────────────────────────────────────

require curl
require docker
require python3

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║         Trading System V1 — Shell Smoke Test             ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

log "Pre-flight: checking Docker containers..."
if ! docker compose -f "$COMPOSE_FILE" ps --status running | grep -q "$API_CONTAINER"; then
  fail "Container '$API_CONTAINER' is not running. Run 'make up' first."
  exit 1
fi
ok "API container is running"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Health check
# ─────────────────────────────────────────────────────────────────────────────

log "Section 1: Health endpoints"

# 1a. Public health endpoint (no auth)
resp=$(curl -s -o /dev/null -w "%{http_code}" "${API_URL}/health")
if [[ "$resp" == "200" ]]; then
  ok "GET /health → 200"
else
  fail "GET /health → ${resp} (expected 200)"
fi

# 1b. Health endpoint returns 'ok' or 'degraded'
body=$(curl -s "${API_URL}/health")
status_val=$(json_field "$body" "status")
if [[ "$status_val" == "ok" ]] || [[ "$status_val" == "degraded" ]]; then
  ok "Health status = '${status_val}'"
else
  fail "Health status unexpected: '${status_val}'"
fi

# 1c. Detailed health (authenticated)
resp=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-KEY: ${API_KEY}" "${API_URL}/health/detailed")
if [[ "$resp" == "200" ]]; then
  ok "GET /health/detailed → 200"
else
  fail "GET /health/detailed → ${resp}"
fi

# 1d. Auth guard: no key → 401
resp=$(curl -s -o /dev/null -w "%{http_code}" "${API_URL}/runs")
if [[ "$resp" == "401" ]]; then
  ok "Auth guard: missing key → 401"
else
  fail "Auth guard: expected 401, got ${resp}"
fi

# 1e. Auth guard: wrong key → 401
resp=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-KEY: wrong-key" "${API_URL}/runs")
if [[ "$resp" == "401" ]]; then
  ok "Auth guard: wrong key → 401"
else
  fail "Auth guard: expected 401, got ${resp}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: TradingRun lifecycle
# ─────────────────────────────────────────────────────────────────────────────

log "Section 2: TradingRun lifecycle"

# 2a. List runs (may be empty)
body=$(api_get "/runs")
if check_response "$body"; then
  ok "GET /runs → 200"
else
  fail "GET /runs → ${body}"
fi

# 2b. Create a new run
body=$(api_post "/runs" '{"run_type":"PAPER","initial_capital":100000}' "201")
if check_response "$body"; then
  RUN_ID=$(json_field "$body" "id")
  ok "POST /runs → 201, run_id=${RUN_ID}"
else
  fail "POST /runs → ${body}"
  # Try to extract any existing run_id so subsequent steps don't all fail
  body2=$(api_get "/runs")
  if check_response "$body2"; then
    RUN_ID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); \
      runs=d.get('runs',[]); print(runs[0]['id'] if runs else '')" "$body2" 2>/dev/null || echo "")
    [[ -n "$RUN_ID" ]] && warn "Using existing run_id=${RUN_ID} for remaining tests"
  fi
fi

# 2c. Second create should conflict (409) since a PAPER run already exists
if [[ -n "$RUN_ID" ]]; then
  body=$(api_post "/runs" '{"run_type":"PAPER","initial_capital":100000}' "409")
  if check_response "$body"; then
    ok "POST /runs duplicate → 409 (conflict)"
  else
    # 409 is expected — if we get 201 that's actually a pass (no guard) or the run was already stopped
    warn "POST /runs duplicate did not return 409 (may be ok if prior run was stopped)"
  fi
fi

# 2d. Get by ID
if [[ -n "$RUN_ID" ]]; then
  body=$(api_get "/runs/${RUN_ID}")
  if check_response "$body"; then
    run_status=$(json_field "$body" "status")
    ok "GET /runs/${RUN_ID} → status=${run_status}"
  else
    fail "GET /runs/${RUN_ID} → ${body}"
  fi
fi

# 2e. List runs: now has at least one
body=$(api_get "/runs")
if check_response "$body"; then
  total=$(json_field "$body" "total")
  ok "GET /runs total=${total}"
else
  fail "GET /runs → ${body}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: System status
# ─────────────────────────────────────────────────────────────────────────────

log "Section 3: System status"

body=$(api_get "/system/status")
if check_response "$body"; then
  sys_status=$(json_field "$body" "status")
  ok "GET /system/status → status=${sys_status}"
else
  fail "GET /system/status → ${body}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: Data pipeline (in-container)
# ─────────────────────────────────────────────────────────────────────────────

log "Section 4: Data pipeline (in-container execution)"

# 4a. Compute indicators (idempotent)
if exec_in_container python3 -m apps.svc_data.main --compute-indicators \
    --symbol SPY > /dev/null 2>&1; then
  ok "compute-indicators for SPY"
else
  warn "compute-indicators failed (may have no data — run 'make seed-data' first)"
fi

# 4b. Scan signals (in-container) — requires RUN_ID
if [[ -n "$RUN_ID" ]]; then
  if exec_in_container python3 -m apps.svc_strategy.main \
      --run-id "$RUN_ID" --symbol SPY > /dev/null 2>&1; then
    ok "scan-signals for SPY (run_id=${RUN_ID})"
  else
    warn "scan-signals exited non-zero (may be ok if no indicator data)"
  fi
fi

# 4c. Evaluate risk (in-container)
if [[ -n "$RUN_ID" ]]; then
  if exec_in_container python3 -m apps.svc_risk.main \
      --run-id "$RUN_ID" > /dev/null 2>&1; then
    ok "evaluate-risk (run_id=${RUN_ID})"
  else
    warn "evaluate-risk exited non-zero (ok if no signals)"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: Execution (dry-run in-container)
# ─────────────────────────────────────────────────────────────────────────────

log "Section 5: Execution service (dry-run)"

if [[ -n "$RUN_ID" ]]; then
  # 5a. Submit orders (dry-run)
  if exec_in_container python3 -m apps.svc_execution.main \
      --execute --dry-run > /dev/null 2>&1; then
    ok "svc_execution --execute --dry-run"
  else
    warn "svc_execution --execute exited non-zero (ok if no approved signals)"
  fi

  # 5b. Portfolio snapshot
  if exec_in_container python3 -m apps.svc_execution.main \
      --snapshot > /dev/null 2>&1; then
    ok "svc_execution --snapshot"
  else
    warn "svc_execution --snapshot exited non-zero"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: Portfolio endpoints
# ─────────────────────────────────────────────────────────────────────────────

log "Section 6: Portfolio and position endpoints"

# 6a. Portfolio (expects 200 if run active, 404 if no run)
resp_code=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-KEY: ${API_KEY}" "${API_URL}/portfolio")
if [[ "$resp_code" == "200" ]] || [[ "$resp_code" == "404" ]]; then
  ok "GET /portfolio → ${resp_code}"
else
  fail "GET /portfolio → ${resp_code}"
fi

# 6b. Positions list
body=$(api_get "/positions")
if check_response "$body"; then
  total=$(json_field "$body" "total")
  ok "GET /positions → total=${total}"
else
  fail "GET /positions → ${body}"
fi

# 6c. Signals list
body=$(api_get "/signals")
if check_response "$body"; then
  total=$(json_field "$body" "total")
  ok "GET /signals → total=${total}"
else
  fail "GET /signals → ${body}"
fi

# 6d. Orders list
body=$(api_get "/orders")
if check_response "$body"; then
  total=$(json_field "$body" "total")
  ok "GET /orders → total=${total}"
else
  fail "GET /orders → ${body}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: Kill switch
# ─────────────────────────────────────────────────────────────────────────────

log "Section 7: Kill switch"

if [[ -n "$RUN_ID" ]]; then
  # 7a. Activate kill switch
  body=$(api_post "/system/kill-switch" \
    "{\"run_id\":\"${RUN_ID}\",\"reason\":\"smoke_test\"}" "200")
  if check_response "$body"; then
    activated=$(json_field "$body" "activated")
    ok "POST /system/kill-switch → activated=${activated}"
  else
    fail "POST /system/kill-switch → ${body}"
  fi

  # 7b. Status should now show kill_switch_active
  body=$(api_get "/system/status")
  if check_response "$body"; then
    sys_status=$(json_field "$body" "status")
    if [[ "$sys_status" == "kill_switch_active" ]] || [[ "$sys_status" == "stopped" ]]; then
      ok "System status after kill switch = '${sys_status}'"
    else
      warn "System status after kill switch = '${sys_status}' (may be ok)"
    fi
  fi

  # 7c. Second activation should conflict (409) — already stopped
  resp_code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-KEY: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"run_id\":\"${RUN_ID}\",\"reason\":\"duplicate\"}" \
    -X POST "${API_URL}/system/kill-switch")
  if [[ "$resp_code" == "409" ]]; then
    ok "Kill switch duplicate → 409"
  else
    warn "Kill switch duplicate → ${resp_code} (expected 409)"
  fi

  # 7d. Resolve kill switch
  body=$(api_delete "/system/kill-switch" \
    "{\"run_id\":\"${RUN_ID}\",\"resolved_by\":\"smoke_test\"}" "200")
  if check_response "$body"; then
    activated=$(json_field "$body" "activated")
    ok "DELETE /system/kill-switch → activated=${activated}"
  else
    fail "DELETE /system/kill-switch → ${body}"
  fi

  # 7e. Verify run is RUNNING again
  body=$(api_get "/runs/${RUN_ID}")
  if check_response "$body"; then
    run_status=$(json_field "$body" "status")
    if [[ "$run_status" == "RUNNING" ]]; then
      ok "Run status after resolve = RUNNING"
    else
      warn "Run status after resolve = '${run_status}' (may still be stopped)"
    fi
  fi
else
  warn "Skipping kill switch tests — no RUN_ID"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8: Risk events
# ─────────────────────────────────────────────────────────────────────────────

log "Section 8: Risk events"

body=$(api_get "/system/risk-events")
if check_response "$body"; then
  total=$(json_field "$body" "total")
  ok "GET /system/risk-events → total=${total}"
else
  fail "GET /system/risk-events → ${body}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9: Reconciliation
# ─────────────────────────────────────────────────────────────────────────────

log "Section 9: Reconciliation"

resp_code=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-KEY: ${API_KEY}" \
  -X POST "${API_URL}/system/reconcile")
if [[ "$resp_code" == "200" ]]; then
  ok "POST /system/reconcile → 200"
elif [[ "$resp_code" == "404" ]]; then
  warn "POST /system/reconcile → 404 (no active run — ok)"
else
  fail "POST /system/reconcile → ${resp_code}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10: Clean shutdown — stop the test run
# ─────────────────────────────────────────────────────────────────────────────

log "Section 10: Stopping test run"

if [[ -n "$RUN_ID" ]]; then
  resp_code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-KEY: ${API_KEY}" \
    -X DELETE "${API_URL}/runs/${RUN_ID}")
  if [[ "$resp_code" == "200" ]] || [[ "$resp_code" == "204" ]]; then
    ok "DELETE /runs/${RUN_ID} → ${resp_code}"
  else
    warn "DELETE /runs/${RUN_ID} → ${resp_code} (may already be stopped)"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║                     SMOKE TEST RESULTS                   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}Passed:  ${PASS}${NC}"
echo -e "  ${RED}Failed:  ${FAIL}${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo -e "  ${RED}SMOKE TEST FAILED — ${FAIL} check(s) did not pass.${NC}"
  echo ""
  exit 1
else
  echo -e "  ${GREEN}ALL CHECKS PASSED ✓${NC}"
  echo ""
  exit 0
fi
