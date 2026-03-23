"""
FastAPI dependencies shared across routers.

Usage:
    from apps.api.dependencies import require_api_key, get_db_session

    @router.get("/endpoint")
    def my_endpoint(
        db: Session = Depends(get_db_session),
        _: None = Depends(require_api_key),
    ): ...
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from config.settings import get_settings
from packages.shared.db import get_db

# ── API Key authentication ────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


def require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """
    Dependency that validates the X-API-KEY header.
    Raises HTTP 401 if missing or invalid.

    Usage: add `_: None = Depends(require_api_key)` to any protected endpoint.
    Public endpoints (e.g., /health) do NOT use this dependency.
    """
    settings = get_settings()
    if not api_key or api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-KEY header",
        )


# ── Database session ──────────────────────────────────────────────────────────
def get_db_session() -> Session:
    """Alias for the shared DB dependency — used in routers."""
    return Depends(get_db)
