#!/usr/bin/env python3
"""
kaizen_monthly_report.py — F6.2: snapshot mensual de métricas KAIZEN +
email a Charlie con HTML que tiene cards por regla y links de aprobación
para propuestas pendientes.

Lo invoca el workflow `kaizen_monthly.yml` el día 1 de cada mes a las
12:00 UTC.

Flujo:
1. Snapshot de `kaizen_monthly_metrics` para el mes que terminó.
2. Cargar todas las reglas (active + propuestas pendientes).
3. Cargar todos los param_overrides (incluidas propuestas).
4. Identificar reglas que requieren decisión (F6.3: 2 meses negativos
   consecutivos con >=10 blocks).
5. Renderizar HTML.
6. Enviar email.
7. Versionar markdown en `kaizen_reports/YYYY-MM.md`.

Env:
    EMAIL_FROM / EMAIL_PASSWORD / EMAIL_TO (los de siempre)
    KAIZEN_MONTHLY_BOT          que DB tickear (default ambos)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _kaizen_monthly_metrics import (  # noqa: E402
    compute_month_aggregate,
    ensure_table as ensure_metrics_table,
    get_rule_history,
    rules_needing_decision,
    snapshot_monthly_metrics,
)
from _shadow_trades import ensure_table as ensure_shadow_table  # noqa: E402


def previous_month_str() -> str:
    """Mes anterior al actual (YYYY-MM). El día 1 del mes nuevo
    miramos el mes que terminó."""
    today = datetime.now(tz=timezone.utc).date()
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_shadow_table(conn)
    ensure_metrics_table(conn)
    return conn


def _load_rules() -> dict:
    rules_path = Path(os.environ.get(
        "KAIZEN_RULES_PATH", str(ROOT / "kaizen_rules.json")
    ))
    if not rules_path.exists():
        return {"rules": [], "param_overrides": []}
    try:
        return json.loads(rules_path.read_text())
    except json.JSONDecodeError:
        return {"rules": [], "param_overrides": []}


def _github_repo_slug() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "your-user/trading-system")


def _approval_link(target_id: str, target_type: str, decision: str) -> str:
    """Link a kaizen_decision.yml. GitHub no soporta prefill nativo de
    inputs, así que el link manda a la página del workflow donde Charlie
    clickea Run + completa con los inputs prefillados desde un comentario
    o desde memoria. El email muestra qué valores poner."""
    slug = _github_repo_slug()
    return f"https://github.com/{slug}/actions/workflows/kaizen_decision.yml"


def _render_html(month: str, report: dict) -> str:
    """Renderiza el HTML del email."""
    rules_data = report["rules_data"]
    overrides_data = report["overrides_data"]
    needs_decision = set(report["needs_decision"])
    total_net = report["total_net_impact"]
    repo_slug = _github_repo_slug()

    css = "font-family:system-ui,sans-serif;color:#222"
    badge = lambda txt, color: f"<span style='background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px'>{txt}</span>"

    has_action = bool(needs_decision) or any(
        not r.get("active") for r in rules_data
    ) or any(
        not o.get("active") for o in overrides_data
    )

    # Header
    h = []
    h.append(f"<h1 style='margin:0 0 4px 0'>KAIZEN Monthly · {month}</h1>")
    impact_color = "#27ae60" if total_net >= 0 else "#c0392b"
    h.append(
        f"<p style='font-size:18px;margin:0 0 16px 0'>"
        f"Net impact del mes: <strong style='color:{impact_color}'>"
        f"${total_net:+,.2f}</strong>"
        f"</p>"
    )

    # Active rules — cards
    h.append("<h2>Reglas activas</h2>")
    active = [r for r in rules_data if r.get("active")]
    if not active:
        h.append("<p style='color:#999'>(ninguna)</p>")
    for r in active:
        flagged = r["id"] in needs_decision
        sev_badge = badge("REQUIERE TU DECISIÓN", "#c0392b") if flagged else ""
        net = r.get("net_impact_usd", 0)
        net_color = "#27ae60" if net >= 0 else "#c0392b"
        h.append(f"""
        <div style='border:1px solid #ddd;border-radius:6px;padding:12px;margin:8px 0'>
          <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>
            <strong style='font-family:monospace'>{r['id']}</strong>
            {sev_badge}
            <span style='color:{net_color};font-weight:bold'>${net:+,.2f}</span>
          </div>
          <div style='font-size:13px;color:#555;margin-bottom:6px'>{r.get('description', '')}</div>
          <div style='font-size:11px;color:#888;font-family:monospace'>
            blocks: {r.get('n_blocks', 0)} ·
            shadows closed: {r.get('n_shadows_closed', 0)}
            (W:{r.get('n_shadows_winners', 0)} L:{r.get('n_shadows_losers', 0)}) ·
            saved: ${r.get('gross_saved_usd', 0):.2f} ·
            missed: ${r.get('gross_missed_usd', 0):.2f}
          </div>
        </div>""")

    # Propuestas pendientes (reglas + overrides)
    pending_rules = [r for r in rules_data if not r.get("active")
                     and not r.get("dismissed_at")]
    pending_overrides = [o for o in overrides_data if not o.get("active")
                         and not o.get("dismissed_at")]
    if pending_rules or pending_overrides:
        h.append("<h2>Propuestas pendientes de tu aprobación</h2>")
        h.append(
            "<p style='font-size:12px;color:#666'>Para aprobar/rechazar: clickear "
            f"<a href='https://github.com/{repo_slug}/actions/workflows/kaizen_decision.yml'>"
            "Run workflow</a> en GitHub Actions y completar con los valores "
            "indicados abajo.</p>"
        )
    for r in pending_rules:
        conf = r.get("confidence", "?")
        conf_color = {"high": "#27ae60", "medium": "#e67e22"}.get(conf, "#999")
        h.append(f"""
        <div style='border:1px solid #2980b9;border-radius:6px;padding:12px;margin:8px 0;background:#f7fcff'>
          <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>
            <strong style='font-family:monospace'>{r['id']}</strong>
            {badge(f"REGLA · confidence {conf}", conf_color)}
          </div>
          <div style='font-size:13px;color:#222;margin-bottom:6px'>{r.get('description', '')}</div>
          <div style='font-size:12px;color:#555;font-style:italic;margin-bottom:6px'>{r.get('rationale', '')}</div>
          <div style='font-size:11px;color:#888;font-family:monospace'>
            n_trades: {r.get('n_trades', '?')} ·
            win_rate: {r.get('win_rate', 0):.2f} ·
            loss_rate: {r.get('loss_rate', 0):.2f} ·
            expectancy: ${r.get('expectancy_usd', 0):.2f}
          </div>
          <div style='margin-top:8px;font-size:12px'>
            <strong>Para aprobar:</strong> inputs = target_id=<code>{r['id']}</code>,
            target_type=<code>rule</code>, decision=<code>approve</code>
          </div>
        </div>""")
    for o in pending_overrides:
        h.append(f"""
        <div style='border:1px solid #8e44ad;border-radius:6px;padding:12px;margin:8px 0;background:#fdf7ff'>
          <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>
            <strong style='font-family:monospace'>{o['id']}</strong>
            {badge("OVERRIDE PARAM", "#8e44ad")}
          </div>
          <div style='font-size:13px;color:#222;margin-bottom:6px'>
            <code>{o.get('param', '?')}</code> ({o.get('applies_to_bot', 'BOTH')}):
            <strong>{o.get('previous_value', '?')}</strong> →
            <strong>{o.get('value', '?')}</strong>
          </div>
          <div style='font-size:12px;color:#555;font-style:italic;margin-bottom:6px'>
            {o.get('rationale', '')}
          </div>
          <div style='font-size:11px;color:#888'>
            n_trades: {o.get('n_trades', '?')} · confidence: {o.get('confidence', '?')}
          </div>
          <div style='margin-top:8px;font-size:12px'>
            <strong>Para aprobar:</strong> target_id=<code>{o['id']}</code>,
            target_type=<code>override</code>, decision=<code>approve</code>
          </div>
        </div>""")

    h.append(
        "<hr style='margin:24px 0;border:none;border-top:1px solid #ddd'>"
        "<p style='font-size:11px;color:#999'>"
        "F6.2 — KAIZEN monthly report. Para silenciar: setear "
        "<code>EMAIL_MONTHLY_ENABLED=false</code>.</p>"
    )
    body = "\n".join(h)
    return f"<html><body style='{css}'>{body}</body></html>"


def _render_markdown(month: str, report: dict) -> str:
    """Markdown versionado en kaizen_reports/YYYY-MM.md."""
    lines = [f"# KAIZEN Monthly · {month}", ""]
    lines.append(f"**Net impact:** ${report['total_net_impact']:+,.2f}")
    lines.append("")
    lines.append("## Reglas activas")
    lines.append("")
    lines.append("| ID | Description | Blocks | Shadows | Saved | Missed | Net |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in report["rules_data"]:
        if r.get("active"):
            lines.append(
                f"| {r['id']} | {r.get('description', '')} | "
                f"{r.get('n_blocks', 0)} | "
                f"{r.get('n_shadows_closed', 0)} | "
                f"${r.get('gross_saved_usd', 0):.2f} | "
                f"${r.get('gross_missed_usd', 0):.2f} | "
                f"${r.get('net_impact_usd', 0):+.2f} |"
            )
    return "\n".join(lines)


def main() -> int:
    month = previous_month_str()
    print(f"→ KAIZEN monthly report para {month}")

    rules_doc = _load_rules()
    rules = rules_doc.get("rules", [])
    overrides = rules_doc.get("param_overrides", [])

    # Snapshot por bot
    rftm_db = Path(os.environ.get("RFTM_DB_PATH", str(ROOT / "trading_paper.db")))
    mrev_db = Path(os.environ.get("MREV_DB_PATH", str(ROOT / "mrev_paper.db")))

    rule_metrics: dict = {}
    flagged_for_decision = set()

    for db_path in (rftm_db, mrev_db):
        if not db_path.exists():
            print(f"  ⚠ {db_path} no existe, skip")
            continue
        conn = _open_db(db_path)
        n = snapshot_monthly_metrics(conn, month=month)
        print(f"  {db_path.name}: snapshot escribió {n} filas")
        for a in compute_month_aggregate(conn, month=month):
            # Sumar entre bots si una regla aparece en ambos
            rid = a["rule_id"]
            existing = rule_metrics.setdefault(rid, {
                "rule_id": rid,
                "n_shadows_closed": 0, "n_shadows_winners": 0,
                "n_shadows_losers": 0,
                "gross_saved_usd": 0.0, "gross_missed_usd": 0.0,
                "net_impact_usd": 0.0,
            })
            for k in ("n_shadows_closed", "n_shadows_winners", "n_shadows_losers",
                      "gross_saved_usd", "gross_missed_usd", "net_impact_usd"):
                existing[k] = existing.get(k, 0) + a.get(k, 0)
        # F6.3
        for rid in rules_needing_decision(conn):
            flagged_for_decision.add(rid)
        conn.close()

    # Combinar con rules de kaizen_rules.json
    combined_rules = []
    for r in rules:
        rid = r["id"]
        merged = {**r, **rule_metrics.get(rid, {})}
        combined_rules.append(merged)
    # Agregar reglas en metrics que no están en rules (raro pero defensivo)
    rule_ids = {r["id"] for r in rules}
    for rid, m in rule_metrics.items():
        if rid not in rule_ids:
            combined_rules.append({**m, "active": False})

    total_net = sum(r.get("net_impact_usd", 0) for r in combined_rules
                    if r.get("active"))

    report = {
        "month": month,
        "rules_data": combined_rules,
        "overrides_data": overrides,
        "needs_decision": list(flagged_for_decision),
        "total_net_impact": round(total_net, 2),
    }

    # Render
    html = _render_html(month, report)
    md = _render_markdown(month, report)

    # Versionar markdown
    out_dir = ROOT / "kaizen_reports"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"{month}.md"
    md_path.write_text(md)
    print(f"  ✓ markdown escrito: {md_path}")

    # Email
    subject_prefix = "[ACCIÓN REQUERIDA] " if flagged_for_decision else ""
    subject = f"{subject_prefix}KAIZEN Monthly · {month} · ${total_net:+,.2f}"
    try:
        from _email_helpers import send_smtp
        sent = send_smtp(subject=subject, html_body=html)
        print(f"  email sent: {sent}")
    except Exception as e:
        print(f"  ⚠ email failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
