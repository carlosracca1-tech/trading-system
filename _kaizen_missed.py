"""
_kaizen_missed — post-mortem JSONL para entries bloqueadas por cooldown
de precio (F1 del plan KAIZEN).

Cada vez que el cooldown de precio bloquea una entrada que el bot
quería tomar, escribimos una línea JSON con todo el contexto: % de
subida desde el exit, días transcurridos, estado de indicadores hoy,
volumen relativo (proxy de catalizador). KAIZEN consume este archivo
semanalmente para detectar patrones — qué rebotes estamos dejando
pasar y si valen la pena perseguir con otra estrategia.

Diseño:
- Append-only JSONL, una línea por evento bloqueado.
- Nunca levanta excepción — best effort idéntico a _trade_logger.
- Path overrideable vía KAIZEN_MISSED_PATH para tests.
- fcntl flock para concurrencia segura (mismo patrón que _trade_logger).

Schema por línea:
- timestamp_utc: cuándo se bloqueó la entrada
- bot: RFTM | MREV
- symbol
- block_reason: el `reason` que devolvió check_cooldown
- runup_pct: % de subida vs last_exit_price
- days_since_exit: días calendar desde el exit
- last_exit_price, last_exit_reason
- current_price: precio que motivaba la entrada
- indicators: dict {rsi14, atr14_pct, ema21_dist, ema50_dist,
  vol_ratio_20d, dist_to_20d_high, ...} — cualquier subset que el
  caller tenga a mano
- catalyst_proxy: bool — True si vol_ratio_20d > 2.0 (heurística
  burda para "hubo noticia")

Las funciones acá NO toman conn — el post-mortem es independiente de
la DB del bot.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _default_path() -> str:
    override = os.environ.get("KAIZEN_MISSED_PATH", "").strip()
    if override:
        return override
    script_dir = Path(__file__).resolve().parent
    return str(script_dir / "logs" / "kaizen_missed_moves.jsonl")


def _ts_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def log_missed_move(
    *,
    bot: str,
    symbol: str,
    block_reason: str,
    runup_pct: Optional[float],
    days_since_exit: Optional[float],
    last_exit_price: Optional[float],
    last_exit_reason: Optional[str],
    current_price: float,
    indicators: Optional[dict] = None,
    timestamp_utc: Optional[str] = None,
) -> bool:
    """Append una línea al JSONL de rebotes perdidos.

    Devuelve True si se escribió, False si falló (no-op para el caller).
    """
    payload = {
        "timestamp_utc": timestamp_utc or _ts_now(),
        "bot": (bot or "").upper(),
        "symbol": symbol,
        "block_reason": block_reason,
        "runup_pct": runup_pct,
        "days_since_exit": days_since_exit,
        "last_exit_price": last_exit_price,
        "last_exit_reason": last_exit_reason,
        "current_price": current_price,
        "indicators": indicators or {},
    }
    # Heurística catalyst: vol_ratio > 2x average
    vol_ratio = None
    if indicators:
        vol_ratio = indicators.get("vol_ratio_20d") or indicators.get("vol_ratio")
    payload["catalyst_proxy"] = bool(vol_ratio and vol_ratio > 2.0)

    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    path = _default_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass

    try:
        with open(path, "ab") as f:
            try:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line.encode("utf-8"))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except ImportError:
                f.write(line.encode("utf-8"))
        return True
    except Exception as e:
        print(f"[kaizen_missed] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return False
