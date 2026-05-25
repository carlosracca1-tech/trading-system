"""
_bracket_orders — F3.1: safety-stop orders en Alpaca para protección
broker-side de posiciones RFTM.

Contexto: el plan KAIZEN pide "bracket orders" para que si el watchdog
se cae, Alpaca igual ejecute el stop loss. El problema: Alpaca NO soporta
multi-TP en una sola bracket order (solo 1 SL + 1 TP). Nuestra cascada
necesita TP1 50% + TP2 25% + trailing, que no se puede expresar
nativamente.

Solución: en vez de `order_class: "bracket"`, usamos un STOP order
**separado** después de cada BUY:

  1. BUY market → fill (sin cambios respecto al flujo actual).
  2. Inmediatamente después, SELL STOP por la qty TOTAL al SL price.
  3. Guardamos el `order_id` en la columna `safety_stop_order_id` de
     positions.

Ciclo de vida del safety stop:

  • Watchdog dispara TP1 (vende 50% market):
      → ANTES de vender, cancelar el safety stop (qty entera era 100,
        el stop pisaría las 100 si dispara entre la cancelación y la
        venta — pero la ventana son ms).
      → Vender parcial (50).
      → Submit nuevo safety stop por la qty restante (50) al breakeven.
  • Watchdog dispara full exit (E3/E5/E6):
      → Cancelar el safety stop.
      → Vender market.
      → close position.
  • Bot crashea: el safety stop sigue vivo en Alpaca. Si el precio toca
    el SL, Alpaca ejecuta y nos protege.

INVARIANTE: en cualquier momento, si la posición está abierta, debe
haber UN safety stop activo en Alpaca con qty == qty local. La única
ventana sin stop son los pocos ms entre cancelación y submit del nuevo.

Feature flag: `RFTM_BRACKET_ORDERS_ENABLED=0` por default. Cuando se
activa, el bot empieza a enviar safety stops sin tocar nada más del
flujo (los stops software-side del watchdog siguen activos como
"belt + suspenders").

Diseño:
- Funciones puras donde es posible.
- Las que tocan Alpaca reciben `request_fn` y `submit_fn` para que los
  tests las mockeen sin tocar la red.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


def bracket_orders_enabled() -> bool:
    """True si el feature flag está activo. Default OFF para activación
    gradual en paper antes de live."""
    return os.environ.get("RFTM_BRACKET_ORDERS_ENABLED", "0").lower() in (
        "1", "true", "yes",
    )


@dataclass(frozen=True)
class SafetyStopRequest:
    """Parámetros del SELL STOP que se envía a Alpaca."""

    symbol: str
    qty: int
    stop_price: float
    time_in_force: str = "gtc"  # good-till-cancelled — sobrevive cierre de mercado


@dataclass(frozen=True)
class SafetyStopResult:
    """Resultado de una operación con Alpaca."""

    ok: bool
    order_id: Optional[str] = None
    error: Optional[str] = None


# ── Cálculo (puro) ───────────────────────────────────────────────────────────


def calc_safety_stop_price(
    *,
    entry_price: float,
    stage: int,
    atr: Optional[float],
    atr_mult: float = 1.5,
    fallback_pct: float = 0.05,
    current_stop: Optional[float] = None,
) -> float:
    """Wrapper alrededor de `_exit_logic.recalc_stop_for_stage`.

    Mantiene el invariante "stop solo sube" — el safety stop nuevo
    nunca puede ser más bajo que el current.
    """
    from _exit_logic import recalc_stop_for_stage

    return recalc_stop_for_stage(
        entry_price=entry_price,
        stage=stage,
        atr=atr,
        current_stop=current_stop,
        atr_mult=atr_mult,
        fallback_pct=fallback_pct,
    )


# ── Submit / cancel ──────────────────────────────────────────────────────────


def submit_safety_stop(
    req: SafetyStopRequest,
    *,
    submit_fn: Callable[..., Optional[dict]],
) -> SafetyStopResult:
    """Envía a Alpaca un SELL STOP order.

    `submit_fn(method, path, body)` debe llamar a la API; ese es el mismo
    `_alpaca_request` que ya usa el bot. Lo recibimos como parámetro para
    que los tests puedan reemplazarlo sin tocar la red.
    """
    body = {
        "symbol": req.symbol,
        "qty": str(req.qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(req.stop_price, 2)),
        "time_in_force": req.time_in_force,
    }
    res = submit_fn("POST", "/orders", body)
    if not isinstance(res, dict):
        return SafetyStopResult(ok=False, error="alpaca submit returned non-dict / None")
    oid = res.get("id")
    if not oid:
        return SafetyStopResult(ok=False, error=f"no id in response: {res}")
    return SafetyStopResult(ok=True, order_id=str(oid))


def cancel_safety_stop(
    order_id: Optional[str],
    *,
    submit_fn: Callable[..., Optional[dict]],
) -> SafetyStopResult:
    """Cancela un safety stop. Si `order_id` es None/empty, no-op exitoso.

    Si Alpaca dice 422/404 (la orden ya no existe), también devuelve OK —
    el resultado deseado (que esa orden no esté activa) se cumple.
    """
    if not order_id:
        return SafetyStopResult(ok=True)
    res = submit_fn("DELETE", f"/orders/{order_id}", None)
    # Alpaca devuelve None si fue 404/422 — interpretamos como "ya cancelada"
    if res is None:
        return SafetyStopResult(ok=True, error="already_gone")
    return SafetyStopResult(ok=True, order_id=order_id)


def replace_safety_stop(
    old_order_id: Optional[str],
    new_req: SafetyStopRequest,
    *,
    submit_fn: Callable[..., Optional[dict]],
) -> SafetyStopResult:
    """Cancel + Submit. Devuelve el resultado del submit.

    Hay una ventana de ~milisegundos sin stop activo entre cancel y
    submit. El watchdog corre cada 90s, así que la probabilidad de un
    gap adverso en esa ventana es bajísima — pero notar el riesgo.
    """
    cancel_safety_stop(old_order_id, submit_fn=submit_fn)
    return submit_safety_stop(new_req, submit_fn=submit_fn)
