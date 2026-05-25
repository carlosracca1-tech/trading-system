"""
_cooldowns — lógica compartida de cooldowns post-exit para RFTM y MREV.

F1 del plan KAIZEN: anti-whipsaw doble.

Dos chequeos por separado:

1. **Cooldown temporal**: tras un exit por stop / trailing / time (NO tras
   TPs), bloquear re-entrada por N días hábiles (RFTM) o N horas (MREV).
   Evita el patrón sell-low → rebuy-higher típico de whipsaw.

2. **Cooldown de precio**: aunque expire el temporal, si el símbolo
   hoy cotiza más de `max_runup` arriba de su `last_exit_price`,
   bloquear igual. Evita perseguir rebotes después de un stop —
   estadísticamente reversionan o nos quemamos pagando arriba.

Cada bot tiene su propia tabla porque los esquemas y el horizonte
temporal difieren (RFTM = días hábiles, MREV = horas). Las funciones
acá son **puras** y reciben la `conn` para que cada caller maneje su
DB sin acoplar este módulo.

API:

- ensure_cooldown_table(conn, table_name): crea/migra la tabla.
- record_cooldown(conn, table_name, symbol, exit_price, reason): upsert.
- check_cooldown(conn, table_name, symbol, entry_price, ...) → CooldownDecision

Diseño:

- Todas las funciones devuelven dataclasses simples para que el caller
  pueda hacer `if decision.blocked:` sin parsear strings.
- El `reason` que devuelve la decisión es human-readable y se usa
  directamente como texto del bot.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class CooldownDecision:
    """Resultado de un chequeo de cooldown.

    blocked: True si la entrada debe rechazarse.
    reason: texto corto para log/email/signal (ej. "cooldown_time:3.5d",
        "cooldown_price:+12.4%_vs_exit").
    kind: "time" | "price" | None — qué chequeo bloqueó.
    runup_pct: float | None — % de subida respecto al last_exit_price
        (sólo poblado si kind == "price").
    days_since_exit: float | None — días calendar transcurridos desde el
        último exit (siempre poblado si hay row de cooldown).
    last_exit_price: float | None — precio del exit (siempre poblado si
        hay row de cooldown).
    last_exit_reason: str | None — motivo del exit que disparó el cooldown.
    """

    blocked: bool
    reason: str = ""
    kind: Optional[str] = None
    runup_pct: Optional[float] = None
    days_since_exit: Optional[float] = None
    last_exit_price: Optional[float] = None
    last_exit_reason: Optional[str] = None


# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    symbol TEXT PRIMARY KEY,
    last_exit_dt TEXT NOT NULL,
    last_exit_price REAL,
    reason TEXT NOT NULL
)
"""


def ensure_cooldown_table(conn: sqlite3.Connection, table_name: str) -> None:
    """Crea la tabla si no existe + ALTER idempotente para `last_exit_price`.

    Idempotente: las tablas MREV viejas tenían solo (symbol, last_exit_dt,
    reason). Esta función agrega `last_exit_price` con ALTER y NO falla si
    ya existe.
    """
    if not _is_safe_table_name(table_name):
        raise ValueError(f"unsafe table name: {table_name!r}")
    conn.execute(_CREATE_SQL.format(table=table_name))
    # ALTER idempotente para schemas viejos sin last_exit_price
    try:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN last_exit_price REAL")
    except sqlite3.OperationalError:
        pass  # columna ya existía
    conn.commit()


def _is_safe_table_name(name: str) -> bool:
    """Whitelist para evitar SQL injection vía table_name."""
    return name in {"rftm_cooldowns", "mrev_cooldowns"}


# ── Write ────────────────────────────────────────────────────────────────────


def record_cooldown(
    conn: sqlite3.Connection,
    table_name: str,
    symbol: str,
    exit_price: float,
    reason: str,
    now: Optional[datetime] = None,
) -> None:
    """UPSERT del cooldown para `symbol`.

    Debe llamarse SOLO en exits por stop / trailing / time (NO en TPs).
    Es responsabilidad del caller filtrar el reason apropiado.
    """
    if not _is_safe_table_name(table_name):
        raise ValueError(f"unsafe table name: {table_name!r}")
    now = now or datetime.now(tz=timezone.utc)
    conn.execute(
        f"""INSERT OR REPLACE INTO {table_name}
            (symbol, last_exit_dt, last_exit_price, reason)
            VALUES (?, ?, ?, ?)""",
        (symbol, now.isoformat(), float(exit_price), reason),
    )
    conn.commit()


# ── Read / decide ────────────────────────────────────────────────────────────


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _business_days_between(start: datetime, end: datetime) -> float:
    """Aproximación de días hábiles entre `start` y `end`.

    No considera feriados de NYSE (overkill para esta lógica). Cuenta
    días de lun-vie a partir de `start.date()`. Si el cooldown se
    registra en sábado, el primer día hábil cuenta como el lunes
    siguiente.

    Devuelve float para preservar la fracción del último día.
    """
    if end <= start:
        return 0.0
    total_secs = (end - start).total_seconds()
    total_days = total_secs / 86400.0
    # Approach: contar fines de semana completos entre start y end y
    # restarlos. Es una aproximación buena enough para días hábiles.
    full_days = int(total_days)
    leftover = total_days - full_days
    # Contar fines de semana en los días completos
    weekends = 0
    cursor_wd = start.weekday()  # 0=Mon, 6=Sun
    for _ in range(full_days):
        cursor_wd = (cursor_wd + 1) % 7
        if cursor_wd >= 5:  # Sat/Sun
            weekends += 1
    business = max(0.0, total_days - weekends)
    # leftover ya está incluido en total_days
    return business


def check_cooldown(
    conn: sqlite3.Connection,
    table_name: str,
    symbol: str,
    entry_price: float,
    *,
    temporal_window: float,
    temporal_unit: str,
    max_runup: float,
    now: Optional[datetime] = None,
) -> CooldownDecision:
    """Decide si `symbol` está en cooldown.

    temporal_window: cuántos días/horas debe esperar como mínimo.
    temporal_unit: "business_days" | "hours" | "calendar_days".
    max_runup: fracción (ej. 0.10 = 10%). Si entry_price > exit_price *
        (1 + max_runup), bloquea por precio aunque el temporal haya
        expirado.

    Lógica:
    1. Si no hay row → blocked=False.
    2. Si temporal NO expiró → blocked=True, kind="time".
    3. Si temporal expiró pero precio supera el max_runup → blocked=True,
       kind="price".
    4. Si todo OK → blocked=False, igual devolvemos contexto para logging.
    """
    if not _is_safe_table_name(table_name):
        raise ValueError(f"unsafe table name: {table_name!r}")
    now = now or datetime.now(tz=timezone.utc)
    row = conn.execute(
        f"SELECT last_exit_dt, last_exit_price, reason FROM {table_name} WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if not row:
        return CooldownDecision(blocked=False)

    last_exit_dt_raw = row["last_exit_dt"] if isinstance(row, sqlite3.Row) else row[0]
    last_exit_price = row["last_exit_price"] if isinstance(row, sqlite3.Row) else row[1]
    last_exit_reason = row["reason"] if isinstance(row, sqlite3.Row) else row[2]
    last_exit_dt = _parse_dt(last_exit_dt_raw)
    if last_exit_dt is None:
        return CooldownDecision(blocked=False)

    # Días calendar siempre los reportamos (para post-mortem)
    days_calendar = (now - last_exit_dt).total_seconds() / 86400.0
    if days_calendar < 0:
        days_calendar = 0.0

    # ── 1. Chequeo temporal ─────────────────────────────────────────
    elapsed: float
    remaining: float
    if temporal_unit == "hours":
        elapsed = (now - last_exit_dt).total_seconds() / 3600.0
        remaining = temporal_window - elapsed
        unit_label = "h"
    elif temporal_unit == "business_days":
        elapsed = _business_days_between(last_exit_dt, now)
        remaining = temporal_window - elapsed
        unit_label = "bd"
    else:  # calendar_days
        elapsed = days_calendar
        remaining = temporal_window - elapsed
        unit_label = "d"

    if remaining > 0:
        return CooldownDecision(
            blocked=True,
            kind="time",
            reason=f"cooldown_time:{remaining:.1f}{unit_label}_left",
            days_since_exit=days_calendar,
            last_exit_price=last_exit_price,
            last_exit_reason=last_exit_reason,
        )

    # ── 2. Chequeo de precio ────────────────────────────────────────
    if last_exit_price and last_exit_price > 0 and entry_price > 0:
        runup = (entry_price - last_exit_price) / last_exit_price
        if runup > max_runup:
            return CooldownDecision(
                blocked=True,
                kind="price",
                reason=f"cooldown_price:+{runup*100:.1f}%_vs_exit",
                runup_pct=runup,
                days_since_exit=days_calendar,
                last_exit_price=last_exit_price,
                last_exit_reason=last_exit_reason,
            )

    return CooldownDecision(
        blocked=False,
        days_since_exit=days_calendar,
        last_exit_price=last_exit_price,
        last_exit_reason=last_exit_reason,
    )
