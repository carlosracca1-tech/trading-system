#!/usr/bin/env python3
"""
kaizen_decision_email.py — manda email de confirmación tras una decisión.

Lo invoca el workflow kaizen_decision.yml después de aplicar el cambio.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    target_id = os.environ.get("TARGET_ID", "?")
    target_type = os.environ.get("TARGET_TYPE", "?")
    decision = os.environ.get("DECISION", "?")
    run_result = os.environ.get("RUN_RESULT", "success")

    badge_color = "#27ae60" if run_result == "success" else "#c0392b"
    badge_text = "OK" if run_result == "success" else "FALLÓ"
    action_text = "aprobada" if decision == "approve" else "rechazada"
    action_color = "#27ae60" if decision == "approve" else "#e67e22"

    html = f"""
    <html><body style='font-family:system-ui,sans-serif;color:#222'>
      <h2 style='margin:0 0 8px 0'>
        <span style='background:{badge_color};color:white;padding:2px 8px;
                     border-radius:4px;font-size:13px'>{badge_text}</span>
        Decisión KAIZEN aplicada
      </h2>
      <p>
        <strong>{target_type}</strong>
        <code style='background:#eee;padding:2px 6px;border-radius:3px'>
          {target_id}
        </code>
        fue <strong style='color:{action_color}'>{action_text}</strong>.
      </p>
      <p style='font-size:13px;color:#666'>
        El cambio se commiteó a la branch <code>state/db</code>. El
        próximo run del bot va a leerlo automáticamente.
      </p>
      <hr style='margin:16px 0;border:none;border-top:1px solid #ddd'>
      <p style='font-size:11px;color:#999'>
        F6.4 — KAIZEN decision workflow.
        Si esto no era lo que querías, podés correr el workflow de nuevo
        con la decisión opuesta.
      </p>
    </body></html>
    """
    try:
        from _email_helpers import send_smtp
        ok = send_smtp(
            subject=f"[KAIZEN] {target_id} {action_text}",
            html_body=html,
        )
        return 0 if ok else 1
    except Exception as e:
        print(f"✗ email failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
