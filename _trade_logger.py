"""
_trade_logger — wrapper unificado para el logging de eventos de trade.

Objetivo F5.0 (plan KAIZEN): garantizar que CADA evento queda persistido
localmente en JSONL, independientemente de si Google Sheets está
configurado / disponible / online. KAIZEN consume el JSONL como fuente
de verdad; Sheets queda como espejo conveniente para Charlie.

Comportamiento:
- log_trade_event(...) escribe SIEMPRE una línea a
  $TRADE_EVENTS_JSONL_PATH (default <script_dir>/logs/trade_events.jsonl)
- Adicionalmente, si _sheets_logger está habilitado, delega ahí.
- Nunca levanta excepción — best effort idéntico a _sheets_logger.

Diseño:
- Misma firma keyword-only que _sheets_logger.log_trade_event para que
  el reemplazo en los call sites sea drop-in.
- make_trade_id / make_event_id se re-exportan para que el call site no
  tenga que importar dos módulos.
- El JSONL incluye los mismos 16 campos del header de Sheets + un
  `source` adicional ("rftm_entry" / "mrev_entry" / "rftm_watchdog" /
  "mrev_watchdog" / "sync") para que KAIZEN distinga el origen.

Concurrencia:
- En GitHub Actions, los workflows usan `concurrency:` para evitar runs
  paralelos del mismo bot, así que la escritura es serializada.
- En local, un advisory lock con fcntl protege append simultáneo.
- En Windows / sistemas sin fcntl, fallback a write-and-pray (raro, el
  bot productivo corre Linux).

Idempotencia:
- El JSONL es append-only. Si el mismo event_id se loggea dos veces,
  quedan dos líneas. KAIZEN dedupea por (trade_id, event_id) al leer
  para evitar contar dos veces.

Env vars:
- TRADE_EVENTS_JSONL_PATH: override del path del JSONL.
- TRADE_EVENTS_DISABLE_SHEETS=1: skip del envío a Sheets (útil para
  tests que no quieren tocar la cuenta de Google).
- TRADE_EVENTS_DEBUG=1: stdout verbose.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Re-exportamos make_trade_id / make_event_id para drop-in replacement
try:
    from _sheets_logger import (
        make_trade_id as _sheets_make_trade_id,
        make_event_id as _sheets_make_event_id,
    )
except Exception:  # _sheets_logger podría no existir en algún test aislado
    _sheets_make_trade_id = None
    _sheets_make_event_id = None


_DEBUG = os.environ.get("TRADE_EVENTS_DEBUG", "").lower() in ("1", "true", "yes")
_DISABLE_SHEETS = os.environ.get("TRADE_EVENTS_DISABLE_SHEETS", "").lower() in (
    "1",
    "true",
    "yes",
)


def _default_jsonl_path() -> str:
    """logs/trade_events.jsonl al lado del script principal.

    Sigue la misma convención que RFTM_DB_PATH/MREV_DB_PATH: por default
    queda relativo al script (compatible con cache de GHA Actions), pero
    se puede overridear con TRADE_EVENTS_JSONL_PATH.
    """
    override = os.environ.get("TRADE_EVENTS_JSONL_PATH", "").strip()
    if override:
        return override
    script_dir = Path(__file__).resolve().parent
    return str(script_dir / "logs" / "trade_events.jsonl")


def make_trade_id(bot: str, position_id: str) -> str:
    if _sheets_make_trade_id is not None:
        return _sheets_make_trade_id(bot, position_id)
    short = str(position_id).replace("-", "")[:8]
    return f"{bot.upper()}-{short}"


def make_event_id(trade_id: str, side: str, suffix: Optional[str] = None) -> str:
    if _sheets_make_event_id is not None:
        return _sheets_make_event_id(trade_id, side, suffix=suffix)
    base = f"{trade_id}-{side}"
    if suffix:
        return f"{base}-{suffix}"
    return base


def _ts_now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _safe_float(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _append_jsonl(payload: dict) -> bool:
    """Append atómico de una línea JSON al archivo de eventos.

    Usa fcntl.flock para serializar contra runs paralelos. En sistemas
    sin fcntl, fallback a append sin lock (riesgo bajo en GH Actions
    porque los workflows usan `concurrency:`).
    """
    path = _default_jsonl_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception as e:
        if _DEBUG:
            print(f"[trade_logger] makedirs failed: {e}", file=sys.stderr)
        # Intentamos igual el open — si falla el dir tampoco vamos a poder

    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"

    try:
        # Modo binario + fcntl flock para append atómico
        with open(path, "ab") as f:
            try:
                import fcntl  # POSIX only

                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line.encode("utf-8"))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except ImportError:
                # Windows / sin fcntl — escritura sin lock
                f.write(line.encode("utf-8"))
        if _DEBUG:
            print(f"[trade_logger] jsonl OK: {payload.get('event_id')}")
        return True
    except Exception as e:
        # Imprimir SIEMPRE — perder eventos por un IOError es lo que
        # justamente queremos evitar con esta capa.
        print(f"[trade_logger] FAIL jsonl write: {type(e).__name__}: {e}")
        return False


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
    source: Optional[str] = None,
    extra: Optional[dict] = None,
) -> bool:
    """Persiste el evento a JSONL (siempre) y a Sheets (best effort).

    Devuelve True si AL MENOS el JSONL local se escribió. False solo si
    el JSONL falló — en ese caso es un bug serio porque KAIZEN se queda
    sin data.

    `source` y `extra` son extensiones nuevas:
    - source: string libre para indicar quién generó el evento
      (ej. "rftm_entry", "mrev_watchdog", "sync_with_alpaca").
    - extra: dict opcional con campos enriquecidos (RSI, ATR%, etc.)
      que F5.1 va a usar. Hoy se serializa al JSONL pero NO se envía a
      Sheets (el sheet tiene un schema fijo de 16 columnas).
    """
    if event_id is None:
        event_id = make_event_id(trade_id, side, suffix=str(uuid.uuid4())[:6])

    ts = timestamp_utc or _ts_now_utc()

    payload = {
        "trade_id": trade_id,
        "event_id": event_id,
        "timestamp_utc": ts,
        "bot": (bot or "").upper(),
        "symbol": symbol,
        "side": side,
        "qty": _safe_float(qty),
        "price": _safe_float(price),
        "notional": round((float(qty) * float(price)), 4) if qty and price else None,
        "stage": int(stage) if stage is not None else 0,
        "running_qty": _safe_float(running_qty) or 0.0,
        "initial_qty": _safe_float(initial_qty),
        "entry_price": _safe_float(entry_price),
        "realized_pnl_event": (
            round(_safe_float(realized_pnl_event), 4)
            if _safe_float(realized_pnl_event) is not None
            else None
        ),
        "reason": reason or "",
        "broker_order_id": broker_order_id or "",
        "source": source or "",
    }
    if extra:
        # Mergeamos en un sub-key para no chocar con campos nuevos del
        # schema base. KAIZEN va a leer payload["enriched"].
        payload["enriched"] = extra

    jsonl_ok = _append_jsonl(payload)

    # Sheets best effort — sólo si está habilitado y no fue deshabilitado
    # explícitamente en este proceso.
    if not _DISABLE_SHEETS:
        try:
            from _sheets_logger import log_trade_event as sheets_log

            sheets_log(
                bot=bot,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                trade_id=trade_id,
                event_id=event_id,
                stage=stage,
                running_qty=running_qty,
                initial_qty=initial_qty,
                entry_price=entry_price,
                realized_pnl_event=realized_pnl_event,
                reason=reason,
                broker_order_id=broker_order_id,
                timestamp_utc=ts,
            )
        except Exception as e:
            # No fatal — el JSONL ya guardó la fuente de verdad.
            print(f"[trade_logger] sheets forward failed (non-fatal): {type(e).__name__}: {e}")

    return jsonl_ok
