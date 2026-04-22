"""
_email_helpers.py — SMTP + HTML helpers compartidos entre RFTM y MREV.

Qué vive acá:
    - send_smtp(): wrapper SMTP con TLS, lee credenciales de env vars.
    - send_stage_event_email(): email inmediato por TP1/TP2/TP_FINAL
      (idéntico en ambos bots antes de este refactor).
    - build_css(): bloque CSS con dark/light mode y responsive (para el
      reescribe del mensual en Fase 4).
    - position_card(): renderea el item "Lo que tengo en cartera"
      (cuadrados SL/Precio/TP + línea de stage). Sólo stdlib.

No cambia el contrato visual de ningún email ya funcionando.
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── Logging shim (import-safe: no imposes a logger) ──────────────────────────
def _log(level: str, msg: str) -> None:
    """Print-based log for helper side effects. Each bot has its own coloured
    logger but we don't depend on it — keep this module standalone."""
    prefix = {"ok": "[OK]  ", "info": "[INFO]", "warn": "[WARN]", "err": "[ERR] "}.get(level, "[INFO]")
    print(f"{prefix} {msg}")


# ── Low-level SMTP wrapper ───────────────────────────────────────────────────

def send_smtp(
    subject: str,
    html_body: str,
    *,
    smtp_server: Optional[str] = None,
    smtp_port: Optional[int] = None,
    email_from: Optional[str] = None,
    email_password: Optional[str] = None,
    email_to: Optional[str] = None,
) -> bool:
    """
    Send one HTML email via SMTP with STARTTLS.

    Any argument not supplied falls back to its env var counterpart
    (EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT, EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO).

    Returns True on success, False on any failure. Never raises; the caller
    decides whether to warn the user.
    """
    smtp_server    = smtp_server    or _env("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    smtp_port      = smtp_port      or int(_env("EMAIL_SMTP_PORT", "587"))
    email_from     = email_from     or _env("EMAIL_FROM", "")
    email_password = email_password or _env("EMAIL_PASSWORD", "")
    email_to       = email_to       or _env("EMAIL_TO", "")

    if not email_from or not email_password or not email_to:
        _log("warn", "Email not configured — set EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_from
        msg["To"]      = email_to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_from, email_password)
            server.sendmail(email_from, email_to, msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        _log("err", f"SMTP send failed: {e}")
        return False


# ── Stage event email (TP1 / TP2 / TP_FINAL) ─────────────────────────────────

def send_stage_event_email(
    bot_tag: str,
    event: str,           # "TP1" | "TP2" | "TP_FINAL"
    symbol: str,
    entry_price: float,
    sell_price: float,
    sell_qty: float,
    realized_pnl: float,
    remaining_qty: float,
    new_stage: int,
    next_target: Optional[float],
    next_target_label: str,
    current_price: Optional[float] = None,
    dry_run: bool = False,
    email_enabled: Optional[bool] = None,
    old_stop_loss: Optional[float] = None,
    new_stop_loss: Optional[float] = None,
) -> None:
    """
    Send a single-event email when TP1/TP2/TP_FINAL fires for a position.

    - bot_tag: short label shown in subject (e.g. "RFTM" or "MREV").
    - event: "TP1", "TP2" or "TP_FINAL".
    - next_target: price of the next stage target. None if fully closed.
    - next_target_label: human label ("TP2", "TP final", etc).
    - old_stop_loss / new_stop_loss: si el stop subió en este evento (típico
      TP1 → breakeven), se muestra un bloque destacado en el email.

    Respects `dry_run` (just logs). Falls back to env var `EMAIL_ENABLED` when
    `email_enabled` is not passed explicitly.
    """
    if dry_run:
        _log("info", f"[DRY] stage event email skipped · {bot_tag} {event} {symbol}")
        return

    if email_enabled is None:
        email_enabled = _env("EMAIL_ENABLED", "true").lower() == "true"
    if not email_enabled:
        return

    pct_gain = ((sell_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    qty_disp = f"{sell_qty:g}"
    subject = f"[{event}] {symbol} {pct_gain:+.1f}% · vendí {qty_disp} @ ${sell_price:,.2f}"
    if bot_tag:
        subject = f"[{bot_tag}] {subject}"

    if next_target is None or remaining_qty <= 0:
        next_line = "Posición cerrada completamente."
    else:
        cur = float(current_price) if current_price else sell_price
        if cur > 0:
            delta_pct = (next_target - cur) / cur * 100
            if delta_pct >= 0:
                next_line = (
                    f"Próximo: <b>{next_target_label}</b> a <b>${next_target:,.2f}</b> "
                    f"(faltan <b>{delta_pct:.1f}%</b>)."
                )
            else:
                next_line = (
                    f"Próximo: <b>{next_target_label}</b> a <b>${next_target:,.2f}</b> "
                    f"— ya superado, dispara en la próxima corrida."
                )
        else:
            next_line = f"Próximo: <b>{next_target_label}</b> a <b>${next_target:,.2f}</b>."

    pnl_color = "#1b9e4b" if realized_pnl >= 0 else "#d63031"

    # Bloque de stop-loss: si el stop subió en este evento (típicamente TP1 →
    # breakeven), lo resaltamos. Si además el nuevo stop quedó exactamente en
    # el entry_price, lo llamamos "breakeven" y explicamos qué significa.
    stop_block = ""
    if new_stop_loss is not None and new_stop_loss > 0:
        old_sl = float(old_stop_loss) if old_stop_loss is not None else 0.0
        stop_moved = new_stop_loss > old_sl + 1e-6  # tolerancia float
        is_breakeven = abs(new_stop_loss - entry_price) < 1e-4
        if stop_moved and is_breakeven:
            stop_block = (
                f'<div class="stop">'
                f'🛡️ <b>Stop loss movido a breakeven: ${new_stop_loss:,.2f}</b><br>'
                f'<span style="font-size:12px; color:#555;">'
                f'(antes estaba en ${old_sl:,.2f}). A partir de ahora el '
                f'{int(remaining_qty)} remanente <b>no puede volver a pérdida</b> — '
                f'si cae a ${new_stop_loss:,.2f} sale en empate.'
                f'</span>'
                f'</div>'
            )
        elif stop_moved:
            stop_block = (
                f'<div class="stop">'
                f'🛡️ Stop subido: ${old_sl:,.2f} → <b>${new_stop_loss:,.2f}</b>'
                f'</div>'
            )
        else:
            stop_block = (
                f'<div class="stop" style="background:#f7f7f7; border-left-color:#888;">'
                f'Stop sigue en <b>${new_stop_loss:,.2f}</b>.'
                f'</div>'
            )

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family:-apple-system,Helvetica,Arial,sans-serif; background:#f4f4f4; margin:0; padding:16px; color:#222; }}
.box {{ max-width:520px; margin:0 auto; background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
h1 {{ margin:0 0 8px; font-size:17px; }}
.tag {{ display:inline-block; padding:3px 10px; border-radius:6px; font-size:11px; font-weight:700; background:#e8f5e9; color:#1b9e4b; margin-bottom:8px; }}
.row {{ font-size:14px; line-height:1.7; }}
.pnl {{ font-weight:700; color:{pnl_color}; }}
.stop {{ background:#fff7e6; border-left:3px solid #f39c12; padding:10px 12px; border-radius:6px; margin-top:10px; font-size:13px; color:#333; }}
.next {{ background:#f0f7ff; border-left:3px solid #2980b9; padding:10px 12px; border-radius:6px; margin-top:10px; font-size:13px; color:#333; }}
.foot {{ text-align:center; color:#bbb; font-size:11px; padding:10px 0 0; }}
@media (prefers-color-scheme: dark) {{
  body {{ background:#0f1115; color:#e5e8ee; }}
  .box {{ background:#1a1d24; box-shadow:0 1px 4px rgba(0,0,0,.4); }}
  h1 {{ color:#f2f4f8; }}
  .row {{ color:#e5e8ee; }}
  .stop {{ background:#2a2410; color:#e5e8ee; }}
  .stop span {{ color:#b0b7c3 !important; }}
  .next {{ background:#10202a; color:#e5e8ee; }}
  .foot {{ color:#7b8595; }}
}}
</style></head>
<body><div class="box">
<span class="tag">{bot_tag} · {event}</span>
<h1>{symbol} — {event} disparado</h1>
<div class="row">
    Vendí <b>{qty_disp}</b> a <b>${sell_price:,.2f}</b> (entrada ${entry_price:,.2f}, {pct_gain:+.2f}%).<br>
    Realizado: <span class="pnl">${realized_pnl:+,.2f}</span>.<br>
    Qty restante: <b>{remaining_qty:g}</b> · stage: <b>{new_stage}</b>.
</div>
{stop_block}
<div class="next">{next_line}</div>
<div class="foot">{bot_tag} Bot · notificación de stage</div>
</div></body></html>"""

    ok = send_smtp(subject, body)
    if ok:
        _log("ok", f"Email de {event} enviado para {symbol}")
    else:
        _log("warn", f"Falló email de {event} para {symbol}")


# ── CSS for report emails (dark / light mode, responsive) ────────────────────

def build_css() -> str:
    """
    Return a CSS block compatible with dark + light mode and mobile
    (down to ~380px). Consumed by the monthly MREV report (Fase 4) and
    any future shared report.

    Keep selectors short and avoid JS-only features: many email clients
    strip external CSS and <style> inside <head> is the safest bet.
    """
    return """
    * { box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        margin: 0; padding: 16px;
        background: #f4f6fa; color: #1c1f25;
        line-height: 1.55;
    }
    .wrap { max-width: 720px; margin: 0 auto; }
    .card {
        background: #ffffff; border-radius: 14px;
        padding: 18px 20px; margin-bottom: 14px;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.05);
        border: 1px solid #e7eaf0;
    }
    h1 { font-size: 20px; margin: 0 0 4px; font-weight: 700; }
    h2 { font-size: 16px; margin: 0 0 10px; font-weight: 700; color: #2c3440; }
    h3 { font-size: 14px; margin: 0 0 6px; font-weight: 600; color: #48525f; }
    p  { margin: 4px 0; font-size: 14px; }
    small { color: #7b8595; font-size: 12px; }
    .hero {
        text-align: center;
        background: linear-gradient(135deg, #1b9e4b 0%, #128a3d 100%);
        color: #fff; padding: 24px 18px; border-radius: 14px;
        margin-bottom: 14px;
    }
    .hero.loss { background: linear-gradient(135deg, #d63031 0%, #a82323 100%); }
    .hero .metric { font-size: 32px; font-weight: 700; letter-spacing: -0.5px; }
    .hero .label  { font-size: 12px; opacity: 0.85; margin-bottom: 2px; text-transform: uppercase; letter-spacing: 0.5px; }
    .kpis { display: table; width: 100%; }
    .kpi  { display: table-cell; text-align: center; padding: 8px 4px; vertical-align: top; }
    .kpi .v { font-size: 18px; font-weight: 700; color: #1c1f25; }
    .kpi .l { font-size: 11px; color: #7b8595; text-transform: uppercase; letter-spacing: 0.4px; }
    table.data {
        width: 100%; border-collapse: collapse; font-size: 13px;
    }
    table.data th {
        text-align: left; font-weight: 600;
        padding: 6px 8px; color: #48525f;
        border-bottom: 1px solid #e7eaf0;
    }
    table.data td {
        padding: 6px 8px; border-bottom: 1px solid #f0f2f7;
        color: #2c3440; vertical-align: top;
    }
    .pos { color: #1b9e4b; font-weight: 600; }
    .neg { color: #d63031; font-weight: 600; }
    .muted { color: #7b8595; }
    .box3 { display: table; width: 100%; table-layout: fixed; border-spacing: 6px 0; margin-top: 8px; }
    .box3 > div {
        display: table-cell; text-align: center; padding: 8px 4px;
        background: #f4f6fa; border-radius: 8px; font-size: 12px;
    }
    .box3 .v { display: block; font-size: 14px; font-weight: 700; }
    .box3 .sl  .v { color: #d63031; }
    .box3 .tp  .v { color: #1b9e4b; }
    .stage-line { margin-top: 6px; font-size: 12px; color: #48525f; }
    .spark { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 14px; letter-spacing: 2px; }
    .foot { text-align: center; color: #a4aab5; font-size: 11px; padding: 12px 0 0; }

    @media (prefers-color-scheme: dark) {
        body { background: #0f1115; color: #e5e8ee; }
        .card { background: #1a1d24; border-color: #2a2e38; box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4); }
        h1, h2 { color: #f2f4f8; }
        h3 { color: #b0b7c3; }
        small, .muted, .kpi .l, .foot { color: #7b8595; }
        .kpi .v { color: #f2f4f8; }
        table.data th { color: #b0b7c3; border-bottom-color: #2a2e38; }
        table.data td { color: #e5e8ee; border-bottom-color: #22262f; }
        .box3 > div { background: #22262f; }
        .stage-line { color: #b0b7c3; }
    }

    @media (max-width: 480px) {
        body { padding: 8px; }
        .card { padding: 14px 14px; margin-bottom: 10px; border-radius: 10px; }
        .hero { padding: 18px 12px; margin-bottom: 10px; }
        .hero .metric { font-size: 26px; }
        .kpis { display: block; }
        .kpi  { display: block; text-align: left; padding: 6px 0; border-bottom: 1px solid #e7eaf0; }
        .kpi .v { display: inline-block; margin-right: 8px; }
        .kpi .l { display: inline-block; }
        table.data th, table.data td { padding: 5px 4px; font-size: 12px; }
        .box3 { border-spacing: 4px 0; }
        .box3 > div { padding: 6px 2px; font-size: 11px; }
        .box3 .v { font-size: 13px; }
    }
    """


# ── Position card renderer (SL / Precio / TP + stage line) ───────────────────

def position_card(
    *,
    symbol: str,
    entry_price: float,
    current_price: float,
    stop_loss: float,
    take_profit: float,
    stage: int,
    qty: float,
    next_target_pct: Optional[float] = None,
    next_target_label: Optional[str] = None,
    next_target_price: Optional[float] = None,
) -> str:
    """
    Render one "Lo que tengo en cartera" card: header + three boxes
    (SL / Precio / TP) + stage line (faltan X% para TPN a $Y).

    This helper doesn't open/close its own .card container — callers wrap
    as needed so they can group multiple cards into one section.
    """
    unrealized_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    pnl_cls = "pos" if unrealized_pct >= 0 else "neg"

    stage_line = ""
    if next_target_label and next_target_price and next_target_price > 0:
        if next_target_pct is not None and next_target_pct >= 0:
            stage_line = (
                f'<div class="stage-line">Stage <b>{stage}</b> · '
                f'faltan <b>{next_target_pct:.1f}%</b> para <b>{next_target_label}</b> '
                f'a <b>${next_target_price:,.2f}</b></div>'
            )
        else:
            stage_line = (
                f'<div class="stage-line">Stage <b>{stage}</b> · '
                f'<b>{next_target_label}</b> a <b>${next_target_price:,.2f}</b> '
                f'(dispara en la próxima corrida)</div>'
            )
    else:
        stage_line = f'<div class="stage-line">Stage <b>{stage}</b></div>'

    return f"""
    <div style="padding:10px 0; border-bottom:1px solid #eee;">
        <div style="display:flex; justify-content:space-between; align-items:baseline;">
            <strong>{symbol}</strong>
            <span class="{pnl_cls}">{unrealized_pct:+.2f}% · qty {qty:g}</span>
        </div>
        <div class="box3">
            <div class="sl"><small>SL</small><span class="v">${stop_loss:,.2f}</span></div>
            <div>    <small>Precio</small><span class="v">${current_price:,.2f}</span></div>
            <div class="tp"><small>TP</small><span class="v">${take_profit:,.2f}</span></div>
        </div>
        {stage_line}
    </div>
    """
