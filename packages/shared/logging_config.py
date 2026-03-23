"""
Structured logging configuration.

Tries to use structlog if available; falls back to stdlib logging
so the rest of the system can import this module even without structlog installed.

Usage:
    from packages.shared.logging_config import get_logger

    log = get_logger(__name__)
    log.info("signal_generated", asset="SPY", rsi=61.3, run_id="run_paper_...")
    log.error("order_failed", asset="QQQ", reason="insufficient_buying_power")

Every log entry ALWAYS includes:
    ts         — ISO8601 UTC timestamp
    level      — debug | info | warning | error | critical
    logger     — module name
    event      — the message string
    + any kwargs passed to the log call
"""
from __future__ import annotations

import logging
import sys
from typing import Any

# ── Attempt to import structlog; fall back to stdlib if not installed ──────────
try:
    import structlog
    from structlog.types import EventDict, WrappedLogger
    _STRUCTLOG_AVAILABLE = True
except ImportError:
    _STRUCTLOG_AVAILABLE = False


# ── Fallback logger (mirrors structlog's event+kwargs API) ────────────────────

class _FallbackLogger:
    """
    Minimal drop-in for structlog.stdlib.BoundLogger.
    Accepts log.info("event", key=value, ...) calls and forwards them
    to stdlib logging as: "event | key=value key2=value2".
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kwargs: Any) -> None:
        if kwargs:
            extras = " | " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        else:
            extras = ""
        self._log.log(level, "%s%s", event, extras)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, event, **kwargs)

    warn = warning

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        self._log.exception(event, **kwargs)

    def bind(self, **kwargs: Any) -> "_FallbackLogger":
        """Return self (no-op binding for compatibility)."""
        return self


# ── configure_logging ─────────────────────────────────────────────────────────

def configure_logging() -> None:
    """
    Call once at application startup.
    When structlog is available, wires everything through it for structured JSON/console output.
    When structlog is not installed, configures a clean stdlib logging setup instead.
    """
    try:
        from config.settings import get_settings, LogFormat
        settings = get_settings()
        log_level_str = settings.log_level
        log_format = settings.log_format
        debug = settings.debug
        trading_mode = settings.trading_mode.value
        app_version = settings.app_version
    except Exception:
        log_level_str = "INFO"
        log_format = None
        debug = False
        trading_mode = "unknown"
        app_version = "unknown"

    log_level = getattr(logging, log_level_str, logging.INFO)

    if _STRUCTLOG_AVAILABLE:
        _configure_structlog(log_level, log_format, debug, trading_mode, app_version)
    else:
        _configure_stdlib(log_level)


def _add_service_info(logger: Any, method: str, event_dict: Any) -> Any:
    """Inject trading_mode and app_version into every structlog entry."""
    try:
        from config.settings import get_settings
        settings = get_settings()
        event_dict["trading_mode"] = settings.trading_mode.value
        event_dict["app_version"] = settings.app_version
    except Exception:
        pass
    return event_dict


def _configure_structlog(
    log_level: int,
    log_format: Any,
    debug: bool,
    trading_mode: str,
    app_version: str,
) -> None:
    try:
        from config.settings import LogFormat
    except Exception:
        LogFormat = None

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_info,
        structlog.stdlib.ExtraAdder(),
    ]

    if LogFormat and log_format == LogFormat.JSON:
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()
    else:
        shared_processors.append(structlog.dev.set_exc_info)
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.DEBUG if debug else logging.WARNING
    )


def _configure_stdlib(log_level: int) -> None:
    """Simple stdlib logging setup used when structlog is not installed."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(
        level=log_level,
        format=fmt,
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# ── get_logger ────────────────────────────────────────────────────────────────

def get_logger(name: str) -> Any:
    """
    Return a bound logger for the given module name.
    Returns a structlog BoundLogger when structlog is available,
    otherwise returns a _FallbackLogger with the same API.
    """
    if _STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    return _FallbackLogger(name)
