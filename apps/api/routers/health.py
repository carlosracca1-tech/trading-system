"""
Health check endpoints.

GET /api/v1/health         — public, lightweight ping (no DB)
GET /api/v1/health/detailed — protected, checks DB + config

These are the first endpoints to implement and the last to break.
They should ALWAYS return 200 (with status=degraded) rather than 5xx,
so that load balancers and monitoring systems can distinguish between
"app is up but unhealthy" vs "app is completely down".
"""
from __future__ import annotations

import platform
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from apps.api.dependencies import require_api_key
from config.settings import get_settings
from packages.shared.db import check_db_health
from packages.shared.logging_config import get_logger

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)


# ── Response schemas ──────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: str           # ok | degraded
    timestamp: str
    app_name: str
    app_version: str
    trading_mode: str
    dry_run: bool
    uptime_seconds: float


class DetailedHealthResponse(HealthResponse):
    database: dict
    config_valid: bool
    python_version: str
    platform: str


# Module-level startup time for uptime calculation
_startup_time = time.monotonic()


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("", response_model=HealthResponse, summary="Lightweight health ping")
def health_check() -> HealthResponse:
    """
    Public endpoint. No authentication required.
    Returns 200 always. status=ok means fully operational.

    Used by:
    - Docker health checks
    - Load balancer probes
    - UptimeRobot monitoring
    """
    settings = get_settings()
    return HealthResponse(
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
        app_name=settings.app_name,
        app_version=settings.app_version,
        trading_mode=settings.trading_mode.value,
        dry_run=settings.dry_run,
        uptime_seconds=round(time.monotonic() - _startup_time, 2),
    )


@router.get(
    "/detailed",
    response_model=DetailedHealthResponse,
    summary="Detailed health check with DB connectivity",
)
def health_check_detailed(
    _: None = Depends(require_api_key),
) -> DetailedHealthResponse:
    """
    Protected endpoint. Requires X-API-KEY header.
    Checks database connectivity and returns latency.
    Returns 200 even if DB is down (status=degraded in that case).

    Use this endpoint for operational monitoring dashboards.
    """
    settings = get_settings()
    db_health = check_db_health()

    overall_status = "ok" if db_health["status"] == "ok" else "degraded"

    if overall_status == "degraded":
        log.warning(
            "health_check_degraded",
            db_status=db_health.get("status"),
            db_detail=db_health.get("detail"),
        )

    return DetailedHealthResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        app_name=settings.app_name,
        app_version=settings.app_version,
        trading_mode=settings.trading_mode.value,
        dry_run=settings.dry_run,
        uptime_seconds=round(time.monotonic() - _startup_time, 2),
        database=db_health,
        config_valid=True,  # if we got here, settings validation passed at startup
        python_version=platform.python_version(),
        platform=platform.system(),
    )
