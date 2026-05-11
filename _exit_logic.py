"""
_exit_logic — Funciones puras de evaluación de exits y partial-TPs.

Contexto: los watchdogs (rftm_watchdog.py, mrev_watchdog.py) necesitan
evaluar TPs/stops sin duplicar la lógica de los bots entry. Este módulo
expone la lógica compartida como funciones puras (sin I/O, sin estado
global).

Los bots entry (standalone_*.py) siguen con su lógica inline tal cual
estaba — este módulo NO reemplaza nada: es aditivo. Los tests prueban
que `evaluate_partial_tp()` matchea la decisión que toma el bot inline,
símbolo a símbolo.

Convenciones:
- `stage` == partial_tp_taken: 0 (nada), 1 (TP1 hit), 2 (TP2 hit).
- TP1 sube el stop a breakeven (entry_price). TP2 no cambia el stop.
- Si el sell_qty resultante es 0, igual a 0 o >= current_qty, no firea
  (evita "venta de 100%" accidental).
- Si el notional (sell_qty * close) < min_notional, tampoco firea.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class PartialTPAction:
    """Describe una acción de partial TP a ejecutar."""
    stage: int          # nuevo stage tras ejecutar (1 o 2)
    sell_qty: float     # cantidad a vender
    new_stop: Optional[float]  # nuevo stop_loss (sube a entry en TP1; None en TP2)
    reason: str         # label para logs / exit_reason
    trigger_pct: float  # el threshold que disparó (tp1_pct o tp2_pct)


@dataclass(frozen=True)
class ExitAction:
    """Describe un exit (stop/trailing/time/take-profit final)."""
    sell_qty: float
    reason: str


def evaluate_partial_tp(
    *,
    stage: int,
    entry_price: float,
    current_price: float,
    current_qty: float,
    tp1_pct: float,
    tp2_pct: float,
    tp1_ratio: float,
    tp2_ratio: float,
    min_notional: float,
    round_qty: Callable[[float], float],
) -> Optional[PartialTPAction]:
    """
    Evalúa si se debe ejecutar un partial TP en la posición descripta.

    Parámetros:
    - stage: partial_tp_taken actual (0/1/2).
    - entry_price / current_price: precios.
    - current_qty: cantidad actual (ya descontada del TP1 si stage==1).
    - tp1_pct / tp2_pct: umbrales de unrealized PnL (por ej. 0.05 y 0.075).
    - tp1_ratio / tp2_ratio: fracción a vender (por ej. 0.50 y 0.50).
    - min_notional: piso en USD; si la venta sería inferior no firea.
    - round_qty: callback que redondea qty al tick mínimo del activo
      (floor para ETFs, crypto_min_qty para cripto).

    Devuelve PartialTPAction o None si no aplica.
    """
    if stage not in (0, 1):
        return None
    if entry_price <= 0 or current_qty <= 0 or current_price <= 0:
        return None

    unrealized_pct = (current_price - entry_price) / entry_price

    if stage == 0 and unrealized_pct >= tp1_pct:
        trigger_pct, ratio, target_stage = tp1_pct, tp1_ratio, 1
    elif stage == 1 and unrealized_pct >= tp2_pct:
        trigger_pct, ratio, target_stage = tp2_pct, tp2_ratio, 2
    else:
        return None

    sell_qty = round_qty(current_qty * ratio)
    # Guardas: no vender 0, no vender el total, no vender bajo el mínimo
    if sell_qty <= 0 or sell_qty >= current_qty:
        return None
    if sell_qty * current_price < min_notional:
        return None

    # TP1 sube el stop a breakeven. TP2 deja el stop como está.
    new_stop = entry_price if target_stage == 1 else None

    return PartialTPAction(
        stage=target_stage,
        sell_qty=sell_qty,
        new_stop=new_stop,
        reason=f"partial_tp{target_stage}_{trigger_pct*100:.1f}pct:{unrealized_pct:.2%}",
        trigger_pct=trigger_pct,
    )


def evaluate_final_tp(
    *,
    entry_price: float,
    current_price: float,
    current_qty: float,
    final_tp_pct: float,
    min_notional: float,
) -> Optional[ExitAction]:
    """
    Hard final take-profit: si current_price >= entry_price × (1 + final_tp_pct),
    vende TODO lo que queda. Independiente del stage (0, 1 o 2) — pensado para
    cortar runners que ya están en super-profit en vez de dejarlos andar con
    trailing stop.

    - Si final_tp_pct <= 0 → desactivado (devuelve None).
    - Si entry/qty/price <= 0 → no aplica.
    - Si current_qty * current_price < min_notional → no firea (orden chica).
    - Si unrealized_pct < final_tp_pct → no firea.

    Devuelve ExitAction con sell_qty == current_qty y reason="final_tp_XX.Xpct:..."
    o None si no aplica.
    """
    if final_tp_pct <= 0:
        return None
    if entry_price <= 0 or current_qty <= 0 or current_price <= 0:
        return None

    unrealized_pct = (current_price - entry_price) / entry_price
    if unrealized_pct < final_tp_pct:
        return None

    if current_qty * current_price < min_notional:
        return None

    return ExitAction(
        sell_qty=current_qty,
        reason=f"final_tp_{final_tp_pct*100:.1f}pct:{unrealized_pct:.2%}",
    )


def floor_int_qty(q: float) -> float:
    """Redondeo típico para ETFs — enteras. No emite acciones fraccionales."""
    import math
    return float(math.floor(q))


def make_crypto_round_qty(min_qty: float) -> Callable[[float], float]:
    """Devuelve un round_qty para cripto con el min tick especificado."""
    import math

    precision_s = str(min_qty).rstrip("0")
    if "." in precision_s:
        precision = len(precision_s.split(".")[-1])
    else:
        precision = 0

    def _r(q: float) -> float:
        if min_qty <= 0:
            return 0.0
        return round(math.floor(q / min_qty) * min_qty, precision)

    return _r
