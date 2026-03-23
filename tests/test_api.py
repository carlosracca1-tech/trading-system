"""
tests/test_api.py
API endpoint tests — no Docker, no real PostgreSQL required.

Strategy:
- Set env vars before importing the app (same as test_health.py)
- Override FastAPI's get_db dependency with a MagicMock session
- Patch check_db_health so lifespan doesn't fail
- Test all major router groups:
    - /health
    - /runs
    - /system/status, /system/kill-switch, /system/risk-events, /system/reconcile
    - /portfolio, /positions
    - /signals, /orders

Run:
    pytest tests/test_api.py -v
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Environment MUST be set before any app import ─────────────────────────────
os.environ.setdefault("TRADING_MODE", "dev")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("API_KEY", "test-api-key-1234")
os.environ.setdefault("DATABASE_URL", "postgresql://trading:trading_dev_pass@localhost:5433/trading_dev")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

from config.settings import get_settings  # noqa: E402
get_settings.cache_clear()

# ── Constants ──────────────────────────────────────────────────────────────────
API_KEY = "test-api-key-1234"
HEADERS = {"X-API-KEY": API_KEY}
RUN_ID = str(uuid.uuid4())
NOW = datetime(2024, 6, 15, 16, 0, 0, tzinfo=timezone.utc)


# ── ORM object factories ──────────────────────────────────────────────────────

def _make_run(
    run_id: str = RUN_ID,
    status: str = "RUNNING",
    run_type: str = "PAPER",
    initial_capital: float = 100_000.0,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.run_type = run_type
    run.status = status
    run.started_at = NOW
    run.ended_at = None
    run.initial_capital = initial_capital
    run.final_capital = None
    run.total_return_pct = None
    run.max_drawdown_pct = None
    run.total_trades = 0
    run.winning_trades = 0
    run.losing_trades = 0
    run.notes = None
    run.config_snapshot = None
    return run


def _make_position(
    position_id: str | None = None,
    symbol: str = "SPY",
    status: str = "OPEN",
    qty: int = 10,
    entry_price: float = 480.0,
    current_price: float = 490.0,
) -> MagicMock:
    pos = MagicMock()
    pos.id = position_id or str(uuid.uuid4())
    pos.run_id = RUN_ID
    pos.symbol = symbol
    pos.status = status
    pos.direction = "LONG"
    pos.qty = qty
    pos.entry_price = entry_price
    pos.exit_price = None
    pos.stop_loss = 460.0
    pos.current_price = current_price
    pos.unrealized_pnl = (current_price - entry_price) * qty
    pos.realized_pnl = None
    pos.opened_at = NOW
    pos.closed_at = None
    pos.close_reason = None
    return pos


def _make_snapshot(
    equity: float = 105_000.0,
    drawdown: float = 0.0,
    peak: float = 105_000.0,
    cash: float = 85_000.0,
) -> MagicMock:
    snap = MagicMock()
    snap.id = str(uuid.uuid4())
    snap.run_id = RUN_ID
    snap.snapshot_type = "INTRADAY"
    snap.snapshot_at = NOW
    snap.cash = cash
    snap.positions_value = equity - cash
    snap.total_equity = equity
    snap.open_positions_count = 1
    snap.peak_equity = peak
    snap.drawdown_pct = drawdown
    snap.daily_pnl = None
    snap.cumulative_return_pct = (equity - 100_000.0) / 100_000.0
    return snap


def _make_signal(
    signal_id: str | None = None,
    symbol: str = "QQQ",
    signal_type: str = "ENTER",
    risk_decision: str = "APPROVED",
) -> MagicMock:
    sig = MagicMock()
    sig.id = signal_id or str(uuid.uuid4())
    sig.run_id = RUN_ID
    sig.symbol = symbol
    sig.signal_date = date(2024, 6, 15)
    sig.signal_type = signal_type
    sig.direction = "LONG"
    sig.close_price = 420.0
    sig.ema_50 = 410.0
    sig.ema_200 = 390.0
    sig.rsi_14 = 62.0
    sig.atr_14 = 5.5
    sig.volume_ratio = 1.3
    sig.regime_ok = True
    sig.risk_decision = risk_decision
    sig.risk_rejection_reason = None
    sig.position_size_shares = 20
    sig.entry_price = 421.0
    sig.stop_loss = 405.0
    sig.created_at = NOW
    return sig


def _make_order(
    order_id: str | None = None,
    symbol: str = "SPY",
    side: str = "BUY",
    status: str = "FILLED",
) -> MagicMock:
    order = MagicMock()
    order.id = order_id or str(uuid.uuid4())
    order.run_id = RUN_ID
    order.symbol = symbol
    order.side = side
    order.order_type = "MARKET"
    order.direction = "LONG"
    order.qty = 10
    order.status = status
    order.submitted_price = 480.0
    order.filled_price = 480.5
    order.filled_qty = 10
    order.broker_order_id = "broker-123"
    order.rejection_reason = None
    order.signal_id = str(uuid.uuid4())
    order.created_at = NOW
    order.updated_at = NOW
    order.filled_at = NOW
    order.is_filled = True
    return order


def _make_risk_event(
    rule_code: str = "P1_MAX_DRAWDOWN",
    decision: str = "REJECTED",
) -> MagicMock:
    ev = MagicMock()
    ev.id = str(uuid.uuid4())
    ev.run_id = RUN_ID
    ev.correlation_id = str(uuid.uuid4())
    ev.rule_code = rule_code
    ev.rule_priority = "P1"
    ev.decision = decision
    ev.symbol = "SPY"
    ev.rejection_reason = "Drawdown limit breached"
    ev.metrics_snapshot = None
    ev.triggered_at = NOW
    return ev


# ── DB mock session ────────────────────────────────────────────────────────────

def _make_db_session() -> MagicMock:
    """Build a flexible mock SQLAlchemy session."""
    session = MagicMock()
    # scalar() default → None (routers check for None)
    session.scalar.return_value = None
    # scalars() chaining: .all() → []
    session.scalars.return_value.all.return_value = []
    session.scalars.return_value.first.return_value = None
    session.get.return_value = None
    session.execute.return_value = MagicMock()
    return session


# ── App fixture ────────────────────────────────────────────────────────────────

DB_HEALTH_OK = {"status": "ok", "latency_ms": 1.2, "version": "PostgreSQL 15 (mock)"}


@pytest.fixture(scope="session")
def app():
    """Create a single FastAPI app instance for the whole test session."""
    with patch("packages.shared.db.check_db_health", return_value=DB_HEALTH_OK):
        with patch("apps.api.main.check_db_health", return_value=DB_HEALTH_OK):
            from apps.api.main import create_app
            return create_app()


@pytest.fixture()
def db_session():
    """Fresh mock DB session for each test."""
    return _make_db_session()


@pytest.fixture()
def client(app, db_session):
    """TestClient with overridden DB dependency."""
    from packages.shared.db import get_db

    # Override the routers' local _get_db (which yields from get_db)
    # by overriding get_db globally in the app dependency map
    app.dependency_overrides[get_db] = lambda: db_session

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, db_session

    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_liveness_no_auth(self, client):
        c, _ = client
        resp = c.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")

    def test_health_requires_no_api_key(self, client):
        """Health endpoint should be publicly accessible."""
        c, _ = client
        resp = c.get("/api/v1/health")
        assert resp.status_code != 401

    def test_health_detailed_no_auth(self, client):
        c, _ = client
        with patch("apps.api.routers.health.check_db_health", return_value=DB_HEALTH_OK):
            resp = c.get("/api/v1/health/detailed")
        assert resp.status_code == 200

    def test_health_rejects_unknown_path(self, client):
        c, _ = client
        resp = c.get("/api/v1/nonexistent")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_missing_api_key_returns_401(self, client):
        c, _ = client
        resp = c.get("/api/v1/runs")
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, client):
        c, _ = client
        resp = c.get("/api/v1/runs", headers={"X-API-KEY": "wrong-key"})
        assert resp.status_code == 401

    def test_correct_api_key_passes(self, client, db_session):
        c, db = client
        # scalar for count returns 0, scalars for list returns []
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/runs", headers=HEADERS)
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# RUNS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

class TestRuns:
    def test_list_runs_empty(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/runs", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["runs"] == []

    def test_list_runs_with_data(self, client, db_session):
        c, db = client
        run = _make_run()
        db.scalar.return_value = 1
        db.scalars.return_value.all.return_value = [run]
        resp = c.get("/api/v1/runs", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["runs"]) == 1
        assert data["runs"][0]["id"] == RUN_ID
        assert data["runs"][0]["status"] == "RUNNING"

    def test_get_run_by_id_not_found(self, client, db_session):
        c, db = client
        db.get.return_value = None
        resp = c.get(f"/api/v1/runs/{RUN_ID}", headers=HEADERS)
        assert resp.status_code == 404

    def test_get_run_by_id_found(self, client, db_session):
        c, db = client
        run = _make_run()
        db.get.return_value = run
        resp = c.get(f"/api/v1/runs/{RUN_ID}", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == RUN_ID
        assert data["run_type"] == "PAPER"
        assert data["initial_capital"] == 100_000.0

    def test_create_run_success(self, client, db_session):
        c, db = client
        run = _make_run()
        with patch("apps.svc_orchestrator.runner.create_run", return_value=RUN_ID):
            db.get.return_value = run
            resp = c.post(
                "/api/v1/runs",
                json={"run_type": "PAPER", "initial_capital": 100000.0},
                headers=HEADERS,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == RUN_ID

    def test_create_run_conflict(self, client, db_session):
        c, db = client
        with patch(
            "apps.svc_orchestrator.runner.create_run",
            side_effect=RuntimeError("Already an active RUNNING run."),
        ):
            resp = c.post(
                "/api/v1/runs",
                json={"run_type": "PAPER", "initial_capital": 100000.0},
                headers=HEADERS,
            )
        assert resp.status_code == 409

    def test_delete_run_not_found(self, client, db_session):
        c, db = client
        db.get.return_value = None
        resp = c.delete(f"/api/v1/runs/{RUN_ID}", headers=HEADERS)
        assert resp.status_code == 404

    def test_list_runs_pagination(self, client, db_session):
        c, db = client
        db.scalar.return_value = 5
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/runs?limit=2&offset=4", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["total"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM STATUS
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemStatus:
    def test_status_no_run(self, client, db_session):
        c, db = client
        db.scalar.return_value = None
        resp = c.get("/api/v1/system/status", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "no_active_run"

    def test_status_operational(self, client, db_session):
        c, db = client
        run = _make_run()
        snap = _make_snapshot()

        # db.scalar called multiple times: first for RUNNING run, then for STOPPED run
        db.scalar.side_effect = [run, None]
        with (
            patch("apps.svc_risk.kill_switch.is_active", return_value=False),
            patch("apps.svc_execution.repository.get_latest_snapshot", return_value=snap),
            patch("apps.svc_execution.repository.get_open_positions", return_value=[]),
        ):
            resp = c.get("/api/v1/system/status", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "operational"
        assert data["run_id"] == RUN_ID
        assert data["kill_switch_active"] is False

    def test_status_kill_switch_active(self, client, db_session):
        c, db = client
        run = _make_run()
        snap = _make_snapshot(drawdown=0.16)

        db.scalar.side_effect = [run, None]
        with (
            patch("apps.svc_risk.kill_switch.is_active", return_value=True),
            patch("apps.svc_execution.repository.get_latest_snapshot", return_value=snap),
            patch("apps.svc_execution.repository.get_open_positions", return_value=[]),
        ):
            resp = c.get("/api/v1/system/status", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["kill_switch_active"] is True
        assert data["status"] == "kill_switch_active"


# ─────────────────────────────────────────────────────────────────────────────
# KILL SWITCH
# ─────────────────────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_activate_run_not_found(self, client, db_session):
        c, db = client
        db.get.return_value = None
        resp = c.post(
            "/api/v1/system/kill-switch",
            json={"run_id": RUN_ID, "reason": "test"},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_activate_run_not_running(self, client, db_session):
        c, db = client
        run = _make_run(status="STOPPED")
        db.get.return_value = run
        resp = c.post(
            "/api/v1/system/kill-switch",
            json={"run_id": RUN_ID, "reason": "test"},
            headers=HEADERS,
        )
        assert resp.status_code == 409

    def test_activate_success(self, client, db_session):
        c, db = client
        run = _make_run(status="RUNNING")
        snap = _make_snapshot()
        db.get.return_value = run
        with (
            patch("apps.svc_execution.repository.get_latest_snapshot", return_value=snap),
            patch("apps.svc_risk.kill_switch.activate"),
        ):
            resp = c.post(
                "/api/v1/system/kill-switch",
                json={"run_id": RUN_ID, "reason": "manual test"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["activated"] is True
        assert data["run_id"] == RUN_ID

    def test_resolve_not_active(self, client, db_session):
        c, db = client
        with patch("apps.svc_risk.kill_switch.is_active", return_value=False):
            resp = c.delete(
                "/api/v1/system/kill-switch",
                json={"run_id": RUN_ID, "resolved_by": "operator"},
                headers=HEADERS,
            )
        assert resp.status_code == 409

    def test_resolve_success(self, client, db_session):
        c, db = client
        with (
            patch("apps.svc_risk.kill_switch.is_active", return_value=True),
            patch("apps.svc_risk.kill_switch.resolve"),
        ):
            resp = c.delete(
                "/api/v1/system/kill-switch",
                json={"run_id": RUN_ID, "resolved_by": "operator"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["activated"] is False

    def test_kill_switch_requires_auth(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/system/kill-switch",
            json={"run_id": RUN_ID, "reason": "test"},
        )
        assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# RISK EVENTS
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskEvents:
    def test_list_risk_events_empty(self, client, db_session):
        c, db = client
        # active run_id query
        db.scalar.return_value = None
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/system/risk-events", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["total"] == 0

    def test_list_risk_events_with_data(self, client, db_session):
        c, db = client
        ev = _make_risk_event()
        db.scalar.side_effect = [RUN_ID, 1]
        db.scalars.return_value.all.return_value = [ev]
        resp = c.get("/api/v1/system/risk-events", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["events"][0]["rule_code"] == "P1_MAX_DRAWDOWN"

    def test_list_risk_events_filter_by_run(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get(
            f"/api/v1/system/risk-events?run_id={RUN_ID}&limit=10",
            headers=HEADERS,
        )
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILE
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcile:
    def test_reconcile_no_active_run(self, client, db_session):
        c, db = client
        db.scalar.return_value = None
        resp = c.post("/api/v1/system/reconcile", headers=HEADERS)
        assert resp.status_code == 404

    def test_reconcile_success(self, client, db_session):
        c, db = client
        db.scalar.return_value = RUN_ID
        with (
            patch("apps.svc_execution.repository.get_latest_snapshot", return_value=_make_snapshot()),
            patch("apps.svc_execution.repository.get_pending_orders", return_value=[]),
            patch("apps.svc_execution.repository.get_open_positions", return_value=[]),
        ):
            resp = c.post("/api/v1/system/reconcile", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == RUN_ID
        assert "message" in data


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolio:
    def test_portfolio_no_active_run(self, client, db_session):
        c, db = client
        db.scalar.return_value = None
        resp = c.get("/api/v1/portfolio", headers=HEADERS)
        assert resp.status_code == 404

    def test_portfolio_ok(self, client, db_session):
        c, db = client
        run = _make_run()
        snap = _make_snapshot()
        pos = _make_position()

        # First scalar call: get active run_id
        db.scalar.return_value = RUN_ID
        db.get.return_value = run
        db.scalars.return_value.all.return_value = [pos]

        with patch("apps.svc_execution.repository.get_latest_snapshot", return_value=snap):
            resp = c.get("/api/v1/portfolio", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == RUN_ID
        assert data["run_status"] == "RUNNING"
        assert len(data["open_positions"]) == 1

    def test_portfolio_snapshots_no_run(self, client, db_session):
        c, db = client
        db.scalar.return_value = None
        resp = c.get("/api/v1/portfolio/snapshots", headers=HEADERS)
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

class TestPositions:
    def test_list_positions_empty(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/positions", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["positions"] == []
        assert data["total"] == 0

    def test_list_positions_with_data(self, client, db_session):
        c, db = client
        pos = _make_position()
        db.scalar.return_value = 1
        db.scalars.return_value.all.return_value = [pos]
        resp = c.get("/api/v1/positions", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["positions"][0]["symbol"] == "SPY"
        assert data["positions"][0]["status"] == "OPEN"

    def test_get_position_by_id_not_found(self, client, db_session):
        c, db = client
        pos_id = str(uuid.uuid4())
        db.get.return_value = None
        resp = c.get(f"/api/v1/positions/{pos_id}", headers=HEADERS)
        assert resp.status_code == 404

    def test_get_position_by_id_found(self, client, db_session):
        c, db = client
        pos = _make_position()
        db.get.return_value = pos
        resp = c.get(f"/api/v1/positions/{pos.id}", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "SPY"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

class TestSignals:
    def test_list_signals_empty(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/signals", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["signals"] == []
        assert data["total"] == 0

    def test_list_signals_with_data(self, client, db_session):
        c, db = client
        sig = _make_signal()
        db.scalar.return_value = 1
        db.scalars.return_value.all.return_value = [sig]
        resp = c.get("/api/v1/signals", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["signals"][0]["symbol"] == "QQQ"
        assert data["signals"][0]["signal_type"] == "ENTER"

    def test_list_signals_filter_type(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/signals?signal_type=EXIT", headers=HEADERS)
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────────────────────────────────────

class TestOrders:
    def test_list_orders_empty(self, client, db_session):
        c, db = client
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        resp = c.get("/api/v1/orders", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["orders"] == []
        assert data["total"] == 0

    def test_list_orders_with_data(self, client, db_session):
        c, db = client
        order = _make_order()
        db.scalar.return_value = 1
        db.scalars.return_value.all.return_value = [order]
        resp = c.get("/api/v1/orders", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["orders"][0]["symbol"] == "SPY"
        assert data["orders"][0]["side"] == "BUY"

    def test_get_order_not_found(self, client, db_session):
        c, db = client
        db.get.return_value = None
        resp = c.get(f"/api/v1/orders/{uuid.uuid4()}", headers=HEADERS)
        assert resp.status_code == 404

    def test_get_order_found(self, client, db_session):
        c, db = client
        order = _make_order()
        db.get.return_value = order
        resp = c.get(f"/api/v1/orders/{order.id}", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "FILLED"


# ─────────────────────────────────────────────────────────────────────────────
# EDGE CASES
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_large_limit_capped(self, client, db_session):
        """Ensure limit over max is rejected by FastAPI validation."""
        c, db = client
        resp = c.get("/api/v1/runs?limit=99999", headers=HEADERS)
        # FastAPI should return 422 Unprocessable Entity
        assert resp.status_code == 422

    def test_negative_offset_rejected(self, client, db_session):
        c, db = client
        resp = c.get("/api/v1/runs?offset=-1", headers=HEADERS)
        assert resp.status_code == 422

    def test_create_run_negative_capital_rejected(self, client, db_session):
        c, db = client
        resp = c.post(
            "/api/v1/runs",
            json={"run_type": "PAPER", "initial_capital": -5000.0},
            headers=HEADERS,
        )
        assert resp.status_code == 422

    def test_create_run_zero_capital_rejected(self, client, db_session):
        c, db = client
        resp = c.post(
            "/api/v1/runs",
            json={"run_type": "PAPER", "initial_capital": 0.0},
            headers=HEADERS,
        )
        assert resp.status_code == 422
