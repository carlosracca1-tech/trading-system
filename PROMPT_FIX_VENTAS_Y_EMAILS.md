# MEGAPROMPT — Fix de ventas + emails con trade cards progresivos

**Ámbito:** sistema RFTM + MREV de paper trading sobre cuenta Alpaca compartida.
**Estado actual:** posiciones que pasaron TPs sin venderse, mail diario RFTM no llega.
**Meta:** dejar el sistema vendiendo siempre que toque + emails con tarjetas por trade que se van llenando con cada hito (TP1 ✓ → TP2 ✓ → cierre ✓) + balance general acumulado por día/mes.

> **Cómo usar este prompt:** copialo entero a Claude Code (o pasala como contexto a otra sesión Cowork) y pedí "ejecutá este plan en commits chicos, uno por sección, abriendo PRs separados". Cada sección está pensada para ser un PR independiente y revisable.

---

## 0. Contexto que tenés que cargar antes de tocar nada

1. Leer **`CLAUDE.md`** entero. Notas de arquitectura, env vars, rituales.
2. Leer estos archivos para entender el contrato actual antes de modificar:
   - `standalone_paper_trader.py` (RFTM bot entry — daily)
   - `standalone_mrev_trader.py` (MREV bot entry — hourly)
   - `rftm_watchdog.py` y `mrev_watchdog.py` (los que ejecutan exits)
   - `_email_helpers.py` (SMTP + plantillas compartidas)
   - `_exit_logic.py` (función pura `evaluate_partial_tp` que consume el watchdog)
   - `_db_health.py` (chequeos de schema al arranque)
   - `.github/workflows/daily_trade.yml`, `mrev_hourly.yml`, `rftm_watchdog.yml`, `mrev_watchdog.yml`
3. Confirmar que tests pasan en verde antes de empezar:
   ```bash
   python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
       _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py
   python3 -m pytest tests/test_indicators.py tests/test_strategy.py \
       tests/test_health.py tests/test_mrev tests/test_watchdog \
       tests/test_exit_logic.py tests/test_db_health.py tests/test_db_schema.py \
       tests/test_universes_disjoint.py tests/test_mode_entry_only.py
   python3 scripts/ops/preflight.py
   ```
4. **Reglas innegociables (de `CLAUDE.md`):**
   - No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
   - No tocar `check_entry`, `check_exit`, `_calc_take_profit`, `size_position` sin preguntar.
   - `.env.paper` nunca se imprime ni se commitea.
   - Cambios de schema DB sólo con `ALTER TABLE … ADD COLUMN` o `CREATE TABLE IF NOT EXISTS`, idempotentes.
   - Env vars con default hardcodeado, backward-compat preservada.
   - Errores de Alpaca son `warn`, no abortan el run (excepto donde el `_db_health` ya aborta).

---

## 1. Diagnóstico — los dos bugs que explican todo

### Bug A — Watchdog corre en DRY_RUN cuando lo dispara el cron

**Archivo:** `.github/workflows/rftm_watchdog.yml` línea ~62 y `.github/workflows/mrev_watchdog.yml` línea ~58.

**Línea actual:**
```yaml
DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}
```

Cuando lo dispara `schedule:`, `github.event.inputs.dry_run` es vacío → fallback a `'true'` → el watchdog evalúa exits pero `_execute_sell()` corta en el `if DRY_RUN: return {"status": "filled_dry"}` (rftm_watchdog.py línea 128). **Nunca manda la orden real.** Por eso hay posiciones muy por encima del TP sin venderse.

Vigente desde el commit `22fa0d2` (23-abr) que prendió los schedules.

### Bug B — Bot diario RFTM no carga `.env.paper`

**Archivo:** `standalone_paper_trader.py` líneas 218-220.

```python
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
```

El workflow `daily_trade.yml` escribe esos secrets a `.env.paper` pero el step `Run RFTM Bot` solo pasa `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `MODE` como `env:`. RFTM no tiene `_load_env()` (MREV sí, líneas 53-65 de `standalone_mrev_trader.py`). Resultado: las EMAIL_* llegan vacías al proceso → `send_email_report()` ejecuta:

```python
if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
    warn("Email not configured …")
    return
```

…y sale callado. Por eso el mail diario no llega aunque el bot corra OK.

### Bug C — XLE con `qty=438` e `initial_qty=136` (datos inconsistentes)

DB tiene una posición con `qty > initial_qty`, lo cual rompe los cálculos de % vendido y la lógica de stage. Hay que reconciliar contra Alpaca antes de seguir.

---

## 2. FASE 0 — Apagar el sangrado (HOY, ~30 min)

Todos commits chicos, uno por bullet. Branch sugerida: `fix/sales-and-emails-p0`.

### 2.1 Watchdogs salen de DRY_RUN por default

**Cambio:**
- `.github/workflows/rftm_watchdog.yml`: línea con `DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}` → cambiar `'true'` por `'false'`.
- `.github/workflows/mrev_watchdog.yml`: idem.
- `workflow_dispatch.inputs.dry_run.default` se queda en `'true'` para que los runs manuales sean seguros por default.

**Mensaje commit:** `fix(watchdogs): cron triggers default DRY_RUN=false (kept manual default true)`

**Verificación:**
- En GitHub: disparar `workflow_dispatch` del watchdog RFTM con `dry_run=false`, mirar el log y Alpaca → debería mandar las órdenes que estaban atrasadas.
- Confirmar con `ALPACA_API_KEY` real que las posiciones se cerraron / partial-cerraron.

### 2.2 RFTM carga `.env.paper`

**Cambio:** copiar la función `_load_env()` de `standalone_mrev_trader.py` (líneas 53-65) a `standalone_paper_trader.py`, ubicarla **antes** de la lectura de las EMAIL_* / ALPACA_* env vars (alrededor de la línea 60, antes de `DB_PATH = …`). Llamarla con `_load_env()` inmediatamente.

```python
# standalone_paper_trader.py — agregar después de los `import os, sys, ...`
def _load_env():
    """Lee .env.paper si existe y mete las vars en os.environ (igual que MREV)."""
    from pathlib import Path
    here = Path(__file__).parent
    for name in (".env.paper", ".env"):
        p = here / name
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

_load_env()
```

**Verificación:**
- Local: `EMAIL_FROM= python3 -c "import standalone_paper_trader as r; print(r.EMAIL_FROM)"` debería imprimir lo que está en `.env.paper`.
- GitHub: workflow_dispatch del daily, ver en logs `Email report sent to …` antes del exit.

**Mensaje commit:** `fix(rftm): load .env.paper at startup (mirrors MREV behavior)`

### 2.3 Reconciliar XLE

Crear `scripts/ops/reconcile_position.py SYMBOL [--apply]`:

- Sin `--apply`: imprime side-by-side `db_qty / db_initial_qty / db_entry / db_stop / db_stage` vs `alpaca_qty / alpaca_avg_entry / alpaca_current_price`.
- Con `--apply`: si `alpaca_qty > 0` → updatea `positions.qty = alpaca_qty`, `entry_price = avg_entry_price`, mantiene `stage` actual pero ajusta `initial_qty = max(qty, initial_qty)`. Si `alpaca_qty == 0` → cierra la fila local con `close_reason='reconcile_alpaca_empty'`.

Correr **sin** `--apply` primero, mostrar a Charlie el diff, después con `--apply`.

**Mensaje commit:** `chore(ops): add reconcile_position.py + reconcile XLE`

### 2.4 Liquidar TPs atrasados

Una vez 2.1 y 2.2 mergeados:

1. Disparar `workflow_dispatch` de `rftm_watchdog.yml` con `dry_run=false` y `force_run=false`.
2. Repetir para `mrev_watchdog.yml`.
3. Confirmar en Alpaca y por mail (los `send_stage_event_email` deberían disparar para cada TP/cierre).

**No es un commit; es una operación.** Loguearlo en `RUNBOOK_WATCHDOGS.md` con fecha, qué se cerró, P&L total liquidado.

---

## 3. FASE 1 — Trade cards + balance general (esta semana, ~6 hs)

Branch sugerida: `feat/trade-cards-emails`. Una serie de commits, uno por sección. PR final.

### 3.1 Schema nuevo: tabla `position_events`

**Archivos a tocar:** `standalone_paper_trader.py` (`init_db`), `standalone_mrev_trader.py` (`init_db`), `_db_health.py` (agregar requirement), `tests/test_db_schema.py` (assertion nueva).

**SQL:**
```sql
CREATE TABLE IF NOT EXISTS position_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,    -- 'open' | 'tp1' | 'tp2' | 'close' | 'tp1_inferred'
    event_at      TEXT    NOT NULL,    -- ISO8601 UTC
    qty           INTEGER NOT NULL,
    price         REAL    NOT NULL,
    notional      REAL    NOT NULL,
    realized_pnl  REAL,                -- null para 'open' y los inferidos
    pnl_pct       REAL,
    reason        TEXT,                -- 'partial_tp1_5pct', 'E7_take_profit', 'stop_loss', etc
    stop_after    REAL,                -- stop_loss después del evento (cuando aplica)
    inferred      INTEGER DEFAULT 0,   -- 1 = backfill sin precio real
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_position_events_pid    ON position_events(position_id);
CREATE INDEX IF NOT EXISTS idx_position_events_run    ON position_events(run_id);
CREATE INDEX IF NOT EXISTS idx_position_events_symdt  ON position_events(symbol, event_at);
```

Idempotente. `_db_health.py` añade `position_events` y sus columnas a la lista de checks.

**Tests nuevos** en `tests/test_db_schema.py`:
- Tabla existe en RFTM y MREV después de `init_db()`.
- Columnas correctas.
- Indexes correctos.

**Commit:** `feat(db): add position_events table for trade timeline`

### 3.2 Hooks de inserción de eventos

**Archivos a tocar:** `rftm_watchdog.py`, `mrev_watchdog.py`, los bots entry, y un nuevo helper `_position_events.py` en la raíz.

**Helper centralizado** `_position_events.py`:
```python
"""Inserción idempotente de eventos en position_events."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Optional

def record_event(
    conn: sqlite3.Connection,
    *,
    position_id: str,
    run_id: str,
    symbol: str,
    event_type: str,         # 'open' | 'tp1' | 'tp2' | 'close' | 'tp1_inferred'
    qty: int,
    price: float,
    realized_pnl: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    reason: Optional[str] = None,
    stop_after: Optional[float] = None,
    inferred: bool = False,
    event_at: Optional[str] = None,
) -> int:
    event_at = event_at or datetime.now(tz=timezone.utc).isoformat()
    notional = round(qty * price, 2)
    cur = conn.execute(
        """INSERT INTO position_events
           (position_id, run_id, symbol, event_type, event_at, qty, price,
            notional, realized_pnl, pnl_pct, reason, stop_after, inferred)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (position_id, run_id, symbol, event_type, event_at, qty, price,
         notional, realized_pnl, pnl_pct, reason, stop_after,
         1 if inferred else 0),
    )
    return cur.lastrowid
```

**Hooks:**
- `rftm_watchdog._handle_partial_tp` y `mrev_watchdog._handle_partial_tp`: después del UPDATE de `positions`, llamar `record_event(event_type='tp1' o 'tp2', ...)` con el `filled_avg_price` real del order de Alpaca.
- `rftm_watchdog._handle_full_exit` y `mrev_watchdog._handle_full_exit`: después del UPDATE, `record_event(event_type='close', ...)`.
- En los bots entry, dentro de la rama de creación de posición (donde se hace `INSERT INTO positions` + `alpaca_submit_order`), después del fill: `record_event(event_type='open', ...)`.

**Tests nuevos** en `tests/test_position_events.py`:
- `record_event` inserta correctamente.
- Insertar dos veces el mismo evento NO duplica (idempotencia por `position_id+event_type` — opcional, podemos permitir múltiples). **Decisión:** permitimos múltiples y fileamos por `(position_id, event_type)` LIMIT 1 al consultar; backfilling chequea existencia previa.
- Hook del watchdog inserta `tp1` cuando dispara partial TP1 (mock de Alpaca).

**Commit:** `feat(events): record open/tp1/tp2/close events into position_events`

### 3.3 Render del trade card

**Archivo:** `_email_helpers.py` — agregar `render_trade_card(card_data)` y `render_balance_table(closes_today, closes_month, account)`.

**Contrato de `render_trade_card`:**

```python
def render_trade_card(c: dict) -> str:
    """Renderiza una tarjeta HTML self-contained (CSS inline) para email.
    
    `c` tiene esta forma:
    {
      "symbol": "ARGT",
      "name": "Argentina (Global X MSCI Argentina)",
      "open":  {"qty": 270, "price": 92.35, "at": "2026-04-15", "stop_initial": 87.85},
      "tp1":   {"hit": True, "qty": 135, "price": 96.97, "pnl": 623.70, "pct": 5.0, "at": "..."} | None,
      "tp2":   {"hit": True, "qty": 67,  "price": 99.28, "pnl": 464.31, "pct": 7.5, "at": "..."} | None,
      "close": {"hit": False, "qty_remaining": 68, "stop_now": 96.97}  # o {"hit": True, "qty": 68, "price": ..., "pnl": ..., "pct": ..., "reason": ...}
      "current_price": 99.65,
      "realized_pnl": 1088.01,
      "unrealized_pnl": 182.43,
      "total_pnl": 1270.44,
      "total_pct": 5.10
    }
    """
```

**Reglas visuales:**
- Cada hito (TP1, TP2, cierre) es una fila. Estado `pending` = ☐ gris atenuado. Estado `hit` = ✓ verde con qty/precio/$/%.
- Header: símbolo + nombre largo + badge de P&L total.
- Footer: 3 columnas (realizado / no realizado / total).
- CSS inline, ancho máx 540px, `@media (prefers-color-scheme: dark)` para dark mode.
- Sin JavaScript, sin imágenes externas (Gmail bloquea).
- Compatible con Gmail web/mobile, Apple Mail, Outlook 365.

**Helpers internos sugeridos** (private):
- `_row_pending(label, value_text)` y `_row_hit(label, value_text, pnl, pct)` — devuelven HTML.
- `_format_money(x)` y `_format_pct(x)` — `+$1,270.44` y `+5.10%` con coloreo.

**`render_balance_table`:**
```python
def render_balance_table(
    closes_today: list[dict],   # rows de position_events filtradas
    closes_month: list[dict],
    account: dict,              # {"equity": ..., "last_equity": ..., "initial_capital": ...}
) -> str:
    """Tabla compacta con todos los cierres del día + acumulados."""
```

**Tests nuevos** en `tests/test_trade_card.py`:
- Render con fixture de trade abierto sin TPs (todo pending).
- Render con TP1 hit.
- Render con TP1+TP2 hit.
- Render con close hit (trade cerrado).
- Render con close por stop_loss (P&L negativo, color rojo).
- Render con `inferred=True` muestra el ✓ atenuado.
- Edge case: qty=1 (no se puede halvear, TP1 no dispara, card debe mostrar TP1 como "pending — qty insuficiente").

**Smoke test visual:** flag `MOCK_SMTP=1` que en lugar de mandar SMTP escribe el HTML a `email_preview_<timestamp>.html` para inspección visual. Dejarlo documentado en `RUNBOOK.md`.

**Commit:** `feat(email): render_trade_card + render_balance_table helpers`

### 3.4 Builder de card desde DB

**Archivo:** `_email_helpers.py` o nuevo `_card_builder.py`.

```python
def build_card_from_position(
    conn: sqlite3.Connection,
    position_id: str,
    current_price: float,    # de Alpaca
) -> dict:
    """Lee positions + position_events + arma el dict para render_trade_card."""
```

Lógica:
- `SELECT * FROM positions WHERE id=?` → datos base.
- `SELECT * FROM position_events WHERE position_id=? ORDER BY event_at` → la timeline.
- Buscar el primer evento `tp1` o `tp1_inferred` → llena `card.tp1`.
- Buscar `tp2` → llena `card.tp2`.
- Buscar `close` → llena `card.close`.
- Calcular `realized_pnl` sumando `realized_pnl` de todos los eventos no-`open`.
- `unrealized_pnl` = `qty_actual * (current_price - entry_price)`.
- `total_pnl = realized_pnl + unrealized_pnl`.
- `total_pct = total_pnl / (initial_qty * entry_price)`.

**Tests nuevos** en `tests/test_card_builder.py`:
- Build de card con solo `open` → todos los TPs pending.
- Build después de `tp1` → tp1.hit=True, resto pending.
- Build después de `close` → close.hit=True con reason.
- Build con `tp1_inferred` → tp1.hit=True pero `card.tp1.inferred=True`.

**Commit:** `feat(email): build_card_from_position reads timeline from DB`

### 3.5 Triggers de email rediseñados

**Archivo:** `_email_helpers.py` — reemplazar `send_stage_event_email` con `send_trade_event_email`. El nuevo:

```python
def send_trade_event_email(
    *,
    kind: str,                  # 'tp1' | 'tp2' | 'close' | 'daily'
    primary_card: Optional[dict],   # card del trade que disparó (None para 'daily' sin movimiento)
    open_cards: list[dict],     # cards de todas las posiciones abiertas (compactas)
    closes_today: list[dict],
    closes_month: list[dict],
    account: dict,
    dry_run: bool = False,
) -> None:
    ...
```

**Subject builder:**
- `tp1` → `[Bot] {SYM} — TP1 ✓ ({+$X}, {+Y%})`
- `tp2` → `[Bot] {SYM} — TP2 ✓ ({+$X}, {+Y%})`
- `close` → `[Bot] {SYM} — Cerrado ({±$X}, {±Y%}) — {reason}`
- `daily` con cierres → `[Bot] Resumen {DD/MM} — {N} cierres, {±$X}`
- `daily` sin movimiento → `[Bot] {DD/MM} — Sin operaciones`

**Body:**
1. Hero con equity actual + P&L del día + P&L total.
2. Si hay `primary_card`: card grande arriba.
3. Si hay `closes_today`: balance table.
4. Si hay `open_cards`: bloque "Posiciones abiertas" con cards compactas.

**Cableado:**
- En `rftm_watchdog._handle_partial_tp` y `_handle_full_exit`: después del `record_event`, llamar `send_trade_event_email(kind='tp1' | 'tp2' | 'close', ...)` con el card recién buildeado.
- Idem en `mrev_watchdog`.
- En `standalone_paper_trader.send_email_report` (al final del run diario): llamar `send_trade_event_email(kind='daily', primary_card=None, ...)`.
- Idem en MREV daily summary.

**Anti-spam:** si en un mismo run del watchdog se disparan N TPs/cierres, agruparlos en un único email "consolidado" con N cards en lugar de N emails. Implementar como buffer en el watchdog: acumula eventos, al final del run manda un solo `send_trade_event_email(kind='consolidated', cards=[...])`.

**Tests nuevos** en `tests/test_email_triggers.py`:
- Mock SMTP, simular TP1 fire → 1 email enviado con el subject correcto y card en body.
- Simular 3 cierres en mismo run → 1 email consolidado, no 3.
- Daily sin movimiento → email con subject "Sin operaciones" y body con cards de abiertas.

**Commit:** `feat(email): consolidated trade event emails with progressive cards`

### 3.6 Backfill para los 12 trades abiertos hoy

**Archivo:** `scripts/ops/backfill_position_events.py [--apply]`.

Lógica:
- Para cada `position WHERE status='open'`:
  - Insertar evento `open` con `qty=initial_qty`, `price=entry_price`, `event_at=opened_at`.
  - Si `partial_tp_taken >= 1`: insertar `tp1_inferred` con `qty=initial_qty/2`, `price=entry_price * 1.05`, `pnl=qty * (price - entry)`, `inferred=1`. **Sin** `at` real (usar `opened_at + 1 day` como placeholder o dejar null).
  - Si `partial_tp_taken >= 2`: insertar `tp2_inferred` similar con `entry * 1.075`.
- Sin `--apply`: solo print del plan (`dry-run`).
- Con `--apply`: ejecuta los inserts dentro de una sola transacción.

**Verificación:** correr el script, abrir el daily summary que se mande después → debería mostrar las 12 cards con TP1 ✓ atenuado para los stage>=1.

**Commit:** `chore(ops): backfill_position_events.py + run for current 12 open trades`

### 3.7 Feature flag

**Env nueva:** `EMAIL_NEW_FORMAT` (default `false`).

- Si `true`: usar el nuevo `send_trade_event_email`.
- Si `false`: usar el camino viejo (`send_email_report` original + `send_stage_event_email` original).

Esto deja rollback en 1 cambio de env si el HTML se ve roto en algún cliente.

Después de 2-3 días con `EMAIL_NEW_FORMAT=true` y todo OK, otro PR borra el camino viejo.

**Commit:** `feat(email): EMAIL_NEW_FORMAT feature flag`

---

## 4. FASE 2 — Mensual + dashboard (próxima semana, ~3 hs)

### 4.1 Email mensual rediseñado para RFTM

MREV ya tiene un mensual lindo (commit `0ad5e82`). Replicar la misma estructura para RFTM, leyendo de `position_events` filtrado al mes.

Bloques:
- KPIs: trades cerrados, win rate, avg P&L, mejor trade, peor trade.
- Tabla de todos los trades cerrados del mes (símbolo, abrió, cerró, qty, entry, exit, P&L, %, motivo).
- Comparativa contra SPY buy-and-hold del mismo mes (opcional).

**Cron:** workflow nuevo `rftm_monthly.yml` que corre el día `EMAIL_MONTHLY_DAY` (default 1) a la hora de `EMAIL_HOURS_UTC[0]` (default 12).

**Commit:** `feat(rftm): monthly email report`

### 4.2 Dashboard HTML estático opcional

Workflow nuevo `dashboard.yml` que después de cada run del bot:
1. Exporta `position_events.json` y `positions.json`.
2. Renderiza `dashboard.html` con la misma plantilla del email pero con todas las posiciones (abiertas + cerradas del mes en curso).
3. Pushea a la branch `gh-pages`.

GitHub Pages sirve `https://<user>.github.io/trading-system/` y Charlie lo abre cuando quiere ver el estado actual sin esperar el mail.

**Commit:** `feat(ops): static HTML dashboard via gh-pages`

---

## 5. FASE 3 — Polishing (cuando haya tiempo)

- Notifs Telegram (`TELEGRAM_BOT_TOKEN` ya está en `.env.paper`, sin usar).
- Resumen semanal domingo noche.
- Comparativa benchmark (SPY) en mensual y semanal.
- Detección de "stuck position" (más de N días sin movimiento de stage) → email de alerta para revisar manualmente.

---

## 6. Criterios de aceptación

Antes de cerrar este prompt como done, todos verde:

1. `python3 -m py_compile` de los archivos tocados, exit 0.
2. Suite completa de tests verde (la del CLAUDE.md + los nuevos archivos `tests/test_*.py` agregados en este plan).
3. `python3 scripts/ops/preflight.py` exit 0.
4. **Verificación funcional:**
   - El watchdog disparado por cron manda órdenes reales (no DRY_RUN).
   - El mail diario RFTM llega los días hábiles después de las 13:35 UTC.
   - Cuando dispara un TP1, llega un mail con la card mostrando ☐ TP1 → ✓ TP1 + tabla balance del día.
   - Lo mismo para TP2 y cierre final.
   - El balance general acumulado del día y del mes coincide con sumar `realized_pnl` de `position_events`.
5. **Verificación visual:** abrir el HTML preview de cada tipo de email en Gmail desktop, Gmail mobile, Apple Mail. Sin overflow, sin texto roto, con dark mode funcionando.
6. **Verificación de no-regresión:** los 12 trades existentes siguen abiertos en Alpaca después del backfill. El watchdog NO los cierra incorrectamente. La DB sigue íntegra.

---

## 7. Mensajes de commit sugeridos (resumen)

```
fix(watchdogs): cron triggers default DRY_RUN=false (kept manual default true)
fix(rftm): load .env.paper at startup (mirrors MREV behavior)
chore(ops): add reconcile_position.py + reconcile XLE
feat(db): add position_events table for trade timeline
feat(events): record open/tp1/tp2/close events into position_events
feat(email): render_trade_card + render_balance_table helpers
feat(email): build_card_from_position reads timeline from DB
feat(email): consolidated trade event emails with progressive cards
chore(ops): backfill_position_events.py + run for current 12 open trades
feat(email): EMAIL_NEW_FORMAT feature flag
feat(rftm): monthly email report
feat(ops): static HTML dashboard via gh-pages
```

---

## 8. Decisiones que necesito de Charlie antes de mergear nada

1. **Email por evento o consolidado por run?** → recomendación: consolidado (1 mail cada 5 min máx, agrupa todos los TPs/cierres del run). Más limpio, menos spam. Cambia trivialmente si querés el otro.
2. **Daily se manda siempre o solo con movimiento?** → recomendación: siempre. Si no operó, mail con "Sin operaciones" + cards compactas de abiertas. Da certeza de que el bot corre.
3. **Backfill de los 12 trades viejos?** → recomendación: sí, marcado como `inferred`. Mejor algo que vacío.
4. **Anti-regresión de checks de schema en `_db_health.py`?** → recomendación: sí, agregar `position_events` y sus columnas. Si la DB no la tiene, abortar el run. Ya hay precedente.
5. **¿El feature flag `EMAIL_NEW_FORMAT` arranca en `true` o `false` cuando se mergee Fase 1?** → recomendación: `false`, prender a mano cuando vos hayas visto un preview HTML local y lo apruebes. Después del primer mail OK por 2-3 días, otro PR lo prende por default.

---

## 9. Apéndice — Cómo ejecutar este prompt en Claude Code

```bash
# Desde el repo
cd ~/Desktop/trading-system

# Asumir que hay una sesión Claude Code abierta. Pegar este texto al final
# del prompt o como /context.
# Luego pedir:
"Ejecutá la Fase 0 de PROMPT_FIX_VENTAS_Y_EMAILS.md.
 Hacé un commit por sección, abrí un PR llamado 'fix/sales-and-emails-p0',
 y pará antes de mergear para que yo lo revise."
```

Para Fase 1, mismo patrón pero con `feat/trade-cards-emails`.

Para Fase 2, `feat/monthly-and-dashboard`.

---

## 10. Apéndice — mock visual del card

```
┌─ ARGT — Argentina (Global X MSCI Argentina) ─────────────┐
│                                            +$1,270 (+5.10%) │
│                                                          │
│  Apertura:    270 sh @ $92.35 = $24,934.50               │
│  Stop ini:    $87.85 (-4.9%)                             │
│                                                          │
│  ─── Ventas parciales ───                                │
│  ✓ TP1 (+5.0%)   135 sh @ $96.97  →  +$623.70 (+5.0%)    │
│  ✓ TP2 (+7.5%)    67 sh @ $99.28  →  +$464.31 (+7.5%)    │
│                                                          │
│  ─── Cierre final ───                                    │
│  ☐ Pendiente — abierto 68 sh, stop $96.97 (breakeven)    │
│                                                          │
│  Realizado     No realizado     Total                    │
│  +$1,088.01    +$182.43         +$1,270.44               │
└──────────────────────────────────────────────────────────┘
```

Cuando el cierre final dispare, esa fila pasa a:
```
  ✓ Cerrado (E7) — 68 sh @ $99.65 → +$496.04 (+5.4%) — 02-may
```

Y el footer queda:
```
  Realizado     No realizado     Total
  +$1,584.05    $0.00            +$1,584.05 (+6.35%)
```

Ahí el trade está done y se archiva en el resumen mensual.

---

**Fin del prompt.** Cuando arranques, marcá tareas en `TaskCreate` y andá completándolas. Si encontrás algo que el prompt no contempla, pará y pregunta a Charlie antes de improvisar.
