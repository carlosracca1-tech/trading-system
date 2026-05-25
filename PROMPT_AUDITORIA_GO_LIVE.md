# AUDITORÍA FORENSE PRE-GO-LIVE — Sistema Dual Bot (RFTM + MREV)

## CONTEXTO QUE VOS YA SABÉS (lo reafirmo por si acaso)

- Cuenta Alpaca Paper compartida, $100K inicial.
- **RFTM** (`standalone_paper_trader.py`) — trend-following diario, solo ETFs.
- **MREV** (`standalone_mrev_trader.py`) — mean-reversion horario, solo cripto
  (`BTC, ETH, SOL, AVAX, DOGE, LINK`).
- Universos disjuntos desde **2026-04-22**. Migración via
  `migrate_legacy_etf_positions()`.
- Ambos bots corren en **GitHub Actions** (no en la Mac). Workflows:
  `.github/workflows/daily_trade.yml` y `mrev_hourly.yml`.
- Credenciales en `.env.paper`.

## SITUACIÓN ACTUAL OBSERVADA (23/04/2026)

Consultado vía API:

- Equity: **$107,544.16** (+7.5% desde inicial).
- Cash: $29,607.51. Long MV: $77,936.65.
- `last_equity` (ayer): $107,934.16 → **−$390 intradiario**.
- **12 posiciones abiertas en Alpaca.**

**Hay desincronización grave entre la DB local y Alpaca.** La DB local dice
que IWM tiene qty=24, Alpaca dice qty=12. Lo mismo con QQQ (11 vs 3),
SPY (10 vs 5), y XLE (438 en DB, no existe en Alpaca). MREV tiene en DB
abiertas AVAX/DOGE/SOL que ya no están en Alpaca. LINK/USD en DB tiene
qty=2136, en Alpaca qty=1195 (fue vendida y re-comprada).

**Ventas críticas detectadas en las últimas 48h:**

- 22/04 16:18 UTC: cascada (SPY 5, QQQ 5, IWM 12, DOGE 113k full, XLE 438 full).
- 22/04 18:11: QQQ 3 extra.
- 22/04 23:01: SOL full.
- 23/04 03:42: AVAX full. BTC compra 0.0328 @ $77,679 (sin posición previa).
- 23/04 06:15: BTC full @ $78,254.
- 23/04 09:01: LINK/USD full (2136) @ $9.16.
- 23/04 15:21: LINK/USD rebuy 1198 @ $9.42 **— compró 2.8% más caro de lo
  que vendió 6h antes**.

El usuario sospecha:

1. Que el bot puede estar tomando decisiones pobres que no se ven en el
   resultado global (+7.5% podría ser suerte o deriva del mercado, no alpha).
2. Que la lógica de re-entrada de MREV tiene un bug (vender abajo, comprar arriba).
3. Que hubo un **−2% en un día** recientemente que el usuario notó y lo
   espantó (tiene que verificarse contra el historial de equity real).
4. Que los full exits de XLE y DOGE del 22/04 pueden haber sido stops
   o exits discrecionales disfrazados de señal.

---

## TU TRABAJO

Hacer una **auditoría forense exhaustiva, no toques código de producción**,
y producir un reporte en `AUDITORIA_PRE_GOLIVE.md` con las secciones de abajo.
Todo lo que digas debe estar **respaldado por datos reales** (Alpaca API o
DBs locales), no por heurísticas.

### Reglas

- Escribí scripts de consulta en `scripts/audit/*.py` (si no existe,
  creá la carpeta). Nada se commitea a producción.
- Alpaca API: usá `.env.paper`. Endpoints
  `/v2/account`, `/v2/positions`, `/v2/orders`,
  `/v2/account/portfolio/history`, `/v2/account/activities`.
- Para joins entry↔exit usá **FIFO** sobre `filled_at`.
- Todos los % son sobre precio promedio de entrada para ese lote.
- Horarios en UTC. Si mostrás NY time, aclaralo.
- **NO cambies** `standalone_*.py`, `check_entry`, `check_exit`,
  `_calc_take_profit`, `size_position`, ni `ETF_UNIVERSE` / `CRYPTO_SYMBOLS`.
- Si tenés que correr un bot, **solo en `dry_run=True`**.

---

## SECCIONES OBLIGATORIAS DEL REPORTE

### 1. Reconciliación DB local ↔ Alpaca

Tabla `DB vs Alpaca` para cada símbolo que exista en alguno de los dos
lados. Columnas: `symbol, bot (RFTM/MREV), db_qty, alpaca_qty, db_entry,
alpaca_avg_entry, diff_qty, diff_pct_entry, verdict`. Verdict ∈
{IN_SYNC, QTY_DRIFT, ENTRY_DRIFT, ONLY_IN_DB, ONLY_IN_ALPACA}.

Explicá por qué esto pasa (hipótesis: GitHub Actions no pushea la DB).
Confirmalo o desmentilo leyendo `.github/workflows/daily_trade.yml` y
`mrev_hourly.yml` — mostrá los steps que hacen (o no hacen) commit+push
de `*.db`, y si suben las DBs como artifact.

### 2. Equity curve + drawdown real

Pegale a `/v2/account/portfolio/history?period=90D&timeframe=1D`.

- Tabla de 30 días más recientes: `date, equity_eod, daily_return_%, peak, dd_from_peak_%`.
- Identificá explícitamente:
  - Mayor drawdown intradiario en los últimos 90 días.
  - Días con daily return < −1.5%.
  - ¿Hubo efectivamente un −2% en un día? Si sí, cuándo y qué operaciones
    hubo ese día (cruzá con `/v2/orders` del día).
- Métricas: CAGR anualizado (asumiendo los días que lleva vivo),
  volatilidad anualizada, Sharpe ratio (rf=0), max drawdown, %
  días positivos.

### 3. Trade-by-trade P&L (últimos 60 días)

Reconstruí cada trade cerrado cruzando `/v2/orders?status=filled` con
FIFO. Guardá un CSV en `scripts/audit/trades_closed.csv` con columnas:
`symbol, bot, entry_dt, entry_price, exit_dt, exit_price, qty_closed,
gross_pnl_usd, gross_pnl_pct, holding_hours, exit_reason (si inferible)`.

Después:

- Top 10 winners / top 10 losers por %.
- Stats agregados **por bot** (RFTM vs MREV):
  win rate, avg winner %, avg loser %, expectancy, profit factor,
  trades count, trade promedio en USD.
- Stats agregados **por símbolo**.
- Distribución de holding time (mediana, p90).
- **¿El resultado agregado +7.5% es estadísticamente distinguible de
  "seguir al SPY"?** Compará P&L vs buy-and-hold de SPY desde la primera
  compra real (`/v2/orders` más vieja).

### 4. Forensic de la cascada 2026-04-22 16:18 UTC

Mostrá cronológicamente qué ejecutó el bot ese día:

- Logs de los runs de GitHub Actions (si los podés bajar con `gh run list`
  y `gh run view --log`). Si no hay `gh` auth, decilo y pedí al usuario
  que te dé los logs.
- Por cada venta del día, qué función del código la disparó:
  ¿fue `check_exit` (stop/TP)? ¿`_partial_take_profit_check`?
  ¿`migrate_legacy_etf_positions` (no debería vender, pero
  verificalo)? ¿Otra cosa?
- **XLE 438 full exit** — inexplicable a primera vista porque venía con
  entry $54.13 y Alpaca muestra haber salido a $56.34 (+4.1%), bajo TP1
  (+5%). Ergo no fue TP. ¿Trailing stop? ¿Time stop? ¿Señal de reversión?
  Mostrá el branch del código que ejecutó el exit.
- **DOGE/USD 113k full exit** — mismo análisis para MREV.
- Cruzá con `mrev_signals` (tabla en `mrev_paper.db`) del día.

### 5. El bug de LINK/USD (sell low, buy higher)

Hoy 23/04:

- 09:01 UTC: vendió 2136.45 @ $9.1618 (exit_reason?)
- 15:21 UTC: compró 1198.22 @ $9.4212 (+2.83% respecto al precio de venta)

Pregunta central: **¿esto es un bug, una feature del mean-reversion, o
mala señalización?**

Investigá:

- La razón de la salida a las 09:01 (STOP? TP2 con trailing? E7? Señal?).
- Entre 09:01 y 15:21, ¿qué indicadores cambiaron para justificar la
  re-entrada? Mirá `mrev_signals` y/o simulá `check_entry(LINK/USD)`
  con los datos horarios que estaban disponibles en esas ventanas
  (sin lookahead — usá solo velas cerradas hasta el momento de cada
  decisión).
- ¿Existe cooldown post-stop en MREV? Si sí, ¿por qué no lo respetó?
  Si no, proponé uno. (Recomendación: mínimo 24h después de un stop
  loss, o N barras horarias. **No implementes**, solo proponé en el reporte.)
- ¿Está entrando a **todo el capital disponible**? El rebuy fue de 1198
  vs 2136 originales: qty menor. Buen sizing o fue por cash limitado?

### 6. Revisión del código sensible (sin modificar)

Releé:

- `check_entry`, `check_exit` en ambos bots.
- `_calc_take_profit` y `_partial_take_profit_check`.
- `size_position` y cálculo de `stop_loss`.
- Cualquier "trailing" o "breakeven raise" (el CLAUDE.md menciona que
  TP1 sube el stop a breakeven).
- `sync_with_alpaca` de ambos — ¿puede este por sí solo borrar
  posiciones de la DB equivocadamente?

Para cada función, reportá:

- Condiciones exactas de entrada/salida (en pseudocódigo claro).
- Parámetros hardcodeados vs env vars.
- Edge cases preocupantes (división por cero, NaN en RSI, datos faltantes,
  gaps horarios en cripto 24/7 vs ETFs solo horario NY, etc.).

### 7. Workflows de GitHub Actions

Para `daily_trade.yml` y `mrev_hourly.yml`:

- ¿Qué cron? ¿Hay concurrency correcta?
- ¿Setean las env vars correctas (ALPACA_*, EMAIL_*)?
- ¿Levantan la DB de versiones anteriores? ¿La suben de nuevo?
  Si **no** persisten la DB, cada run arranca de DB vacía y eso explica
  el drift masivo con Alpaca (porque `sync_with_alpaca` intenta
  reconstruir todo en cada run).
- ¿Hay notificaciones de falla? ¿Suben logs como artifact?

### 8. Veredicto de go-live

En base a todo lo anterior, dame un **semáforo** en este formato:

```
GO-LIVE READINESS: [RED | YELLOW | GREEN]

BLOQUEANTES (deben arreglarse antes de plata real):
  - [lista]

RIESGOS ACEPTABLES PERO A MONITOREAR:
  - [lista]

EVIDENCIA DE RENTABILIDAD:
  - Es alpha real: [sí/no/inconclusivo] + razón
  - Win rate por bot
  - Sharpe
  - Comparación vs buy-and-hold SPY
  - Muestra suficiente: [sí/no — trades necesarios para significancia]
```

Si faltan trades o historia para concluir, decilo. No inventes certezas.

### 9. Recomendaciones priorizadas

Lista ordenada. Cada ítem con:

- Qué cambiar (archivo + función + línea si aplica).
- Por qué (evidencia concreta de los datos).
- Esfuerzo estimado (S/M/L).
- Si toca lógica de producción (`check_entry`, `check_exit`,
  `_calc_take_profit`, `size_position`, universos), **marcá que requiere
  aprobación explícita antes de implementar** — esas están protegidas
  por CLAUDE.md.

### 10. Anexos

- Dump JSON/CSV de órdenes de los últimos 60 días
  (`scripts/audit/orders_60d.json`).
- CSV de trades cerrados reconstruidos.
- Scripts de análisis que escribiste en `scripts/audit/`.
- Los valores exactos de las env vars de trading relevantes
  (MAX_DRAWDOWN, PARTIAL_TP1/2_*, MAX_LEVERAGE, MREV_RISK_PER_TRADE,
  MREV_MAX_POSITIONS), sin exponer credenciales.

---

## CHEQUEOS DE SANIDAD ANTES DE EMPEZAR

1. `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py`
2. `python3 -m pytest tests/ -x` → 0 fails. Si fallan, primer paso del reporte es por qué.
3. Confirmá que podés pegarle a `/v2/account` con un print del status y equity.

## LO QUE NO QUIERO

- Resúmenes vagos del estilo "el bot funciona bien en general".
- Numeritos sin fuente.
- Cambios a la lógica de trading sin mi aprobación.
- Borrar, modificar, o "limpiar" las DBs locales.
- "Mejoras" cosméticas fuera de scope.

## LO QUE SÍ QUIERO

- Datos crudos.
- Trazabilidad (cada afirmación apuntando al endpoint/tabla/línea de código).
- Un veredicto honesto sobre si estoy listo para plata real o no.
- Si no estoy listo, la lista mínima de fixes para estarlo.
