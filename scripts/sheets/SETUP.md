# Trade Event Logging — Setup

Cómo dejar configurado el logging de eventos de trade para que KAIZEN
pueda procesarlos y que vos puedas mirar todo desde una hoja de Google
Sheets.

**Tiempo total:** 10-15 minutos.

> **Cambio respecto a la versión vieja (Apps Script Webhook):**
> El sistema ahora usa **Service Account auth** (gspread + google-auth)
> directamente contra la API v4 de Google Sheets. El webhook se
> descontinuó porque daba errores genéricos "FAIL" sin diagnóstico, y
> los redeployments rompían silenciosamente. Si todavía tenés un
> `SHEETS_WEBHOOK_URL` en GitHub Secrets, lo podés borrar.

---

## Arquitectura

```
   ┌──────────────────┐         ┌──────────────────────┐
   │  Bot / Watchdog  │ ──BUY──▶│  _trade_logger.py    │
   └──────────────────┘         │   (wrapper único)    │
                                └──────────┬───────────┘
                                           │
                  ┌────────────────────────┴───────────────────┐
                  ▼  SIEMPRE (fuente de verdad para KAIZEN)    ▼ best effort
       ┌────────────────────────┐                  ┌─────────────────────────┐
       │ logs/trade_events_*    │                  │  _sheets_logger.py      │
       │ .jsonl (append only)   │                  │  → Google Sheets (API)  │
       └────────────────────────┘                  └─────────────────────────┘
```

Reglas:

1. El **JSONL local** es la fuente de verdad. KAIZEN lo lee, no lee
   Sheets. Si Sheets se cae, KAIZEN sigue funcionando.
2. El **Sheet** es un espejo conveniente para que Charlie revise desde
   el celular. Si no está configurado, el bot no rompe — solo logea
   `[sheets] DESACTIVADO`.
3. **Un archivo por bot** (RFTM / MREV) para evitar race conditions
   entre runs paralelos de GitHub Actions.

---

## 1. (Opcional pero recomendado) Configurar Google Sheets

Si querés tener el espejo visual:

### 1.1 Crear el spreadsheet

1. Abrir [sheets.google.com](https://sheets.google.com) → **Blank**.
2. Renombrar a `Trading Bot Log` (o lo que quieras).
3. Crear dos pestañas vacías: **RFTM** y **MREV**. El logger pone los
   headers automáticamente la primera vez que recibe un evento.
4. Copiar el ID del spreadsheet (sale de la URL, es el string entre
   `/d/` y `/edit`).

### 1.2 Crear un Service Account en GCP

1. Ir a [console.cloud.google.com](https://console.cloud.google.com)
   → crear un proyecto nuevo (ej. `trading-bot-logger`).
2. **APIs & Services → Library** → activar **Google Sheets API** y
   **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service
   account**:
   - Nombre: `trade-logger`
   - Rol: ninguno (no necesita roles a nivel proyecto).
4. Una vez creado, click en el service account → tab **Keys → Add Key
   → JSON**. Te baja un `.json` — guardalo seguro.
5. Anotá el `client_email` del JSON (algo como
   `trade-logger@trading-bot-logger.iam.gserviceaccount.com`).

### 1.3 Compartir la hoja con el service account

1. Volver al spreadsheet → **Share** → pegar el `client_email` →
   permiso **Editor** → enviar.

### 1.4 Setear los secrets en GitHub

1. **Settings → Secrets and variables → Actions → New repository
   secret**:
   - Name: `SHEETS_SPREADSHEET_ID` → pegar el ID del paso 1.1.
   - Name: `SHEETS_SERVICE_ACCOUNT_JSON` → pegar el CONTENIDO COMPLETO
     del JSON del paso 1.2 (todo en una línea está bien, GitHub no
     toca los newlines del `private_key`).

Los workflows (`daily_trade.yml`, `mrev_hourly.yml`, `rftm_watchdog.yml`,
`mrev_watchdog.yml`) ya están cableados para pasar estos secrets como
env vars.

### 1.5 Test rápido en local

```bash
cd ~/Desktop/trading-system

# Setear las env vars en la shell (NO en .env.paper si no querés
# committearlas accidentalmente)
export SHEETS_SPREADSHEET_ID='1ABC...XYZ'
export SHEETS_SERVICE_ACCOUNT_JSON=$(cat /path/al/service-account.json)

python3 scripts/sheets/test_sheets.py
```

Si todo OK vas a ver en la hoja una fila de prueba con `trade_id=TEST-...`.

---

## 2. JSONL local (obligatorio — KAIZEN lo necesita)

**No necesitás hacer nada.** El JSONL se crea automáticamente al primer
evento. Por default queda en:

```
<root_repo>/logs/trade_events_rftm.jsonl    # RFTM bot + watchdog
<root_repo>/logs/trade_events_mrev.jsonl    # MREV bot + watchdog
```

En GitHub Actions, cada workflow setea `TRADE_EVENTS_JSONL_PATH` para
mantener los archivos separados, y los cachea entre runs (key
`rftm-events-v1` / `mrev-events-v1`).

### Si tu macOS local rompe fcntl

Si tu repo vive en una carpeta con FUSE / iCloud / red mount que rompe
SQLite (mismo problema que `RFTM_DB_PATH`), también te puede romper el
`fcntl.flock` del JSONL. Exportar:

```bash
export TRADE_EVENTS_JSONL_PATH="$TMPDIR/trade_events.jsonl"
```

---

## 3. Schema de los eventos

Cada línea del JSONL es un evento. Campos:

| Campo | Tipo | Notas |
|---|---|---|
| `trade_id` | string | ID único del trade (ej. `RFTM-abc12345`). Mismo `trade_id` agrupa BUY + todos los SELL del mismo posición. |
| `event_id` | string | ID único de esta línea. Usado por Sheets para idempotencia. |
| `timestamp_utc` | ISO 8601 | Cuándo pasó el evento. |
| `bot` | `RFTM`\|`MREV` | |
| `symbol` | string | |
| `side` | string | `BUY`, `SELL_TP1`, `SELL_TP2`, `SELL_FINAL_TP`, `SELL_STOP`, `SELL_TRAIL`, `SELL_TIME`, `SELL_SYNC`. |
| `qty` | float | Cantidad de esta operación (no del trade total). |
| `price` | float | Precio de fill. |
| `notional` | float | `qty × price`. |
| `stage` | 0\|1\|2 | Stage post-evento. |
| `running_qty` | float | Qty restante después del evento. |
| `initial_qty` | float | Qty original de la compra. |
| `entry_price` | float | Precio de compra (sirve para calcular P&L de SELLs). |
| `realized_pnl_event` | float | P&L de este sell (null en BUYs). |
| `reason` | string | Detalle (ej. `partial_tp1_5.0pct:5.04%`, `E3_stop_loss`). |
| `broker_order_id` | string | ID de Alpaca para cruzar contra su UI. |
| `source` | string | Quién generó el evento: `rftm_entry`, `rftm_watchdog`, `rftm_sync`, `mrev_entry`, `mrev_watchdog`, `mrev_sync`. |
| `enriched` | dict (opcional) | F5.1 — indicadores en el momento (RSI, ATR%, vol_ratio, etc.). |

Las primeras 16 columnas son las que también van al Sheet. `source` y
`enriched` solo viven en el JSONL.

---

## 4. Backfill histórico

Para popular el JSONL con trades de los últimos 90 días desde Alpaca:

```bash
# Dry run primero
python3 scripts/sheets/backfill_to_sheets.py --days 90 --dry-run

# Si todo se ve bien, correr de verdad
python3 scripts/sheets/backfill_to_sheets.py --days 90
```

El script es **idempotente**: si lo corrés dos veces, los eventos
duplicados se skipean (Sheets dedupea por `event_id`; el JSONL es
append-only pero KAIZEN dedupea al leer).

---

## 5. Verificar live

A partir del próximo run del bot:

- **Cada nuevo evento** va a aparecer en `logs/trade_events_*.jsonl`
  (siempre — el JSONL no falla).
- Si Sheets está configurado, también aparece en la hoja en <5s.

Para forzar un trigger manual sin esperar al próximo cron:

```bash
# Trigger manual desde GitHub Actions UI:
# rftm_watchdog.yml → "Run workflow" con DRY_RUN=true.
# Cualquier partial TP o exit que detecte aparece en el JSONL.
```

---

## 6. Diagnóstico

### "El JSONL está vacío"

```bash
# 1. ¿El bot está logueando? Revisar bot_output.txt del último run.
grep "trade_logger\|sheets_logger" bot_output.txt

# 2. ¿La env var está seteada en el workflow?
grep TRADE_EVENTS_JSONL_PATH .github/workflows/*.yml

# 3. Test directo del módulo:
TRADE_EVENTS_DEBUG=1 python3 -c "
from _trade_logger import log_trade_event
log_trade_event(bot='RFTM', symbol='TEST', side='BUY',
                qty=1, price=100, trade_id='TEST-001')
"
ls -la logs/
```

### "El Sheet no recibe nada pero el JSONL sí"

El JSONL es la fuente de verdad — si esto pasa, KAIZEN sigue
funcionando. Para arreglar Sheets:

```bash
# Diagnose desde el repo
SHEETS_SPREADSHEET_ID=... \
SHEETS_SERVICE_ACCOUNT_JSON="$(cat /path/sa.json)" \
SHEETS_DEBUG=1 \
python3 -c "from _sheets_logger import _get_client; _get_client()"
```

Errores típicos:

- `[sheets] FAIL open_by_key: APIError 404`: el service account no
  fue compartido en el spreadsheet. Ir a Share, pegar el `client_email`.
- `[sheets] FAIL auth: ValueError`: el JSON del secret se rompió
  (newlines mal escapadas). Re-pegar el JSON completo.
- `[sheets] DESACTIVADO`: el secret no llegó al workflow. Verificar
  `Settings → Secrets → Actions` y que el step del workflow tenga el
  `env:` correcto.

### "Eventos duplicados en el Sheet"

`_sheets_logger` dedupea por `event_id` antes del append. Si ves dups,
es bug del logger — abrir issue.

En el JSONL no se dedupea — es append-only. KAIZEN dedupea al
consumir.

---

## 7. Costos

- **Google Sheets API**: gratis hasta 60 reads + 60 writes / minuto
  por proyecto. Con ~50 eventos/día estamos lejísimo del límite.
- **GCP Service Account**: gratis.
- **GitHub Actions cache** del JSONL: ~10KB/mes, gratis.

---

## 8. Privacidad

- El JSON del service account es **una credencial sensible** — tratalo
  como password.
- El `client_email` del SA puede leer y escribir SOLO los spreadsheets
  que vos compartiste explícitamente con él. No tiene acceso al resto
  de tu Drive.
- Si rotás el JSON, en GCP:
  Service account → Keys → Add key → JSON → guardar nuevo →
  actualizar el secret en GitHub → eventualmente borrar la key vieja.
