# CLAUDE.md — Notas de arquitectura para Claude Code

## Arquitectura

Dos bots independientes sobre **una sola cuenta Alpaca Paper compartida** ($100K):

- **RFTM** — `standalone_paper_trader.py`. Trend-following / breakout. Diario. ETFs.
- **MREV** — `standalone_mrev_trader.py`. Mean-reversion. Horario. Cripto + algunos ETFs.

Servicios del stack viejo (`apps/svc_*`, `packages/shared`) existen pero los bots
productivos son los dos archivos `standalone_*.py`. El RUNBOOK.md habla del stack
viejo; los bots vivos están fuera de Docker.

## Puntos importantes

1. **Los dos bots comparten cuenta Alpaca.** Consumen del mismo `buying_power`.
   El primero que corre se come el cash.
2. **Los universos se solapan**: `SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO, ARKK`
   están en ambos. RFTM y MREV pueden abrir el mismo símbolo simultáneamente —
   cada bot solo ve su propia DB.
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

Equivalentes MREV-específicos: `MREV_INITIAL_CAPITAL`, `MREV_MAX_POSITIONS`,
`MREV_RISK_PER_TRADE`, `MREV_PARTIAL_TP{1,2}_PCT/RATIO`.

## Workflows activos

- `.github/workflows/daily_trade.yml` — RFTM, cron `35 13 * * 1-5`.
- `.github/workflows/mrev_hourly.yml` — MREV, cron `5 * * * *`,
  `concurrency: mrev-hourly`.

## Rituales de seguridad antes de tocar código

1. `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py`
2. `python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev` — esperado 0 fails.
3. No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
4. `.env.paper` nunca se imprime ni se commitea (está en `.gitignore`).
5. Cambios en `check_entry`, `check_exit`, `_calc_take_profit`, `size_position`
   requieren preguntar antes — cambian el comportamiento del bot en producción.
