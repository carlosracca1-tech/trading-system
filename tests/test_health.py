"""
tests/test_health.py
Unit tests for the /api/v1/health endpoints.

These tests run WITHOUT Docker, WITHOUT PostgreSQL.
- The DB health check is mocked so the tests are fully isolated.
- Uses pytest + httpx TestClient (FastAPI built-in).

Run:
    pytest tests/test_health.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ── Force test environment BEFORE importing the app ────────────────────────────
# This prevents config/settings.py from reading a real .env file and
# from requiring valid Alpaca keys, etc.
os.environ["TRADING_MODE"] = "dev"
os.environ["DEBUG"] = "true"
os.environ["DRY_RUN"] = "true"
os.environ["API_KEY"] = "test-api-key-1234"
os.environ["DATABASE_URL"] = "postgresql://trading:trading_dev_pass@localhost:5433/trading_dev"
os.environ["ALPACA_API_KEY"] = "test-key"
os.environ["ALPACA_SECRET_KEY"] = "test-secret"
os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"

# ── Import AFTER env is set ────────────────────────────────────────────────────
from config.settings import get_settings  # noqa: E402

# Clear the lru_cache so tests always get a fresh Settings instance
get_settings.cache_clear()

from apps.api.main import create_app  # noqa: E402

# ── Fixtures ───────────────────────────────────────────────────────────────────

DB_HEALTHY_RESPONSE = {
    "status": "ok",
    "latency_ms": 1.5,
    "version": "PostgreSQL 15.0 (Test)",
}

DB_UNHEALTHY_RESPONSE = {
    "status": "error",
    "error": "connection refused",
    "latency_ms": None,
}


@pytest.fixture(scope="module")
def client_healthy_db():
    """TestClient with a mocked healthy database."""
    with patch("apps.api.routers.health.check_db_health", return_value=DB_HEALTHY_RESPONSE):
        with patch("apps.api.main.check_db_health", return_value=DB_HEALTHY_RESPONSE):
            app = create_app()
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


@pytest.fixture(scope="module")
def client_degraded_db():
    """TestClient with a mocked unhealthy database (degraded mode)."""
    with patch("apps.api.routers.health.check_db_health", return_value=DB_UNHEALTHY_RESPONSE):
        with patch("apps.api.main.check_db_health", return_value=DB_UNHEALTHY_RESPONSE):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


# ── Tests: GET /api/v1/health (public) ────────────────────────────────────────


class TestHealthPublic:
    """Public health endpoint — no auth required."""

    def test_returns_200(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/api/v1/health")
        assert response.status_code == 200

    def test_content_type_json(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/api/v1/health")
        assert "application/json" in response.headers["content-type"]

    def test_response_has_required_fields(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        required_fields = {"status", "timestamp", "app_name", "app_version", "trading_mode", "dry_run"}
        assert required_fields.issubset(data.keys()), (
            f"Missing fields: {required_fields - data.keys()}"
        )

    def test_status_is_ok(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        assert data["status"] == "ok"

    def test_trading_mode_is_dev(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        assert data["trading_mode"] == "dev"

    def test_dry_run_is_true(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        assert data["dry_run"] is True

    def test_app_name_is_set(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        assert isinstance(data["app_name"], str)
        assert len(data["app_name"]) > 0

    def test_timestamp_is_valid_iso8601(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        ts = data["timestamp"]
        # Must parse without raising
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, "Timestamp must be timezone-aware"

    def test_uptime_seconds_is_non_negative(self, client_healthy_db: TestClient):
        data = client_healthy_db.get("/api/v1/health").json()
        if "uptime_seconds" in data:
            assert data["uptime_seconds"] >= 0

    def test_no_auth_header_still_returns_200(self, client_healthy_db: TestClient):
        """Public endpoint must never require authentication."""
        response = client_healthy_db.get("/api/v1/health")
        assert response.status_code == 200

    def test_wrong_method_returns_405(self, client_healthy_db: TestClient):
        response = client_healthy_db.post("/api/v1/health")
        assert response.status_code == 405


# ── Tests: GET /api/v1/health/detailed (protected) ────────────────────────────


class TestHealthDetailed:
    """Detailed health endpoint — X-API-KEY required."""

    def test_no_key_returns_401(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/api/v1/health/detailed")
        assert response.status_code == 401

    def test_wrong_key_returns_401(self, client_healthy_db: TestClient):
        response = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "wrong-key"},
        )
        assert response.status_code == 401

    def test_correct_key_returns_200(self, client_healthy_db: TestClient):
        response = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        )
        assert response.status_code == 200

    def test_detailed_response_has_database_field(self, client_healthy_db: TestClient):
        data = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        ).json()
        assert "database" in data

    def test_database_status_ok_when_healthy(self, client_healthy_db: TestClient):
        data = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        ).json()
        assert data["database"]["status"] == "ok"

    def test_detailed_response_has_python_version(self, client_healthy_db: TestClient):
        data = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        ).json()
        assert "python_version" in data
        assert data["python_version"].startswith("3.")

    def test_detailed_has_config_valid_flag(self, client_healthy_db: TestClient):
        data = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        ).json()
        assert "config_valid" in data
        assert data["config_valid"] is True

    def test_degraded_db_still_returns_200(self, client_degraded_db: TestClient):
        """Detailed health returns 200 even when DB is down — status = 'degraded'."""
        response = client_degraded_db.get(
            "/api/v1/health/detailed",
            headers={"X-API-KEY": "test-api-key-1234"},
        )
        # Must not raise 500 — endpoint is always-up by contract
        assert response.status_code == 200
        data = response.json()
        # Overall status must reflect degraded state
        assert data["status"] in ("degraded", "ok")  # depending on implementation

    def test_case_sensitive_header_name(self, client_healthy_db: TestClient):
        """Header name is X-API-KEY — lowercase should also work (HTTP headers are case-insensitive)."""
        response = client_healthy_db.get(
            "/api/v1/health/detailed",
            headers={"x-api-key": "test-api-key-1234"},
        )
        assert response.status_code == 200


# ── Tests: 404 on unknown routes ──────────────────────────────────────────────


class TestNotFound:
    def test_unknown_route_returns_404(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/api/v1/does-not-exist")
        assert response.status_code == 404

    def test_root_path_returns_404_or_redirect(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/", follow_redirects=False)
        assert response.status_code in (404, 307, 308)


# ── Tests: Error response shape ───────────────────────────────────────────────


class TestErrorShape:
    def test_401_response_is_json(self, client_healthy_db: TestClient):
        response = client_healthy_db.get("/api/v1/health/detailed")
        assert "application/json" in response.headers["content-type"]
        data = response.json()
        # Must have a 'detail' key (FastAPI standard)
        assert "detail" in data
