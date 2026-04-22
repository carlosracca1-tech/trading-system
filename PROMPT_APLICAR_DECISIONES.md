# Prompt para Claude Code — Aplicar decisiones post-auditoría

Copiá **todo lo que está debajo de la línea `---`** y pegalo como prompt en Claude Code dentro de `/Users/charlie/Desktop/trading-system`. No edites el texto.

---

## Contexto

Seguimos trabajando sobre el trading system en `/Users/charlie/Desktop/trading-system`. Recién se hizo una auditoría que identificó 10 items a resolver. El usuario ya tomó las decisiones. Tu trabajo es **ejecutar todo en orden, con guardrails estrictos, sin romper lo que ya está andando**.

**Contexto previo que NO podés ignorar:**

- Dos bots: `standalone_paper_trader.py` (RFTM, diario, ETFs) y `standalone_mrev_trader.py` (MREV, 1h, cripto + algunos ETFs).
- DBs: `trading_paper.db` (RFTM) y `mrev_paper.db` (MREV).
- La columna `partial_tp_taken` es un **stage counter** (0, 1, 2), NO un booleano.
- Partial TP en 2 etapas (5% vende 50%, 7.5% vende 50% del remanente) ya está implementado.
- `E7_take_profit` (exit final a +10%, gateado por stage≥2) ya está.
- Breakeven raise post-TP1 ya está (pero sólo en el flujo post-fill real, no en el seed).
- `sync_with_alpaca` en ambos bots ya reconcilia posiciones.
- El informe de auditoría está en `AUDIT_REPORT_20260422.md`. Leelo antes de empezar — tenés todo el mapa ahí.

## Decisiones del usuario (las 10 respuestas)

| # | Tema | Decisión |
|---|------|----------|
| 1 | Scheduling | **Solo GitHub Actions.** Deshabilitar launchd local. |
| 2 | `seed_missing_positions.py` | **Correrlo en modo real.** |
| 3 | Tests drift (23 fails) | **Borrar los tests obsoletos.** Los nuevos se escriben después. |
| 4 | Email mensual MREV | **Reescribir completo**, consistente con el resto del sistema, datos reales, largo, legible en dark/light y mobile/desktop. |
| 5 | Buffer `0.90` hardcoded | **Extraer a env var** (`ALPACA_BP_SAFETY`, default `0.90`). |
| 6 | `MAX_DRAWDOWN=0.20` | **Extraer a env var** (default `0.20`). |
| 7 | Partial TP cripto fracciones chicas | **Mantener la lógica 50% / 50%-del-remanente** pero agregar **guard de notional mínimo $10** (= mínimo de Alpaca para cripto) para que no falle la orden. Si el 50% queda por debajo de $10 → skip parcial y esperar al exit final. |
| 8 | Archivo `bb_lower=9.20` (0 bytes) | **Borrarlo.** Es basura de una redirección accidental. |
| 9 | `CLAUDE.md` con notas de arquitectura | **Crearlo** y dejarlo como referencia viva. |
| 10 | Refactor a `_email_helpers.py` | **Hacerlo.** Conviene antes de tocar el mail mensual para no duplicar más. |

---

## Fase 0 · Preparación (leé antes de tocar nada)

1. Abrí y releé:
   - `AUDIT_REPORT_20260422.md` — tenés el mapa completo.
   - `standalone_paper_trader.py` secciones: 60-173 (env), 320-480 (indicadores y check_exit), 1442 (`send_stage_event_email`), 1600-1646 (partial TP), 1817-1870 (call-sites de emails stage), 1893-1915 (breakeven raise).
   - `standalone_mrev_trader.py` secciones: 85-115 (env), 297-346 (signals), 852-1122 (email mensual — acá va el reescribe grande), 1264-1317 (partial TP), 1395-1432 (breakeven raise).
   - `seed_missing_positions.py` entero.
2. Corré `python3 -m py_compile` sobre los 3 archivos principales, para tener baseline "todo compila antes de empezar".
3. Hacé `git status` — debería estar limpio. Si hay cambios sin commitear, preguntale al usuario qué hacer antes de seguir.

## Fase 1 · Quick wins (bajo riesgo, commit por item)

### 1.1 Borrar `bb_lower=9.20` (item 8)

Archivo de 0 bytes en root, creado por una redirección accidental de shell. Confirmá que sigue siendo 0 bytes y borralo.

```bash
test -f bb_lower=9.20 && [ ! -s bb_lower=9.20 ] && rm -v bb_lower=9.20
```

Commit: `chore: remove accidental empty file bb_lower=9.20`.

### 1.2 Deshabilitar launchd local — solo GitHub Actions (item 1)

**NO borres los archivos** `com.rftm.trader.plist` ni `setup_autorun.sh` — el usuario puede querer reusarlos en el futuro. Solo agregá un comentario en la cabecera de `com.rftm.trader.plist` y en `setup_autorun.sh` que diga:

```
# DEPRECATED 2026-04-22: this bot now runs via GitHub Actions (.github/workflows/daily_trade.yml).
# Do NOT run this locally — it causes double execution.
# To uninstall any existing launchd agent, run:
#     launchctl unload ~/Library/LaunchAgents/com.rftm.trader.plist
#     rm ~/Library/LaunchAgents/com.rftm.trader.plist
```

Agregá un bloque equivalente en `RUNBOOK.md` sección "Scheduling" (o creala si no existe): dejá claro que el único scheduler activo es GitHub Actions y que el local debe estar apagado.

**Chequeo**: si `~/Library/LaunchAgents/com.rftm.trader.plist` existe en la máquina del usuario, **no lo toques** — el usuario lo va a desinstalar manualmente. Solo dejá el warning en el repo.

Commit: `docs(scheduler): mark launchd as deprecated, only GitHub Actions now`.

### 1.3 Borrar tests obsoletos (item 3)

Los 23 tests que fallan prueban lógica removida. Listá primero los archivos afectados y los tests específicos:

```bash
python3 -m pytest tests/test_strategy.py tests/test_mrev -v 2>&1 | grep -E "FAIL|PASS" > /tmp/test_report.txt
```

Borrá **únicamente** las funciones test que fallan. Buscá por nombre (ejemplos conocidos):

- `TestCheckExitSignal::test_death_cross_triggers_exit` — E1 removida
- `TestCheckExitSignal::test_close_below_ema50_triggers_exit` — E2 removida
- `TestCheckExitSignal::test_rsi_overbought_triggers_exit` — E4 removida
- `TestMrevExitSignal::test_rsi_normalized_exit` — X3 removida
- `TestMrevExitSignal::test_time_stop_after_24_bars` — cambió a 120h
- `TestCheckEntrySignal::test_low_volume_returns_hold` — umbral cambió

Para cada test que falla, abrí el archivo, confirmá que testea lógica que ya no existe (miralo en el código del bot), y borralo. **No borres tests que pasan ni tests que fallan por otra razón** (ej. import error por SQLAlchemy mismatch — esos no los tocás).

Al terminar, corré:

```bash
python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev -v
```

Esperado: 0 fails, todos passed o skipped.

Commit: `test: remove tests for removed logic (E1/E2/E4/X3, old time-stop)`.

### 1.4 Env var `ALPACA_BP_SAFETY` (item 5)

El valor `0.90` aparece hardcodeado 5 veces:

- `standalone_paper_trader.py` líneas 1660, 1691, 1997
- `standalone_mrev_trader.py` líneas 1348, 1513

Agregá en ambos archivos:

```python
ALPACA_BP_SAFETY = float(os.environ.get("ALPACA_BP_SAFETY", "0.90"))
```

Reemplazá cada `0.90` en contexto de buying power por `ALPACA_BP_SAFETY`. **Solo en esos contextos** — no hagas find-replace ciego. Verificá con `grep -n "0.90" standalone_*.py` que no te comiste otro `0.90` que no tenía que ver.

Documentá en el bloque de env vars al tope: `ALPACA_BP_SAFETY: safety margin over Alpaca buying power (default 0.90 = use at most 90% of reported BP)`.

Actualizá `.env.example` y `.env.paper.example` si existen.

Commit: `refactor: extract hardcoded BP safety factor to ALPACA_BP_SAFETY env var`.

### 1.5 Env var `MAX_DRAWDOWN` (item 6)

**Contexto** (para dejar en un comentario cerca de la var): el `MAX_DRAWDOWN` es el **kill switch** del bot. Si el equity cae más de X% desde su peak histórico, el bot deja de abrir posiciones nuevas (no cierra las abiertas — sólo frena entradas). Es una red de seguridad para que una mala racha no abra trades nuevos encima.

Default 20% se queda.

- `standalone_paper_trader.py` línea 146: `MAX_DRAWDOWN = 0.20` → `MAX_DRAWDOWN = float(os.environ.get("MAX_DRAWDOWN", "0.20"))`.
- `standalone_mrev_trader.py` línea ~1235: mismo cambio. Confirmá el número de línea con `grep -n "MAX_DRAWDOWN\|drawdown >= 0.20" standalone_mrev_trader.py`.

Documentalo en el header de env vars con la explicación de arriba.

Commit: `refactor: extract MAX_DRAWDOWN kill switch to env var (default 0.20)`.

## Fase 2 · Mejora del partial TP cripto (item 7)

**Objetivo**: preservar la lógica 50% / 50%-del-remanente pero evitar órdenes que Alpaca va a rechazar por ser demasiado chicas.

Alpaca tiene un **mínimo de notional de $10 para órdenes de cripto**. Si el parcial queda por debajo de $10, no hay que disparar la orden — dejá que el 100% corra hasta el siguiente trigger (o hasta el exit final).

En `standalone_mrev_trader.py`, dentro del bloque del partial TP (líneas 1264-1317 aprox.), agregá este guard:

```python
PARTIAL_MIN_NOTIONAL_USD = float(os.environ.get("PARTIAL_MIN_NOTIONAL_USD", "10.0"))

# Dentro del cálculo del partial:
notional = sell_qty * cur_close
if notional < PARTIAL_MIN_NOTIONAL_USD:
    info(f"PARTIAL_TP skipped for {sym}: notional ${notional:.2f} < min ${PARTIAL_MIN_NOTIONAL_USD:.2f}")
    continue  # no tomar este parcial, seguir holdeando
```

**Importante**: este guard va **después** de calcular `sell_qty` y **antes** de enqueue al signals/sells. No cambia la lógica del 50/50 — solo evita los parciales micro.

Agregá el mismo guard en RFTM también (línea ~1605), aunque en RFTM el qty es entero y el MIN_SHARES=1 ya protege parcialmente. Ahí el mínimo notional sería irrelevante para ETFs caros pero útil para ETFs baratos (ej. SLV $70 × 1 acción = $70, ya está arriba de $10). Dejalo por consistencia — no interfiere.

Documentá la env var nueva `PARTIAL_MIN_NOTIONAL_USD` en el header.

Commit: `feat(mrev): skip partial TP when notional < $10 (Alpaca crypto min)`.

## Fase 3 · Refactor a `_email_helpers.py` (item 10) — antes del reescribe del mensual

**Por qué primero**: el reescribe del email mensual (fase 4) sería duplicar aún más si no extraemos antes. Hacelo ahora.

Creá `_email_helpers.py` en la raíz del repo con:

```python
"""
Shared email helpers for RFTM and MREV bots.

Uso:
    from _email_helpers import build_css, position_card, send_stage_event_email, send_email

Contrato:
    - Compatible con dark mode y light mode (usa @media prefers-color-scheme).
    - Responsive hasta 380px de ancho (smartphones).
    - Sin dependencias externas fuera de stdlib + smtplib.
"""
```

Extraé:

- **`build_css()`**: devuelve el string de CSS con dark/light mode vía `@media (prefers-color-scheme: dark)` y breakpoint mobile. Hoy el CSS está duplicado textual entre RFTM y MREV.
- **`position_card(pos, current_price, stage_info) -> str`**: renderea el item de "Lo que tengo en cartera" con los 3 cuadrados SL / Precio / TP + línea "Stage X · faltan Y% para TPZ a $W". Recibe dict con todo lo necesario, devuelve HTML.
- **`send_email(subject, html_body, dry_run, config)`**: helper SMTP que ya existe en ambos bots, unificado. Respeta `dry_run` — en dry-run solo loguea y no envía.
- **`send_stage_event_email(symbol, stage, entry, sell_price, qty_sold, remaining_qty, next_target_pct, next_target_price, dry_run, config)`**: email breve por evento de TP1/TP2/E7. Subject conciso, body de 6-8 líneas útiles.

Reemplazá las implementaciones dup en ambos bots con `from _email_helpers import …`.

**Reglas del extract**:

1. **No cambies el output HTML visible** en este commit. Solo moves el código. Si el email se veía igual antes, debe verse igual después.
2. **Preservá todos los call-sites**: cualquier función pública usada desde otro archivo tiene que seguir existiendo en el mismo lugar, o ser un re-export.
3. **Testeá**: generá previews HTML antes y después del refactor y compará (diff) — solo deberían diferir en whitespace.

Commit: `refactor: extract shared email helpers to _email_helpers.py`.

## Fase 4 · Reescribir email mensual MREV (item 4)

**Contexto**: `_build_monthly_email_report` (línea ~852 en `standalone_mrev_trader.py`) y su companion `send_monthly_email_report` hoy mezclan datos RFTM y MREV. El usuario quiere un email:

- **Consistente** con el resto del sistema (mismos estilos, misma estructura conceptual que el daily pero más detallado).
- **Datos reales de todo**: equity, P&L realizado y no realizado, número de trades, win rate, profit factor, max drawdown del mes, sharpe aproximado, breakdown por símbolo, top winners / losers, comportamiento del kill switch, tiempo promedio en posición, hit rate de cada stage del partial TP, etc.
- **Puede ser largo** — es mensual, se lee una vez al mes.
- **Dark mode y light mode** (usá `@media (prefers-color-scheme: dark)` del `build_css()` de la fase 3).
- **Mobile y desktop**: responsive, legible desde 380px.

**Secciones propuestas** (ordenadas):

1. **Hero**: equity al cierre del mes · P&L mensual absoluto y % · max drawdown del mes.
2. **KPIs mes**: trades totales · win rate · profit factor · avg hold time · % de posiciones que tocaron TP1 / TP2 / E7 / stop-loss / trailing / time-stop.
3. **Top 5 winners / Top 5 losers** del mes (símbolo, P&L $, % return).
4. **Breakdown por símbolo**: tabla con cada símbolo operado en el mes, # trades, P&L total, avg R:R.
5. **Equity curve** (ASCII sparkline o texto breve tipo `[████▄▂▂▄███] +$2,347 este mes`).
6. **Comportamiento del sistema**:
   - Partial TP stats: cuántos TP1 firearon, cuántos TP2, cuántos E7.
   - Días con kill switch activo (si los hubo).
   - Rechazos de Alpaca (si hubo).
7. **Próximo mes**: posiciones aún abiertas al cierre, con stage actual y distancia al próximo target.

**Datos a extraer** (de `mrev_paper.db`, tablas `mrev_positions` + `mrev_snapshots` + `mrev_hourly_log` + `mrev_signals`):

- P&L realizado del mes: `SUM(pnl) WHERE exit_dt BETWEEN start_of_month AND end_of_month`.
- Equity start/end: primera y última fila de `mrev_snapshots` del mes.
- Trades: count de `mrev_positions` con `entry_dt` o `exit_dt` en el mes.
- Win rate: `count WHERE pnl > 0 / count total`.
- Profit factor: `sum(pnl WHERE pnl>0) / abs(sum(pnl WHERE pnl<0))`.
- Hit rate por stage: contar cuántas posiciones del mes llegaron a stage=1, stage=2, y cuántas cerraron por E7 (exit_reason contiene "take_profit" o "partial_tp").

**IMPORTANTE**: que sea **100% MREV**. Nada de "Tus 2 robots". Nada de `ACCOUNT_TOTAL_CAPITAL`. Nada de `DAILY_BOT_CAPITAL`. Si necesitás el total de la cuenta Alpaca para mostrar contexto ("tu MREV es el X% del portfolio total"), traelo desde `alpaca_get_account()` y mostralo en **una línea chica** de contexto, no como figura central.

Si algún dato del mes no está disponible en la DB (ej. no hay snapshots), **no inventes** — mostrá `—` o "sin datos".

Dry-run respect: si se corre con `dry_run=True`, generá el HTML en `mrev_monthly_preview.html` pero no lo envíes.

Commit: `feat(mrev): rewrite monthly email with MREV-only data, dark/light, responsive`.

## Fase 5 · Correr `seed_missing_positions.py` en real (item 2)

**Contexto** (lo que el usuario no entendía):

Alpaca tiene posiciones abiertas (las que ves en tu portfolio). Nuestro bot las trackea en una DB local (`trading_paper.db` para ETFs, `mrev_paper.db` para cripto). Cuando hubo algún sync roto en el pasado, quedaron posiciones **en la DB equivocada** o **sin aparecer en ninguna DB local**. Sin tracking local, el bot no puede disparar partial TPs, trailing stops, ni nada — ni sabe que existen.

`seed_missing_positions.py` hace 3 cosas:

1. **Migra cripto atrapada en trading_paper.db** → la cierra ahí (`status='closed', close_reason='migrated_to_mrev'`) y la inserta en `mrev_paper.db`. Hoy SOLUSD está en la DB equivocada.
2. **Inserta posiciones que Alpaca tiene pero ninguna DB local tracea**. Hoy son GLD, SLV, y las cripto AVAX/DOGE/LINK si están abiertas.
3. **Hace todas estas inserciones con `stage=0` e `initial_qty=qty_actual`**, así el bot las evalúa para partial TPs a partir del próximo run.

Lo corremos en modo real. Pasos:

```bash
# 1. Preview primero (obligatorio)
python3 seed_missing_positions.py --dry-run

# 2. Confirmar que el output tiene sentido:
#    - SOLUSD debe aparecer como "MARK_CLOSED (era cripto, va a MREV)"
#    - GLD/SLV deben aparecer como "RFTM INSERT stage=0"
#    - Cripto (AVAX/DOGE/LINK si están) deben aparecer como "MREV INSERT stage=0"
#    - Ninguna otra posición debería ser tocada

# 3. Aplicar
python3 seed_missing_positions.py
```

**Antes de correr sin `--dry-run`**: guardá un backup de ambas DBs:

```bash
cp trading_paper.db trading_paper.db.bak-$(date +%Y%m%d-%H%M%S)
cp mrev_paper.db mrev_paper.db.bak-$(date +%Y%m%d-%H%M%S)
```

**Después de correr**: verificá con queries SQL:

```sql
-- Confirma que SOLUSD ya no está abierto en RFTM
SELECT * FROM positions WHERE symbol='SOLUSD' AND status='open';  -- esperado: 0 filas

-- Confirma que está abierto en MREV
-- (conectá a mrev_paper.db)
SELECT * FROM mrev_positions WHERE symbol LIKE 'SOL%' AND status='OPEN';  -- esperado: 1 fila, stage=0

-- Confirma GLD/SLV insertadas en RFTM
SELECT symbol, stage AS partial_tp_taken, initial_qty, entry_price FROM positions WHERE symbol IN ('GLD','SLV') AND status='open';
```

**Mejora del seed para este item**: además de insertar con stage=0, cuando el seed encuentra una posición existente que ya tiene stage≥1 **y el stop_loss está por debajo del entry_price**, **subir el stop a breakeven** (`stop_loss = max(stop_loss_actual, entry_price)`). Esto resuelve el hallazgo del audit de que las 11 posiciones actuales tienen stage=1 pero stops viejos (el seed las sembró antes de que existiera el breakeven raise). Documentá con `info()` log.

Commit: `feat(seed): raise stop to breakeven when upserting positions at stage>=1`.

Corré el seed en real solo después de esto.

Commit: `chore(db): reconcile positions via seed_missing_positions.py real run`. Incluí en el mensaje el output del script.

## Fase 6 · `CLAUDE.md` (item 9)

Creá `/Users/charlie/Desktop/trading-system/CLAUDE.md` con estas secciones (si ya existe, mergealo sin duplicar):

```markdown
# CLAUDE.md — Notas de arquitectura para Claude Code

## Arquitectura

Dos bots independientes sobre **una sola cuenta Alpaca Paper compartida** ($100K):

- **RFTM** — `standalone_paper_trader.py`. Trend-following / breakout. Diario. ETFs.
- **MREV** — `standalone_mrev_trader.py`. Mean-reversion. Horario. Cripto + algunos ETFs.

## Puntos importantes

1. **Los dos bots comparten cuenta Alpaca.** Consumen del mismo `buying_power`. El primero que corre se come el cash.
2. **Los universos se solapan**: `SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO, ARKK` están en ambos. RFTM y MREV pueden abrir el mismo símbolo simultáneamente — cada bot solo ve su propia DB.
3. **`partial_tp_taken` es un stage counter, NO un booleano**:
   - `0` = ninguna parcial ejecutada
   - `1` = TP1 (+5%) vendió 50% del qty original + stop subido a breakeven
   - `2` = TP2 (+7.5%) vendió otro 25% (= 50% del remanente)
   - `>2` no existe; la posición se cierra por E7 / trailing / time stop
4. **No hay bracket orders en Alpaca.** Todos los stops son software-side. Si el bot se cae, la posición queda desnuda.
5. **Scheduling: SOLO GitHub Actions.** El launchd local está deprecated. Nunca correr ambos al mismo tiempo.

## Convenciones del código

- Logging via `ok()` / `info()` / `warn()` / `err()` / `hdr()`. No agregar loggers nuevos.
- Errores de Alpaca no abortan el run — se logean como `warn`.
- `dry_run` = simulación: no envía órdenes ni emails.
- Cambios de schema DB solo con `ALTER TABLE ... ADD COLUMN` envueltos en try/except.
- Env vars con default hardcodeado — backward compat preservada.

## Env vars importantes

| Var | Default | Descripción |
|-----|---------|-------------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Credenciales (en `.env.paper`) |
| `ALPACA_BP_SAFETY` | `0.90` | Safety margin sobre buying power Alpaca |
| `MAX_DRAWDOWN` | `0.20` | Kill switch: bloquea entradas si equity cae más de 20% desde peak |
| `PARTIAL_TP1_PCT` / `_SELL_RATIO` | `0.05 / 0.50` | TP1: al +5% vende 50% |
| `PARTIAL_TP2_PCT` / `_SELL_RATIO` | `0.075 / 0.50` | TP2: al +7.5% vende 50% del remanente |
| `PARTIAL_MIN_NOTIONAL_USD` | `10.0` | Mínimo notional para que un parcial dispare (match min de Alpaca cripto) |
| `EMAIL_ENABLED` | `true` | Envío de emails |
| `EMAIL_HOURS_UTC` | `12` | Ventana de envío del diario MREV (UTC) |
| `EMAIL_MONTHLY_ENABLED` | `true` | Habilita el reporte mensual de MREV |

## Workflows activos

- `.github/workflows/daily_trade.yml` — RFTM, cron `35 13 * * 1-5`.
- `.github/workflows/mrev_hourly.yml` — MREV, cron `5 * * * *`.

## Rituales de seguridad antes de tocar código

1. `python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py _email_helpers.py seed_missing_positions.py`
2. `python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev` — esperado 0 fails.
3. No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
4. `.env.paper` nunca se imprime ni se commitea.
```

Commit: `docs: add CLAUDE.md with architecture notes and conventions`.

## Fase 7 · Verificación final

Corré esto y reportá output:

```bash
# Todo compila
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
                      _email_helpers.py seed_missing_positions.py \
                      mark_partial_tp_done.py sell_half_profits.py analyze_trades.py

# Tests pasan
python3 -m pytest tests/test_indicators.py tests/test_strategy.py \
                  tests/test_health.py tests/test_mrev -v

# DB consistente
python3 -c "
import sqlite3
for db, tbl in [('trading_paper.db','positions'), ('mrev_paper.db','mrev_positions')]:
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    print(f'=== {db} ===')
    for r in con.execute(f\"SELECT symbol, qty, entry_price, stop_loss, initial_qty, partial_tp_taken FROM {tbl} WHERE status IN ('open','OPEN') ORDER BY symbol\"):
        print(dict(r))
"

# Previews de email generados OK
ls -la *_preview*.html
```

**Criterios para declarar "listo":**

- Los 8 archivos compilan sin errores.
- Tests dan 0 fails.
- Ninguna posición cripto quedó en `trading_paper.db`.
- Todas las posiciones con `partial_tp_taken ≥ 1` tienen `stop_loss >= entry_price`.
- El preview del email mensual MREV se ve bien en browser (abrí `mrev_monthly_preview.html` manualmente).
- Los logs no contienen `EMAIL_PASSWORD` ni `ALPACA_SECRET_KEY` (grep sobre `logs/`).

## Guardrails — qué NO podés hacer

1. **NO tocar `.env.paper`**. Nunca.
2. **NO modificar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS`**.
3. **NO cambiar el comportamiento de `check_entry`, `check_exit`, `_calc_take_profit`, `size_position`** salvo lo explícitamente pedido.
4. **NO mandar emails reales durante el proceso** — todo dry-run.
5. **NO enviar órdenes a Alpaca** desde este prompt — la única excepción es `seed_missing_positions.py` que solo hace DB inserts/updates locales (no POST a Alpaca).
6. **NO refactorear archivos enteros** — edits quirúrgicos.
7. **Commits chicos y atómicos**, uno por feature. Mensajes descriptivos en inglés corto.
8. **Después de cada commit**, correr `python3 -m py_compile` sobre los archivos tocados.
9. **Si algo del plan no se puede hacer** por una razón que descubriste en el código (ej. la función ya no existe, el schema cambió), **parálo ahí y dejá una nota** — no improvises.
10. **Si te bloqueás o dudás**, parate y dejá un TODO en el código + un mensaje claro en el chat — no adivines.

## Entregable final

Al terminar todo, imprimí:

1. Lista de commits hechos (hash + mensaje).
2. Archivos tocados y rangos de líneas.
3. Output de la verificación final (fase 7).
4. Env vars nuevas agregadas (con su default y descripción).
5. Qué NO se pudo hacer (si algo) y por qué.
6. Preview del email mensual MREV: mandame el path al HTML generado.

Fin del prompt. Arrancá por Fase 0, no saltes el orden.
