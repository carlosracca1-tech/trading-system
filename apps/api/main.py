"""
FastAPI application factory for svc_api.

Startup sequence:
1. Configure logging (structlog)
2. Validate settings (pydantic — fails fast on bad config)
3. Verify TRADING_MODE safety guards
4. Include routers
5. Register exception handlers

Entry point: uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import TradingMode, get_settings
from packages.shared.db import check_db_health
from packages.shared.exceptions import TradingSystemError
from packages.shared.logging_config import configure_logging, get_logger

# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Logging MUST be configured before anything else so all startup messages
# are captured in the correct format.
configure_logging()
log = get_logger(__name__)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Runs startup checks before accepting traffic.
    Runs shutdown cleanup when the process receives SIGTERM.
    """
    settings = get_settings()

    log.info(
        "api_starting",
        trading_mode=settings.trading_mode.value,
        dry_run=settings.dry_run,
        app_version=settings.app_version,
    )

    # ── Critical safety check ─────────────────────────────────────────────────
    # Belt-and-suspenders: settings validator already checks this, but we
    # re-verify at runtime to catch any accidental config mutation.
    if settings.trading_mode == TradingMode.LIVE and settings.dry_run:
        log.critical(
            "startup_aborted",
            reason="TRADING_MODE=live but DRY_RUN=true — refusing to start",
        )
        raise SystemExit(1)

    # ── Database connectivity check ───────────────────────────────────────────
    db_health = check_db_health()
    if db_health["status"] != "ok":
        log.critical(
            "startup_db_unreachable",
            detail=db_health.get("detail"),
        )
        # In live/paper: fail hard. In dev: warn and continue.
        if settings.is_paper_or_live:
            raise SystemExit(1)
        else:
            log.warning("startup_continuing_without_db", mode=settings.trading_mode.value)
    else:
        log.info(
            "startup_db_ok",
            latency_ms=db_health.get("latency_ms"),
            db_version=db_health.get("version", "")[:40],
        )

    log.info("api_ready", host=settings.api_host, port=settings.api_port)

    yield  # application runs here

    log.info("api_shutdown")


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Internal REST API for the Trading System V1",
        # Disable docs in live to reduce attack surface
        docs_url="/docs" if not settings.is_live else None,
        redoc_url="/redoc" if not settings.is_live else None,
        openapi_url="/openapi.json" if not settings.is_live else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Only allow localhost in dev. In production: Grafana on same host.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"]
        if settings.is_dev
        else ["http://localhost:3000"],
        allow_methods=["GET", "POST", "DELETE", "PUT"],
        allow_headers=["X-API-KEY", "Content-Type"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    from packages.shared.constants import API_PREFIX
    from apps.api.routers.health import router as health_router
    from apps.api.routers.runs import router as runs_router
    from apps.api.routers.portfolio import router as portfolio_router
    from apps.api.routers.signals import router as signals_router
    from apps.api.routers.orders import router as orders_router
    from apps.api.routers.system import router as system_router

    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(runs_router, prefix=API_PREFIX)
    app.include_router(portfolio_router, prefix=API_PREFIX)
    app.include_router(signals_router, prefix=API_PREFIX)
    app.include_router(orders_router, prefix=API_PREFIX)
    app.include_router(system_router, prefix=API_PREFIX)

    # ── Exception handlers ────────────────────────────────────────────────────
    @app.exception_handler(TradingSystemError)
    async def trading_system_error_handler(
        request: Request, exc: TradingSystemError
    ) -> JSONResponse:
        log.error(
            "unhandled_domain_error",
            error_type=type(exc).__name__,
            message=exc.message,
            context=exc.context,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled_exception",
            error_type=type(exc).__name__,
            message=str(exc),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "InternalServerError", "message": "An unexpected error occurred"},
        )

    return app


# ── Module-level app instance (used by uvicorn) ───────────────────────────────
app = create_app()
