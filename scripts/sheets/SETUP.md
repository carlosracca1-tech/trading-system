# Google Sheets — Trade Logger Setup

Guía paso a paso para que cada trade que hagan los bots se loggee
automáticamente a una hoja de Google Sheets tuya.

Tiempo total estimado: **10 minutos**.

---

## 1. Crear la hoja

1. Abrir [sheets.google.com](https://sheets.google.com) → **Blank spreadsheet**
2. Renombrar el archivo a algo descriptivo, ej. `Trading Bot Log`
3. En la pestaña de abajo, renombrar la hoja por defecto a **`RFTM`**
4. Click en el `+` para crear otra hoja y nombrarla **`MREV`**
5. Las dos hojas pueden quedar vacías — el script va a poner los headers
   automáticamente la primera vez que reciba un evento

---

## 2. Pegar el Apps Script

1. En la hoja: **Extensions → Apps Script**
2. Se abre el editor. Borrar todo lo que dice `function myFunction() { ... }`
3. Abrir el archivo `scripts/sheets/apps_script.gs` del repo y copiarlo
   completo
4. Pegarlo en el editor de Apps Script
5. **Ctrl+S** (o Cmd+S) para guardar. Te va a pedir un nombre para el
   proyecto — `trade-logger` está bien

---

## 3. Deploy como Web App

1. Arriba a la derecha: **Deploy → New deployment**
2. Click en el ícono del engranaje al lado de "Select type" → elegir
   **Web app**
3. Configurar:
   - Description: `trade-events-webhook`
   - Execute as: **Me (tu email)**
   - Who has access: **Anyone**
     > *La URL es secreta y no se va a publicar — solo tu repo de GitHub
     > la va a tener como secret*
4. **Deploy** → la primera vez Google te pide autorización:
   - "Authorize access" → elegir tu cuenta
   - "Google hasn't verified this app" → click **Advanced** → click
     **Go to trade-logger (unsafe)** → **Allow**
     > *No es unsafe, es porque vos sos el dev. Es tu propio código.*
5. Después del deploy te muestra una **Web app URL** tipo:
   `https://script.google.com/macros/s/AKfycby.../exec`
6. **Copiá esa URL completa**. Esa es tu `SHEETS_WEBHOOK_URL`.

---

## 4. Test rápido del webhook

Pegá tu URL en el navegador. Debería responder algo como:

```json
{"status":"alive","timestamp":"2026-05-11T15:00:00.000Z"}
```

Si ves eso, el webhook está vivo.

---

## 5. Agregar la URL a GitHub Secrets

1. Ir a tu repo en GitHub → **Settings → Secrets and variables → Actions**
2. **New repository secret**:
   - Name: `SHEETS_WEBHOOK_URL`
   - Secret: pegar la URL del paso 3
3. **Add secret**

Los workflows ya están cableados (`daily_trade.yml`, `mrev_hourly.yml`,
`rftm_watchdog.yml`, `mrev_watchdog.yml`) para pasar este secret como
env var al bot. No tenés que editar nada del workflow.

---

## 6. Backfill histórico (corre UNA SOLA VEZ)

Para que la hoja tenga los trades de los últimos 90 días desde Alpaca:

```bash
cd ~/Desktop/trading-system

# Previsualizar primero (no escribe nada)
export SHEETS_WEBHOOK_URL='https://script.google.com/macros/s/...../exec'
python3 scripts/sheets/backfill_to_sheets.py --days 90 --dry-run

# Si todo se ve bien, correr de verdad
python3 scripts/sheets/backfill_to_sheets.py --days 90
```

El script es **idempotente**: si lo corrés dos veces, los eventos que
ya están en la hoja se skipean (el Apps Script chequea `event_id`).

---

## 7. Verificar live

A partir del próximo `git push` con esta versión del código, **cada
nuevo trade va a aparecer en la hoja en menos de 5 segundos** después
del fill.

Para probar en frío sin esperar al próximo cron:

```bash
# Trigger manual del watchdog desde GH Actions UI con DRY_RUN=false
# → cuando ejecute un partial TP o full exit, el evento aparece en la hoja
```

---

## Estructura de la hoja

Cada fila es un evento (BUY / SELL_TP1 / SELL_TP2 / SELL_FINAL_TP /
SELL_STOP / SELL_TRAIL / SELL_TIME). Mismo `trade_id` agrupa todos los
eventos de una misma compra. Si comprás QQQ, cerrás todo, y volvés a
comprar QQQ, ese es **otro `trade_id`** — no se mezclan.

Columnas:

| Columna | Qué es |
|---|---|
| `trade_id` | ID único del trade (ej. `RFTM-abc12345`) |
| `event_id` | ID único de esta fila |
| `timestamp_utc` | Cuándo pasó el evento (UTC) |
| `bot` | `RFTM` o `MREV` |
| `symbol` | El activo |
| `side` | `BUY`, `SELL_TP1`, `SELL_TP2`, `SELL_FINAL_TP`, `SELL_STOP`, etc. |
| `qty` | Cantidad en esta operación |
| `price` | Precio de fill |
| `notional` | qty × price |
| `stage` | 0/1/2 — etapa del trade después del evento |
| `running_qty` | Cantidad que queda abierta después del evento |
| `initial_qty` | Cantidad de la compra original |
| `entry_price` | Precio de compra (para SELLs, sirve para calcular P&L) |
| `realized_pnl_event` | P&L de este sell (null en BUYs) |
| `reason` | Detalle textual (ej. `partial_tp1_5.0pct:5.21%`, `E3_stop_loss`) |
| `broker_order_id` | ID de Alpaca para cruzar contra su UI |

---

## Si algo falla

- **El webhook responde pero no aparecen filas**: chequear que la
  hoja se llame exactamente `RFTM` o `MREV` (case-sensitive). El
  Apps Script las crea automáticamente si no existen, pero si las
  renombraste mal puede confundirse.
- **No aparece nada y los logs del bot tampoco dicen "sheets log
  failed"**: chequear que `SHEETS_WEBHOOK_URL` esté en GitHub Secrets.
  El logger es no-op silencioso si la URL no está.
- **Eventos duplicados**: el Apps Script dedupea por `event_id`. Si
  ves duplicados, abrir un issue — bug del logger.
- **Quiero borrar todo y arrancar de cero**: borrar las filas a mano
  en Sheets (dejar el header). El backfill es idempotente, podés
  re-correrlo.

---

## Costos

Google Apps Script: **gratis** (cuota generosa, ~20K invocaciones/día
para usuarios free; tu bot hará ~20-50/día).

Google Sheets: **gratis** hasta 10M celdas por hoja. Con 16 columnas
y ~50 eventos/día, da para 30+ años.
