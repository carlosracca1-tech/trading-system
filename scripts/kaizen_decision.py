#!/usr/bin/env python3
"""
kaizen_decision.py — F6.4: aplica decisión (approve/reject) a una regla
o param_override.

Env vars (los setea el workflow):
    TARGET_ID    K_xxx o O_xxx
    TARGET_TYPE  "rule" | "override"
    DECISION     "approve" | "reject"

Política:
- approve rule        → active=True, activated_at=now, activation_mode="manual"
- reject rule         → active=False, dismissed_at=now
- approve override    → active=True, activated_at=now
- reject override     → active=False, dismissed_at=now

Si el target no existe, exit 2.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TARGET_ID = os.environ.get("TARGET_ID", "").strip()
TARGET_TYPE = os.environ.get("TARGET_TYPE", "rule").strip()
DECISION = os.environ.get("DECISION", "approve").strip()


def main() -> int:
    if not TARGET_ID:
        print("✗ TARGET_ID vacío")
        return 2
    if TARGET_TYPE not in ("rule", "override"):
        print(f"✗ TARGET_TYPE inválido: {TARGET_TYPE!r}")
        return 2
    if DECISION not in ("approve", "reject"):
        print(f"✗ DECISION inválida: {DECISION!r}")
        return 2

    path = ROOT / "kaizen_rules.json"
    if not path.exists():
        print(f"✗ {path} no existe")
        return 2
    data = json.loads(path.read_text())

    key = "rules" if TARGET_TYPE == "rule" else "param_overrides"
    targets = data.get(key, [])
    match = next((t for t in targets if t.get("id") == TARGET_ID), None)
    if match is None:
        print(f"✗ no se encontró {TARGET_TYPE} con id={TARGET_ID}")
        print(f"  Disponibles: {[t.get('id') for t in targets]}")
        return 2

    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    if DECISION == "approve":
        match["active"] = True
        match["activated_at"] = now
        match["activation_mode"] = "manual"
        # Si fue rejected previamente, levantar el flag
        match.pop("dismissed_at", None)
        print(f"✓ {TARGET_TYPE} {TARGET_ID} APROBADA (active=True)")
    else:  # reject
        match["active"] = False
        match["dismissed_at"] = now
        # KAIZEN no re-propone reglas dismissed por 90 días — el job
        # weekly chequea este flag al mergear.
        print(f"✓ {TARGET_TYPE} {TARGET_ID} RECHAZADA (active=False, dismissed_at={now})")

    path.write_text(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
