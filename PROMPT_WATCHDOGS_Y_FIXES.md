# IMPLEMENTACIÓN — Watchdogs de Exits + Fixes P0

**Ámbito:** Paper trading. No vamos a plata real todavía. Pero queremos dejar
el sistema como si fuera a salir en producción en 30 días.

**Principio guía:** el bot principal entra. Un watchdog dedicado cuida los
stops y take-profits. Ambos son procesos independientes — si uno muere, el
otro sigue. Para cripto Alpaca no soporta bracket orders, así que el watchdog
es la única defensa.

---

## Arquitectura acordada

### RFTM (ETFs)

- `standalone_paper_trader.py` sigue existiendo pero en **modo entry-only**
  cuando corre en el cron diario. Chequea `check_entry` y compra. **No
  ejecuta exits.**
- **Nuevo**: `rftm_watchdog.py` corre cada 5 min durante horario de mercado
  (9:30–16:00 ET, L-V). Lee posiciones abiertas de Alpaca (no de la DB
  como fuente de verdad — la DB es caché). Para cada posición evalúa:

  1. TP1: si `pnl_pct ≥ +5%` y `stage == 0` → vende 50% qty, `stage=1`,
     sube stop a breakeven (`stop_loss = entry_price`).
  2. TP2: si `pnl_pct ≥ +7.5%` y `stage == 1` → vende 50% del remanente,
     `stage=2`.
  3. Stop loss: si `close ≤ stop_loss` → cierra el resto.
  4. Trailing / time stop / E7: seguir la lógica de `check_exit` del bot
     actual — reusarla, no duplicarla.

- Efecto: aunque el bot entry-only no corra durante días, el watchdog
  cuida las posiciones cada 5 min.

### MREV (cripto)

- `standalone_mrev_trader.py` sigue corriendo cada hora **en modo
  entry-only**. Busca nuevas entradas, no ejecuta exits.
- **Nuevo**: `mrev_watchdog.py` corre cada 5 min 24/7. Misma lógica que
  el RFTM watchdog (TP1 → vende 50%, sube stop a breakeven → TP2 vende 50%
  del remanente → stop loss → trailing → time stop).
- **Cooldown post-exit**: tras un exit por stop/trailing, el watchdog
  registra `(symbol, exit_dt)` en una tabla `mrev_cooldowns`. El bot entry
  al intentar entrar lee esta tabla y **rechaza** si `now - last_exit < 6h`.
  Esto evita el sell-low-rebuy-higher que vimos con LINK.

### Estado y fuente de verdad

- **Alpaca = verdad operativa** (posiciones abiertas, qty, avg_entry).
- **DB local = estado de estrategia** (stage, highest_since_entry,
  stop_loss, entry_dt, initial_qty). Este estado NO existe en Alpaca, hay
  que mantenerlo nosotros.
- Al arranque de cada proceso (bot entry y watchdog), hacer
  `reconcile_with_alpaca()`:
  - Si Alpaca tiene un symbol que la DB no → insertar con `stage=0`,
    `highest_since_entry = avg_entry`, `stop_loss = avg_entry - 1.5×ATR`
    (o fórmula actual del bot).
  - Si la DB tiene un symbol que Alpaca ya no → cerrar en la DB con
    `exit_reason='closed_outside_bot'`.
  - **Si hay diff de qty** → confiar en Alpaca, actualizar la DB, logear
    warning. Nunca submitear compras/ventas para "reconciliar" qty.

---

## Fixes P0 (hacer ANTES de tocar nada más)

### Fix A — INSERT bug en MREV (`standalone_mrev_trader.py:1701-1703`)

```python
conn.execute("INSERT INTO mrev_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    (str(uuid.uuid4())[:8], run_id, b["symbol"], b["qty"], b["price"],
     b["stop"], now.isoformat(), "OPEN", None, None, None, None))
```

Son 12 placeholders y la tabla tiene 15 columnas. **Cambiar a INSERT con
columnas explícitas**, mismo patrón que usa `sync_with_alpaca` en la
línea ~590. Incluir `highest_since_entry, partial_tp_taken, initial_qty`.

Después del fix, correr un `ENTER` dry-run y confirmar que el insert
funciona. Agregar un test en `tests/test_mrev/test_insert_enter.py`
que verifique que el número de placeholders matchee el número de
columnas (usar `PRAGMA table_info(mrev_positions)`).

### Fix B — No tragarse excepciones de DB

El `except Exception` en `standalone_mrev_trader.py:1709` (y su equivalente
en RFTM si existe) se come errores de persistencia y sigue. Cambiar por:

```python
except sqlite3.OperationalError as e:
    err(f"DB FAIL on buy {symbol}: {e}")
    # Revertir: cancelar la orden en Alpaca si todavía está abierta
    try:
        alpaca_cancel_order(order_id)
        warn(f"Canceled order {order_id} due to DB failure")
    except Exception as ce:
        err(f"ALSO failed to cancel order {order_id}: {ce}")
    # Abortar run: no queremos seguir comprando con persistencia rota
    raise SystemExit(2)
```

Principio: **si la DB no puede persistir una compra, es un bug crítico —
hay que abortar y alertar, no seguir como si nada**.

### Fix C — DB persistente en `daily_trade.yml`

Replicar el patrón de `mrev_hourly.yml`:

```yaml
- name: Restore trading_paper.db cache
  uses: actions/cache/restore@v4
  with:
    path: trading_paper.db
    key: rftm-db-v1-${{ github.ref_name }}-${{ github.run_id }}
    restore-keys: |
      rftm-db-v1-${{ github.ref_name }}-

- ... (run bot) ...

- name: WAL checkpoint
  run: python3 -c "import sqlite3; c=sqlite3.connect('trading_paper.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

- name: Save trading_paper.db cache
  uses: actions/cache/save@v4
  if: always()
  with:
    path: trading_paper.db
    key: rftm-db-v1-${{ github.ref_name }}-${{ github.run_id }}
```

### Fix D — Health check al inicio de cada run

Función `assert_db_health(db_path)`:

- ¿Existe el archivo? Si no, warn + crear schema.
- ¿El número de columnas de `positions` / `mrev_positions` matchea lo
  que el código espera? Si no, abortar con mensaje claro.
- ¿Hay runs abiertos (status='open')? Si hay más de uno, cerrar los
  viejos y warn.
- `PRAGMA integrity_check`.

Llamarla al entrar a cualquier script (entry bot, watchdog, auditoría).

---

## Tarea 1 — `rftm_watchdog.py`

**Archivo nuevo** en la raíz. ~200-300 líneas.

### Qué hace

1. Lee `.env.paper` + `assert_db_health('trading_paper.db')`.
2. `/v2/clock` → si mercado cerrado y no es `FORCE_RUN=1`, salir con
   mensaje "market closed, skipping".
3. `reconcile_with_alpaca()` — sync de posiciones abiertas.
4. Para cada posición abierta, calcular `pnl_pct`, actualizar
   `highest_since_entry` si aplica, y evaluar en este orden:
   - `partial_take_profit_check(pos, close)` — TP1/TP2 (reusar lógica
     de `standalone_paper_trader.py:1510-1572`, extraída a función pura).
   - `check_exit(pos, close, indicators)` — stop, trailing, time, E7.
5. Por cada acción, submitear la order a Alpaca y, **SOLO TRAS
   confirmar el fill (poll `/v2/orders/{id}` hasta `status='filled'` o
   timeout 10s)**, actualizar la DB.
6. Si la order no llena en 10s, cancelarla y logear. No mover la DB.
7. Al final, `PRAGMA wal_checkpoint`.

### Reglas

- **No toca** `check_entry`, `size_position`, `_calc_take_profit`,
  `ETF_UNIVERSE`, ni `ALL_SYMBOLS`. Solo consume.
- Si `check_exit` / `_partial_take_profit` no están en funciones puras
  ya, **extraerlas mínimamente** (sin cambiar su lógica). Los cambios a
  ese código tienen que ser no-semánticos (refactor only). Que el test
  suite pase 1:1 antes y después.
- `dry_run=True` por default. En producción, el workflow pasa
  `DRY_RUN=false` via env.
- Logging: mismo estilo `ok/info/warn/err/hdr` que los bots.
- **Idempotencia**: si el watchdog corrió hace 5 min y ya ejecutó TP1,
  el próximo run debe leer `stage=1` y no re-firear.
- **Atomicidad por símbolo**: si un TP1 falla, no avanzar a TP2 en el
  mismo símbolo ese run.

### Workflow asociado — `.github/workflows/rftm_watchdog.yml`

```yaml
name: RFTM Watchdog (Exits)

on:
  schedule:
    - cron: '*/5 13-20 * * 1-5'   # 13:00-20:55 UTC L-V (9-16 ET EDT con buffer)
  workflow_dispatch:

concurrency:
  group: rftm-watchdog-${{ github.ref }}
  cancel-in-progress: false

jobs:
  watchdog:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Restore DB cache
        uses: actions/cache/restore@v4
        with:
          path: trading_paper.db
          key: rftm-db-v1-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: rftm-db-v1-${{ github.ref_name }}-
      - name: Write .env.paper
        run: |
          cat > .env.paper <<EOF
          ALPACA_API_KEY=${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY=${{ secrets.ALPACA_SECRET_KEY }}
          ALPACA_BASE_URL=${{ secrets.ALPACA_BASE_URL }}
          EOF
      - name: Run watchdog
        env:
          DRY_RUN: 'false'
        run: python3 rftm_watchdog.py
      - name: WAL checkpoint
        if: always()
        run: python3 -c "import sqlite3; c=sqlite3.connect('trading_paper.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
      - name: Save DB cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: trading_paper.db
          key: rftm-db-v1-${{ github.ref_name }}-${{ github.run_id }}
      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: rftm-watchdog-${{ github.run_id }}
          path: |
            trading_paper.db
            *.log
          retention-days: 7
```

### Ajuste del workflow existente `daily_trade.yml`

- Agregar env var `MODE=entry_only` (o similar).
- `standalone_paper_trader.py` debe respetar `MODE` y saltearse la
  rama de exits si está en `entry_only`. **Cambio chico**: envolver el
  loop de exits en `if os.getenv('MODE', 'full') != 'entry_only':`.

---

## Tarea 2 — `mrev_watchdog.py`

Mismo patrón que RFTM, con tres diferencias:

1. **24/7**: no hay "market closed". Cron `*/5 * * * *`.
2. **Universo** = `CRYPTO_SYMBOLS` (`BTC, ETH, SOL, AVAX, DOGE, LINK`).
3. **Cooldown**: al cerrar una posición por stop/trailing, INSERT OR
   REPLACE en `mrev_cooldowns(symbol, last_exit_dt, reason)`.

### Schema nuevo

```sql
CREATE TABLE IF NOT EXISTS mrev_cooldowns (
    symbol TEXT PRIMARY KEY,
    last_exit_dt TEXT NOT NULL,
    reason TEXT NOT NULL
);
```

`ALTER TABLE ... ADD COLUMN` style — idempotente, envuelto en try/except.
Agregar a `init_mrev_db()` o equivalente.

### Cambio mínimo al entry bot MREV

En `check_entry` o justo antes del submit de order, chequear:

```python
cooldown_hours = float(os.getenv('MREV_COOLDOWN_HOURS', '6'))
row = conn.execute(
    "SELECT last_exit_dt FROM mrev_cooldowns WHERE symbol=?",
    (symbol,)
).fetchone()
if row:
    last_exit = datetime.fromisoformat(row[0])
    elapsed = (datetime.now(tz=timezone.utc) - last_exit).total_seconds() / 3600
    if elapsed < cooldown_hours:
        info(f"SKIP {symbol}: en cooldown ({elapsed:.1f}h / {cooldown_hours}h)")
        return None  # no entrar
```

- Default cooldown: 6h. Configurable via env.
- Solo aplica a exits por stop/trailing/time — **no** tras un TP1 o TP2
  (si cerró con ganancia por señal válida, re-entrar tiene sentido).
- Registrar en `mrev_cooldowns` solo en los exits negativos o neutros.

### Workflow — `.github/workflows/mrev_watchdog.yml`

Igual que RFTM pero con `cron: '*/5 * * * *'`, sin restricción de
horario, y DB=`mrev_paper.db`.

### Ajuste del workflow existente `mrev_hourly.yml`

Modo entry-only igual que RFTM.

---

## Tarea 3 — Refactor mínimo de lógica de exits

El watchdog tiene que usar **exactamente** la misma lógica de exits
que el bot entry-only. La forma más limpia es extraer a funciones puras:

- `def evaluate_exit(position: dict, indicators: dict, config: dict) -> Optional[ExitAction]`
- `def evaluate_partial_tp(position: dict, close: float, config: dict) -> Optional[PartialTPAction]`

Donde `ExitAction = {'reason': str, 'sell_qty': float}` y
`PartialTPAction = {'stage': int, 'sell_qty': float, 'new_stop': float}`.

**Cambios permitidos**: mover código existente a funciones nombradas, sin
alterar la lógica. Input/output determinístico.

**Tests**: los tests existentes de `tests/test_strategy.py` y
`tests/test_mrev/*` tienen que pasar 1:1 sin modificación después del
refactor. Si un test falla, el refactor está mal hecho.

Agregar tests nuevos específicos del watchdog:

- `tests/test_watchdog/test_rftm_watchdog.py`
  - TP1 con stage=0: dispara sell 50% + stop a breakeven
  - TP1 con stage=1: NO re-dispara
  - TP2 con stage=1: dispara sell 50% remanente + stage=2
  - Stop post-breakeven: vende todo si `close ≤ entry`
  - Trailing stop: dispara si `close ≤ highest - 1.0×ATR`
  - Idempotencia: correr watchdog dos veces con el mismo input no dobla
    las sells
- `tests/test_watchdog/test_mrev_watchdog.py`
  - Idem RFTM
  - Cooldown: tras stop, registra en `mrev_cooldowns`
  - Entry rechazado mientras cooldown activo
  - Entry permitido tras cooldown expirado

Mocks de Alpaca: usar `unittest.mock` o un fake client. No pegar a la
API real en tests.

---

## Tarea 4 — Sanity checks y anti-regresiones

### Test de invariante: universos disjuntos

`tests/test_universes_disjoint.py`:

```python
def test_etf_and_crypto_do_not_overlap():
    from standalone_paper_trader import ETF_UNIVERSE
    from standalone_mrev_trader import CRYPTO_SYMBOLS, ALL_SYMBOLS, ETF_SYMBOLS
    assert set(ETF_UNIVERSE) & set(CRYPTO_SYMBOLS) == set()
    assert ETF_SYMBOLS == []  # MREV no debe tocar ETFs
    assert set(ALL_SYMBOLS) == set(CRYPTO_SYMBOLS)
```

### Test de schema

`tests/test_db_schema.py`:

```python
def test_mrev_positions_schema():
    # Confirmar que INSERT explícito del ENTER coincide con PRAGMA table_info
    ...
def test_positions_schema(): ...
```

### Script `scripts/ops/preflight.py`

Lo corrés antes de cualquier arranque. Chequea:

1. DBs locales existen y `integrity_check` OK.
2. Schema de tablas matchea lo que el código espera.
3. `.env.paper` tiene los keys requeridos.
4. Alpaca API responde con account activo y BP > $0.
5. No hay posiciones "huérfanas" (en Alpaca y no en DB) ni al revés.
6. Los workflows yml no tienen errores sintácticos.

Retorno: exit 0 si todo OK, exit 1 con reporte claro si no.

---

## Tarea 5 — Documentación

### Actualizar `CLAUDE.md`

Agregar sección **Arquitectura post-watchdog** resumiendo:

- Dos procesos por bot: entry (cron lento) + watchdog (cron rápido).
- Estado de estrategia vive en DB, posiciones en Alpaca.
- Cooldown MREV de 6h post-exit negativo.
- Fixes P0 aplicados con fecha (2026-04-23).

### Crear `RUNBOOK_WATCHDOGS.md`

Guía operativa:

- Cómo diagnosticar si un watchdog se cayó.
- Cómo forzar un run manual (workflow_dispatch).
- Cómo interpretar un exit loguado.
- Procedimiento si la cache de DB se pierde.
- Qué hacer si hay drift masivo entre DB y Alpaca.

---

## Rituales de seguridad (CLAUDE.md — siguen aplicando)

1. Antes de cada edit no trivial:
   `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py`
2. Antes de cada PR: `python3 -m pytest tests/ -x`. Todos pass.
3. **No modificar** `check_entry`, `check_exit`, `_calc_take_profit`,
   `size_position`, `ETF_UNIVERSE`, `CRYPTO_SYMBOLS`, `ALL_SYMBOLS`
   **sin preguntarme**. El refactor a funciones puras es OK si es
   literalmente mover código sin cambiar semántica y los tests pasan
   idénticos.
4. `.env.paper` nunca se imprime ni se commitea.
5. Cada cambio en `check_entry` / `check_exit` / `_calc_take_profit` /
   `size_position` **requiere aprobación explícita mía antes de
   implementarse**. El refactor es la única excepción.

---

## Orden de implementación sugerido

1. **Fixes P0** (A, B, C, D). Commit aparte. Tests pasando.
2. **Refactor de `check_exit` y partial TPs a funciones puras**. Commit
   aparte. Tests existentes intactos.
3. **Cooldown table y lógica en MREV entry bot**. Commit aparte.
4. **`rftm_watchdog.py` + workflow + tests**. Commit aparte.
5. **`mrev_watchdog.py` + workflow + tests**. Commit aparte.
6. **Modo `entry_only` en bots existentes + ajuste de workflows**.
   Commit aparte.
7. **Sanity/preflight + docs**. Commit aparte.

Cada commit en su PR o al menos en su propio commit, con mensaje
descriptivo. No metas todo en un commit gigante.

---

## Criterios de aceptación (DoD)

- [ ] `python3 -m pytest tests/ -v` → todos los tests existentes pasan +
      los nuevos.
- [ ] Correr `rftm_watchdog.py` con `DRY_RUN=true` sobre el estado actual
      de Alpaca no dispara ninguna orden (porque las posiciones actuales
      no cumplen ninguna condición). Log limpio.
- [ ] Correr `mrev_watchdog.py` con `DRY_RUN=true` ídem.
- [ ] `scripts/ops/preflight.py` retorna exit 0.
- [ ] `AUDITORIA_PRE_GOLIVE.md` §1 actualizada: reconciliación DB↔Alpaca
      sin QTY_DRIFT ni ONLY_IN_DB tras correr el refactor y un sync.
- [ ] `CLAUDE.md` y `RUNBOOK_WATCHDOGS.md` al día.
- [ ] Workflows de GH Actions configurados pero **NO habilitados en
      schedule** todavía (probar primero con workflow_dispatch manual
      durante 1-2 días).
- [ ] Ningún cambio a `check_entry`, `size_position`, `ETF_UNIVERSE`,
      `CRYPTO_SYMBOLS`. Los cambios a `check_exit` y partial-TPs son
      SOLO refactor (extracción a función pura, mismo output).

---

## Qué NO hacer

- No cambies la lógica de entradas ni de sizing.
- No "optimices" los thresholds de TP/SL sin preguntar.
- No implementes bracket orders de Alpaca "por las dudas" además del
  watchdog — decidimos explícitamente que el watchdog es la solución,
  no el bracket. Bracket era una alternativa; elegimos watchdog por
  uniformidad con cripto.
- No sobrescribas la cache de DB de GH Actions sin back-up del estado
  previo.
- No borres archivos (`*.db`, `*.py`, `*.md`) existentes.
- No pushees nada a `main` que rompa los workflows actuales.

---

## Entregables

1. Código: `rftm_watchdog.py`, `mrev_watchdog.py`, refactor mínimo de
   exits, fixes P0.
2. Workflows: `rftm_watchdog.yml`, `mrev_watchdog.yml`, ajuste de
   `daily_trade.yml` y `mrev_hourly.yml`.
3. Tests nuevos y existentes pasando.
4. `scripts/ops/preflight.py`.
5. `CLAUDE.md` actualizado + `RUNBOOK_WATCHDOGS.md` nuevo.
6. PR/commits separados según el orden sugerido.
7. Al final, un mensaje de cierre con:
   - Qué se cambió archivo por archivo.
   - Cómo se verificó (comandos concretos que corriste).
   - Riesgos residuales.
   - Qué queda para la siguiente iteración (si algo).

## Si algo no cierra

**Parar y preguntar.** No inventes. No "completes" con suposiciones.
Preferible un PR más chico y seguro que uno grande y dudoso.
