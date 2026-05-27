# CLAUDE.md — Notas de arquitectura para Claude Code

> **⚠️ IMPORTANTE (2026-05-24):** Hay un plan de mejoras estructurales pendiente en
> [`PLAN_V2.md`](./PLAN_V2.md). Si esta sesión es para implementar mejoras a MREV
> (timeframe 4H, nuevo universo cripto, filtro de régimen), **leer ese plan completo
> antes de tocar código**. Incluye PRE-WORK obligatorio de validación.
>
> El sistema viene de un fix crítico de micro-pérdidas (commits `4409888` y `3537efc`).
> Performance histórica previa: +11% en <2 meses. Cualquier cambio debe preservar lo
> que ya funciona.

## Arquitectura

Dos bots independientes sobre **una sola cuenta Alpaca Paper compartida** ($100K).
**Universos disjuntos** — cada bot opera sobre sus propios activos, nunca se cruzan:

- **RFTM** — `standalone_paper_trader.py`. Trend-following / breakout. Diario. **Solo ETFs** (universo en `ETF_UNIVERSE`, ~55 símbolos).
- **MREV** — `standalone_mrev_trader.py`. Mean-reversion. Cada 4h. **Solo cripto** (universo en `CRYPTO_SYMBOLS`: BTC, ETH, LINK, AAVE, UNI, DOT).

Servicios del stack viejo (`apps/svc_*`, `packages/shared`) existen pero los bots
productivos son los dos archivos `standalone_*.py`. El RUNBOOK.md habla del stack
viejo; los bots vivos están fuera de Docker.

## Puntos importantes

1. **Los dos bots comparten cuenta Alpaca.** Consumen del mismo `buying_power`.
   El primero que corre se come el cash. Pero **no comparten activos** (ver #2).
2. **Universos disjuntos** (desde 2026-04-22): RFTM solo opera ETFs; MREV solo
   opera cripto. Previamente compartían `SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO,
   ARKK` y MREV terminaba reclamando posiciones que había comprado RFTM (bug
   histórico). El split se enforza vía:
   - `ETF_SYMBOLS = []` en MREV (línea ~113).
   - `migrate_legacy_etf_positions()` al inicio de cada run cierra cualquier ETF
     que haya quedado atrapado en `mrev_positions`.
   - `sync_with_alpaca` de cada bot solo reclama símbolos de su propio universo.
3. **`partial_tp_taken` es un stage counter, NO un booleano**:
   - `0` = ninguna parcial ejecutada
   - `1` = TP1 (+5%) vendió 50% del qty original + stop subido a breakeven
   - `2` = TP2 (+7.5%) vendió otro 25% (= 50% del remanente)
   - `>2` no existe; la posición se cierra por E7 / trailing / time stop
4. **No hay bracket orders en Alpaca.** Todos los stops son software-side. Si el
   bot se cae, la posición queda desnuda.
5. **Scheduling: SOLO GitHub Actions.** El launchd local está deprecated desde
   2026-04-22 (com.rftm.trader.plist / setup_autorun.sh tienen banners).
   Nunca correr ambos al mismo tiempo.
6. **Breakeven raise post-TP1**: el stop sube a `entry_price` cuando dispara TP1.
   `seed_missing_positions.py` también aplica esto cuando detecta una posición
   existente con stage>=1 y stop bajo el entry (parche histórico).

## Convenciones del código

- Logging via `ok()` / `info()` / `warn()` / `err()` / `hdr()`. No agregar
  loggers nuevos — printf-style, uniformes entre bots.
- Errores de Alpaca no abortan el run — se logean como `warn` y el bot sigue.
- `dry_run=True` = simulación: no envía órdenes ni emails reales. El email
  mensual MREV además escribe `mrev_monthly_preview.html` en dry-run.
- Cambios de schema DB solo con `ALTER TABLE ... ADD COLUMN` envueltos en
  try/except idempotentes.
- Env vars con default hardcodeado — backward compat preservada en cada cambio.
- **Emails compartidos viven en `_email_helpers.py`**: `send_smtp`,
  `send_stage_event_email`, `build_css`, `position_card`.

## Env vars importantes

| Var | Default | Descripción |
|-----|---------|-------------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Credenciales (en `.env.paper`) |
| `ALPACA_BP_SAFETY` | `0.90` | Safety margin sobre buying power Alpaca |
| `MAX_DRAWDOWN` | `0.20` | Kill switch: bloquea entradas si equity cae más de 20% desde peak |
| `PARTIAL_TP1_PCT` / `_SELL_RATIO` | `0.05 / 0.50` | TP1: al +5% vende 50% |
| `PARTIAL_TP2_PCT` / `_SELL_RATIO` | `0.075 / 0.50` | TP2: al +7.5% vende 50% del remanente |
| `PARTIAL_MIN_NOTIONAL_USD` | `10.0` | Mínimo notional para que un parcial dispare (match min Alpaca cripto) |
| `EMAIL_ENABLED` | `true` | Envío de emails |
| `EMAIL_HOURS_UTC` | `12` | Ventana de envío del diario MREV (UTC) |
| `EMAIL_MONTHLY_ENABLED` | `true` | Habilita el reporte mensual de MREV |
| `EMAIL_MONTHLY_DAY` | `1` | Día del mes para el envío mensual |
| `MAX_LEVERAGE` (RFTM) | `1.5` | Cap de leverage del RFTM vs equity |
| `RFTM_DB_PATH` | `<script_dir>/trading_paper.db` | Override del path de la DB RFTM |
| `MREV_DB_PATH` | `<script_dir>/mrev_paper.db` | Override del path de la DB MREV |
| `RFTM_COOLDOWN_DAYS` | `5` | F1: días hábiles de cooldown post-exit RFTM |
| `RFTM_REENTRY_MAX_RUNUP` | `0.10` | F1: % máximo de runup permitido para re-entrar RFTM |
| `MREV_COOLDOWN_HOURS` | `6` | Horas de cooldown post-exit MREV |
| `MREV_REENTRY_MAX_RUNUP` | `0.10` | F1: % máximo de runup permitido para re-entrar MREV |
| `TRADE_EVENTS_JSONL_PATH` | `<script_dir>/logs/trade_events.jsonl` | F5.0: JSONL fuente de verdad de KAIZEN. En GHA se separa por bot (`_rftm.jsonl`/`_mrev.jsonl`) |
| `TRADE_EVENTS_DISABLE_SHEETS` | `0` | F5.0: skip del forward a Google Sheets |
| `KAIZEN_MISSED_PATH` | `<script_dir>/logs/kaizen_missed_moves.jsonl` | F1: JSONL de rebotes perdidos por cooldown de precio |
| `SHEETS_SPREADSHEET_ID` / `SHEETS_SERVICE_ACCOUNT_JSON` | — | Auth Service Account para Sheets espejo (ver `scripts/sheets/SETUP.md`) |

Equivalentes MREV-específicos: `MREV_INITIAL_CAPITAL`, `MREV_MAX_POSITIONS`,
`MREV_RISK_PER_TRADE`, `MREV_PARTIAL_TP{1,2}_PCT/RATIO`.

### DB paths — nota local macOS

El default de `RFTM_DB_PATH` es `<script_dir>/trading_paper.db` para que el
cache de GitHub Actions (que persiste `./trading_paper.db` desde cwd) levante
correctamente entre runs. En **macOS local**, si el repo vive en una carpeta
con FUSE / iCloud / mount de red que rompe SQLite WAL, exportá:

```bash
export RFTM_DB_PATH="$TMPDIR/rftm_trader/trading_paper.db"
export MREV_DB_PATH="$TMPDIR/mrev_trader/mrev_paper.db"
```

así la DB queda en disco local sin tocar el path por default que usa CI.

## Workflows activos

- `.github/workflows/daily_trade.yml` — RFTM entry bot, cron `35 13 * * 1-5`,
  `MODE=entry_only`.
- `.github/workflows/mrev_4h.yml` — MREV entry bot, cron `5 1,5,9,13,17,21 * * *`,
  `concurrency: mrev-4h`, `MODE=entry_only`.
- `.github/workflows/mrev_hourly.yml` — **DEPRECATED (V2-A).** Pendiente de borrar
  tras validar `mrev_4h.yml` verde.
- `.github/workflows/rftm_watchdog.yml` — RFTM watchdog, `workflow_dispatch`
  por ahora; schedule `*/5 13-20 * * 1-5` listo para habilitar.
- `.github/workflows/mrev_watchdog.yml` — MREV watchdog 24/7,
  `workflow_dispatch`; schedule `*/5 * * * *` listo para habilitar.

## Arquitectura post-watchdog (desde 2026-04-23)

Dos procesos por bot: **entry** (cron lento) + **watchdog** (cron rápido).

- **Entry bots** (`standalone_*.py` con `MODE=entry_only`): evalúan
  `check_entry`, sizing y buy. NO ejecutan exits.
- **Watchdogs** (`rftm_watchdog.py`, `mrev_watchdog.py`): corren cada 5m.
  Evalúan partial TPs (TP1 +5%→50%+breakeven; TP2 +7.5%→50% remanente),
  stop loss, trailing y time stop. Ejecutan las sells vía Alpaca con
  fill polling (timeout 10s, cancela si no llena).

Estado:

- **Alpaca = verdad operativa** (qty real, avg_entry, current_price).
- **DB local = estado de estrategia** (`stage`, `highest_since_entry`,
  `stop_loss`, `entry_dt`). Cada proceso corre `sync_with_alpaca()` al
  arranque.

Cooldowns post-exit (F1, desde 2026-05-15):

- **Tabla** `rftm_cooldowns` / `mrev_cooldowns` con schema
  `(symbol, last_exit_dt, last_exit_price, reason)`. El módulo
  compartido `_cooldowns.py` maneja create/ALTER idempotente y la
  lógica de decisión.
- **Doble chequeo**: al evaluar una entry (después de `check_entry`,
  antes de sizing):
  1. **Temporal**: bloquea si el último exit fue hace menos de
     `RFTM_COOLDOWN_DAYS` días hábiles (RFTM) o `MREV_COOLDOWN_HOURS`
     horas (MREV).
  2. **Precio**: aunque expire el temporal, bloquea si el precio
     actual está más de `*_REENTRY_MAX_RUNUP` arriba del `last_exit_price`
     (default 10% en ambos bots).
- **Quién registra**: el watchdog escribe el cooldown SOLO cuando el
  exit es `E3_stop_loss` / `E5_*` / `E6_time_stop` (RFTM) o
  `stop_loss` / `trailing_stop` / `time_stop` (MREV). NUNCA después
  de TPs — re-entrar tras ganancia sigue siendo válido.
- **Post-mortem**: cuando el cooldown de precio bloquea una entrada,
  `_kaizen_missed.log_missed_move()` deja una línea en
  `logs/kaizen_missed_moves.jsonl` con runup, días, indicadores
  actuales y un `catalyst_proxy` heurístico (vol_ratio > 2x). KAIZEN
  consume este JSONL semanalmente para detectar qué rebotes valen la
  pena perseguir con otra estrategia.

Trade event logging (F5.0, desde 2026-05-15):

- `_trade_logger.log_trade_event(...)` es el único punto de log de
  eventos de trade. Persiste **siempre** a JSONL local (fuente de
  verdad para KAIZEN) y **best effort** a Google Sheets (espejo
  conveniente).
- Los bots ahora importan `from _trade_logger import log_trade_event`
  en vez de `from _sheets_logger import ...`. La firma es la misma —
  reemplazo drop-in.
- Path overrideable vía `TRADE_EVENTS_JSONL_PATH`. En GHA cada
  workflow setea su path (`logs/trade_events_rftm.jsonl` /
  `logs/trade_events_mrev.jsonl`) y los cachea entre runs (key
  `rftm-events-v1` / `mrev-events-v1`).
- Setup de Sheets: ver `scripts/sheets/SETUP.md` (Service Account,
  no Webhook).

Health check:

- `_db_health.assert_db_health()` corre al inicio de ambos bots y ambos
  watchdogs. Chequea `integrity_check`, presencia de columnas, y cierra
  runs viejos con status=RUNNING/running si hay más de uno.

Fixes P0 aplicados 2026-04-23:

- **A**: `INSERT INTO mrev_positions` tenía 12 placeholders, la tabla
  tiene 15 columnas. Ahora usa columnas explícitas.
- **B**: `except Exception` en el buy-loop MREV se tragaba errores de
  DB. Ahora atrapa `sqlite3.Error`, cancela la order y `SystemExit(2)`.
- **C**: `daily_trade.yml` persiste `trading_paper.db` via
  `actions/cache` (antes cada run arrancaba con DB vacía).
- **D**: `_db_health.py` + `assert_db_health()` se cablea en ambos bots
  y aborta temprano si la DB está rota.

## Módulos auxiliares (desde 2026-05-15)

| Módulo | Qué hace |
|---|---|
| `_trade_logger.py` | Wrapper del logger: JSONL siempre + Sheets best-effort |
| `_sheets_logger.py` | Google Sheets via Service Account (no Webhook) |
| `_cooldowns.py` | Tabla + chequeo temporal/precio (F1) |
| `_kaizen_missed.py` | Post-mortem JSONL de rebotes perdidos |
| `_kaizen_enrichment.py` | Indicadores + régimen + execution para eventos (F5.1) |
| `_kaizen_rules.py` | Load/auto-activate/evaluate reglas (F5.3/F5.4) |
| `_kaizen_overrides.py` | Param overrides (F5.5) — siempre manual approval |
| `_shadow_trades.py` | Simulación de trades bloqueados (F6.1) |
| `_kaizen_monthly_metrics.py` | Snapshot mensual de métricas (F6.5) |
| `_watchdog_health.py` | HealthReport + email + JSONL (F3.2) |
| `_bracket_orders.py` | Safety SELL STOP en Alpaca (F3.1, feature flag) |
| `_regime_filter.py` | Filtro de régimen C7: BTC macro + ADX (V2-C) |
| `_exit_logic.py` | TPs + `recalc_stop_for_stage` (F3.3) |
| `_db_health.py` | DB integrity check + close stale runs |
| `_email_helpers.py` | `send_smtp` compartido |

Scripts:
- `scripts/state_db_push.sh` / `scripts/sync_db.sh` — F2 (CI fuente de verdad)
- `scripts/kaizen_review.py` — F5.2 (Claude semanal)
- `scripts/shadow_tick.py` — F6.1 (tick diario)
- `scripts/kaizen_monthly_report.py` — F6.2 (email mensual)
- `scripts/kaizen_decision.py` + `scripts/kaizen_decision_email.py` — F6.4

## Rituales de seguridad antes de tocar código

1. `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py _trade_logger.py _cooldowns.py _kaizen_missed.py _kaizen_enrichment.py _kaizen_rules.py _kaizen_overrides.py _shadow_trades.py _kaizen_monthly_metrics.py _watchdog_health.py _bracket_orders.py _exit_logic.py _regime_filter.py`
2. `python3 -m unittest tests.test_trade_logger tests.test_cooldowns tests.test_stop_recalc tests.test_watchdog_health tests.test_bracket_orders tests.test_kaizen_enrichment tests.test_kaizen_review tests.test_kaizen_rules tests.test_kaizen_overrides tests.test_shadow_trades tests.test_kaizen_monthly_metrics` — esperado 141/141 OK.
3. `python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev tests/test_watchdog tests/test_exit_logic.py tests/test_db_health.py tests/test_db_schema.py tests/test_universes_disjoint.py tests/test_mode_entry_only.py` — esperado 0 fails. (Nota: `test_indicators_1h.py` fue renombrado a `test_indicators_4h.py` en V2-A.)
4. `python3 scripts/ops/preflight.py` — exit 0 (excepto checks de red si estás offline).
5. No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
6. `.env.paper` nunca se imprime ni se commitea (está en `.gitignore`).
7. Cambios en `check_entry`, `check_exit`, `_calc_take_profit`, `size_position`
   requieren preguntar antes — cambian el comportamiento del bot en producción.
   El refactor a funciones puras (`_exit_logic.evaluate_partial_tp` /
   `recalc_stop_for_stage`) es aditivo — los bots siguen con su lógica inline,
   el watchdog consume la versión pura.
8. La capa C6 KAIZEN en check_entry (F5.4) está FUERA de `check_entry()` — es
   un filtro adicional después que solo rechaza más cosas, nunca afloja.
   Idem para el cooldown F1. Esto NO es violación del invariante #7.
9. Param overrides (`PARTIAL_TP1_PCT`, `ATR_MULT`, etc.) NUNCA se auto-aplican.
   KAIZEN puede *proponer* cambios pero requieren aprobación manual via
   `kaizen_decision.yml`. Las reglas de bloqueo SÍ pueden auto-aplicarse si
   cumplen los criterios estrictos de F5.3.
10. Stop loss solo SUBE, nunca baja. Enforced en `recalc_stop_for_stage` y
    en el watchdog cuando dispara TP1.
