# Trading System — Audit Report

**Fecha:** 2026-04-22
**Modo:** SOLO LECTURA — no se modificó código, no se mandó email, no se envió orden a Alpaca.
**Scope:** `/Users/charlie/Desktop/trading-system/` — bots standalone (`standalone_paper_trader.py`, `standalone_mrev_trader.py`) + helpers de root + `tests/`.

---

## 1. Arquitectura general

**Dos bots independientes sobre una sola cuenta Alpaca Paper compartida:**

- **RFTM** (`standalone_paper_trader.py`, 2091 líneas) — estrategia trend-following / breakout. Corre **1×/día**. Universo = 55 ETFs (`ETF_UNIVERSE` líneas 60-136). DB: `trading_paper.db` tabla `positions`.
- **MREV** (`standalone_mrev_trader.py`, ~2100 líneas) — estrategia mean-reversion sobre velas 1h. Corre **cada hora** (cripto 24/7 + ETFs selectos en horario de mercado). Universo = 6 cripto + 9 ETFs (`CRYPTO_SYMBOLS + ETF_SYMBOLS` líneas 102-104). DB: `mrev_paper.db` tabla `mrev_positions`.

**Pipeline común:** fetch bars (Alpaca v2 stocks / v1beta3 crypto) → `compute_indicators` → `check_entry` / partial-TP evaluator / `check_exit` → ranking/cap → `alpaca_submit_order` → UPDATE DB local → snapshot → email.

**Archivos principales (root):**
| Archivo | Qué hace |
|---|---|
| `standalone_paper_trader.py` | Bot RFTM diario (ETFs) |
| `standalone_mrev_trader.py`  | Bot MREV horario (cripto + ETFs) |
| `seed_missing_positions.py`  | Reconciliación one-shot: siembra posiciones faltantes + migra cripto mal guardada |
| `analyze_trades.py`          | Extractor de datos Alpaca para análisis offline |
| `mark_partial_tp_done.py`    | Tool one-shot: marca `partial_tp_taken=1` (para posiciones legacy) |
| `sell_half_profits.py`       | Tool manual: vende 50% de posiciones con ganancia |
| `sqlalchemy_stub.py`         | Shim mínimo de SQLAlchemy para los tests |
| `conftest.py`                | pytest path setup |
| `run_tests.py`               | Test runner propio (roto — crashea al instanciar clase que pide `app`) |

**Coordinación de capital:** ambos bots corren sobre la misma cuenta Alpaca ($100K).
- RFTM: `INITIAL_CAPITAL=75_000`, `MAX_LEVERAGE=1.5` (línea 1554) → headroom = `equity×1.5 − long_market_value`.
- MREV: `MREV_INITIAL_CAPITAL=25_000`, chequea `alpaca_buying_power * 0.90` antes de cada compra.
- Prevención de doble-compra del mismo símbolo: **ninguna**. `[ALERTA]` Ambos universos comparten `SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO, ARKK` — RFTM y MREV pueden abrir la misma posición al mismo tiempo; cada bot sólo ve su propia DB. El único sync es por símbolo raw (`sync_with_alpaca`), y MREV por lógica de prefijos en `seed_missing_positions.py` evita solapar cripto, pero **no evita** que ambos bots compren QQQ simultáneamente.

---

## 2. Lógica de entrada (compras)

### RFTM — `check_entry` líneas 378-423

| # | Condición | File:line |
|---|-----------|-----------|
| C1 | `close > EMA21` AND `close > EMA50` | 392-398 |
| C2 | `55 ≤ RSI14 ≤ 70` | 400-403 |
| C3 | `close > 20-day high` | 405-408 |
| C4 | `volume ≥ 0.8 × vol_ma20` | 410-414 |
| C5 | `0.003 ≤ atr14_pct ≤ 0.08` | 416-419 |

**Indicadores** (`compute_indicators` 320-363):
- EMA21/50/200: `ewm(span=…, adjust=False).mean()`
- RSI14: Wilder `ewm(com=13)` sobre ganancias/pérdidas
- ATR14: EWMA del true range con `com=13`
- `atr14_pct = atr14 / close`
- `vol_ma20`: rolling 20
- `high20`: rolling 20 del close shifted (-1)
- `bars_since_last_high`: cumcount por grupo de nuevos máximos

**Ranking** (líneas 1677-1684): sort por `abs(RSI − 62)` (preferir momentum cerca del ideal), capado a `MAX_POSITIONS − len(open_symbols)`.

**Sizing** (`size_position` 485-494):
```
stop_dist    = ATR_MULT × ATR14         # ATR_MULT=1.5
risk_amount  = portfolio_value × 0.05   # RISK_PCT
shares_risk  = floor(risk_amount / stop_dist)
shares_cap   = floor(portfolio_value × 0.25 / close)
shares       = min(shares_risk, shares_cap)
```

**Stop-loss inicial:** `close − 1.5 × ATR14` (línea 1808-1812, al persistir la posición).

**Límites:** `MAX_POSITIONS=10`, `MAX_POS_PCT=0.25`, `MIN_SHARES=1`. Antes de cada orden se hace re-query de buying-power y se recapa a `min(cost, bp_now × 0.90)` (líneas 1990-2006).

### MREV — `check_entry` líneas 297-319

| # | Condición | File:line |
|---|-----------|-----------|
| X1 | `RSI14 ≤ 45` (oversold) | 298-301 |
| X2 | `close ≤ BB_lower` (20-SMA − 1.8σ) | 302-303 |
| X3 | `volume ≥ 0.5 × vol_ma20` | 304-305 |
| X4 | `0.002 ≤ atr_pct ≤ 0.15` | 306-307 |

**Indicadores** (similar a RFTM pero agrega `sma_20`, `bb_lower`). `[NOTA]` MREV no calcula EMA ni bars_since_last_high.

**Ranking:** ninguno. Los candidatos se procesan en orden del universo.

**Sizing** (`size_position` 364-377):
```
stop_dist   = 2.0 × ATR14
risk_amount = equity × MREV_RISK_PER_TRADE   # 0.05
qty_risk    = risk_amount / stop_dist
qty_cap     = equity × 0.40 / close          # [MAGIC NUMBER] 40% notional cap
qty         = min(qty_risk, qty_cap)
min order   = $10 notional (sino qty=0)
```

**Stop-loss inicial:** `close − 2.0 × ATR14` (línea 357).

**Límites:** `MREV_MAX_POSITIONS=6`. Enforza en línea 1334: `if qty > 0 and len(open_positions) + len(buys) < MREV_MAX_POSITIONS`.

---

## 3. Lógica de salida (ventas)

### RFTM — `check_exit` líneas 426-480

**Orden de evaluación:**
1. **E3** hard stop (442): `close ≤ stop_loss` → exit.
2. **E7** take-profit (450-457): gateado por `partial_tp_taken ≥ 2`. Fórmula `entry + 2×(entry − stop_loss)`. `[OK]` ya está ejecutado, no es cosmético.
3. **E5** trailing 3-fases (459-471): fase-3 (profit≥1.5 ATR) trail a high−1 ATR; fase-2 (profit≥0.5 ATR) stop en breakeven.
4. **E6** time stop (473-476): `bars_since_last_high ≥ 20`.

**E1 (death cross) y E2 (close < EMA50) y E4 (RSI>80): REMOVIDAS** (comentado líneas 429-433). `[NOTA]` Los tests `test_strategy.py::TestCheckExitSignal::test_death_cross_*` siguen probándolas y fallan — ver sección 13.

**Partial TP 2 etapas** (líneas 1600-1646, evaluado ANTES de `check_exit`):
- stage 0 + unrealized ≥ 5% + qty ≥ 2 → vende `floor(qty × 0.50)`, pasa a stage 1.
- stage 1 + unrealized ≥ 7.5% + qty ≥ 2 → vende `floor(qty × 0.50)`, pasa a stage 2.
- Si disparó parcial, `continue` — no evalúa check_exit ese ciclo.

**Env vars** (159-173 con retro-compat): `PARTIAL_TP1_PCT=0.05`, `PARTIAL_TP1_SELL_RATIO=0.50`, `PARTIAL_TP2_PCT=0.075`, `PARTIAL_TP2_SELL_RATIO=0.50`. Legacy `PARTIAL_TP_PCT` / `PARTIAL_TP_SELL_RATIO` mapean a TP1.

**Breakeven después de TP1** (líneas 1893-1905): al escribir el fill de TP1, `new_stop = max(old_stop, entry_price)`. Nunca baja.

### MREV — `check_exit` líneas 322-346

**Orden:**
1. **X1** take-profit (321-324): `close ≥ SMA20 + 1.5 × ATR14`. No gateado por stage — puede dispararse antes de parciales.
2. **X2** stop loss (326-330): `close ≤ entry − 2.0 × ATR14`.
3. **X4** time stop (335-338): 120h (5 días).
4. **X5** trailing (340-344): `close ≤ highest − 1.0 × ATR14` (una sola fase, no tres como RFTM).

**X3 (RSI normalized): REMOVIDA** (línea 332).

**Partial TP** (líneas 1264-1317, env vars `MREV_PARTIAL_TP1_PCT / ..._RATIO`, defaults iguales a RFTM). Breakeven post-TP1 (líneas 1411-1423). `[ALERTA]` MREV no tiene guard `qty ≥ 2` — en cripto puede vender fracciones muy chicas (la check `sell_qty > 0 and sell_qty < qty_full` permite cualquier fracción positiva).

`[ALERTA]` Desalineación: en MREV, una posición con stage=0 o 1 puede cerrar por X1 (take_profit dinámico) **antes** de tomar parciales. Cuando eso pasa, no se emite email de TP_FINAL (mi implementación lo gatea en `prev_stage ≥ 2`), lo cual es intencional — pero significa que hay casos donde no hay notificación inmediata.

---

## 4. Estado `partial_tp_taken`

**Significado:**
- `0` = ninguna parcial ejecutada.
- `1` = TP1 (+5%) vendió 50% del qty inicial. Stop subido a breakeven.
- `2` = TP2 (+7.5%) vendió 50% del remanente = 25% del total. Stop queda en breakeven.
- Posición cerrada: status='closed' (RFTM) / 'CLOSED' (MREV), no se usa stage ≥ 3.

**Dónde se escribe:**
- RFTM: líneas 1755-1775, 1893-1915, migración 309, seed_missing_positions.py.
- MREV: líneas 1395-1432, migración 406, seed_missing_positions.py.

**Dónde se lee:**
- RFTM: 453, 1469-1472, 1605-1608, 1331-1333 (email), check_exit.
- MREV: 1258-1260, check_exit indirecto (via tp_stage).

**Transición 2 → closed por E7 (RFTM):** gateado en check_exit (456). Confirmado. ✅
**Transición MREV stage 2 → cerrado por X1:** no gateado — puede cerrar por X1 en cualquier stage.

**Qty < 2:**
- RFTM: requiere `qty ≥ 2` (líneas 1478, 1491) — si queda 1 sola acción, no hay más parciales; se espera E7/trailing.
- MREV: **sin guard** [ALERTA leve].

**Legacy binario:** `seed_missing_positions.py` (dry-run mostró stage=1 para 9 posiciones, stage=0 para 2 nuevas, stage=2 para XLK). La DB actual no tiene stage > 2 (verificado en queries). ✅

---

## 5. Base de datos

### Schema `positions` (trading_paper.db)
```
id, run_id, symbol, status DEFAULT 'open', qty, entry_price,
stop_loss, exit_price, realized_pnl, unrealized_pnl DEFAULT 0,
close_reason, opened_at, closed_at,
highest_since_entry REAL DEFAULT 0.0,
partial_tp_taken INTEGER DEFAULT 0,    -- migrada
initial_qty INTEGER                    -- migrada (nullable)
```

### Schema `mrev_positions` (mrev_paper.db)
```
id, run_id, symbol, qty REAL, entry_price REAL, stop_loss REAL,
entry_dt, status DEFAULT 'OPEN', exit_price, exit_dt, pnl, exit_reason,
highest_since_entry REAL DEFAULT 0.0,
partial_tp_taken INTEGER DEFAULT 0,
initial_qty REAL
```

### Tablas auxiliares
- RFTM: `runs`, `market_data`, `orders`, `snapshots`.
- MREV: `mrev_runs`, `mrev_signals`, `mrev_snapshots`, `mrev_hourly_log`, `mrev_email_log`.

### Índices
Solo los auto-índices de `PRIMARY KEY`. **No hay índices sobre `(status, symbol)`** que son los filtros más frecuentes. `[NOTA]` Con pocas filas es irrelevante; si la tabla crece a miles de posiciones históricas, `WHERE status='open'` hace full-scan.

### Estado actual en vivo

**RFTM (11 posiciones abiertas):**

| symbol | qty | entry | stop | initial | stage | high | opened |
|--------|-----|-------|------|---------|-------|------|--------|
| ARGT   | 135 | 92.35 | 87.73 | 270 | 1 | 92.35 | 2026-04-15 |
| ECH    | 133 | 41.72 | 39.64 | 266 | 1 | 41.72 | 2026-04-15 |
| EWJ    | 141 | 87.38 | 83.01 | 282 | 1 | 87.38 | 2026-04-15 |
| FLBR   | 260 | 24.94 | 23.69 | 520 | 1 | 24.94 | 2026-04-15 |
| IWM    | 24  | 259.49| 246.51| 48 | 1 | 259.49| 2026-04-15 |
| PAVE   | 234 | 53.90 | 51.20 | 468 | 1 | 53.90 | 2026-04-15 |
| QQQ    | 11  | 605.00| 574.75| 22 | 1 | 605.00| 2026-04-15 |
| **SOLUSD** | **3** | **81.82** | **77.73** | **6** | **1** | 81.82 | 2026-04-15 |
| SPY    | 10  | 674.71| 640.98| 20 | 1 | 674.71| 2026-04-15 |
| XLE    | 68  | 55.63 | 52.85 | 136 | 1 | 55.63 | 2026-04-15 |
| XLK    | 1   | 141.77| 134.68| 2 | 1 | 141.77| 2026-04-15 |

`[ALERTA]` **SOLUSD** está guardado en RFTM cuando debería estar en MREV. `seed_missing_positions.py --dry-run` detecta esto y propone migrarlo (salida: `MARK_CLOSED (era cripto, va a MREV)`), pero todavía no se corrió en modo real.

`[ALERTA]` **Todos los `highest_since_entry` están igualados al `entry_price`.** Eso significa que el trailing stop (E5) nunca se actualizó — las posiciones fueron sembradas por `seed_missing_positions.py` (o el bot todavía no corrió el día en que subieron). Si una posición viene cayendo, el trailing stop parece estar "dormido".

`[ALERTA]` **Todos los stops están bajo el entry** aunque stage=1 → no hubo breakeven raise todavía. Significa que estas posiciones transicionaron 0→1 a través del **seed script** (no del flujo real post-fill), entonces el código de Feature 1 (breakeven post-TP1) no se activó. El seed no sube el stop al breakeven — `[NOTA]` considerar: ¿deberíamos también subir el stop en el seed cuando detectamos stage≥1?

**MREV:** 0 posiciones abiertas actualmente (tabla vacía). La DB tiene las 5 tablas pero `mrev_positions WHERE status='OPEN'` = 0 filas. El seed dry-run propone insertar AVAX/USD, DOGE/USD, LINK/USD, SOL/USD con stage=0.

---

## 6. Integración con Alpaca

### Endpoints usados

**RFTM:**
- `GET /account` (líneas 724, 1992, 1002)
- `GET /positions` (750, 1003)
- `GET /orders/{id}` (741, poll post-fill)
- `GET /orders?status=filled&after=…` (758, órdenes del día para email)
- `GET /account/portfolio/history` (768)
- `POST /orders` (728)
- Data API: `GET /v2/stocks/.../bars` (función `alpaca_get_bars`)

**MREV:**
- `GET /account` (157, 1509)
- `GET /positions` (163)
- `GET /clock` (241)
- `GET /v1beta3/crypto/us/bars` + `/v2/stocks/{sym}/bars` (188-200)
- `POST /orders` (233)

### Órdenes

Ambos bots: **market orders, sin bracket**. RFTM: `time_in_force=day`. MREV: `gtc` para cripto, `day` para ETFs.

`[ALERTA]` **No hay bracket orders.** Todos los stops son software-side: el bot consulta `check_exit` cada run y dispara el SELL si close ≤ stop_loss. Si el bot se cae entre runs (9am–9am siguiente) el "stop" no existe en Alpaca. El prompt original de Feature 1 decía "Alpaca tiene un bracket-stop server-side creado en la compra" — **eso es falso en el código actual** (grep de `order_class`, `bracket`, `stop_loss` sobre el body de la orden = 0 matches).

`[ALERTA]` **DB se actualiza sin confirmar fill real.** El flujo es:
```python
result = alpaca_submit_order(...)
if result:                               # chequea que la respuesta existe, no el status
    filled_price = result.get("filled_avg_price") or close
    # UPDATE DB con filled_price
```
En RFTM esto se mitiga parcialmente porque `alpaca_submit_order` hace un polling corto del order status (línea 741 en adelante), pero si la orden queda `pending` o `partially_filled`, el DB igual se actualiza como si fuera fill total.

### Sync local ↔ Alpaca

**`sync_with_alpaca`**: existe en ambos bots (RFTM línea ~820, MREV línea 467).
- Source of truth: Alpaca.
- Cierra posiciones locales que Alpaca ya no tiene. ✅
- Fixes `entry_price` a `avg_entry_price` de Alpaca. ✅
- Inserta posiciones que faltan con `partial_tp_taken=0`, `initial_qty=qty`. ✅
- `[NOTA]` MREV sólo reclama símbolos que matchean `ALL_SYMBOLS` (línea 491) — previene reclamar ETFs que son de RFTM. RFTM no tiene la simetría: puede terminar reclamando cripto (y ahí sale el bug SOLUSD).

### Partial fills
`[ALERTA]` Ningún bot valida `filled_qty < ordered_qty`. Si Alpaca fillea 3/5 shares, el DB igual se actualiza como si fueran 5.

### Buying power
Ambos re-chequean antes de cada compra (RFTM 1990-2006, MREV 1508-1514). Si falla la consulta, fallback a cash cacheado (RFTM) o permite pasar (MREV línea 1511). RFTM usa safety factor `0.90` (hardcoded 3 veces — [MAGIC NUMBER]).

### Rechazos
Si `alpaca_submit_order` tira excepción, el caller (RFTM línea 1878: `if result:` → skip; MREV 1499-1501: `except Exception as e: err()` → continúa al siguiente). No hay retries.

---

## 7. Emails y notificaciones

### RFTM (`standalone_paper_trader.py`)

- **Resumen diario**: `_build_email_report` línea 983, `send_email_report` línea 1412. Se envía al final de cada run (1×/día).
- **Secciones HTML**: "Reporte del Bot" (hero), "Compras de hoy", "Ventas de hoy", "Hoy no operé" (si aplica, con closest-to-entry), "Lo que tengo en cartera" (con **línea Stage X · próximo…** tras los cuadrados SL/Precio/TP).
- **Email por evento**: `send_stage_event_email` línea 1442, disparado tras cada fill de TP1/TP2/E7 (líneas 1817-1870). Respeta `dry_run` ✅, chequea `EMAIL_ENABLED` ✅, falla con `warn` sin abortar ✅.
- **SMTP**: Gmail `smtp.gmail.com:587` default, TLS, credenciales de `.env.paper`.
- **"Take Profit $X" en el email → exit real**: ✅ Ahora sí, E7 está ejecutado. Antes (comentario línea 431) era cosmético.

### MREV (`standalone_mrev_trader.py`)

- **Resumen diario**: `_build_email_report` línea 1676, `send_email_report` línea ~1985. Ventana horaria `EMAIL_HOURS_UTC=12` default (12 UTC = 09:00 ARG). Dedup por día via `mrev_email_log` + `should_send_email`/`record_email_sent` (líneas 683, 702).
- **Secciones HTML tras la reescritura reciente**: hero (equity MREV vs MREV_CAPITAL), "BOT MREV · Crypto 1h" (KPIs), "Lo que tengo en cartera" (con cuadrados SL/Precio/TP dinámico SMA20+1.5×ATR + línea de stage), "MREV — Compras hoy", "MREV — Ventas hoy".
- **Email por evento**: `send_stage_event_email` (copia espejo del de RFTM). Filtra TP_FINAL por `prev_stage ≥ 2 and reason.startswith("take_profit")`.
- **El email mezclaba datos RFTM**: ✅ corregido en la sesión anterior para `_build_email_report` (daily). `[ALERTA]` **`_build_monthly_email_report` (línea 852+) y `get_account_overview` (1602+) todavía mezclan** — el email mensual muestra "Tus 2 robots" con ACCOUNT_TOTAL_CAPITAL / DAILY_BOT_CAPITAL (líneas 891, 896-898, 970, 982, 1013, 1661-1662). El prompt anterior solo cubrió el email diario.
- **Monthly email**: `send_monthly_email_report` línea ~1122, se dispara el 1° de cada mes (`EMAIL_MONTHLY_ENABLED=true`, `EMAIL_MONTHLY_DAY=1`).

### Previews en disco
```
email_preview.html             (RFTM daily, generado en tests)
email_preview_buy.html         (histórico)
email_preview_sell.html        (histórico)
email_preview_noop.html        (histórico)
mrev_email_preview.html        (MREV daily, post-refactor)
mrev_email_preview_noop.html   (histórico)
```

---

## 8. Configuración

### Env vars leídas

**RFTM (`standalone_paper_trader.py`):**
| Línea | Var | Default | Controla |
|---|---|---|---|
| 55 | `TMPDIR` | `/tmp` | Dir para DB local (evita FUSE/WAL issues) |
| 138 | `INITIAL_CAPITAL` | `75_000` | Capital del bot RFTM |
| 140 | `ACCOUNT_INITIAL_CAPITAL` | `100_000` | Capital total (para % en email) |
| 143 | `RISK_PER_TRADE` | `0.05` | % riesgo por trade |
| 144 | `MAX_POSITIONS` | `10` | Máx posiciones simultáneas |
| 145 | `MAX_POSITION_PCT` | `0.25` | Máx % portfolio por posición |
| 159-162 | `PARTIAL_TP1_PCT` / `..._SELL_RATIO` / `PARTIAL_TP2_PCT` / `..._SELL_RATIO` | `0.05 / 0.50 / 0.075 / 0.50` | Partial TP |
| 165-166 | `PARTIAL_TP_PCT` / `PARTIAL_TP_SELL_RATIO` | — | Alias legacy de etapa 1 |
| 192-194 | `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `POLYGON_API_KEY` | `""` | Credenciales |
| 197-202 | `EMAIL_ENABLED` / `EMAIL_SMTP_SERVER` / `EMAIL_SMTP_PORT` / `EMAIL_FROM` / `EMAIL_PASSWORD` / `EMAIL_TO` | `true / smtp.gmail.com / 587 / "" / "" / ""` | SMTP |
| 1554 | `MAX_LEVERAGE` | `1.5` | Cap de leverage vs equity |

**MREV (`standalone_mrev_trader.py`):**
| Línea | Var | Default | Controla |
|---|---|---|---|
| 65-66 | `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | `""` | Credenciales |
| 69-74 | Email vars iguales a RFTM | — | SMTP |
| 79 | `EMAIL_HOURS_UTC` | `"12"` | Ventana diaria (csv hours) |
| 82-83 | `EMAIL_MONTHLY_ENABLED` / `EMAIL_MONTHLY_DAY` | `true / 1` | Reporte mensual |
| 86-88 | `MREV_INITIAL_CAPITAL` / `MREV_MAX_POSITIONS` / `MREV_RISK_PER_TRADE` | `25000 / 6 / 0.05` | Bot config |
| 94-99 | `MREV_PARTIAL_TP{1,2}_PCT` / `..._SELL_RATIO` (+ legacy aliases) | `0.05 / 0.50 / 0.075 / 0.50` | Partial TP |
| 112-113 | `ACCOUNT_TOTAL_CAPITAL` / `DAILY_BOT_CAPITAL` | `100000 / 75000` | Para email mensual (mixed-bot) |
| 395 | `MREV_DB_PATH` | `mrev_paper.db` junto al script | Override path de DB |

### Magic numbers sin env var `[MAGIC NUMBER]`
- `ATR_MULT = 1.5` (RFTM línea 142)
- `MAX_DRAWDOWN = 0.20` (RFTM línea 146) — kill switch hardcoded
- `MIN_SHARES = 1` (línea 147)
- Buying-power buffer `0.90` (RFTM 1660, 1691, 1997; MREV 1348, 1513) — **hardcoded 5 veces**
- MREV stop multiplier `2.0 × ATR` (línea 328, 356)
- MREV qty cap `0.40 × equity` (línea 362)
- MREV time stop `120h` (línea 337) — el email decía "24h" [desalineación]
- RFTM time stop `20 bars` (línea 475)
- MREV TP dinámico `SMA20 + 1.5 × ATR` (línea 322, 340)
- RFTM E5 trailing `1.0 × ATR` / `0.5 × ATR` / `1.5 × ATR` umbrales (462-469)

### `.env.paper`
Permisos `600` ✅. Contiene (sin imprimir valores): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `EMAIL_FROM`, `EMAIL_PASSWORD` (app password de Gmail), `EMAIL_TO`. `.gitignore` excluye `.env*` ✅.

---

## 9. Scheduling / automatización

**Dos schedulers coexisten:**

### launchd (macOS local)
- `com.rftm.trader.plist`: Lun-Vie 10:35 AM hora local. Llama a `run_rftm_bot.sh`. Lock file `/tmp/rftm_bot.lock` previene doble ejecución. `setup_autorun.sh` lo instala en `~/Library/LaunchAgents/`.

### GitHub Actions (`/.github/workflows/`)
- `daily_trade.yml`: cron `35 13 * * 1-5` (13:35 UTC = 9:35 ET en verano). Ejecuta `standalone_paper_trader.py`. Crea `.env.paper` desde secrets.
- `mrev_hourly.yml`: cron `5 * * * *` (minuto 5 cada hora). Ejecuta `standalone_mrev_trader.py`. Caché de SQLite entre runs con `actions/cache` — incluye `.db`, `.db-wal`, `.db-shm`. Tiene `concurrency group: mrev-hourly` para evitar solapamiento.

`[ALERTA]` **RFTM corre tanto por launchd local como por GitHub Actions.** Si ambos están activos, el bot corre dos veces al día, potencialmente duplicando señales (y si una corre antes del fill de la otra, puede doble-comprar). El lock file `/tmp/rftm_bot.lock` solo protege contra ejecuciones concurrentes en **la misma máquina**.

`[ALERTA]` `daily_trade.yml` hardcodea `INITIAL_CAPITAL=100000`, `MAX_POSITIONS=10`, `MAX_POSITION_PCT=0.25`, `RISK_PER_TRADE=0.05` — **difieren del local** (donde `INITIAL_CAPITAL=75000`). El "capital" del bot diario cambia según dónde corra.

`[NOTA]` `mrev_hourly.yml` no pasa `ACCOUNT_TOTAL_CAPITAL` ni `DAILY_BOT_CAPITAL` como env. **Los toma del default hardcodeado** (líneas 112-113 del .py).

`paper_trade.sh`: wrapper local interactivo (modo standalone o Docker).

---

## 10. Logging y observabilidad

**RFTM:**
- `logs/rftm_YYYY-MM-DD.log` (un archivo por día). Lo escribe `run_rftm_bot.sh` vía redirección. No rota automáticamente (se acumula).
- Sin logger formal — sólo `print()` con helpers de color (`ok`, `err`, `warn`, `info`, `hdr`).
- `launchd_stdout.log` y `launchd_stderr.log` para el scheduler.

**MREV:**
- Logs estructurados en SQLite: tabla `mrev_hourly_log` con `details_json` por run.
- Stdout capturado por GitHub Actions artifact (`mrev_output.txt`).
- No hay archivo persistente en `logs/` para MREV.

**Tamaño `logs/`:** 20K (3 archivos, uno vacío). No es problema hoy pero **no hay rotación configurada**. Si algún día se usa para logs diarios verbosos, crece sin cap.

**Secrets en logs:** escaneé `print`, `info`, `ok`, `err`, `warn` con `EMAIL_PASSWORD`, `ALPACA_SECRET_KEY`, `POLYGON_API_KEY` — **no hay leaks**. ✅

**`except Exception: pass` silenciosos:**
- `standalone_paper_trader.py:305, 315, 1005, 1790, 1836` — todos son para migraciones idempotentes (ALTER TABLE ADD COLUMN) o fallbacks esperados. Aceptable.
- `standalone_paper_trader.py:454, 1605` — parseo defensivo de `partial_tp_taken`. OK.
- `standalone_paper_trader.py:479, 1737` — check_exit y render de conds. Razonable.
- `standalone_mrev_trader.py:164, 172, 243, 413` — errores de red Alpaca (devuelven `[]` o `{}`) + migraciones. Aceptables pero no loguean — si Alpaca está caído, el bot **no emite warning**.

`[ALERTA]` MREV swallowing de errores Alpaca en 164 (`return []`) y 172 (`return {}`) y 243 (`return False`) **sin log ni warn**. Si Alpaca está caído, MREV se comporta como "no hay posiciones / no hay cuenta / mercado cerrado" silenciosamente. Difícil de detectar.

---

## 11. Manejo de errores

| Escenario | RFTM | MREV |
|---|---|---|
| Alpaca caído | `alpaca_submit_order` devuelve `None` → skip orden. `alpaca_get_account` → `except` fallback a cash cacheado. | `alpaca_get_*` devuelve `[]`/`{}` silenciosamente. Sin warn. [ALERTA] |
| Sin datos de mercado para un símbolo | `if df.empty or len(df) < 201: continue` (1447) | `if len(df) >= 25` (1198); si no → `warn + skip` ✅ |
| `.env.paper` vacío | `ALPACA_API_KEY == ""` → se fuerza `dry_run=True` (1935). `run_rftm_bot.sh` chequea que el archivo exista y sale si no. | No valida explícitamente — intenta igual y falla en la primera request. |
| DB locked (SQLite) | Sin retry. Una excepción rompe el run. | Idem. MREV usa WAL (389) que reduce contención, pero no elimina. |
| Rechazo de orden | Excepción se propaga → RFTM `if result:` lo trata como skip (sin reason); MREV `except … err()` continúa al siguiente. |
| Partial fill | No se detecta. DB se actualiza como fill total. [ALERTA] |

**Retries / backoff:** ninguno en ambos bots. Ninguna llamada Alpaca se reintenta.

---

## 12. Risk management

- **Kill switch**: `MAX_DRAWDOWN = 0.20` (RFTM 146, MREV 1235 hardcoded al mismo valor). Si `drawdown ≥ 20%` → `return {"kill_switch": True}` y bloquea entradas (no cierra posiciones).
- **Max positions**: RFTM=10, MREV=6.
- **Max position %**: RFTM `MAX_POS_PCT=0.25`, MREV `0.40` (hardcoded).
- **Risk per trade**: ambos 5%. `[NOTA]` Si ATR explota, `shares_risk` baja, pero `shares_cap` (25%/40% del portfolio) puede darte igual una posición gorda si el precio es bajo. Si ATR colapsa a 0.3%, `shares_risk` sube pero el cap por `shares_cap` la limita. El cap funciona.
- **Correlación entre posiciones**: `[ALERTA]` SPY + QQQ + IWM + XLK están altamente correlacionados con el S&P. El bot puede abrir los 4 y tener ~100% del riesgo en un factor. No hay considerar correlación.
- **[NOTA]** `headroom = equity×MAX_LEVERAGE − long_mkt` (RFTM línea 1558) puede dar negativo si el bot ya excedió 1.5× — en ese caso `max(0, headroom)` lo limita a 0 ✅.
- **[ALERTA] Leverage compartido:** ambos bots consumen del mismo buying_power. Si RFTM corre primero y gasta hasta 1.5×, MREV en la hora siguiente tiene $0 de buying power libre. La versión local dice "MAX_LEVERAGE=1.5" pero la workflow de GA no setea ese env → MREV no lo conoce.

---

## 13. Tests

**Inventario (`tests/` — 15 archivos, ~381 funciones test):**
- `test_api.py`, `test_health.py`, `test_execution.py`, `test_kill_switch.py`, `test_models.py`, `test_orchestrator.py`, `test_risk.py`, `test_smoke.py` — servicios bajo `apps/`.
- `test_indicators.py`, `test_strategy.py` — indicadores y signals RFTM.
- `test_mrev/test_{indicators_1h, risk_mrev, scanner_mrev, pipeline_mrev, backtest_mrev}.py`.

**Ejecución:**
- `python3 run_tests.py` → crashea (`TypeError: __init__() missing 1 required positional argument: 'app'`) — **test runner propio está roto**. [ALERTA]
- `python3 -m pytest tests/` → 5 errores de collection en archivos que importan `packages/shared/models/*` con SQLAlchemy (annotation `str | None` + Python 3.9 + SQLAlchemy version mismatch).
- Subset que sí colecta y corre:
  ```
  pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py tests/test_mrev
  → 127 passed, 23 failed, in 2.3s
  ```
- **Fallos**: todos los 23 son tests que referencian código **ya removido**:
  - `TestCheckExitSignal::test_death_cross_triggers_exit` (E1 REMOVIDA)
  - `TestCheckExitSignal::test_close_below_ema50_triggers_exit` (E2 REMOVIDA)
  - `TestCheckExitSignal::test_rsi_overbought_triggers_exit` (E4 REMOVIDA)
  - `TestMrevExitSignal::test_rsi_normalized_exit` (X3 REMOVIDA)
  - `TestMrevExitSignal::test_time_stop_after_24_bars` (cambiado a 120h)
  - `TestCheckEntrySignal::test_low_volume_returns_hold` (umbral cambió de 1.0× a 0.8×)
  - etc.

`[ALERTA]` Tests drift: **los tests no se actualizaron cuando se cambió la estrategia**. 23 tests fallan contra lógica que está correctamente implementada en el bot. Son tests fantasma — dan falsa sensación de cobertura.

**Tests skipeados:** ninguno con `@pytest.mark.skip`. El shim de `run_tests.py` los soporta pero nadie los usa.

---

## 14. Código sospechoso / deuda técnica

### TODOs / FIXMEs
- `/Users/charlie/Desktop/trading-system/standalone_paper_trader.py:88` — la palabra "HACK" aparece como símbolo ETF (HACK = Ciberseguridad ETF). **No es un TODO**, es literal. OK.
- No hay TODOs/FIXMEs reales en el código de los bots.

### Código REMOVIDO con comentarios
- RFTM `check_exit` líneas 429-433: E1 y E2 comentadas "REMOVED".
- RFTM `check_exit` líneas 429-433: E4 comentado "REMOVED".
- MREV `check_exit` línea 332: X3 "REMOVED".
- `apps/svc_strategy_mrev/scanner.py:187`: "X1: Take profit — REMOVED as fixed exit".
- Los tests referencian la lógica vieja (sección 13).

### Archivos scratchpad / one-shot en root
Ya listados en sección 1. Destacados:
- `bb_lower=9.20`: **archivo de 0 bytes creado por una redirección accidental de shell** (ej: `grep bb_lower ... > bb_lower=9.20`). `[ALERTA]` Eliminalo.
- `.fuse_hidden*`: basura del filesystem FUSE — se pueden borrar.
- `mrev_paper.db.bak`: backup manual, probablemente obsoleto.
- `Plan_Agresivo_8_10_Trading_Bots.docx`, `Trading_Bots_Analisis_Exhaustivo.docx`, `PROMPT_CLAUDE_CODE.md`: documentos del usuario, no código.

### Código duplicado `[ALERTA]`
`_build_email_report` y `send_stage_event_email` están **casi idénticas** entre RFTM y MREV:
- RFTM `_build_email_report`: líneas 983-1366.
- MREV `_build_email_report`: líneas 1676-2030 (post-refactor).
- `send_stage_event_email`: ~95 líneas copiadas textuales en ambos archivos.
- CSS blocks duplicados.

Potencial ganancia: extraer `_email_helpers.py` con `build_css()`, `position_card()`, `send_stage_event_email()`. ~200 líneas menos en total. (Documentado como TODO en el último prompt.)

### Imports sin usar
Escaneé imports — todos usados.

---

## 15. Seguridad

| Check | Estado |
|---|---|
| `.env*` en `.gitignore` | ✅ (líneas 1-4) |
| `.env.paper` commiteado | ✅ No aparece en `git ls-files` |
| Permisos de `.env.paper` | ✅ `-rw-------` (600) |
| Secrets en logs | ✅ Ningún `print(EMAIL_PASSWORD)` ni similar |
| SQL parametrizado | Mostly ✅ — Todos los SELECT/INSERT/UPDATE dinámicos usan `?`. **Excepciones** (benignas pero worth noting): `analyze_trades.py:93,110` (`f"SELECT * FROM {t}"` donde `t` viene de `sqlite_master` — no user input), `standalone_paper_trader.py:304` y `seed_missing_positions.py:102` y `mark_partial_tp_done.py:65,71` (`ALTER TABLE {table} ADD COLUMN {col}` — columnas hardcoded, no input de red). No hay SQL injection explotable. |
| Workflow secrets | GitHub Actions escribe `.env.paper` con `${{ secrets.ALPACA_API_KEY }}` etc. El `.env.paper` vive solo en el runner. OK. |

---

## 16. Diferencias RFTM vs MREV

| Aspecto | RFTM | MREV |
|---|---|---|
| Timeframe | Diario (1 run/día) | 1h (cada hora) |
| Universo | 55 ETFs | 6 cripto + 9 ETFs |
| Capital asignado | $75K (local) / $100K (GA) | $25K |
| Risk per trade | 5% | 5% |
| Max positions | 10 | 6 |
| Max position % | 25% portfolio | 40% notional |
| Entry | C1-C5 (trend + momentum + breakout + vol + atr) | X1-X4 (RSI≤45 + close≤BB_lower + vol + atr) |
| Stop inicial | entry − 1.5×ATR | entry − 2.0×ATR |
| Take profit final | entry + 2×(entry−stop), gateado por stage≥2 (E7) | SMA20 + 1.5×ATR dinámico (X1), sin gate de stage |
| Trailing | 3 fases (≥0.5, ≥1.5 ATR profit) | 1 fase (1.0×ATR desde max) |
| Partial TPs | 5% vende 50% → 7.5% vende 50% remanente | Idéntico |
| Time stop | 20 bars sin nuevo high | 120 horas (5 días) |
| Email window | Al final de cada run (1/día) | `EMAIL_HOURS_UTC=12` (1/día con dedup) |
| DB | `trading_paper.db` / `positions` (INTEGER qty) | `mrev_paper.db` / `mrev_positions` (REAL qty) |
| Kill switch | MAX_DRAWDOWN=20% | Idem |
| Broker stop | Local only (sin bracket) | Local only (sin bracket) |
| Ranking candidatos | Sort por `|RSI−62|` | Sin ranking |

---

## 17. Features recientes — verificación

| # | Feature | Estado | Cita |
|---|---|---|---|
| 1 | Partial TP dos etapas (5% / 7.5%) + env vars + backward compat | ✅ | RFTM 159-173, MREV 94-99, pipeline RFTM 1600-1646 / MREV 1264-1317 |
| 2 | `E7_take_profit` gateado por stage≥2 | ✅ | RFTM 450-457 |
| 3 | `sync_with_alpaca` inserta faltantes con stage=0 e `initial_qty=qty` | ✅ | RFTM ~820-925, MREV 467-541 |
| 4 | Detección cripto por prefijos + sufijos | ✅ en `seed_missing_positions.py` y RFTM 900-904. `[NOTA]` MREV en el bot live sólo usa `"/" in sym` (línea 178) — más simple pero no cubre sufijo `USD` sin slash. Sólo el seed lo normaliza. |
| 5 | `seed_missing_positions.py` migra cripto atrapada + siembra faltantes | ✅ dry-run lo confirma (incluye SOLUSD→MREV, GLD/SLV/AVAXUSD/etc.) |
| 6 | Traducciones email `partial_tp1_*` / `partial_tp2_*` | ✅ RFTM 1219-1223 |

---

## 18. Features recién implementadas en este repo (sesión anterior) — verificación

| # | Feature | Estado | Cita |
|---|---|---|---|
| 1 | SL sube a breakeven cuando dispara TP1 | ✅ RFTM líneas 1893-1905, MREV 1411-1423. `[NOTA]` Sólo dispara en el flujo de fill post-orden. Las posiciones actuales (sección 5) tienen stage=1 pero stop NO movido — fueron sembradas por `seed_missing_positions.py`, no por el fill real. El seed **no** aplica breakeven raise. |
| 2 | Email inmediato por TP1/TP2/E7 | ✅ RFTM `send_stage_event_email` línea 1442 + call-sites 1817-1870. MREV `send_stage_event_email` (espejo) + call-sites en el sells loop. Respeta `dry_run` ✅. |
| 3 | "Faltan X%" por posición en email diario RFTM | ✅ RFTM 1331-1360 |
| 4 | Email MREV habla de MREV (no RFTM) | ✅ **en el email diario** (líneas 1676-2030). `[ALERTA] NO en el email mensual` (líneas 852-1122 siguen con "Tus 2 robots" y `ACCOUNT_TOTAL_CAPITAL`). |

---

## 19. Resumen ejecutivo

### 3 cosas bien hechas — no tocaría
1. **Sync con Alpaca como source of truth** en ambos bots. Reconcilia local-vs-Alpaca en cada run, inserta faltantes con `stage=0, initial_qty=qty`. Tolerante a ejecuciones perdidas.
2. **Partial TP machine en dos etapas + breakeven post-TP1**. Env vars con defaults + backward compat + idempotente. Código limpio y simétrico en ambos bots.
3. **Seguridad básica**: `.env.paper` con permisos 600, `.gitignore` correcto, SQL parametrizado en las rutas críticas, sin leaks de secrets en logs.

### 3 cosas medio armadas — hay que terminarlas
1. **Tests drift**: 23/150 tests fallan contra código ya removido (E1/E2/E4 en RFTM, X3 en MREV). El test runner `run_tests.py` directamente crashea. Hay que actualizar los tests o borrar los obsoletos.
2. **Email mensual MREV** sigue mezclando datos RFTM (`ACCOUNT_TOTAL_CAPITAL`, `DAILY_BOT_CAPITAL`, sección "Tus 2 robots"). La sesión anterior sólo arregló el email diario.
3. **Código duplicado** entre RFTM y MREV: `_build_email_report`, `send_stage_event_email`, CSS. Falta extraer a `_email_helpers.py` (marcado como TODO implícito).

### 3 cosas claramente problema — atacar primero
1. **`[ALERTA]` No hay bracket orders en Alpaca.** Todos los stops son software-side. Si el bot se cae, el mercado gapea, o Alpaca rate-limita, las posiciones quedan sin protección. El prompt anterior asumía que el bracket estaba — no está.
2. **`[ALERTA]` SOLUSD atrapado en la DB de RFTM.** 3 unidades de SOL hoy viven en `trading_paper.db` pero son cripto — deberían estar en `mrev_paper.db`. El `seed_missing_positions.py` detecta esto pero todavía no se corrió en modo real. Mientras tanto, ningún bot maneja esa posición correctamente.
3. **`[ALERTA]` DB se actualiza sin confirmar fill real + partial fills no detectados.** Si Alpaca responde "partially_filled" o "pending", el bot escribe el fill como si fuera total. En mercados con slippage o liquidez baja, esto produce drift.

### 3 unknowns / supuestos a confirmar
1. **¿RFTM corre dos veces (launchd local + GitHub Actions)?** Si ambos están activos, doble ejecución y posible doble-compra. El plist está en `~/Library/LaunchAgents/` — hay que ver si `launchctl list | grep rftm.trader` muestra algo activo.
2. **`MAX_LEVERAGE=1.5` vs `MAX_POSITION_PCT=0.25`**. Con 10 posiciones al 25% c/u = 250% exposición, pero leverage cap es 1.5×. ¿Quién gana? Necesito verificar con una corrida real el equity vs long_market_value.
3. **El email MREV actualmente se dispara en ventana UTC=12.** En dry-run no envía, pero la ventana se aplica sólo al email diario. Los emails de stage (TP1/TP2/TP_FINAL) **no respetan ventana horaria** — pueden llegar a cualquier hora. ¿Es intencional? Creo que sí (son eventos accionables), pero confirmar.

### 3 riesgos potenciales de dinero (no de código)
1. **Correlación ignorada**: SPY + QQQ + IWM + XLK + MDY pueden estar abiertas simultáneamente en RFTM, todas con exposición ~100% al S&P. Con un crash de 5%, las 5 caen juntas y se dispara ~5 × 25% × 5% = 6.25% del portfolio en un día. El kill switch recién dispara a −20%, demasiado tarde para salir "barato".
2. **MREV sobre cripto sin hedge.** AVAX/DOGE/LINK/SOL tienen correlación altísima con BTC. Con stop a −2×ATR (que en cripto es ~5-10%), un dump nocturno de BTC disparando 4 stops iguales en la misma hora es plausible. El MREV time stop de 120h no ayuda.
3. **Ambos bots comparten buying power.** Si RFTM abre 10 posiciones ETF al 90% de buying power (via MAX_LEVERAGE=1.5), MREV a las 00:05 de la noche siguiente no tiene BP para comprar la señal de mean reversion en BTC. No hay coordinación — el primero que corre come el capital.

---

## 20. Necesito confirmar con el usuario

1. ¿`launchctl list | grep rftm.trader` muestra el agente activo? Si sí, ¿querés deshabilitar GA o launchd para no doble-ejecutar?
2. `seed_missing_positions.py` en modo real (sin `--dry-run`) — ¿lo corremos para arreglar SOLUSD y sembrar GLD/SLV/AVAX/etc.?
3. Los 23 tests que fallan (vs E1/E2/E4/X3 removidas) — ¿los borro o los reescribo?
4. El email mensual MREV mezcla datos — ¿lo reescribo igual que hice con el diario?
5. El hardcoded `0.90` como safety factor de buying power (aparece 5 veces) — ¿lo extraemos a env var (`ALPACA_BP_SAFETY=0.90`)?
6. `MAX_DRAWDOWN=0.20` hardcoded — ¿convertir a env var con default 0.20?
7. MREV sin guard `qty ≥ 2` en partial TP — en cripto qty es fraccional, pero ¿hay un mínimo de $ notional para no hacer micro-parciales?
8. El `bb_lower=9.20` vacío en root — ¿lo borro?
9. ¿Documentar en `CLAUDE.md` que ambos bots comparten cuenta y que el mismo símbolo puede abrirse en ambos si está en ambos universos?
10. ¿Querés refactor a `_email_helpers.py` compartido (RFTM + MREV)?

---

`AUDIT OK — informe generado (lectura nada más).`
