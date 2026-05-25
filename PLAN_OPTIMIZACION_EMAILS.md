# PLAN — Fix de ventas + rediseño de emails con trade cards

**Fecha:** 2026-04-27
**Autor:** Claude (en sesión Cowork)
**Estado:** Borrador para revisión de Charlie antes de implementar.

---

## TL;DR — qué está roto y qué vamos a hacer

Dos bugs raíz explican todo lo que estás viendo:

1. **El watchdog corre en DRY_RUN cuando lo dispara el cron**, así que evalúa TPs/stops pero nunca manda órdenes. Por eso ves posiciones muy arriba del TP sin vender. Está pasando desde el 23-abr (commit `22fa0d2` que prendió el schedule).
2. **El bot diario RFTM no carga `.env.paper`**, así que las credenciales SMTP llegan vacías al proceso y `send_email_report()` sale silencioso con un warn que solo queda en logs. El bot MREV sí lo carga, por eso el horario te llega.

Más allá de los bugs, los emails actuales son flacos: un mail diario "compré N + vendí M" sin trazabilidad por trade. Lo que vos pediste —y es lo correcto— es un sistema con **una tarjeta por trade** que se va llenando con cada evento (entrada → TP1 → TP2 → cierre final) y un **email de balance** cuando se cierra cualquier operación, mostrando $$ y % por activo.

Plan en tres fases:

- **Fase 0 (HOY, 30 min)**: apagar el sangrado. Watchdog deja DRY_RUN, RFTM lee `.env.paper`, reconciliar XLE, disparar watchdog manual para liquidar TPs atrasados.
- **Fase 1 (esta semana, ~4-6 hs)**: implementar el trade card + email de cierre por trade. Reescribir el daily con cards.
- **Fase 2 (próxima semana, ~3-4 hs)**: email mensual con todos los trades cerrados, dashboard HTML opcional.

---

## Parte 1 — Fixes urgentes (Fase 0)

### 1.1 Watchdog en DRY_RUN cuando lo dispara cron

**Archivo:** `.github/workflows/rftm_watchdog.yml` y `.github/workflows/mrev_watchdog.yml`

**Línea:**
```yaml
DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}
```

Cuando lo dispara `schedule:`, `github.event.inputs` no existe → el fallback `'true'` se aplica → el watchdog logea `[DRY] SELL …` pero nunca manda la orden a Alpaca.

**Fix:**
```yaml
DRY_RUN: ${{ github.event.inputs.dry_run || 'false' }}
```

Mantenemos `workflow_dispatch` con default `'true'` para los runs manuales de prueba (esos sí pasan inputs y la default vale).

**Verificación post-fix:** disparar manual con `dry_run=false` y mirar Alpaca → debería liquidar TP2 / E7 / trailing de las posiciones que están atrasadas.

### 1.2 RFTM no carga `.env.paper`

**Archivo:** `standalone_paper_trader.py`

El bot lee `EMAIL_FROM/PASSWORD/TO` directo de `os.environ` al importar. El workflow `daily_trade.yml` solo escribe esas vars a `.env.paper`, no las pasa como `env:` al step que corre el bot. Resultado: SMTP arranca sin credenciales y `send_email_report()` retorna sin enviar.

MREV ya tiene la solución implementada (líneas 53-65 de `standalone_mrev_trader.py`).

**Fix (preferido):** portar `_load_env()` a RFTM, llamarlo antes de leer las EMAIL_* vars.

```python
# standalone_paper_trader.py — agregar después de los imports, antes de las constants
from pathlib import Path

def _load_env():
    for name in (".env.paper", ".env"):
        p = Path(__file__).parent / name
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

_load_env()
```

**Fix alternativo (menos limpio):** agregar al `env:` del step `Run RFTM Bot` en `daily_trade.yml`:
```yaml
EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
EMAIL_TO: ${{ secrets.EMAIL_TO }}
EMAIL_ENABLED: 'true'
EMAIL_SMTP_SERVER: smtp.gmail.com
EMAIL_SMTP_PORT: '587'
```

Recomiendo la opción A porque deja el bot autónomo y consistente con MREV.

### 1.3 Reconciliar XLE

DB tiene `qty=438` con `initial_qty=136`. Imposible: qty actual no puede ser mayor que el inicial. Algo se descalibró cuando se reentró post-cierre o el seed dejó dos rows. Acción:

1. Cerrar el run RFTM, ver qué dice Alpaca para XLE.
2. Si Alpaca tiene 438 sh: corregir `initial_qty=438` y `partial_tp_taken=0` (asumir es una posición fresca).
3. Si Alpaca tiene 136 sh: el qty 438 es un fantasma de seed; corregir `qty=136`.
4. Sincronizar `entry_price` con `avg_entry_price` real de Alpaca.

Script auxiliar a escribir: `scripts/ops/reconcile_position.py SYM` que muestre las dos vistas y permita aplicar el fix con `--apply`.

### 1.4 Disparar watchdog manual

Después de 1.1 + 1.2, correr `workflow_dispatch` del watchdog RFTM con `dry_run=false` para que liquide los TPs atrasados de una vez. Mirar el log: cuántas órdenes mandó, cuántas filearon. Idem MREV.

---

## Parte 2 — Rediseño de emails (Fase 1)

### 2.1 La idea: trade card progresivo

Cada trade es una tarjeta. La tarjeta empieza el día que se compra y crece con cada evento:

```
┌─ ARGT — Argentina (Global X MSCI Argentina) ──────────┐
│                                                       │
│  Apertura:    270 sh @ $92.35  =  $24,934.50          │
│  Stop inicial: $87.85 (–4.9%)                         │
│                                                       │
│  ─── Ventas parciales ───                             │
│  ✓ TP1 (+5.0%)   135 sh @ $96.97  →  +$623.70 (+5.0%) │
│  ✓ TP2 (+7.5%)    67 sh @ $99.28  →  +$464.31 (+7.5%) │
│                                                       │
│  ─── Cierre final ───                                 │
│  ✗ Pendiente — abierto 68 sh, stop $96.97 (breakeven) │
│                                                       │
│  P&L realizado:    +$1,088.01                         │
│  P&L no realizado: +$182.43  (precio actual $99.65)   │
│  P&L total:        +$1,270.44  (+5.10%)               │
│                                                       │
└───────────────────────────────────────────────────────┘
```

Cuando cierra el trade entero, la card pasa a estado "cerrada" y se archiva en el resumen del día.

### 2.2 Persistencia: tabla `position_events`

Para que la card pueda mostrar precio real de cada parcial necesitamos guardar cada evento. Hoy `positions` solo guarda el estado actual + `realized_pnl` agregado, no la timeline.

**Nueva tabla** (RFTM y MREV, esquema idéntico):

```sql
CREATE TABLE IF NOT EXISTS position_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,    -- 'open', 'tp1', 'tp2', 'close'
    event_at        TEXT    NOT NULL,    -- ISO8601 UTC
    qty             INTEGER NOT NULL,    -- qty del evento (no acumulado)
    price           REAL    NOT NULL,    -- fill_avg_price
    notional        REAL    NOT NULL,    -- qty × price
    realized_pnl    REAL,                -- null para 'open', valor para tp1/tp2/close
    pnl_pct         REAL,                -- vs entry_price
    reason          TEXT,                -- 'partial_tp1_5pct', 'E7_take_profit', etc
    stop_after      REAL,                -- stop nuevo después del evento (si aplica)
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_position_events_pid ON position_events(position_id);
CREATE INDEX IF NOT EXISTS idx_position_events_run ON position_events(run_id);
```

**Hooks de inserción:**

- En el watchdog, dentro de `_handle_partial_tp()`: después de update de `positions`, insertar evento `tp1` o `tp2` con el fill price del order de Alpaca.
- En el watchdog, dentro de `_handle_full_exit()`: insertar evento `close`.
- En el bot entry, en la rama de creación de posición: insertar evento `open`.

Migración: agregar `CREATE TABLE IF NOT EXISTS` envuelto en try/except, idempotente, en `init_db()` de ambos bots y en `_db_health.py` chequear que la tabla y columnas existan. Para los trades viejos (los 12 abiertos hoy) no podemos reconstruir TP1 retroactivo porque perdimos los fill prices, pero podemos backfillar `open` con `entry_price` y, para los stage>=1, un evento `tp1_inferred` a `entry_price * 1.05` con un flag `inferred=true` (se muestra atenuado en el card).

### 2.3 Triggers de email

Cuatro triggers nuevos, cada uno mandando una **trade card** + el **balance general** del día:

| Evento | Cuándo dispara | Asunto |
|---|---|---|
| `TP1` hit | watchdog ejecuta partial TP1 | `[Bot] ARGT — TP1 ✓ (+$623, +5.0%)` |
| `TP2` hit | watchdog ejecuta partial TP2 | `[Bot] ARGT — TP2 ✓ (+$464, +7.5%)` |
| Cierre final | watchdog ejecuta exit (stop / E7 / trailing / time) | `[Bot] ARGT — Cerrado (+$1,270, +5.1%)` |
| Daily summary | bot diario al final del run | `[Bot] Resumen 27/04 — 2 cierres, +$1,734` |

Cada uno trae:

1. **Card del trade que disparó el evento** (arriba del email, llena al estado actual).
2. **Balance general del día**: tabla con todos los cierres del día + P&L acumulado.
3. **Cards de las posiciones abiertas** (chicas, colapsadas, mostrando solo símbolo + P&L y next-target).

El balance general es la "hoja de saldo" que pediste: cada vez que cierra algo (parcial o total), te llega con todo lo cerrado en el día sumado.

### 2.4 Plantilla HTML del card

Vivirá en `_email_helpers.py` como función pura `render_trade_card(card_data: dict) -> str`. Toma este diccionario:

```python
{
  "symbol": "ARGT",
  "name": "Argentina (Global X MSCI Argentina)",
  "open": {"qty": 270, "price": 92.35, "at": "2026-04-15", "stop_initial": 87.85},
  "tp1": {"hit": True, "qty": 135, "price": 96.97, "pnl": 623.70, "pct": 5.0, "at": "..."},
  "tp2": {"hit": True, "qty": 67, "price": 99.28, "pnl": 464.31, "pct": 7.5, "at": "..."},
  "close": {"hit": False, "stop_now": 96.97},
  "open_qty_now": 68,
  "current_price": 99.65,
  "realized_pnl": 1088.01,
  "unrealized_pnl": 182.43,
  "total_pnl": 1270.44,
  "total_pct": 5.10,
}
```

Renderiza con CSS inline (Gmail/Apple Mail compatible), dark mode aware (`@media (prefers-color-scheme: dark)`), responsive a 540px de ancho como el actual. Los checks ✓/✗ son emoji + color verde/gris. La lógica de "pendiente" se ve atenuada (`opacity: 0.5`).

Mock visual aproximado en HTML está en `examples/trade_card_mock.html` (a crear en Fase 1).

### 2.5 Tabla de balance general

Compacta, debajo del card del trade que disparó:

```
┌─ Cerrado hoy (27/04) ────────────────────────────────┐
│  ETF    Acción  Qty  Precio   P&L $       P&L %      │
│  ARGT   TP2      67  $99.28   +$464.31    +7.5%      │
│  XLK    Cierre    1  $148.85  +$7.08      +5.0%      │
│  ─────────────────────────────────────────────────── │
│  Total cerrado hoy:           +$471.39    +6.4% avg  │
│  Total cerrado este mes:      +$3,212.45             │
│  Equity Alpaca:               $103,847.22  (+3.85%)  │
└──────────────────────────────────────────────────────┘
```

Acumulados se calculan con queries SQL sobre `position_events`:
- Hoy: `WHERE date(event_at) = date('now', 'utc') AND event_type IN ('tp1','tp2','close')`
- Mes: `WHERE event_at >= datetime('now', 'start of month')`.

### 2.6 Daily summary rediseñado

El daily ya no es "compré N + vendí M". Pasa a ser:

1. Hero: equity actual, P&L del día, P&L total.
2. Si hubo cierres hoy: tabla balance general (igual que 2.5).
3. Cards de **todas las posiciones abiertas** (forma compacta — entry, stage actual, P&L abierto, próximo target).
4. Watchlist (igual que ahora pero más chico).

Si no hubo movimiento hoy se manda igual con `[Bot] 27/04 — Sin operaciones hoy`. Antes el bot se quedaba callado los días sin señales y vos no sabías si corrió o estaba muerto.

---

## Parte 3 — Implementación por fases

### Fase 0 — HOY (30 min)

- [ ] PR `fix/watchdog-dry-run-default`: cambiar `|| 'true'` → `|| 'false'` en los dos watchdog YAML.
- [ ] PR `fix/rftm-load-env`: portar `_load_env()` a `standalone_paper_trader.py`.
- [ ] Reconciliar XLE (script o ALTER manual con backup previo).
- [ ] `workflow_dispatch` del watchdog RFTM con `dry_run=false` → liquidar TPs atrasados.
- [ ] Mismo para MREV watchdog.
- [ ] Confirmar que llegó email diario (cuando vuelva a correr el cron 13:35 UTC del próximo día hábil).

### Fase 1 — Esta semana (4-6 hs)

- [ ] Migración DB: tabla `position_events` en RFTM y MREV (idempotente).
- [ ] Hooks: insertar evento en open / tp1 / tp2 / close (en watchdog y entry bot).
- [ ] `_email_helpers.render_trade_card(data)` con su HTML + CSS.
- [ ] `_email_helpers.render_balance_table(closes_today, closes_month, account)`.
- [ ] `_email_helpers.send_trade_event_email(card, balance, kind)`.
- [ ] Re-cablear `send_stage_event_email` para que use el nuevo formato.
- [ ] Reescribir `_build_email_report` (RFTM) y la función equivalente de MREV para que usen cards.
- [ ] Tests:
  - `tests/test_trade_card.py`: render con fixtures (open-only, tp1-only, tp1+tp2, fully-closed, edge: qty=1).
  - `tests/test_balance_table.py`: agregados correctos.
  - `tests/test_position_events.py`: hooks insertan correctamente, idempotencia, schema.
- [ ] Smoke test SMTP: correr bot con `MOCK_SMTP=1` que escriba el HTML a `email_preview.html` en lugar de mandarlo. Visual inspection.

### Fase 2 — Próxima semana (3-4 hs)

- [ ] Email mensual rediseñado (MREV ya tiene uno, replicar para RFTM): tabla con todos los trades cerrados del mes, % win rate, avg P&L, mejor y peor trade.
- [ ] Backfill `position_events` para los 12 trades abiertos hoy (eventos `open` reales + `tp1_inferred` para los stage>=1, sin pnl real porque no lo tenemos).
- [ ] Dashboard HTML estático opcional: `dashboard.html` que se sirve via GitHub Pages, lee `position_events.json` exportado en cada run del bot. Igual al email pero accesible cuando querés.

### Fase 3 — Polishing (cuando haya tiempo)

- [ ] Notificaciones Telegram opcionales (ya tenés `TELEGRAM_BOT_TOKEN` en `.env.paper`, está sin usar).
- [ ] Resumen semanal los domingos noche.
- [ ] Comparativa contra benchmark (SPY buy & hold) en el mensual.

---

## Parte 4 — Testing y rollback

### Pre-merge

- `python3 -m py_compile` los archivos tocados (parte de los rituales del CLAUDE.md).
- Suite de tests vigente debe seguir verde (los tests de `test_health`, `test_db_schema`, `test_universes_disjoint`, etc).
- Tests nuevos del trade card: 100% coverage del render.
- Smoke test SMTP con preview a archivo, abrir en navegador para inspección visual.

### Post-merge

- Primer run del watchdog en modo real: monitor en GitHub Actions, capturar logs, si algo se manda mal abortar con `workflow_dispatch dry_run=true` para futuros runs.
- Ver que el primer email diario nuevo llegue OK.
- Si el HTML se ve roto en Gmail (mobile vs desktop): revisar el CSS, probar en `litmus.com` o `mailtrap.io`.

### Rollback

- Watchdog DRY_RUN: revertir el commit, deja al sistema sin liquidar pero seguro.
- Email rediseñado: feature flag `EMAIL_NEW_FORMAT=true|false` (default `false` los primeros 2-3 días, prendido a mano cuando esté validado).

---

## Parte 5 — Riesgos

1. **Liquidación masiva al primer run del watchdog real**: 12 posiciones abiertas, varias con stage=1 que probablemente ya rompieron TP2. Va a haber un "fire sale" del watchdog. Mitigación: correr primero con `--limit 1` (a implementar) o liquidar manualmente las más obvias antes de prender el cron.
2. **Backfill `position_events` para trades viejos sin fill prices**: vamos a tener `tp1_inferred` con flag pero el monto realizado real es desconocido. Aceptable para histórico, marcado claro en UI.
3. **Costo SMTP**: si TP1, TP2, close, daily todos disparan, podemos terminar con 5-8 emails por día. Mitigación: agrupar TPs del mismo run en un solo email "consolidated" (`run_id` igual = un solo email con todas las cards afectadas).
4. **XLE qty inconsistente**: si lo tocamos mal podemos perder histórico real. Hacer dump de `positions` antes de tocar.

---

## Decisiones que necesito de Charlie antes de implementar

1. **Email por cada cierre o agrupados?** Cada vez que cierra parcial o total → 1 email separado. O bien: agrupar todos los cierres de un mismo run del watchdog en un solo email. Sugerencia: agrupar por run (cap a 1 email cada 5 min máximo, igual al ciclo del watchdog).
2. **¿Mantener el email "no operé hoy" o solo mandar cuando hay movimiento?** Sugerencia: mandar siempre — saber que el bot corrió es información.
3. **¿Backfillamos los 12 trades viejos como `tp1_inferred` o los dejamos sin historia?** Sugerencia: backfill, marcado como inferido.
4. **¿Implementás vos o querés que lo haga yo en PRs separados?** Te puedo armar Fase 0 ya mismo (3 cambios chicos) y Fase 1 en una serie de commits chicos para revisar.

---

## Apéndice A — Mock visual rápido del card en HTML

Pseudo-HTML (sin estilos, solo estructura):

```html
<div class="card">
  <div class="card-head">
    <span class="sym">ARGT</span>
    <span class="name">Argentina (Global X MSCI Argentina)</span>
    <span class="pnl-badge green">+$1,270 (+5.10%)</span>
  </div>
  <div class="card-body">
    <div class="row open">
      <span class="lbl">Apertura</span>
      <span class="val">270 sh @ $92.35 = $24,934.50</span>
    </div>
    <div class="row stop">
      <span class="lbl">Stop inicial</span>
      <span class="val">$87.85 (-4.9%)</span>
    </div>
    <div class="row tp1 hit">
      <span class="check">✓</span>
      <span class="lbl">TP1 (+5.0%)</span>
      <span class="val">135 sh @ $96.97 → +$623.70 (+5.0%)</span>
    </div>
    <div class="row tp2 hit">
      <span class="check">✓</span>
      <span class="lbl">TP2 (+7.5%)</span>
      <span class="val">67 sh @ $99.28 → +$464.31 (+7.5%)</span>
    </div>
    <div class="row close pending">
      <span class="check">☐</span>
      <span class="lbl">Cierre final</span>
      <span class="val">68 sh abiertos, stop $96.97 (breakeven)</span>
    </div>
  </div>
  <div class="card-foot">
    <div class="foot-cell">
      <span class="lbl">Realizado</span>
      <span class="val green">+$1,088.01</span>
    </div>
    <div class="foot-cell">
      <span class="lbl">No realizado</span>
      <span class="val green">+$182.43</span>
    </div>
    <div class="foot-cell">
      <span class="lbl">Total</span>
      <span class="val green">+$1,270.44</span>
    </div>
  </div>
</div>
```

CSS final con dark mode + mobile breakpoint queda para el commit que implemente el render. El "cuadrito que se va llenando" es exactamente esto: las filas `tp1`, `tp2`, `close` empiezan en estado `pending` (gris, ☐) y van pasando a `hit` (verde, ✓) a medida que el watchdog las dispara.
