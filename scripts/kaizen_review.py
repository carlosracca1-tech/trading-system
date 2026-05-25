#!/usr/bin/env python3
"""
kaizen_review.py — F5.2: análisis semanal de trades cerrados.

Workflow:
1. Leer eventos del JSONL (RFTM + MREV) de los últimos N días.
2. Agruparlos por trade_id y armar trade-summary: entry/exit context.
3. Mandar a Claude API con prompt estructurado pidiendo patrones.
4. Recibir JSON con rules: id, descripción, condición, n_trades,
   win/loss rate, expectancy, confidence.
5. Mergear contra `kaizen_rules.json` versionado (preserva existentes).

Decisión de activación (F5.3) NO la toma este script — solo escribe
las propuestas. Otro paso (`scripts/kaizen_apply.py` o el workflow
mensual) decide si auto-aplicarlas.

Uso:
    python3 scripts/kaizen_review.py --days 30 --dry-run    # preview
    python3 scripts/kaizen_review.py --days 30              # escribe rules

Env vars:
    ANTHROPIC_API_KEY     credencial para Claude API
    KAIZEN_RULES_PATH     path del JSON con reglas (default: kaizen_rules.json)
    KAIZEN_REVIEW_MODEL   modelo (default: claude-opus-4-6)
    KAIZEN_REVIEW_DAYS    ventana en días (default: 30)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── I/O helpers ──────────────────────────────────────────────────────────────


def load_events(jsonl_paths: list[Path], cutoff_iso: str) -> list[dict]:
    """Lee múltiples JSONLs y devuelve eventos posteriores al cutoff."""
    cutoff = datetime.fromisoformat(cutoff_iso)
    events: list[dict] = []
    seen_ids: set[str] = set()
    for p in jsonl_paths:
        if not p.exists():
            print(f"  ⚠ skip missing: {p}")
            continue
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Dedupe by event_id
                eid = ev.get("event_id")
                if eid and eid in seen_ids:
                    continue
                if eid:
                    seen_ids.add(eid)
                ts = ev.get("timestamp_utc", "")
                try:
                    ev_dt = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                if ev_dt < cutoff:
                    continue
                events.append(ev)
    return events


def group_into_trades(events: list[dict]) -> list[dict]:
    """Agrupa eventos por trade_id y devuelve un resumen por trade.

    Cada trade tiene:
    - trade_id, bot, symbol
    - entry: dict del evento BUY (con indicadores enriquecidos)
    - exits: lista de SELL_* events
    - outcome: 'winner' | 'loser' | 'breakeven' (basado en realized_pnl total)
    - total_pnl, exit_reason_primary, time_held_hours
    """
    by_trade = defaultdict(list)
    for ev in events:
        tid = ev.get("trade_id")
        if tid:
            by_trade[tid].append(ev)

    trades: list[dict] = []
    for tid, evs in by_trade.items():
        evs_sorted = sorted(evs, key=lambda e: e.get("timestamp_utc", ""))
        entry = next((e for e in evs_sorted if e.get("side") == "BUY"), None)
        if not entry:
            continue  # trade abierto antes del cutoff
        exits = [e for e in evs_sorted if e.get("side", "").startswith("SELL_")]
        if not exits:
            continue  # trade todavía abierto
        total_pnl = sum(e.get("realized_pnl_event") or 0 for e in exits)
        # Outcome simple basado en pnl total
        if total_pnl > 1:
            outcome = "winner"
        elif total_pnl < -1:
            outcome = "loser"
        else:
            outcome = "breakeven"
        # Exit primario = el evento con más qty vendida (o el último)
        primary_exit = max(exits, key=lambda e: e.get("qty") or 0)
        # Time held
        try:
            t0 = datetime.fromisoformat(entry["timestamp_utc"])
            t1 = datetime.fromisoformat(primary_exit["timestamp_utc"])
            time_held = (t1 - t0).total_seconds() / 3600
        except (KeyError, ValueError):
            time_held = None

        trades.append({
            "trade_id": tid,
            "bot": entry.get("bot"),
            "symbol": entry.get("symbol"),
            "entry": entry,
            "exits": exits,
            "outcome": outcome,
            "total_pnl": round(total_pnl, 2),
            "exit_reason_primary": primary_exit.get("reason"),
            "exit_side_primary": primary_exit.get("side"),
            "time_held_hours": time_held,
        })
    return trades


# ── Claude API ───────────────────────────────────────────────────────────────


PROMPT_TEMPLATE = """Sos un analista cuantitativo revisando el log de trades de un bot
de trading. Tu objetivo: identificar PATRONES REPLICABLES en los losers
vs winners.

Hay DOS tipos de mejoras que podés proponer:

1. **Reglas de bloqueo** (`rules`): condiciones booleanas que rechazan
   entradas adicionales. Son aditivas — solo bloquean más, nunca aflojan.
   Auto-activables si tienen evidencia fuerte.

2. **Param overrides** (`param_overrides`): cambios a valores numéricos
   de parámetros (TP%, cooldown days, ATR mult, etc.). NUNCA se
   auto-activan — siempre requieren aprobación humana. Solo propuestas.

Schema de salida (SOLO JSON, sin markdown):
{{
  "rules": [
    {{
      "id": "K_<descripcion_corta>",
      "description": "frase corta humana",
      "condition": "expresión python booleana sobre el dict del row,
                    ej. 'row.get(\\\"ind_rsi14\\\") and row[\\\"ind_rsi14\\\"] > 70'",
      "applies_to": "entry" | "exit",
      "n_trades": int,
      "win_rate": float,
      "loss_rate": float,
      "expectancy_usd": float,
      "confidence": "high" | "medium" | "low",
      "rationale": "1-2 oraciones de por qué"
    }}
  ],
  "param_overrides": [
    {{
      "id": "O_<descripcion_corta>",
      "param": "<NOMBRE_DEL_PARAM>",   // ver lista abajo
      "applies_to_bot": "RFTM" | "MREV" | "BOTH",
      "value": <nuevo_valor_numérico>,
      "previous_value": <valor_actual>,
      "rationale": "1-2 oraciones",
      "n_trades": int,
      "confidence": "high" | "medium" | "low"
    }}
  ]
}}

Params permitidos para overrides:
- PARTIAL_TP1_PCT (default 0.05) / PARTIAL_TP1_SELL_RATIO (default 0.50)
- PARTIAL_TP2_PCT (default 0.075) / PARTIAL_TP2_SELL_RATIO (default 0.50)
- ATR_MULT (default 1.5)              — solo RFTM
- RFTM_COOLDOWN_DAYS (default 5)
- MREV_COOLDOWN_HOURS (default 6)
- RFTM_REENTRY_MAX_RUNUP / MREV_REENTRY_MAX_RUNUP (default 0.10)
- FINAL_TP_PCT (default 0.10)
- MAX_DRAWDOWN (default 0.20)

Reglas:
- Confidence high: n_trades >= 10 y patrón replicable.
- Si no encontrás nada útil: `{{"rules": [], "param_overrides": []}}`.
- NO inventes campos. Usá solo los que aparecen en cada trade del input
  (top-level + `ind_*`/`reg_*`/`exe_*`).

Trades de los últimos {days} días ({n_trades} total: {n_winners}W / {n_losers}L / {n_be}BE):

{trades_json}

Devolveme las top 5 reglas y top 3 param overrides más significativos.
"""


def call_claude(prompt: str, model: str, api_key: str, max_tokens: int = 4096) -> dict:
    """Llama a Claude Messages API y parsea el JSON de salida."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude API {e.code}: {body[:500]}") from e
    # Extraer texto del response
    parts = resp.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    # Intentar parsear JSON. Claude a veces wrappea en markdown — extraerlo.
    text = text.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fences
        text = text.split("```", 2)
        if len(text) >= 2:
            text = text[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        else:
            text = ""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned non-JSON: {text[:500]}") from e


# ── Rules merge ──────────────────────────────────────────────────────────────


def merge_rules(existing_path: Path, new_payload: dict) -> dict:
    """Mergea reglas + param_overrides nuevas con las existentes
    preservando flags (`active`, `created_at`, `dismissed_at`)."""
    existing: dict = {"rules": [], "param_overrides": [], "last_review_iso": None}
    if existing_path.exists():
        try:
            existing = json.loads(existing_path.read_text())
        except json.JSONDecodeError:
            pass
    existing_rules = {r["id"]: r for r in existing.get("rules", [])}
    new_rules = new_payload.get("rules", [])
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    for nr in new_rules:
        rid = nr.get("id")
        if not rid:
            continue
        if rid in existing_rules:
            # Update stats; preservar active / created_at / dismissed_at
            old = existing_rules[rid]
            old.update({
                "description": nr.get("description", old.get("description")),
                "condition": nr.get("condition", old.get("condition")),
                "n_trades": nr.get("n_trades"),
                "win_rate": nr.get("win_rate"),
                "loss_rate": nr.get("loss_rate"),
                "expectancy_usd": nr.get("expectancy_usd"),
                "confidence": nr.get("confidence"),
                "rationale": nr.get("rationale"),
                "applies_to": nr.get("applies_to", old.get("applies_to")),
                "last_reviewed_at": now_iso,
            })
        else:
            existing_rules[rid] = {
                **nr,
                "active": False,  # default: propuesta — F5.3 decide activación
                "created_at": now_iso,
                "last_reviewed_at": now_iso,
            }

    # F5.5: merge de param_overrides — NUNCA auto-activos
    from _kaizen_overrides import merge_overrides
    existing_overrides = existing.get("param_overrides", [])
    new_overrides = new_payload.get("param_overrides", [])
    merged_overrides = merge_overrides(existing_overrides, new_overrides)

    out = {
        "rules": list(existing_rules.values()),
        "param_overrides": merged_overrides,
        "last_review_iso": now_iso,
    }
    return out


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int,
                        default=int(os.environ.get("KAIZEN_REVIEW_DAYS", "30")))
    parser.add_argument("--dry-run", action="store_true",
                        help="No llamar a Claude ni escribir kaizen_rules.json")
    parser.add_argument("--no-api", action="store_true",
                        help="Skipear llamada a Claude — útil para validar el prompt")
    args = parser.parse_args()

    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).isoformat()
    print(f"→ Buscando eventos desde {cutoff}")

    jsonl_paths = [
        ROOT / "logs" / "trade_events_rftm.jsonl",
        ROOT / "logs" / "trade_events_mrev.jsonl",
        ROOT / "logs" / "trade_events.jsonl",  # legacy/local fallback
    ]
    events = load_events(jsonl_paths, cutoff)
    print(f"  {len(events)} eventos cargados")

    trades = group_into_trades(events)
    print(f"  {len(trades)} trades cerrados")
    if not trades:
        print("Nada para analizar. Saliendo.")
        return 0

    n_w = sum(1 for t in trades if t["outcome"] == "winner")
    n_l = sum(1 for t in trades if t["outcome"] == "loser")
    n_be = sum(1 for t in trades if t["outcome"] == "breakeven")
    print(f"  outcomes: {n_w} winners · {n_l} losers · {n_be} breakeven")

    # Compactar trades para el prompt
    def _compact(t):
        e = t.get("entry") or {}
        enriched = e.get("enriched") or {}
        out = {
            "id": t["trade_id"],
            "bot": t["bot"],
            "symbol": t["symbol"],
            "outcome": t["outcome"],
            "total_pnl": t["total_pnl"],
            "exit_reason": t["exit_reason_primary"],
            "exit_side": t["exit_side_primary"],
            "time_held_hours": (
                round(t["time_held_hours"], 1) if t["time_held_hours"] else None
            ),
            # Incluir todos los enriched (prefijos ind_/reg_/exe_)
            **enriched,
        }
        return out

    compact_trades = [_compact(t) for t in trades]

    prompt = PROMPT_TEMPLATE.format(
        days=args.days,
        n_trades=len(trades),
        n_winners=n_w,
        n_losers=n_l,
        n_be=n_be,
        trades_json=json.dumps(compact_trades, indent=2, default=str),
    )

    if args.dry_run or args.no_api:
        print()
        print("─── PROMPT PREVIEW ───")
        print(prompt[:1500])
        print(f"... ({len(prompt)} chars total)")
        print()
        if args.dry_run:
            return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("✗ ANTHROPIC_API_KEY no seteado")
        return 2

    model = os.environ.get("KAIZEN_REVIEW_MODEL", "claude-opus-4-6")
    print(f"→ Calling Claude ({model})...")
    payload = call_claude(prompt, model=model, api_key=api_key)
    print(f"  recibidas {len(payload.get('rules', []))} reglas")

    rules_path = Path(os.environ.get(
        "KAIZEN_RULES_PATH", str(ROOT / "kaizen_rules.json")
    ))
    merged = merge_rules(rules_path, payload)

    # F5.3: auto-activar reglas que cumplen criterios estrictos
    from _kaizen_rules import auto_activate
    newly_activated = auto_activate(merged["rules"])
    rules_path.write_text(json.dumps(merged, indent=2))
    print(f"✓ {rules_path} actualizado ({len(merged['rules'])} reglas totales)")
    if newly_activated:
        print(f"  → {len(newly_activated)} reglas AUTO-ACTIVADAS:")
        for r in newly_activated:
            print(f"    • {r['id']}: {r.get('description', '?')}")
        # F5.3: email a Charlie con rationale + comando para desactivar
        try:
            _send_activation_email(newly_activated)
        except Exception as e:
            print(f"  ⚠ activation email falló: {e}")
    return 0


def _send_activation_email(rules: list) -> None:
    """Email cuando KAIZEN auto-aplica reglas nuevas."""
    from _email_helpers import send_smtp

    rows = []
    for r in rules:
        rows.append(f"""
        <tr>
          <td><code>{r['id']}</code></td>
          <td>{r.get('description', '')}</td>
          <td><code>{r.get('condition', '')}</code></td>
          <td>{r.get('n_trades', '?')} trades</td>
          <td>{r.get('confidence', '?')}</td>
          <td>${r.get('expectancy_usd', 0):.2f}</td>
        </tr>""")
    html = f"""
    <html><body style='font-family:system-ui,sans-serif;color:#222'>
    <h2 style='color:#2980b9'>KAIZEN auto-activó {len(rules)} regla(s) nueva(s)</h2>
    <p>Cumplieron los criterios: <code>n_trades&ge;10</code>, win/loss rate
    &ge; 80%, confidence=high.</p>
    <table border="1" cellpadding="6" cellspacing="0" style='border-collapse:collapse;font-size:13px'>
      <thead style='background:#eee'>
        <tr><th>ID</th><th>Descripción</th><th>Condición</th><th>N</th><th>Confidence</th><th>Expectancy</th></tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <h3>Rationale</h3>
    <ul>
    {''.join(f'<li><strong>{r["id"]}:</strong> {r.get("rationale", "—")}</li>' for r in rules)}
    </ul>
    <h3>¿No estás de acuerdo?</h3>
    <p>Para desactivar una regla, editá <code>kaizen_rules.json</code>
    en la branch <code>state/db</code> seteando <code>"active": false</code>
    y <code>"dismissed_at"</code>. O usá el workflow
    <code>kaizen_decision.yml</code> con <code>decision=reject</code>
    (ver F6.4).</p>
    <hr style='margin:18px 0;border:none;border-top:1px solid #ddd'>
    <p style='font-size:11px;color:#999'>F5.3 — KAIZEN auto-activation.</p>
    </body></html>
    """
    send_smtp(subject=f"[KAIZEN] {len(rules)} regla(s) auto-activada(s)",
              html_body=html)


if __name__ == "__main__":
    sys.exit(main())
