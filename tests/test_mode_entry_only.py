"""
Tests para MODE=entry_only en los bots.

Verifica que cuando MODE=entry_only está presente, el bloque que decide
sells/partials en el pipeline se saltea. No ejecutamos el pipeline completo
(requiere Alpaca mock), solo chequeamos la presencia del guard en el código
— es un test estructural para bloquear regresiones del split entry/watchdog.
"""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_rftm_has_entry_only_guard():
    src = (REPO / "standalone_paper_trader.py").read_text()
    # Tiene que haber un check MODE==entry_only alrededor de la rama de exits
    assert 'os.environ.get("MODE", "full") == "entry_only"' in src, (
        "standalone_paper_trader.py no tiene el guard de MODE=entry_only"
    )
    # Y el guard tiene que estar dentro del scope del pos (si hay una posición abierta)
    lines = src.splitlines()
    idx_guards = [i for i, l in enumerate(lines) if 'MODE", "full") == "entry_only' in l]
    assert idx_guards, "guard line not found"
    # El guard debe estar después de `if pos:` y antes de signals_exit.append
    # Búsqueda barata: que haya un `signals_hold.append(symbol)` dentro de las 5 líneas post-guard
    i = idx_guards[0]
    nearby = "\n".join(lines[i:i + 6])
    assert "signals_hold.append(symbol)" in nearby
    assert "continue" in nearby


def test_mrev_has_entry_only_guard():
    src = (REPO / "standalone_mrev_trader.py").read_text()
    assert 'os.environ.get("MODE", "full") == "entry_only"' in src, (
        "standalone_mrev_trader.py no tiene el guard de MODE=entry_only"
    )
    # Debe estar justo tras detectar `if sym in open_symbols:`
    anchor = src.index("if sym in open_symbols:")
    tail = src[anchor : anchor + 500]
    assert 'MODE", "full") == "entry_only' in tail, (
        "el guard en MREV está fuera del scope de la rama de exits"
    )


def test_workflows_pass_mode_entry_only():
    daily = (REPO / ".github/workflows/daily_trade.yml").read_text()
    hourly = (REPO / ".github/workflows/mrev_hourly.yml").read_text()
    assert "MODE: entry_only" in daily, "daily_trade.yml no pasa MODE=entry_only"
    assert "MODE: entry_only" in hourly, "mrev_hourly.yml no pasa MODE=entry_only"
