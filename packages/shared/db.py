"""
Database engine and session management.

Usage in FastAPI:
    from packages.shared.db import get_db
    def my_endpoint(db: Session = Depends(get_db)): ...

Usage in scripts / jobs:
    from packages.shared.db import SessionLocal
    with SessionLocal() as db:
        ...

Health check:
    from packages.shared.db import check_db_health
    status = check_db_health()
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_settings
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


def _build_engine():
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        pool_pre_ping=True,   # test connection before lending from pool
        pool_recycle=1800,    # recycle connections every 30 minutes
        echo=settings.debug,  # log SQL when DEBUG=true
    )
    log.info(
        "database_engine_created",
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    return engine


# Module-level engine and session factory — created once at import time
engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # avoid lazy-load issues after commit
)


# ── FastAPI dependency ────────────────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a database session per request.
    Session is closed (returned to pool) after the request completes,
    even if an exception was raised.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Context manager for scripts / jobs ───────────────────────────────────────
@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Context manager for use outside of FastAPI (scheduler jobs, scripts).

    Usage:
        with db_session() as db:
            result = db.query(MyModel).all()
            db.commit()
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Health check ──────────────────────────────────────────────────────────────
def check_db_health() -> dict:
    """
    Executes a trivial query to verify database connectivity.
    Returns a dict with status, version, and latency.
    Does NOT raise — always returns a dict so health endpoints stay up.
    """
    import time
    start = time.monotonic()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 AS ok, version() AS version, now() AS db_time")
            ).fetchone()
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "version": row.version,
            "db_time": str(row.db_time),
        }
    except OperationalError as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        log.error("db_health_check_failed", error=str(exc), latency_ms=latency_ms)
        return {
            "status": "error",
            "latency_ms": latency_ms,
            "detail": str(exc),
        }
