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

    # Cascada de stops (nuevo, fixed 2026-05-21):
    #   TP1 → stop a breakeven (entry).
    #   TP2 → stop a entry × (1 + tp1_pct), o sea el nivel de TP1.
    #          Así, después de TP2 el peor exit del remanente es +5% lock.
    if target_stage == 1:
        new_stop = float(entry_price)
    else:  # target_stage == 2
        new_stop = float(entry_price) * (1.0 + float(tp1_pct))

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


# ── highest_since_entry recovery helpers ─────────────────────────────────────

def stage_implied_high_floor(
    *,
    entry_price: float,
    stage: int,
    tp1_pct: float = 0.05,
    tp2_pct: float = 0.075,
) -> float:
    """Devuelve el `highest_since_entry` mínimo que el TP del stage implica.

    Si `partial_tp_taken >= 1`, sabemos que el precio cruzó al menos
    `entry × (1+tp1_pct)` para que el TP1 dispare. Si `>= 2`, lo mismo
    pero con `tp2_pct`. Esto se usa al re-insertar posiciones desde
    Alpaca (`sync_with_alpaca`, `seed_missing_positions`, manual scripts)
    para evitar resetear `highest_since_entry = entry_price`, que
    rompe el trailing stop.

    stage 0 → entry_price (no se garantiza nada por encima).
    stage 1 → entry_price × (1 + tp1_pct).
    stage ≥2 → entry_price × (1 + tp2_pct).

    Devuelve siempre un float positivo si entry > 0.
    """
    if entry_price <= 0:
        return 0.0
    if stage >= 2:
        return float(entry_price) * (1.0 + float(tp2_pct))
    if stage == 1:
        return float(entry_price) * (1.0 + float(tp1_pct))
    return float(entry_price)


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


# ── F3.3: Stop-loss reconciliation por stage ─────────────────────────────────


def recalc_stop_for_stage(
    *,
    entry_price: float,
    stage: int,
    atr: Optional[float],
    current_stop: Optional[float],
    atr_mult: float = 1.5,
    fallback_pct: float = 0.05,
    tp1_pct: float = 0.05,
) -> float:
    """Devuelve el stop_loss correcto para una posición según su stage.

    Esquema NUEVO (fix 2026-05-21) — stops 100% fixed %, sin ATR:

    | stage | regla |
    |-------|-------|
    |   0   | `entry × (1 − fallback_pct)`  →  default `entry × 0.95` (−5%) |
    |   1   | `entry` (breakeven — el watchdog ya subió el stop al TP1) |
    |  >=2  | `entry × (1 + tp1_pct)`  →  default `entry × 1.05` (lock TP1) |

    El usuario pidió eliminar la lógica basada en ATR (trailing, breakeven
    al +0.5×ATR, etc.) que generaba micro-pérdidas en mercados ruidosos.
    Los params `atr` y `atr_mult` quedan en la firma por compat pero ya
    no se usan.

    **Invariante crítico**: stop solo SUBE, nunca baja:
        `new_stop = max(current_stop, calculated_stop)`

    Args:
        entry_price: precio de entrada (de Alpaca = verdad operativa).
        stage: 0/1/2 — partial_tp_taken.
        atr: IGNORADO (compat). Antes calculaba stop=entry-mult×atr.
        current_stop: stop actual de la DB (puede ser None/0/negativo).
        atr_mult: IGNORADO (compat).
        fallback_pct: % a restar del entry en stage 0 (default 0.05 = −5%).
        tp1_pct: % de TP1 para calcular el stop en stage 2 (default 0.05).

    Returns:
        Stop nuevo, garantizado ≥ current_stop.
    """
    cur = float(current_stop) if current_stop and current_stop > 0 else 0.0

    if stage >= 2:
        # Post-TP2: stop sube al nivel de TP1 (entry × 1.05 por default).
        new = float(entry_price) * (1.0 + float(tp1_pct))
    elif stage == 1:
        # Post-TP1: stop = breakeven.
        new = float(entry_price)
    else:  # stage == 0
        # Stop fijo a −fallback_pct desde el entry.
        new = float(entry_price) * (1.0 - float(fallback_pct))

    # Invariante: solo sube.
    return max(cur, new) if cur > 0 else new
