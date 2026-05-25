# PLAN DE MIGRACIÓN — Paper → Live Real Money

**Fecha:** 2026-04-22
**Autor:** Claude (sesión Cowork)
**Sistema actual:** Alpaca Paper, 2 bots (`standalone_paper_trader.py` RFTM + `standalone_mrev_trader.py` MREV)
**Capital objetivo de arranque:** < USD 2.000 (canary ultra-conservador)
**Jurisdicción del operador:** Argentina
**Broker elegido:** Alpaca Markets (Live) — plan A. Interactive Brokers — plan B si Alpaca no aprueba la cuenta.

---

## Resumen ejecutivo — por qué Alpaca y no otro

Decidí recomendarte Alpaca Live por tres razones concretas, no por costumbre:

1. **Zero refactor del execution layer.** Tu código ya llama `https://paper-api.alpaca.markets/v2/orders`. Para pasar a live son 15 archivos con una URL hardcoded a cambiar, más 4-5 guardas nuevas. Si te fueras a IBKR, `apps/svc_execution` + los dos `standalone_*.py` hay que reescribirlos contra `ib_insync`: son ~200 llamadas a la API del broker, 2-3 semanas de trabajo mínimo.
2. **Regulación real.** Alpaca es broker-dealer registrado en SEC y miembro FINRA. SIPC cubre hasta USD 500.000 si Alpaca quiebra. Comisiones 0 en stocks y ETFs US. Spread en cripto razonable.
3. **Margen operativo bajo.** Podés depositar desde USD 1, no hay exigencias de capital mínimo para la API trading.

**Riesgo que sí tenés que aceptar:** Alpaca como empresa es joven (fundada 2015, no es Schwab ni Fidelity). Si te vas a meter más de USD 50.000 en algún momento, moveríamos parte a IBKR como segunda pata. Pero para arrancar con < USD 2.000, Alpaca es la decisión correcta.

**El punto crítico no resuelto:** Alpaca actualizó en marzo 2026 la apertura de cuentas para no-residentes US, pero la lista exacta de países elegibles NO está pública. Hay que aplicar y ver. Si rechazan, el plan B es IBKR. **Esto es la Fase 0 y tenés que hacerla ANTES de tocar una línea de código.**

---

## Baseline — qué tenemos hoy

Resumen auditado (ver `AUDIT_REPORT_20260422.md` para detalle completo):

- **RFTM** (`standalone_paper_trader.py`, 2.091 líneas). Trend-following diario sobre 55 ETFs. DB `trading_paper.db`. 11 posiciones abiertas desde 2026-04-15, con partial TP1 ya tomado en todas.
- **MREV** (`standalone_mrev_trader.py`, ~2.100 líneas). Mean reversion horario sobre 6 cripto + 9 ETFs. DB `mrev_paper.db`. 0 trades cerrados en DB actual, 45 señales observadas, todas HOLD (no disparó entries en el período capturado).
- **Ambos bots comparten la misma cuenta Alpaca Paper** (USD 100k virtual, RFTM tiene asignado 75k, MREV 25k).
- **Tests:** 23 de 150 rotos (drift contra condiciones E1/E2/E4/X3 ya removidas).
- **Problemas críticos identificados en la auditoría** (no corregir antes de ir live es inaceptable):
  - `[CRÍTICO]` No hay bracket orders en Alpaca. Los stops son 100% software-side. Si el bot se cae o Alpaca rate-limitea, las posiciones quedan desprotegidas.
  - `[CRÍTICO]` DB local se actualiza sin confirmar fill real. Partial fills (`partially_filled` / `pending`) se escriben como totales.
  - `[CRÍTICO]` RFTM y MREV pueden comprar el mismo símbolo al mismo tiempo (SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO, ARKK están en ambos universos). No hay mutex.
  - `[CRÍTICO]` SOLUSD hoy vive en la DB de RFTM pero es cripto — debería estar en MREV. Desalineado.
  - `[ALTO]` `MAX_DRAWDOWN=20%` hardcoded. Con USD 2.000 eso es USD 400 de pérdida antes de apagar.
  - `[ALTO]` No hay daily loss limit. El drawdown de 20% es acumulado lifetime, no diario.
  - `[ALTO]` `MAX_LEVERAGE=1.5` vs `MAX_POSITION_PCT=0.25` con 10 posiciones permite 250% notional teórico pero leverage cap es 150%. Relación no verificada — podría abrir menos posiciones de las que cree o rechazar compras.

**No vamos a live hasta que los 4 críticos estén resueltos.** Este es el dique.

---

## Performance paper — qué sabemos y qué no

Honestidad primero: **los DBs locales NO tienen suficientes trades cerrados** para darte un número de Sharpe o win rate confiable.

- `trading_paper.db/positions`: 11 filas, todas `status=open`, `realized_pnl=None`. Único dato cerrado: `partial_tp_taken=1` en las 11 (o sea, la mitad de cada posición se vendió en TP1 ≥5%).
- `mrev_paper.db/mrev_positions`: 0 filas. Nunca disparó una entry completa.

Lo que sí sabemos:
- RFTM entró en 11 ETFs (ARGT, ECH, EWJ, FLBR, IWM, PAVE, QQQ, SOLUSD, SPY, XLE, XLK) el 2026-04-15 con capital inicial 100k, tamañando por 5% risk / 1.5×ATR stop. Ya tomó TP1 (+5%) en todas.
- MREV entre 2026-04-09 y 2026-04-11 escaneó el universo y produjo solo HOLD signals (RSI siempre > 45, o close > bb_lower). Equity se mantuvo en 25k sin trades.

**Conclusión operativa:** antes de ir live, tenemos que correr `analyze_trades.py` contra la cuenta Alpaca Paper real (no contra la DB local, que está desactualizada) para extraer la performance histórica verdadera desde el 2026-03-23 (arranque). Ese análisis manda el orden de rollout: va primero a live el bot con mejor Sharpe ajustado por drawdown máximo.

Spoiler de orden probable basado en lo observado en el AUDIT: **RFTM primero, MREV después.** RFTM ya tiene track record real en Alpaca; MREV apenas ha disparado señales.

---

## FASE 0 — Elegibilidad y cuenta (días 1-7, paralela al código)

**Objetivo:** saber si Alpaca te acepta como residente argentino ANTES de hacer cualquier refactor.

### 0.1 Aplicación Alpaca Live (día 1)
- Entrar a `alpaca.markets` → sign up → "International / Non-US Tax Resident".
- Llenar W-8BEN (formulario fiscal US para no-residentes — te retiene impuesto a dividendos, no a capital gains).
- Subir: pasaporte argentino + comprobante de domicilio (servicio público, <3 meses) + CUIT (opcional pero acelera).
- Esperar 3-5 días hábiles. Si rechazan, saltar a **Plan B IBKR** (ver apéndice D).

### 0.2 Setup de seguridad de la cuenta (día 5-7, una vez aprobada)
- Activar 2FA por app (no SMS — SMS es inseguro, SIM swap).
- Configurar withdrawal whitelist: solo podés retirar a tu cuenta bancaria en AR vía Rapyd o wire transfer. Nada de wallets cripto de salida.
- Generar API keys **separadas para trading**, NO las mismas que usaste en paper. Crear 2 pares: `RFTM_LIVE_*` y `MREV_LIVE_*` (separadas para poder rotar/revocar una sin tumbar la otra).
- Restringir las API keys a "Trading + Account Read". Nunca "Funds Transfer".
- Anotar credenciales en un password manager (1Password / Bitwarden), NO en el `.env`.

### 0.3 Fondeo inicial (día 7)
- Depositar **USD 500** inicial vía Rapyd. Este es el monto canary Semana 1.
- NO depositar los 2.000 de una. Si tu banco AR / MEP / el cambio te cobra comisión alta, hacer 1 test de USD 100 primero.
- Confirmar que el dinero llegó a la cuenta Alpaca antes de seguir.

### 0.4 Tareas paralelas de limpieza del repo (días 1-7)
Mientras esperás la aprobación, arrancás a ensuciarte las manos con la deuda técnica:
- Borrar `bb_lower=9.20` (archivo basura), `.fuse_hidden*`, `mrev_paper.db.bak`.
- Correr `seed_missing_positions.py` en modo real para arreglar el SOLUSD atrapado y sincronizar posiciones faltantes.
- Decidir los 23 tests rotos: borrar los que testean E1/E2/E4/X3 (ya removidas del código), o reescribirlos.

---

## FASE 1 — Hardening del código (días 8-12)

**Objetivo:** cerrar los 4 bugs críticos del AUDIT y parametrizar todo lo que hoy está hardcoded.

### 1.1 Parametrizar URLs hardcoded (día 8, 1-2 horas)
Archivos con `https://paper-api.alpaca.markets` literal:
- `standalone_paper_trader.py:175` (`ALPACA_PAPER_URL`)
- `standalone_mrev_trader.py:140`
- `apps/svc_data_1h/alpaca_client.py:158`
- `seed_missing_positions.py:41`, `mark_partial_tp_done.py:29`, `sell_half_profits.py:31`, `analyze_trades.py:23`
- `.github/workflows/daily_trade.yml:47`, `.github/workflows/mrev_hourly.yml`
- Tests + RUNBOOK (documentación).

**Cambio:** reemplazar todo por `os.environ["ALPACA_BASE_URL"]` leído del `.env`. Nunca más un literal. `config/settings.py` ya tiene la guarda correcta (línea 107-110) — hay que hacerla autoritativa.

### 1.2 Separación estricta paper vs live (día 8)
- `.env.paper` queda como está (USD virtual).
- Crear `.env.live` con `TRADING_MODE=live`, `DRY_RUN=false`, `ALPACA_BASE_URL=https://api.alpaca.markets/v2`, API keys nuevas de la 0.2.
- Permisos `chmod 600` en ambos.
- DBs separadas: `trading_live.db` y `mrev_live.db` (nuevas, vacías). Jamás mezclar.
- Script de arranque `run_live.sh` que:
  1. exige `.env.live` presente,
  2. rechaza si `$ALPACA_BASE_URL` contiene la palabra `paper`,
  3. pide confirmación interactiva (`echo "Escribí LIVE para confirmar"`) la primera vez,
  4. valida que las DBs `*_live.db` existen y están inicializadas.

### 1.3 Bracket orders (día 9, CRÍTICO)
Hoy tus stops son software-side. Si tu VPS se cae a las 10:03 de un lunes crashy, las posiciones quedan sin protección hasta las 10:15 cuando reinicia — ya te puede haber caído el mundo encima.

**Cambio en `alpaca_submit_order`** (RFTM ~línea 1830, MREV ~línea 1380):

```python
order = {
    "symbol": symbol,
    "qty": qty,
    "side": "buy",
    "type": "market",
    "time_in_force": "day",
    "order_class": "bracket",
    "stop_loss": {"stop_price": round(stop_loss, 2)},
    # NO take_profit en bracket — lo manejamos con partial TPs dinámicos
}
```

**Importante:** bracket en Alpaca requiere `take_profit` como campo obligatorio. Truco: setear `take_profit` en un nivel muy lejano (`entry * 10`) que sirva como "absoluto no quiero perder conexión" y seguir manejando el TP dinámico por software. El stop server-side es el único que realmente protege.

Si bracket no funciona en cripto (Alpaca no lo soporta en todas), fallback: disparar `STOP` order separada inmediatamente después del `BUY` fill, con `stop_price = entry - 2×ATR`. Vincularla a la posición por un `client_order_id` trackeable.

### 1.4 Confirmación de fill real (día 10, CRÍTICO)
Hoy el código asume fill total si `result` no es None. Cambio:

```python
# Después de submit_order
order_id = result["id"]
# Poll hasta status terminal, máximo 30s
for _ in range(30):
    order = alpaca_get_order(order_id)
    if order["status"] in ("filled", "canceled", "rejected", "expired"):
        break
    time.sleep(1)
else:
    # Timeout — cancelar y NO actualizar DB
    alpaca_cancel_order(order_id)
    log.error(f"Timeout esperando fill {order_id}, orden cancelada")
    return None

if order["status"] == "filled":
    filled_qty = int(order["filled_qty"])
    filled_price = float(order["filled_avg_price"])
    # UPDATE DB solo con qty real filleada
elif order["status"] == "partially_filled":
    # Escribir solo lo filleado, cancelar el resto
    alpaca_cancel_order(order_id)
    ...
```

Esto elimina el drift DB vs Alpaca.

### 1.5 Mutex RFTM/MREV para mismo símbolo (día 10)
Opciones:
- **A (simple):** sacar los 9 ETFs overlapping de MREV. MREV queda solo con 6 cripto + los ETFs únicos. Es lo más fácil y lo recomiendo para canary.
- **B (correcto):** crear una tabla `shared_positions` en una DB común `coordinator.db` que ambos bots consultan antes de abrir. Requiere más código.

**Para la fase canary uso A. Después del día 60 evaluamos B.**

### 1.6 Daily loss limit + kill switch env vars (día 11)
Variables nuevas en `.env.live`:
```
MAX_DRAWDOWN=0.10            # 10% lifetime (era 20%, lo bajamos para live)
MAX_DAILY_LOSS_USD=40        # stop trading si perdiste USD 40 en el día
MAX_POSITION_USD=300         # una posición nunca excede USD 300
MAX_TOTAL_POSITIONS=4        # máximo 4 posiciones simultáneas entre ambos bots
MAX_ORDERS_PER_HOUR=10       # rate limit, previene loops infinitos
KILL_SWITCH_FILE=/Users/charlie/Desktop/trading-system/KILL    # si existe, ningún bot abre nuevas
ALPACA_BP_SAFETY=0.80        # 80% del buying power (hoy hardcoded 0.90, lo bajamos)
```

Cada bot, al arrancar una iteración:
1. `if os.path.exists(KILL_SWITCH_FILE): exit`
2. Leer equity Alpaca → si equity < `initial_equity × (1 - MAX_DRAWDOWN)` → kill switch ON + email urgente.
3. Leer PnL del día (realized + unrealized) → si < `-MAX_DAILY_LOSS_USD` → kill switch ON hasta día siguiente.
4. Contar órdenes últimas 60 min → si > `MAX_ORDERS_PER_HOUR` → skip iteración + alerta.

### 1.7 Log de auditoría por orden (día 12)
Toda orden live escribe a `logs/orders_live.jsonl` una línea JSON con:
```
{ts, bot, symbol, side, qty, submitted_price, order_id, alpaca_response, 
 equity_before, equity_after, kill_switch_state, dry_run:false}
```
Append-only. Para reconciliar si algo sale mal.

### 1.8 PWA de monitoreo (días 12-13, NUEVO)

**Objetivo:** reemplazar la falta de app oficial de Alpaca con un dashboard propio que se instala en el celu como app (icono en pantalla, sin pasar por App Store). Monitoreo unificado de ambos bots en un solo lugar.

**Stack:** `apps/api/main.py` (FastAPI, ya existe) sirve endpoints JSON + página HTML + `manifest.json` + `service-worker.js`.

**Endpoints nuevos en `apps/api/main.py`:**
```
GET  /api/monitor/equity          → equity actual, cash, 24h change, peak
GET  /api/monitor/positions       → lista posiciones abiertas ambos bots con PnL
GET  /api/monitor/orders          → últimas 20 órdenes de orders_live.jsonl
GET  /api/monitor/heartbeat       → timestamp último ciclo OK de cada bot
GET  /api/monitor/kill_switch     → estado (on/off)
POST /api/monitor/kill_switch/on  → activar kill switch (protegido por token)
POST /api/monitor/kill_switch/off → desactivar (protegido por token)
```

**Frontend (single HTML + Tailwind via CDN + JS vanilla):**
- Header: equity + daily PnL en grande, color-coded.
- Grid: tarjetas de posiciones abiertas, cada una con símbolo, qty, entry, actual, PnL %, stop.
- Log últimas órdenes scrolleable.
- Heartbeat indicator rojo/verde por bot.
- Botón kill switch (requiere confirmación + token).
- Pull-to-refresh + auto-refresh cada 30s.

**PWA manifest (`static/manifest.json`):**
```json
{
  "name": "RFTM/MREV Monitor",
  "short_name": "Trading Monitor",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0b1020",
  "theme_color": "#0b1020",
  "icons": [{"src":"/static/icon-192.png","sizes":"192x192","type":"image/png"},
            {"src":"/static/icon-512.png","sizes":"512x512","type":"image/png"}]
}
```

**Service worker:** cachea shell de la app para abrir offline y mostrar último estado conocido.

**Instalación en el celu (Safari iOS):** abrir la URL → compartir → "Agregar a pantalla de inicio" → queda como app nativa con icono.

**Push notifications:** para iOS necesitaría Apple Developer account + APNS. Demasiado overhead para el canary. **Las notificaciones las manejamos por Telegram**, que ya tenemos. La PWA es para mirar estado cuando vos querés abrir.

**Autenticación:** basic auth simple (usuario + password en `.env`) + HTTPS. La URL queda detrás del VPN o solo accesible desde tu IP fija. Nunca pública sin auth.

**Cuándo corre:** endpoint servido por `apps/api` que ya está en tu repo. Se levanta junto con el bot o como servicio separado en el VPS.

---

## FASE 2 — Testing exhaustivo (días 14-17)

### 2.1 Reparar los 23 tests rotos (día 14)
Según el AUDIT los tests que fallan son contra condiciones E1/E2/E4 de RFTM y X3 de MREV ya removidas. Dos opciones:
- Borrar. Son regresión histórica, el código ya no las tiene.
- Reescribir contra las condiciones actuales (C1-C5 para RFTM, X1-X4 para MREV).

**Recomiendo:** borrar los que testean features muertas, reescribir los que validan invariantes (position sizing, stop calculation, partial TP logic).

### 2.2 Tests nuevos de seguridad live (días 15-16)
Tests a agregar en `tests/test_live_safety.py` (nuevo):
- `test_refuses_live_with_paper_url`: si `TRADING_MODE=live` pero `ALPACA_BASE_URL` contiene `paper`, el bot DEBE crashear al arrancar.
- `test_kill_switch_file_blocks_entry`: crear el archivo KILL y confirmar que no se abre ninguna posición.
- `test_daily_loss_limit_triggers`: simular PnL day de -USD 45 y verificar que el bot no abre.
- `test_max_position_usd_cap`: señal que pediría USD 500 debe recortarse a USD 300.
- `test_bracket_order_has_stop`: toda orden buy debe tener `stop_loss` en el payload.
- `test_partial_fill_updates_correctly`: mock respuesta `partially_filled` → DB refleja solo lo filleado.
- `test_symbol_mutex`: si RFTM abrió SPY, MREV no debe abrir SPY en la misma iteración.

### 2.3 Dry-run de 48h apuntando a Alpaca Paper con código live (día 17)
Una última prueba: correr el bot **con el código de la rama live** (nuevas guardas, bracket orders, kill switch, etc.) pero con `ALPACA_BASE_URL` apuntando todavía al **paper**. DRY_RUN=false.

- Si el bot opera bien 48 horas paper con el código live, estás listo.
- Si algo explota, lo arreglás antes de tocar USD real.
- Al terminar: commit tag `v1.0-pre-live`.

---

## FASE 3 — Canary rollout (días 18-46, 4 semanas)

**Filosofía: no estás tratando de ganar, estás tratando de validar que el sistema live se comporta igual que paper + no pierde más que lo esperado.**

### Semana 1 (días 18-24) — USD 500, solo RFTM, tamaño micro
Configuración `.env.live`:
```
MAX_POSITION_USD=100
MAX_TOTAL_POSITIONS=3
MAX_DAILY_LOSS_USD=20
INITIAL_CAPITAL=500
RFTM_CAPITAL=500
MREV_CAPITAL=0                 # MREV apagado esta semana
```
- Solo corre RFTM, 1 vez por día en la ventana normal.
- Capital asignado: USD 500 real.
- Máximo USD 100 por trade, 3 trades simultáneos max (USD 300 deployed, USD 200 cash buffer).
- Criterio de salida: si al final de la semana perdiste más de USD 30, pausar y revisar.

### Semana 2 (días 25-31) — USD 1.000, RFTM + MREV observación
```
MAX_POSITION_USD=150
MAX_TOTAL_POSITIONS=4
MAX_DAILY_LOSS_USD=30
INITIAL_CAPITAL=1000
RFTM_CAPITAL=800
MREV_CAPITAL=200
```
- RFTM sigue operando normal.
- MREV arranca PERO con `MREV_DRY_RUN=true` (log de señales sin submit). Es para validar que las señales que genera MREV en el universo reducido son razonables con datos reales.
- Si MREV dispara < 3 señales viables en la semana → pausar MREV y revisar parámetros antes de la semana 3.

### Semana 3 (días 32-38) — MREV live micro
```
MAX_POSITION_USD=200
MAX_TOTAL_POSITIONS=5
MAX_DAILY_LOSS_USD=40
INITIAL_CAPITAL=1500
RFTM_CAPITAL=1200
MREV_CAPITAL=300
```
- MREV sale de dry_run, empieza a operar con capital USD 300.
- Si ambos bots quieren abrir SPY/QQQ (overlapping si lo dejamos con 1.5 A) → el primero que arranca la iteración gana. El otro skip.

### Semana 4 (días 39-46) — escalado normal
```
MAX_POSITION_USD=300
MAX_TOTAL_POSITIONS=6
MAX_DAILY_LOSS_USD=60
INITIAL_CAPITAL=2000
RFTM_CAPITAL=1500
MREV_CAPITAL=500
```
- Operación normal con los USD 2.000 comprometidos.
- A partir de acá, si el sistema prueba ser estable 2 semanas más, evaluás depositar más.

### Criterios de abort (cualquier fase)
- **Hard stop:** `-8%` del capital inicial de la fase → pausar TODO, apagar, revisar. No volver hasta entender qué pasó.
- **Soft stop:** `-5%` en un día → kill switch ON hasta mañana.
- **Reconciliation fail:** si al final de día la DB local no cuadra con Alpaca `list_positions` → investigar antes de la próxima iteración.

---

## FASE 4 — Monitoreo 24/7 y operación (continuo)

### 4.1 Alertas Telegram (día 18, antes de encender)
Ya hay `TELEGRAM_BOT_TOKEN` en el `.env.example`. Configurar:
- Notificación en cada orden submitted (bot, symbol, qty, price).
- Notificación en cada stop hit (crítica).
- Notificación en cada kill switch trigger (crítica).
- Notificación daily: PnL del día + equity + posiciones abiertas.

### 4.2 Dashboard PWA (ya construido en Fase 1.8)
La PWA construida en Fase 1.8 sirve como dashboard 24/7. Accesible desde el celu como app, desde el browser del laptop, o desde cualquier lado.
- Instalada en Safari iOS como app nativa (icono en pantalla de inicio).
- Auto-refresh cada 30s.
- Basic auth + HTTPS + restricción IP opcional.
- Funciona offline con service worker cacheado.

### 4.3 Reconciliación diaria automática
Job cron a las 00:00 UTC:
```python
alpaca_positions = alpaca_list_positions()
db_positions = db_query("SELECT * FROM positions WHERE status='open'")
diff = compare(alpaca_positions, db_positions)
if diff:
    send_telegram_alert(f"DESYNC DB/Alpaca: {diff}")
    create_kill_switch_file()
```

### 4.4 Backup diario
- Copiar `trading_live.db` y `mrev_live.db` a `backups/YYYYMMDD/` cada día a las 23:59.
- Retener 30 días locales + sync opcional a S3/iCloud Drive.

### 4.5 Weekly review (cada domingo)
Checklist manual:
- Sharpe ratio de la semana (usar `analyze_trades.py`).
- Máx drawdown intradía.
- Win rate.
- Comparar con baseline paper.
- ¿Hay algún trade que "no debería haber pasado"? Investigar.
- Revisar los logs de `orders_live.jsonl` buscando anomalías.

---

## Decisiones confirmadas con el operador (2026-04-22)

- **CUIT activo:** ✅ disponible para W-8BEN y onboarding.
- **Fixes de código:** ya corriendo en Claude Code en paralelo a la aplicación Alpaca.
- **Infraestructura actual:** GitHub Actions corre los bots (ver `.github/workflows/daily_trade.yml` y `mrev_hourly.yml`). **Plan de migración a VPS se pospone para después del canary** (ver sección "Infraestructura" abajo).
- **Rollout Semana 1:** solo RFTM. MREV arranca en Semana 2 en modo observación. Aceptado.
- **Monitoreo:** daily checks confirmados. Telegram + PWA propia + web Alpaca responsive.
- **Broker:** Alpaca Live (no IBKR). PWA resuelve el gap de app móvil.

### Nota sobre infraestructura (GitHub Actions vs VPS)

Hoy `daily_trade.yml` y `mrev_hourly.yml` corren en GitHub Actions con secrets. Pros: gratis, simple, ya funciona. **Cons para live:**
1. Latencia variable: un cron de GA puede arrancar entre 0 y 15 minutos después de la hora programada. Para trades timing-sensitive (apertura de mercado) eso es un problema.
2. Sin control fino: si se cae un run, GA puede no notificar a tiempo.
3. Secrets en GA es OK pero menos control que en un VPS propio.
4. La PWA necesita un endpoint permanente — GA solo corre on-demand. **La PWA requiere sí o sí un VPS o algo always-on.**

**Plan:**
- **Fase 0-3:** GitHub Actions sigue corriendo los bots. VPS Hetzner (USD 6/mes) se levanta para hostear SOLO la API + PWA. Los bots en GA escriben a la misma DB que lee la API del VPS (vía rsync a cada fin de run, o simplemente un endpoint `POST /api/ingest/snapshot` que los bots llaman).
- **Post-canary (día 46+):** evaluamos mover los bots al VPS para eliminar la latencia de GA. Solo si el sistema probó ser estable y vale la pena el trabajo.

---

## Apéndice A — Checklist pre-live (antes de apretar el botón)

Antes de cambiar `DRY_RUN=false` en `.env.live` por primera vez, TODO esto tiene que estar en verde:

- [ ] Cuenta Alpaca Live aprobada y fondeada con USD 500.
- [ ] 2FA activo en cuenta Alpaca.
- [ ] API keys live generadas (distintas de las paper), con scope limitado.
- [ ] Withdrawal whitelist configurado.
- [ ] `.env.live` creado, permisos 600, fuera de git (verificar con `git status`).
- [ ] `trading_live.db` y `mrev_live.db` creadas vacías.
- [ ] URLs paramétricas, sin literals `paper-api` fuera de `.env.paper`.
- [ ] Bracket orders con stop server-side funcionando (test manual con 1 share SPY en paper).
- [ ] Confirmación de fill real implementada, tests pasando.
- [ ] Mutex RFTM/MREV: MREV no tiene SPY/QQQ/IWM/XLE/XLF/GLD/SLV/BITO/ARKK en su universo para canary.
- [ ] Kill switch file probado (crear archivo → bot no abre posiciones).
- [ ] `MAX_DRAWDOWN=0.10`, `MAX_DAILY_LOSS_USD=20`, `MAX_POSITION_USD=100` para Semana 1.
- [ ] Telegram alerts probadas (trigger una fake).
- [ ] Log `orders_live.jsonl` escribiéndose.
- [ ] Cron de reconciliación diaria instalado.
- [ ] Cron de backup diario instalado.
- [ ] Dry-run 48h de código live apuntando a paper completado sin errores.
- [ ] Los 4 críticos del AUDIT cerrados: bracket orders, fill confirmation, mutex símbolos, SOLUSD resuelto.
- [ ] Tests nuevos de live safety pasando.
- [ ] VPS levantado (Hetzner/DO USD 6/mes) con HTTPS + basic auth.
- [ ] PWA accesible desde el celu, instalada en pantalla de inicio, mostrando datos paper correctamente.
- [ ] Kill switch desde PWA probado (botón activa el file, bot frena en próxima iteración).
- [ ] Tag `v1.0-pre-live` creado en git.

---

## Apéndice B — Variables `.env.live` (plantilla)

```ini
# ───── Trading mode ─────
TRADING_MODE=live
DRY_RUN=false
DEBUG=false

# ───── Alpaca Live ─────
ALPACA_API_KEY=<nueva key live, no la paper>
ALPACA_SECRET_KEY=<nueva secret live>
ALPACA_BASE_URL=https://api.alpaca.markets/v2

# ───── Capital / caps (Semana 1) ─────
INITIAL_CAPITAL=500
RFTM_CAPITAL=500
MREV_CAPITAL=0
MAX_DRAWDOWN=0.10
MAX_DAILY_LOSS_USD=20
MAX_POSITION_USD=100
MAX_TOTAL_POSITIONS=3
MAX_ORDERS_PER_HOUR=10
ALPACA_BP_SAFETY=0.80

# ───── Kill switch ─────
KILL_SWITCH_FILE=/Users/charlie/Desktop/trading-system/KILL

# ───── DB ─────
RFTM_DB_PATH=/Users/charlie/Desktop/trading-system/trading_live.db
MREV_DB_PATH=/Users/charlie/Desktop/trading-system/mrev_live.db

# ───── Alertas ─────
TELEGRAM_BOT_TOKEN=<ya configurado>
TELEGRAM_CHAT_ID=<ya configurado>
EMAIL_USER=<ya configurado>
EMAIL_PASSWORD=<ya configurado>

# ───── Logging ─────
LOG_LEVEL=INFO
LOG_FORMAT=json
ORDERS_AUDIT_LOG=/Users/charlie/Desktop/trading-system/logs/orders_live.jsonl

# ───── Sentry opcional ─────
SENTRY_DSN=<opcional, pero recomendado para live>
```

---

## Apéndice C — Kill switch manual

**Si algo huele raro, hacé esto INMEDIATAMENTE desde cualquier terminal:**

```bash
# 1. Impedir que se abran nuevas posiciones
touch /Users/charlie/Desktop/trading-system/KILL

# 2. (Opcional) cerrar todo en Alpaca ahora mismo
curl -X DELETE https://api.alpaca.markets/v2/positions \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"

# 3. Cancelar todas las órdenes pendientes
curl -X DELETE https://api.alpaca.markets/v2/orders \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"
```

Después, con calma, investigás qué pasó antes de borrar el archivo KILL.

---

## Apéndice D — Plan B: Interactive Brokers (si Alpaca rechaza)

Si la Fase 0.1 termina en rechazo de Alpaca para Argentina:

1. **IBKR onboarding** (3-5 días): cuenta IBKR Lite (no Pro, para evitar minimums). Requisitos Argentina: pasaporte, CUIT, comprobante domicilio, W-8BEN.
2. **Fondeo**: IBKR acepta wire transfer desde AR vía Galicia, Santander, BIND, etc. MEP/CCL a USD son la vía típica.
3. **Refactor código**: reemplazar `alpaca_submit_order` / `alpaca_get_order` / etc. por equivalentes en `ib_insync`. Cambio mayor, ~2 semanas:
   - Instalar TWS o IB Gateway (corre como servicio local, el bot se conecta por socket).
   - Reescribir el cliente broker en `apps/svc_execution/ibkr_client.py`.
   - Reescribir los standalone trader calls.
   - Bracket orders en IBKR son nativos y robustos — ventaja clara sobre Alpaca.
4. **Costo distinto**: IBKR cobra USD 0.005/share (min USD 1/order) en acciones US, peor que Alpaca USD 0 para volúmenes chicos. Compensa con mejor ejecución en tamaños grandes.
5. Las fases 1-4 de este plan se mantienen igual, solo cambia el broker adapter.

---

## Timeline consolidado

| Día | Fase | Hito |
|-----|------|------|
| 1 | 0.1 | Aplicar cuenta Alpaca Live |
| 1-7 | 0.4 | Limpieza deuda técnica (en paralelo en Claude Code) |
| 5-7 | 0.2-0.3 | Setup seguridad + primer fondeo USD 500 |
| 8 | 1.1-1.2 | URLs paramétricas + `.env.live` |
| 9 | 1.3 | Bracket orders |
| 10 | 1.4-1.5 | Fill confirmation + mutex |
| 11 | 1.6 | Daily loss + kill switch |
| 12 | 1.7 | Log auditoría |
| 12-13 | 1.8 | **PWA dashboard + VPS + endpoints API** |
| 14-16 | 2.1-2.2 | Tests (existentes + nuevos de live safety) |
| 17 | 2.3 | Dry-run 48h de código live contra paper |
| 18-24 | 3 (S1) | Canary RFTM USD 500 |
| 25-31 | 3 (S2) | MREV observación USD 1.000 |
| 32-38 | 3 (S3) | MREV live USD 1.500 |
| 39-46 | 3 (S4) | Escalado USD 2.000 |
| 47+ | 4 | Operación + monitoreo continuo |

**Total: ~6.5 semanas de día 1 hasta USD 2.000 operando en vivo estable + PWA en el celu.**

---

## Próximos pasos inmediatos (esta semana)

Decisiones ya tomadas — acá el orden de ataque de los próximos 7 días:

1. **Día 1 (hoy o mañana):** entrar a `alpaca.markets`, aplicar a cuenta Live como no-residente US, llenar W-8BEN, subir pasaporte + comprobante domicilio + CUIT. Empezar el reloj de 3-5 días hábiles de review.
2. **Días 1-7 (en paralelo):** las tareas de limpieza de Fase 0.4 (seed_missing_positions, borrar basura, decidir los 23 tests) + empezar la Fase 1.1 (parametrizar URLs, `.env.live`). Las tenés vos corriendo en Claude Code.
3. **Día 3:** cotizar VPS (Hetzner CX11 USD 3.5/mes o DigitalOcean droplet básico USD 6/mes). No comprar todavía — lo levantamos en Fase 1.8.
4. **Día 5:** primer test de fondeo: USD 100 a Alpaca vía Rapyd, ver cuánto tarda, qué comisión te cobran.
5. **Día 7:** si Alpaca aprobó, completar fondeo USD 500. Si rechazó, activar Plan B IBKR (apéndice D).

---

*Fin del plan. Este documento es la base de trabajo — cada fase tiene que terminar con un check antes de arrancar la siguiente. No hay atajos cuando es plata real.*
