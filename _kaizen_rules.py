"""
_kaizen_rules — F5.3/F5.4: load + decide-activación + match de reglas.

Funciones:
- `load_rules(path)`: lee kaizen_rules.json, devuelve lista.
- `load_active_rules(path, applies_to)`: filtra solo `active=True`.
- `should_auto_apply(rule)`: política F5.3 (n>=10, rate>=0.80, confidence=high).
- `auto_activate(rules)`: marca como `active=True` las que pasan el filtro
  y NO fueron descartadas explícitamente (dismissed_at). Devuelve lista
  de las recién activadas para email/notif.
- `rule_matches(rule, row)`: evalúa la `condition` python expr contra
  un dict de contexto. Sandbox restringido — solo lectura de claves.

Diseño de seguridad:
- Las `condition` vienen de Claude — son strings de Python potencialmente
  arbitrarios. `rule_matches` usa `eval()` con un dict de globals MUY
  restringido (sin `__builtins__`, sin imports). Si una regla intenta
  acceder a algo fuera del row, falla con False (no matchea).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


def default_rules_path() -> Path:
    override = os.environ.get("KAIZEN_RULES_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "kaizen_rules.json"


# ── Load ─────────────────────────────────────────────────────────────────────


def load_rules(path: Optional[Path] = None) -> list[dict]:
    """Lee el archivo y devuelve la lista de reglas. [] si no existe."""
    p = path or default_rules_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return data.get("rules", [])


def load_active_rules(
    path: Optional[Path] = None, applies_to: Optional[str] = None
) -> list[dict]:
    """Solo las que tienen `active=True`. Opcionalmente filtra por
    `applies_to` ("entry" | "exit")."""
    rules = load_rules(path)
    out = [r for r in rules if r.get("active") is True]
    if applies_to:
        out = [r for r in out if r.get("applies_to") == applies_to]
    return out


# ── Decide-activación (F5.3) ─────────────────────────────────────────────────


def should_auto_apply(rule: dict) -> tuple[bool, str]:
    """¿Esta regla cumple los criterios estrictos para auto-aplicarse?

    Plan F5.3:
    - n_trades >= 10
    - loss_rate >= 0.80 OR win_rate >= 0.80
    - confidence == "high"
    - NO dismissed previamente
    - Condition no vacía (algo replicable)

    Devuelve (decision, motivo).
    """
    if rule.get("dismissed_at"):
        return False, "dismissed"
    if not rule.get("condition"):
        return False, "no condition"
    n = rule.get("n_trades") or 0
    if n < 10:
        return False, f"n_trades={n} < 10"
    win = rule.get("win_rate") or 0
    loss = rule.get("loss_rate") or 0
    if win < 0.80 and loss < 0.80:
        return False, f"rate too low (w={win:.2f}, l={loss:.2f})"
    if rule.get("confidence") != "high":
        return False, f"confidence={rule.get('confidence')!r}"
    return True, "auto-apply criteria met"


def auto_activate(rules: list[dict]) -> list[dict]:
    """Mutates `rules` in-place: marca `active=True` y `activated_at` en
    las que pasan should_auto_apply. Devuelve la lista de recién activadas
    (para email)."""
    newly = []
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    for r in rules:
        if r.get("active"):
            continue  # ya estaba activa
        ok, _ = should_auto_apply(r)
        if ok:
            r["active"] = True
            r["activated_at"] = now
            r["activation_mode"] = "auto"
            newly.append(r)
    return newly


def save_rules(rules: list[dict], path: Optional[Path] = None) -> None:
    """Persiste rules manteniendo el campo `last_review_iso` si existía."""
    p = path or default_rules_path()
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except json.JSONDecodeError:
            existing = {}
    existing["rules"] = rules
    p.write_text(json.dumps(existing, indent=2))


# ── Match (F5.4) — sandbox de eval ───────────────────────────────────────────


# Constantes seguras que las condiciones de Claude pueden usar.
_SAFE_GLOBALS: dict = {
    "__builtins__": {
        "True": True, "False": False, "None": None,
        "abs": abs, "max": max, "min": min, "round": round,
        "len": len, "isinstance": isinstance, "float": float, "int": int,
        "str": str, "bool": bool, "any": any, "all": all,
    }
}


def rule_matches(rule: dict, row: dict) -> bool:
    """Evalúa la `condition` python contra un dict de contexto.

    Sandbox: globals restringidos a builtins seguros. La condición se
    evalúa con `row` como local (el caller puede acceder vía `row[...]`
    o `row.get(...)`).

    Cualquier excepción se traga como False — una regla rota no debe
    romper el flujo del bot.
    """
    cond = rule.get("condition")
    if not cond:
        return False
    try:
        result = eval(cond, _SAFE_GLOBALS, {"row": row})
        return bool(result)
    except Exception:
        return False


def evaluate_entry_rules(
    row: dict, path: Optional[Path] = None
) -> Optional[dict]:
    """Devuelve la PRIMERA regla activa que matchea contra el row.

    Si ninguna matchea, devuelve None. Si matchea, el caller decide
    qué hacer (ej. rechazar la entry y loguear el rule_id).
    """
    rules = load_active_rules(path, applies_to="entry")
    for r in rules:
        if rule_matches(r, row):
            return r
    return None
