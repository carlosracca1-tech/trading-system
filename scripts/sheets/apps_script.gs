/**
 * apps_script.gs — Webhook receiver para trade events.
 *
 * Setup:
 *   1. Crear un Google Sheet nuevo. Dejarlo vacío.
 *   2. Renombrar la hoja por defecto a "RFTM". Crear una segunda hoja llamada "MREV".
 *   3. Extensions → Apps Script. Borrar lo que haya y pegar este archivo.
 *   4. Guardar (Ctrl+S). Deploy → New deployment → Type: Web app.
 *      - Description: trade-events-webhook
 *      - Execute as: Me
 *      - Who has access: Anyone (la URL es secreta, sin la URL nadie escribe)
 *   5. Authorize (la primera vez Google pregunta).
 *   6. Copiar la "Web app URL" que devuelve. Esa URL es el SHEETS_WEBHOOK_URL.
 *
 * Payload esperado (JSON via POST):
 *   {
 *     "trade_id": "RFTM-abc12345",
 *     "event_id": "evt-xyz",            // único por fila
 *     "timestamp_utc": "2026-05-11T14:35:00Z",
 *     "bot": "RFTM",                    // o "MREV" — define a qué hoja va
 *     "symbol": "QQQ",
 *     "side": "BUY" | "SELL_TP1" | "SELL_TP2" | "SELL_FINAL_TP" |
 *             "SELL_STOP" | "SELL_TRAIL" | "SELL_TIME",
 *     "qty": 22,
 *     "price": 605.00,
 *     "notional": 13310.00,
 *     "stage": 0,
 *     "running_qty": 22,
 *     "initial_qty": 22,
 *     "entry_price": 605.00,
 *     "realized_pnl_event": null,       // null para BUY, número para SELL
 *     "reason": "entry_breakout",
 *     "broker_order_id": "..."
 *   }
 *
 * Idempotencia: si llega un POST con un event_id que ya está en la hoja,
 * lo skipea. Eso permite re-correr el backfill sin duplicar.
 */

const HEADER_ROW = [
  'trade_id', 'event_id', 'timestamp_utc', 'bot', 'symbol', 'side',
  'qty', 'price', 'notional', 'stage', 'running_qty',
  'initial_qty', 'entry_price', 'realized_pnl_event', 'reason',
  'broker_order_id'
];


function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _err('empty body');
    }
    const payload = JSON.parse(e.postData.contents);

    // Validación mínima
    if (!payload.bot || !payload.event_id || !payload.symbol) {
      return _err('missing required fields (bot, event_id, symbol)');
    }
    const bot = String(payload.bot).toUpperCase();
    if (bot !== 'RFTM' && bot !== 'MREV') {
      return _err('bot must be RFTM or MREV, got ' + bot);
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(bot);
    if (!sheet) {
      sheet = ss.insertSheet(bot);
    }
    _ensureHeader(sheet);

    // Idempotencia: chequear si event_id ya existe
    if (_eventExists(sheet, payload.event_id)) {
      return _ok({ status: 'skipped_duplicate', event_id: payload.event_id });
    }

    // Append
    const row = HEADER_ROW.map(col => _coerce(payload[col]));
    sheet.appendRow(row);

    return _ok({ status: 'logged', event_id: payload.event_id });
  } catch (err) {
    return _err('exception: ' + err.toString());
  }
}


function doGet(e) {
  // Health check sencillo desde el navegador
  return _ok({ status: 'alive', timestamp: new Date().toISOString() });
}


function _ensureHeader(sheet) {
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADER_ROW);
    sheet.setFrozenRows(1);
    // Estilo del header
    const range = sheet.getRange(1, 1, 1, HEADER_ROW.length);
    range.setFontWeight('bold');
    range.setBackground('#f0f0f0');
  }
}


function _eventExists(sheet, eventId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return false;
  // Columna event_id es la 2da (índice 2 en getRange, 1-based)
  const values = sheet.getRange(2, 2, lastRow - 1, 1).getValues();
  for (let i = 0; i < values.length; i++) {
    if (values[i][0] === eventId) return true;
  }
  return false;
}


function _coerce(v) {
  if (v === undefined || v === null) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return v;
}


function _ok(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}


function _err(msg) {
  return ContentService.createTextOutput(JSON.stringify({ error: msg }))
    .setMimeType(ContentService.MimeType.JSON);
}
