# CLAUDE.md — Notas de arquitectura para Claude Code

## Arquitectura

Dos bots independientes sobre **una sola cuenta Alpaca Paper compartida** ($100K).
**Universos disjuntos** — cada bot opera sobre sus propios activos, nunca se cruzan:

- **RFTM** — `standalone_paper_trader.py`. Trend-following / breakout. Diario. **Solo ETFs** (universo en `ETF_UNIVERSE`, ~55 símbolos).
- **MREV** — `standalone_mrev_trader.py`. Mean-reversion. Horario. **Solo cripto** (universo en `CRYPTO_SYMBOLS`: BTC, ETH, SOL, AVAX, DOGE, LINK).

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
- `.github/workflows/mrev_hourly.yml` — MREV entry bot, cron `5 * * * *`,
  `concurrency: mrev-hourly`, `MODE=entry_only`.
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

Cooldown MREV:

- Al cerrar por `stop_loss` / `trailing_stop` / `time_stop`, el watchdog
  escribe `mrev_cooldowns`. El entry bot rechaza re-entradas en el
  símbolo por `MREV_COOLDOWN_HOURS` (default 6h). TPs no registran
  cooldown — re-entrar tras ganancia sigue siendo válido.

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

## Rituales de seguridad antes de tocar código

1. `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py`
2. `python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev tests/test_watchdog tests/test_exit_logic.py tests/test_db_health.py tests/test_db_schema.py tests/test_universes_disjoint.py tests/test_mode_entry_only.py` — esperado 0 fails.
3. `python3 scripts/ops/preflight.py` — exit 0.
4. No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
5. `.env.paper` nunca se imprime ni se commitea (está en `.gitignore`).
6. Cambios en `check_entry`, `check_exit`, `_calc_take_profit`, `size_position`
   requieren preguntar antes — cambian el comportamiento del bot en producción.
   El refactor a funciones puras (`_exit_logic.evaluate_partial_tp`) es aditivo
   — los bots siguen con su lógica inline, el watchdog consume la versión pura.
