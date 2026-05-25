"""
_watchdog_health — F3.2 del plan KAIZEN: healthcheck post-run + email.

Cada watchdog (rftm/mrev) acumula un HealthReport durante el run y al
terminar:
1. Lo persiste a `logs/kaizen_health.jsonl` para que KAIZEN procese
   métricas de cobertura.
2. Si hay warnings/errors, dispara un email a Charlie.

Tipos de evento:
- ERROR  : `assert_db_health` falló o excepción crítica durante el run.
- WARN   : `evaluated_count < expected_count` (alguna posición no se
           evaluó), o sell con timeout que se canceló.
- INFO   : run normal, todo bien.

No depende de pandas/numpy — stdlib only. Idéntico patrón que
_trade_logger / _kaizen_missed.

Env:
- KAIZEN_HEALTH_PATH: override del JSONL (default
  <script_dir>/logs/kaizen_health.jsonl).
- WATCHDOG_HEALTH_EMAIL_ENABLED: "0" para skipear emails (testing local).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


# ── Severity ─────────────────────────────────────────────────────────────────

SEV_INFO = "info"
SEV_WARN = "warn"
SEV_ERROR = "error"


@dataclass
class HealthEvent:
    """Un evento individual capturado durante el run del watchdog."""

    level: str  # info / warn / error
    code: str  # ej. "db_health_fail", "coverage_gap", "sell_timeout"
    detail: str = ""


@dataclass
class HealthReport:
    """Resumen del run completo.

    El watchdog construye uno de estos al inicio, lo va alimentando con
    `add_event()`, y al final llama `finalize(...)`.
    """

    bot: str
    started_at: str = ""
    finished_at: str = ""
    latency_seconds: float = 0.0
    expected_count: int = 0  # posiciones abiertas en Alpaca al inicio
    evaluated_count: int = 0  # posiciones que el loop alcanzó a evaluar
    sell_attempts: int = 0
    sell_failures: int = 0
    sell_timeouts: int = 0
    db_health_ok: bool = True
    events: List[HealthEvent] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def add_event(self, level: str, code: str, detail: str = "") -> None:
        self.events.append(HealthEvent(level=level, code=code, detail=detail))

    @property
    def overall_severity(self) -> str:
        """Worst severity across all events.

        Si hay al menos un error → "error". Si hay warn pero no error →
        "warn". Si todo es info → "info".
        """
        levels = {e.level for e in self.events}
        if SEV_ERROR in levels:
            return SEV_ERROR
        if SEV_WARN in levels:
            return SEV_WARN
        return SEV_INFO

    def to_dict(self) -> dict:
        d = asdict(self)
        d["overall_severity"] = self.overall_severity
        return d


# ── Persistencia JSONL ───────────────────────────────────────────────────────


def _default_path() -> str:
    override = os.environ.get("KAIZEN_HEALTH_PATH", "").strip()
    if override:
        return override
    script_dir = Path(__file__).resolve().parent
    return str(script_dir / "logs" / "kaizen_health.jsonl")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def persist_report(report: HealthReport) -> bool:
    """Append del reporte al JSONL. Nunca levanta."""
    path = _default_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    line = json.dumps(report.to_dict(), ensure_ascii=False, default=str) + "\n"
    try:
        with open(path, "ab") as f:
            try:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line.encode("utf-8"))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except ImportError:
                f.write(line.encode("utf-8"))
        return True
    except Exception as e:
        print(f"[watchdog_health] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ── Email ────────────────────────────────────────────────────────────────────


def _email_enabled() -> bool:
    return os.environ.get("WATCHDOG_HEALTH_EMAIL_ENABLED", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def _render_html(report: HealthReport) -> str:
    """HTML mínimo para el email. Sin dependencias — string concatenation."""
    sev = report.overall_severity
    color = {"error": "#c0392b", "warn": "#e67e22", "info": "#2980b9"}.get(sev, "#333")
    header = f"<h2 style='color:{color};margin:0 0 8px 0'>[{report.bot}] Watchdog · {sev.upper()}</h2>"

    summary_rows = [
        ("Bot", report.bot),
        ("Started", report.started_at),
        ("Finished", report.finished_at),
        ("Latency", f"{report.latency_seconds:.1f}s"),
        ("Expected positions", report.expected_count),
        ("Evaluated", report.evaluated_count),
        ("Sell attempts", report.sell_attempts),
        ("Sell failures", report.sell_failures),
        ("Sell timeouts", report.sell_timeouts),
        ("DB health OK", report.db_health_ok),
    ]
    summary_html = "<table style='border-collapse:collapse;font-family:monospace;font-size:13px'>"
    for k, v in summary_rows:
        summary_html += (
            f"<tr><td style='padding:3px 12px 3px 0;color:#666'>{k}</td>"
            f"<td style='padding:3px 0'><strong>{v}</strong></td></tr>"
        )
    summary_html += "</table>"

    events_html = ""
    if report.events:
        events_html = "<h3 style='margin:14px 0 6px 0'>Events</h3><ul style='font-family:monospace;font-size:13px;padding-left:18px'>"
        sev_colors = {"error": "#c0392b", "warn": "#e67e22", "info": "#666"}
        for ev in report.events:
            c = sev_colors.get(ev.level, "#666")
            events_html += (
                f"<li><span style='color:{c};font-weight:bold'>[{ev.level.upper()}]</span> "
                f"<code style='background:#eee;padding:1px 4px;border-radius:3px'>{ev.code}</code> "
                f"{ev.detail}</li>"
            )
        events_html += "</ul>"

    return f"""
    <html><body style='font-family:system-ui,sans-serif;color:#222'>
    {header}
    {summary_html}
    {events_html}
    <hr style='margin:18px 0;border:none;border-top:1px solid #ddd'>
    <p style='font-size:11px;color:#999'>F3.2 watchdog healthcheck — KAIZEN.
    Para silenciar: setear WATCHDOG_HEALTH_EMAIL_ENABLED=0.</p>
    </body></html>
    """


def send_health_email(report: HealthReport) -> bool:
    """Send email solo si severity >= warn y el email está habilitado.

    Devuelve True si se envió, False si se skipeó o falló (no fatal).
    """
    sev = report.overall_severity
    if sev == SEV_INFO:
        return False  # nada urgente
    if not _email_enabled():
        return False

    try:
        from _email_helpers import send_smtp

        subject = f"[{report.bot}] Watchdog {sev.upper()} · {report.finished_at[:16]}"
        return send_smtp(subject=subject, html_body=_render_html(report))
    except Exception as e:
        print(f"[watchdog_health] email failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ── API combinada ────────────────────────────────────────────────────────────


def finalize_report(
    report: HealthReport,
    *,
    started_at_iso: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Cierra el reporte (timestamps + latency), persiste y envía email
    si aplica.

    Pensado para que el watchdog llame esto en un único punto al final
    del main().
    """
    report.finished_at = _now_iso()
    if started_at_iso:
        report.started_at = started_at_iso
        try:
            t0 = datetime.fromisoformat(started_at_iso)
            t1 = datetime.fromisoformat(report.finished_at)
            report.latency_seconds = (t1 - t0).total_seconds()
        except Exception:
            pass

    # Sanity: si no hubo cobertura completa, agregar warn automático
    if report.expected_count > report.evaluated_count and report.evaluated_count >= 0:
        gap = report.expected_count - report.evaluated_count
        report.add_event(
            SEV_WARN,
            "coverage_gap",
            f"{gap}/{report.expected_count} posiciones no evaluadas",
        )

    if extra:
        report.extra.update(extra)

    persist_report(report)
    send_health_email(report)
