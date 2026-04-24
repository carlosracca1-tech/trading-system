"""
Fix B regression test: si la persistencia falla tras submitear la buy,
el bot debe cancelar la order y abortar (no tragar la excepción y seguir).
"""
from __future__ import annotations

from pathlib import Path


SRC = Path(__file__).resolve().parents[2] / "standalone_mrev_trader.py"


def test_buy_loop_catches_sqlite_error_and_aborts():
    src = SRC.read_text()
    anchor = src.index("for b in buys:")
    buy_block = src[anchor : anchor + 6000]

    # 1. Debe atrapar sqlite3.Error (no un `except Exception` genérico sobre la INSERT)
    assert "except sqlite3.Error" in buy_block, (
        "Fix B regresó: la INSERT del buy no está protegida con except sqlite3.Error"
    )

    # 2. Debe intentar cancelar la order
    assert "alpaca_cancel_order" in buy_block, (
        "Fix B regresó: no se cancela la order al fallar la persistencia"
    )

    # 3. Debe abortar el run
    assert "SystemExit(2)" in buy_block, (
        "Fix B regresó: la persistencia rota no aborta el run"
    )


def test_alpaca_cancel_order_exists():
    src = SRC.read_text()
    assert "def alpaca_cancel_order" in src, (
        "Falta helper alpaca_cancel_order — requerido por el rollback de Fix B"
    )
