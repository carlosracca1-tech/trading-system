"""
_sheets_logger — log trade events directamente a Google Sheets via la API v4
con autenticación de Service Account.

Arquitectura:
- Service Account (cuenta de servicio en GCP) tiene su propio email y JSON key.
- El JSON va como secret en GitHub Actions (SHEETS_SERVICE_ACCOUNT_JSON).
- El Sheet se comparte con el email del service account (como editor).
- gspread maneja toda la autenticación y la llamada a la API.

Ventajas sobre Apps Script Webhook:
- Cero redirects raros, cero "versión vieja del deployment".
- Auth oficial de Google (JWT firmado con RSA).
- Errores explícitos (no "FAIL" genérico).
- Idempotencia confiable: buscamos event_id antes de appendear.
- Funciona idéntico en local, en GH Actions, en cualquier máquina.

Env vars:
- SHEETS_SPREADSHEET_ID: el ID del Sheet (sale de la URL).
- SHEETS_SERVICE_ACCOUNT_JSON: contenido COMPLETO del JSON del service account.
- SHEETS_DEBUG=1: imprime errores en stdout.

Si cualquiera de las dos primeras no está → no-op silencioso (no rompe).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional


SHEETS_SPREADSHEET_ID = os.environ.get("SHEETS_SPREADSHEET_ID", "").strip()
SHEETS_SERVICE_ACCOUNT_JSON = os.environ.get("SHEETS_SERVICE_ACCOUNT_JSON", "").strip()
SHEETS_DEBUG = os.environ.get("SHEETS_DEBUG", "").lower() in ("1", "true", "yes")

SHEETS_LOG_ENABLED = bool(SHEETS_SPREADSHEET_ID and SHEETS_SERVICE_ACCOUNT_JSON)


HEADER_ROW = [
    "trade_id", "event_id", "timestamp_utc", "bot", "symbol", "side",
    "qty", "price", "notional", "stage", "running_qty",
    "initial_qty", "entry_price", "realized_pnl_event", "reason",
    "broker_order_id",
]


# Cliente cacheado (auth tarda ~300ms, no queremos hacerla por cada trade)
_CLIENT = None
_WORKSHEETS: dict[str, object] = {}


def _get_client():
    """Lazy init de gspread client. Devuelve None si no está configurado."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if not SHEETS_LOG_ENABLED:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        if SHEETS_DEBUG:
            print(f"[sheets] gspread/google-auth no instalados: {e}")
        return None
    try:
        info = json.loads(SHEETS_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        _CLIENT = gspread.authorize(creds)
        return _CLIENT
    except Exception as e:
        if SHEETS_DEBUG:
            print(f"[sheets] auth failed: {type(e).__name__}: {e}")
        return None


def _get_worksheet(bot: str):
    """Devuelve el worksheet 'RFTM' o 'MREV' (lo crea si no existe).
    Cachea por sesión."""
    bot = bot.upper()
    if bot in _WORKSHEETS:
        return _WORKSHEETS[bot]
    client = _get_client()
    if client is None:
        return None
    try:
        sh = client.open_by_key(SHEETS_SPREADSHEET_ID)
    except Exception as e:
        if SHEETS_DEBUG:
            print(f"[sheets] no se pudo abrir spreadsheet: {e}")
        return None
    try:
        ws = sh.worksheet(bot)
    except Exception:
        # No existe: crearlo
        try:
            ws = sh.add_worksheet(title=bot, rows=1000, cols=len(HEADER_ROW) + 2)
        except Exception as e:
            if SHEETS_DEBUG:
                print(f"[sheets] no se pudo crear worksheet {bot}: {e}")
            return None
    # Asegurar header
    try:
        first_row = ws.row_values(1)
        if not first_row or first_row[: len(HEADER_ROW)] != HEADER_ROW:
            ws.update("A1", [HEADER_ROW])
            ws.format("A1:P1", {"textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94}})
            ws.freeze(rows=1)
    except Exception as e:
        if SHEETS_DEBUG:
            print(f"[sheets] no se pudo asegurar header: {e}")

    _WORKSHEETS[bot] = ws
    return ws


def _event_exists(ws, event_id: str) -> bool:
    """Chequea si event_id ya está en la columna B (event_id)."""
    if not event_id:
        return False
    try:
        cell = ws.find(event_id, in_column=2)
        return cell is not None
    except Exception:
        return False


def _ts_now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def make_trade_id(bot: str, position_id: str) -> str:
    short = str(position_id).replace("-", "")[:8]
    return f"{bot.upper()}-{short}"


def make_event_id(trade_id: str, side: str, suffix: Optional[str] = None) -> str:
    base = f"{trade_id}-{side}"
    if suffix:
        return f"{base}-{suffix}"
    return base


def log_trade_event(
    *,
    bot: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    trade_id: str,
    event_id: Optional[str] = None,
    stage: int = 0,
    running_qty: float = 0.0,
    initial_qty: Optional[float] = None,
    entry_price: Optional[float] = None,
    realized_pnl_event: Optional[float] = None,
    reason: str = "",
    broker_order_id: str = "",
    timestamp_utc: Optional[str] = None,
) -> bool:
    """
    Appendea una fila al worksheet del bot correspondiente.
    Devuelve True si se escribió, False si no (incluye duplicado).
    Nunca levanta excepción — best effort.
    """
    if not SHEETS_LOG_ENABLED:
        return False

    if event_id is None:
        event_id = make_event_id(trade_id, side, suffix=str(uuid.uuid4())[:6])

    ws = _get_worksheet(bot)
    if ws is None:
        return False

    # Idempotencia: si el event_id ya está, no duplicar
    if _event_exists(ws, event_id):
        if SHEETS_DEBUG:
            print(f"[sheets] skip duplicate: {event_id}")
        return False

    row = [
        trade_id,
        event_id,
        timestamp_utc or _ts_now_utc(),
        bot.upper(),
        symbol,
        side,
        float(qty),
        float(price),
        round(float(qty) * float(price), 4),
        int(stage),
        float(running_qty),
        float(initial_qty) if initial_qty is not None else "",
        float(entry_price) if entry_price is not None else "",
        round(float(realized_pnl_event), 4) if realized_pnl_event is not None else "",
        reason,
        broker_order_id,
    ]

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        if SHEETS_DEBUG:
            print(f"[sheets] append failed: {type(e).__name__}: {e}")
        return False
