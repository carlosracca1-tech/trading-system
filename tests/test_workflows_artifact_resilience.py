"""
Regresión: artifact upload no debe romper el job.

Histórico (2026-05-02 a 2026-05-03): la cuota de GitHub Actions Artifacts del
account se llenó (MREV watchdog corre 288×/día + MREV-1H 24×/día × retention=14).
`actions/upload-artifact@v4` empezó a fallar con
"Failed to CreateArtifact: Artifact storage quota has been hit" y como el step
default mata el job, los workflows MREV quedaron rojos por días y mandaron
email cada run a pesar de que los bots ejecutaban OK.

Fix estructural:
1. `continue-on-error: true` en cada step Upload logs — un fallo de upload no
   debe abortar el job (el bot ya hizo el trabajo real arriba).
2. retention-days reducido en workflows de alta frecuencia para que la cuota
   no vuelva a saturarse: watchdogs=1d, hourly=7d, daily=7d.

Este test falla si alguien re-introduce un upload step sin esos guardas.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


# (workflow_file, max_retention_days_allowed)
WORKFLOW_LIMITS = [
    ("mrev_watchdog.yml", 3),    # corre cada 5min — tiene que ser bajo
    ("rftm_watchdog.yml", 3),    # corre cada 5min en mkt hours
    ("mrev_hourly.yml",  10),    # corre 24×/día
    ("daily_trade.yml",  14),    # corre 1×/día
]


def _read(name: str) -> str:
    p = WORKFLOWS / name
    assert p.exists(), f"workflow no encontrado: {p}"
    return p.read_text()


def _split_upload_blocks(text: str):
    """Devuelve cada step que use actions/upload-artifact como una lista de líneas."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if "uses: actions/upload-artifact" in lines[i]:
            # Capturar hacia atrás hasta el inicio del step (línea con "- name:" o "- uses:")
            start = i
            while start > 0 and not lines[start].lstrip().startswith("- "):
                start -= 1
            # Capturar hacia adelante hasta el próximo step o EOF
            end = i + 1
            while end < len(lines):
                stripped = lines[end].lstrip()
                if stripped.startswith("- name:") or stripped.startswith("- uses:"):
                    break
                end += 1
            blocks.append(lines[start:end])
            i = end
        else:
            i += 1
    return blocks


@pytest.mark.parametrize("workflow,max_retention", WORKFLOW_LIMITS)
def test_upload_artifact_has_continue_on_error(workflow, max_retention):
    """Cada step de upload-artifact debe tener `continue-on-error: true`.

    Si la cuota de artifacts del account se llena, el upload va a fallar y
    sin esto el job entero queda rojo y dispara email — aunque el bot haya
    ejecutado bien.
    """
    text = _read(workflow)
    blocks = _split_upload_blocks(text)
    assert blocks, f"{workflow}: no encontré steps de upload-artifact"

    for block in blocks:
        block_text = "\n".join(block)
        assert "continue-on-error: true" in block_text, (
            f"{workflow}: hay un step actions/upload-artifact SIN "
            f"`continue-on-error: true`. Un fallo de cuota va a matar el job.\n"
            f"Step:\n{block_text}"
        )


@pytest.mark.parametrize("workflow,max_retention", WORKFLOW_LIMITS)
def test_upload_artifact_retention_capped(workflow, max_retention):
    """retention-days dentro del límite por workflow.

    Workflows de alta frecuencia con retention alto saturan la cuota.
    """
    text = _read(workflow)
    blocks = _split_upload_blocks(text)
    assert blocks, f"{workflow}: no encontré steps de upload-artifact"

    for block in blocks:
        block_text = "\n".join(block)
        # Buscar `retention-days: N`
        retention = None
        for line in block:
            stripped = line.strip()
            if stripped.startswith("retention-days:"):
                retention = int(stripped.split(":", 1)[1].strip())
                break
        assert retention is not None, (
            f"{workflow}: step sin retention-days explícito.\n{block_text}"
        )
        assert retention <= max_retention, (
            f"{workflow}: retention-days={retention} excede el cap "
            f"{max_retention}. Bajalo o ajustá el límite con justificación."
        )
