"""
_shadow_trades — F6.1: simula trades que KAIZEN bloqueó.

Cuando una regla rechaza una entrada, creamos un "shadow trade":
- entry_price = precio en el momento del bloqueo
- stop, tp1, tp2 = los mismos cálculos que el bot real haría
- qty_simulated = sizing real del momento

Diariamente (cron) simulamos la cascada completa contra precios reales
de Alpaca:
- TP1 (+5%) → vende 50%, sube stop a breakeven
- TP2 (+7.5%) → vende otro 25%
- Stop / trailing / time stop (20 días max) → cierra remanente
- Slippage simulado: 0.05% in/out (no inflar ahorros artificiales)

Al cerrar, calculamos `pnl_simulated`. Si negativo → la regla nos
ahorró plata. Si positivo → costo de oportunidad.

KAIZEN consume esto mensualmente para decir cuáles reglas valen y
cuáles desactivar.

Schema de la tabla `kaizen_shadow_trades` (sqlite):
- id TEXT PRIMARY KEY
- rule_id TEXT       — qué regla la bloqueó
- bot TEXT           — RFTM/MREV
- symbol TEXT
- entry_dt_utc TEXT
- entry_price REAL
- qty_simulated REAL
- stop_loss REAL
- tp1_price REAL
- tp2_price REAL
- atr_at_entry REAL  — para trailing dinámico si aplica
- partial_tp_stage INTEGER DEFAULT 0
- highest_since_entry REAL
- exit_dt_utc TEXT
- exit_price REAL
- exit_reason TEXT
- pnl_simulated REAL
- status TEXT        — "running" | "closed"
- created_at TEXT

Las funciones acá son puras o reciben las dependencies por param.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# Slippage simulado para no inflar artificialmente los ahorros
SLIPPAGE_PCT = 0.0005  # 5 bps


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS kaizen_shadow_trades (
    id TEXT PRIMARY KEY,
    rule_id TEXT,
    bot TEXT,
    symbol TEXT,
    entry_dt_utc TEXT,
    entry_price REAL,
    qty_simulated REAL,
    stop_loss REAL,
    tp1_price REAL,
    tp2_price REAL,
    atr_at_entry REAL,
    partial_tp_stage INTEGER DEFAULT 0,
    highest_since_entry REAL,
    exit_dt_utc TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_simulated REAL,
    status TEXT DEFAULT 'running',
    created_at TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SQL)
    conn.commit()


# ── Create ────────────────────────────────────────────────────────────────────


@dataclass
class ShadowTradeParams:
    bot: str
    symbol: str
    rule_id: str
    entry_price: float
    qty_simulated: float
    stop_loss: float
    tp1_pct: float = 0.05
    tp2_pct: float = 0.075
    atr_at_entry: Optional[float] = None


def create_shadow_trade(conn: sqlite3.Connection, p: ShadowTradeParams) -> str:
    """Inserta un shadow trade `running`. Devuelve su id."""
    ensure_table(conn)
    sid = str(uuid.uuid4())[:12]
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    tp1_price = p.entry_price * (1.0 + p.tp1_pct)
    tp2_price = p.entry_price * (1.0 + p.tp2_pct)
    conn.execute(
        """INSERT INTO kaizen_shadow_trades
           (id, rule_id, bot, symbol, entry_dt_utc, entry_price,
            qty_simulated, stop_loss, tp1_price, tp2_price,
            atr_at_entry, partial_tp_stage, highest_since_entry,
            status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (sid, p.rule_id, p.bot.upper(), p.symbol, now,
         p.entry_price, p.qty_simulated, p.stop_loss,
         tp1_price, tp2_price, p.atr_at_entry,
         p.entry_price,  # highest = entry inicialmente
         "running", now),
    )
    conn.commit()
    return sid


# ── Tick (simulación) ────────────────────────────────────────────────────────


@dataclass
class TickResult:
    """Lo que pasó con un shadow en este tick."""
    sid: str
    closed: bool = False
    exit_reason: Optional[str] = None
    new_stage: Optional[int] = None
    pnl: Optional[float] = None


def _apply_slippage_sell(price: float) -> float:
    """Vendedor: peor precio = menos plata."""
    return price * (1.0 - SLIPPAGE_PCT)


def tick_shadow_trade(
    row: dict,
    *,
    current_price: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    now_iso: Optional[str] = None,
    time_stop_days: int = 20,
    trail_atr_mult: float = 2.0,
) -> TickResult:
    """Evalúa qué le pasa al shadow trade en un tick.

    NO toca la DB. El caller pasa la `row` (resultado de SELECT) y
    decide qué UPDATE hacer según el TickResult.

    Lógica de la cascada (orden de prioridad):
    1. Si current_price ≤ stop_loss en cualquier momento (usamos `low`
       si está disponible para detectar gaps adversos) → CLOSE stop.
    2. Si current_price ≥ tp2_price y stage < 2 → ejecutar TP2 (25% del
       initial). Stage → 2, highest se actualiza, NO hay close todavía.
    3. Si current_price ≥ tp1_price y stage == 0 → ejecutar TP1 (50%
       del initial). Stage → 1, stop sube a entry_price (breakeven).
    4. Si stage >= 2: trailing stop = highest - trail_atr_mult * atr.
       Si current ≤ trail_stop → CLOSE trailing.
    5. Time stop: si han pasado >= time_stop_days desde entry → CLOSE.

    El sell siempre asume slippage del SLIPPAGE_PCT.
    """
    sid = row["id"]
    now_iso = now_iso or datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    entry_price = float(row["entry_price"])
    qty_init = float(row["qty_simulated"])
    stop = float(row["stop_loss"])
    tp1 = float(row["tp1_price"])
    tp2 = float(row["tp2_price"])
    stage = int(row["partial_tp_stage"] or 0)
    highest = float(row["highest_since_entry"] or entry_price)

    # Para gaps adversos, usamos `low` si lo pasaron — sino current_price.
    bar_low = low if low is not None else current_price
    bar_high = high if high is not None else current_price

    # ── 1. Stop hit (priority)
    if bar_low <= stop:
        # Si el bar low ≤ stop, asumimos que se ejecutó al stop con slippage
        exit_px = _apply_slippage_sell(min(stop, current_price))
        # qty remanente depende del stage actual
        if stage == 0:
            qty_remaining = qty_init
        elif stage == 1:
            qty_remaining = qty_init * 0.50
        else:  # stage 2
            qty_remaining = qty_init * 0.25
        # PnL acumulado de los TPs previos + este sell
        pnl = _accumulated_pnl_at_stage(entry_price, qty_init, stage, tp1, tp2)
        pnl += (exit_px - entry_price) * qty_remaining
        return TickResult(sid=sid, closed=True, exit_reason="stop_loss", pnl=pnl)

    # ── 2. TP2 (stage 1→2)
    if stage == 1 and bar_high >= tp2:
        # Mueve a stage 2 (no cierra)
        return TickResult(sid=sid, new_stage=2)

    # ── 3. TP1 (stage 0→1)
    if stage == 0 and bar_high >= tp1:
        return TickResult(sid=sid, new_stage=1)

    # ── 4. Trailing en stage 2
    if stage >= 2:
        atr = float(row["atr_at_entry"] or 0)
        if atr > 0:
            trail_stop = highest - trail_atr_mult * atr
            # Stop solo sube — pero acá calculamos el stop dinámico vs
            # current. El stop real persiste como max(stop_loss, trail_stop).
            effective_stop = max(stop, trail_stop)
            if bar_low <= effective_stop:
                exit_px = _apply_slippage_sell(min(effective_stop, current_price))
                pnl = _accumulated_pnl_at_stage(entry_price, qty_init, 2, tp1, tp2)
                pnl += (exit_px - entry_price) * (qty_init * 0.25)
                return TickResult(sid=sid, closed=True,
                                  exit_reason="trailing_stop", pnl=pnl)

    # ── 5. Time stop
    try:
        entry_dt = datetime.fromisoformat(row["entry_dt_utc"])
        now_dt = datetime.fromisoformat(now_iso)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        if (now_dt - entry_dt).days >= time_stop_days:
            exit_px = _apply_slippage_sell(current_price)
            qty_remaining = qty_init * (
                1.0 if stage == 0 else (0.50 if stage == 1 else 0.25)
            )
            pnl = _accumulated_pnl_at_stage(entry_price, qty_init, stage, tp1, tp2)
            pnl += (exit_px - entry_price) * qty_remaining
            return TickResult(sid=sid, closed=True,
                              exit_reason="time_stop", pnl=pnl)
    except (ValueError, TypeError, KeyError):
        pass

    # No cambios
    return TickResult(sid=sid)


def _accumulated_pnl_at_stage(
    entry_price: float, qty_init: float, stage: int,
    tp1_price: float, tp2_price: float,
) -> float:
    """PnL ya realizado por los TPs previos al stage actual.

    Stage 0: 0 (sin TPs).
    Stage 1: 50% vendido a tp1 con slippage.
    Stage 2: 50% a tp1 + 25% a tp2.
    """
    pnl = 0.0
    if stage >= 1:
        sold = qty_init * 0.50
        pnl += (_apply_slippage_sell(tp1_price) - entry_price) * sold
    if stage >= 2:
        sold = qty_init * 0.25
        pnl += (_apply_slippage_sell(tp2_price) - entry_price) * sold
    return pnl


# ── Apply tick to DB ────────────────────────────────────────────────────────


def apply_tick_to_db(
    conn: sqlite3.Connection, row: dict, result: TickResult, *,
    current_price: float, now_iso: Optional[str] = None,
) -> None:
    """Aplica el TickResult al row de la DB."""
    now_iso = now_iso or datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    if result.closed:
        conn.execute(
            """UPDATE kaizen_shadow_trades
               SET status='closed', exit_dt_utc=?, exit_price=?,
                   exit_reason=?, pnl_simulated=?
               WHERE id=?""",
            (now_iso, current_price, result.exit_reason,
             round(result.pnl or 0, 4), row["id"]),
        )
    elif result.new_stage is not None:
        # Si pasó a stage 1, stop sube a breakeven
        entry = float(row["entry_price"])
        new_stop = float(row["stop_loss"])
        if result.new_stage == 1:
            new_stop = max(new_stop, entry)
        # Actualizar highest también si current > anterior
        highest = max(float(row["highest_since_entry"] or entry), current_price)
        conn.execute(
            """UPDATE kaizen_shadow_trades
               SET partial_tp_stage=?, stop_loss=?, highest_since_entry=?
               WHERE id=?""",
            (result.new_stage, new_stop, highest, row["id"]),
        )
    else:
        # No state change pero actualizamos highest si subió
        highest = max(float(row["highest_since_entry"] or 0), current_price)
        if highest > float(row["highest_since_entry"] or 0):
            conn.execute(
                "UPDATE kaizen_shadow_trades SET highest_since_entry=? WHERE id=?",
                (highest, row["id"]),
            )
    conn.commit()


# ── Aggregation ─────────────────────────────────────────────────────────────


def aggregate_by_rule(conn: sqlite3.Connection) -> list[dict]:
    """Métricas por rule_id sobre shadows CLOSED.

    Devuelve lista de dicts con:
    - rule_id
    - n_shadows_closed, n_winners, n_losers
    - gross_saved_usd (- pnl de losers)
    - gross_missed_usd (+ pnl de winners)
    - net_impact_usd = gross_saved - gross_missed
    - win_rate (si rule fuera mala = % donde fantasma ganó)
    - expectancy = avg pnl simulado por shadow
    """
    ensure_table(conn)
    rows = conn.execute(
        """SELECT rule_id, pnl_simulated FROM kaizen_shadow_trades
           WHERE status='closed' AND pnl_simulated IS NOT NULL"""
    ).fetchall()
    by_rule: dict = {}
    for r in rows:
        rid = r["rule_id"]
        d = by_rule.setdefault(rid, {
            "rule_id": rid,
            "n_shadows_closed": 0,
            "n_winners": 0,
            "n_losers": 0,
            "gross_saved_usd": 0.0,
            "gross_missed_usd": 0.0,
        })
        pnl = float(r["pnl_simulated"])
        d["n_shadows_closed"] += 1
        if pnl > 0:
            d["n_winners"] += 1
            d["gross_missed_usd"] += pnl
        elif pnl < 0:
            d["n_losers"] += 1
            d["gross_saved_usd"] += -pnl  # pnl negativo es ahorro

    out = []
    for d in by_rule.values():
        n = d["n_shadows_closed"]
        d["win_rate"] = (d["n_winners"] / n) if n else 0
        d["net_impact_usd"] = round(
            d["gross_saved_usd"] - d["gross_missed_usd"], 2
        )
        d["expectancy_usd"] = round(d["net_impact_usd"] / n, 2) if n else 0
        d["gross_saved_usd"] = round(d["gross_saved_usd"], 2)
        d["gross_missed_usd"] = round(d["gross_missed_usd"], 2)
        out.append(d)
    return out
