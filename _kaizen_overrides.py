"""
_kaizen_overrides — F5.5: param overrides propuestos por KAIZEN.

Distinto a `_kaizen_rules.py` (que filtra entries):
- Las "reglas" matchean contra rows y rechazan entradas adicionales.
- Los "overrides" cambian VALORES de parámetros globales (PARTIAL_TP1_PCT,
  RFTM_COOLDOWN_DAYS, ATR_MULT, etc.) sin tocar código.

Diferencia clave de política:
- Reglas: auto-aplicables si n>=10 + rate>=0.80 + confidence=high (F5.3).
- Overrides: NUNCA auto-aplicables. SIEMPRE requieren aprobación
  manual (F6.4) — son cambios al comportamiento que pueden romper la
  estrategia. Charlie aprueba via workflow_dispatch.

Schema (extensión de kaizen_rules.json):
{
  "rules": [...],
  "param_overrides": [
    {
      "id": "O_tp1_to_3pct",
      "param": "PARTIAL_TP1_PCT",        // nombre de la env var
      "applies_to_bot": "RFTM" | "MREV" | "BOTH",
      "value": 0.03,                      // nuevo valor
      "previous_value": 0.05,             // baseline
      "rationale": "Trades que llegaron a +3% pero no a +5% reversaron 70%",
      "n_trades": 25,
      "confidence": "high",
      "active": false,                    // F6.4 lo flippea
      "created_at": "2026-05-20T...",
      "activated_at": null,
      "dismissed_at": null
    }
  ]
}

API:
- load_overrides(): lista de todos los overrides
- load_active_overrides(bot): solo `active=True` filtrados por bot
- get_param(name, default, bot): si hay override activo para ese param
  y bot, lo devuelve; sino default

Uso en el bot al inicio:
    from _kaizen_overrides import get_param
    PARTIAL_TP1_PCT = get_param("PARTIAL_TP1_PCT", 0.05, bot="RFTM")
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def _default_path() -> Path:
    override = os.environ.get("KAIZEN_RULES_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "kaizen_rules.json"


def load_overrides(path: Optional[Path] = None) -> list[dict]:
    """Lee `param_overrides` del JSON. [] si no existe o vacío."""
    p = path or _default_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return data.get("param_overrides", [])


def load_active_overrides(
    bot: Optional[str] = None, path: Optional[Path] = None
) -> list[dict]:
    """Solo `active=True`, opcionalmente filtrados por bot."""
    overrides = load_overrides(path)
    active = [o for o in overrides if o.get("active") is True]
    if bot:
        bot_u = bot.upper()
        active = [
            o for o in active
            if o.get("applies_to_bot", "BOTH").upper() in (bot_u, "BOTH")
        ]
    return active


def get_param(
    name: str, default: Any, bot: Optional[str] = None,
    path: Optional[Path] = None,
) -> Any:
    """Devuelve el valor del param, aplicando override si está activo.

    Si hay múltiples overrides activos para el mismo (param, bot), gana
    el más reciente (último `activated_at`).
    """
    actives = load_active_overrides(bot=bot, path=path)
    matches = [o for o in actives if o.get("param") == name]
    if not matches:
        return default
    # Ordenar por activated_at descending, el más reciente gana
    matches.sort(key=lambda o: o.get("activated_at") or "", reverse=True)
    val = matches[0].get("value")
    return val if val is not None else default


def merge_overrides(
    existing: list[dict], proposed: list[dict],
) -> list[dict]:
    """Merge de la lista existente con propuestas nuevas.

    Política:
    - Si propuesta nueva con id ya existente: actualiza stats pero
      preserva `active`, `activated_at`, `dismissed_at`.
    - Si nueva: agrega con active=False (NUNCA auto-activa overrides).
    """
    from datetime import datetime, timezone
    by_id = {o["id"]: o for o in existing}
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    for p in proposed:
        oid = p.get("id")
        if not oid:
            continue
        if oid in by_id:
            old = by_id[oid]
            # Actualizar stats sin tocar flags
            for k in ("rationale", "n_trades", "confidence",
                      "value", "previous_value"):
                if k in p:
                    old[k] = p[k]
            old["last_reviewed_at"] = now
        else:
            by_id[oid] = {
                **p,
                "active": False,  # NUNCA auto-activo
                "created_at": now,
                "activated_at": None,
                "dismissed_at": None,
                "last_reviewed_at": now,
            }
    return list(by_id.values())
