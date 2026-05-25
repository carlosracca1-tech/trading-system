"""
_kaizen_enrichment — F5.1: helpers para enriquecer eventos de trade con
indicadores, régimen de mercado y métricas de ejecución.

Lo consume `_trade_logger.log_trade_event(..., extra=enriched)` desde
los bots y los watchdogs. El `extra` se persiste como sub-key `enriched`
en el JSONL (KAIZEN lo lee semanalmente para detectar patrones).

Diseño:
- Funciones puras donde es posible — los call sites pasan el `row` de
  market_data + datos auxiliares (entry_dt, fill_px, etc.).
- Tolerante: si un indicador falta del row, se persiste como None — no
  rompe el flujo. KAIZEN filtra los Nones al agregar.
- Régimen de mercado (SPY trend, VIX) se fetchea on-demand desde Alpaca
  pero con caché de proceso para no spamear API.

Convenciones:
- Todas las claves del dict de salida usan snake_case con sufijos
  consistentes entre bots. KAIZEN unifica RFTM/MREV al analizar — no
  importa que el origen tenga distinto naming.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional


# ── Caché de proceso para régimen de mercado ─────────────────────────────────
# El fetch a Alpaca de bars de SPY toma ~200ms. Por run del watchdog
# evaluamos hasta 10 posiciones — sin caché serían 10×200ms perdidos.
_REGIME_CACHE: dict = {}


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        # Filtrar NaN
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


# ── RFTM indicators ──────────────────────────────────────────────────────────


def enrich_rftm_indicators(row: dict, close: Optional[float] = None) -> dict:
    """Extrae los indicadores RFTM del row de market_data.

    `row` viene de `get_latest_row(symbol)`. Si `close` no se pasa, se
    intenta extraer del row.

    Devuelve dict con:
    - rsi14, atr14, atr14_pct
    - ema21, ema50, ema200
    - vol_ratio_20d (volume / vol_ma20)
    - high20, dist_to_20d_high_pct
    - ema21_dist_pct, ema50_dist_pct (positivo = close arriba de la EMA)

    Todos los valores son float | None.
    """
    if close is None:
        close = _to_float(row.get("close"))

    rsi14 = _to_float(row.get("rsi14"))
    atr14 = _to_float(row.get("atr14"))
    atr14_pct = _to_float(row.get("atr14_pct"))
    if atr14_pct is None and atr14 and close:
        atr14_pct = atr14 / close

    ema21 = _to_float(row.get("ema21"))
    ema50 = _to_float(row.get("ema50"))
    ema200 = _to_float(row.get("ema200"))
    vol = _to_float(row.get("volume"))
    vol_ma20 = _to_float(row.get("vol_ma20"))
    high20 = _to_float(row.get("high20"))

    vol_ratio = None
    if vol and vol_ma20 and vol_ma20 > 0:
        vol_ratio = vol / vol_ma20

    dist_high20 = None
    if close and high20 and high20 > 0:
        dist_high20 = (close - high20) / high20

    ema21_dist = None
    if close and ema21 and ema21 > 0:
        ema21_dist = (close - ema21) / ema21
    ema50_dist = None
    if close and ema50 and ema50 > 0:
        ema50_dist = (close - ema50) / ema50

    return {
        "rsi14": rsi14,
        "atr14": atr14,
        "atr14_pct": atr14_pct,
        "ema21": ema21,
        "ema50": ema50,
        "ema200": ema200,
        "vol_ratio_20d": vol_ratio,
        "high20": high20,
        "dist_to_20d_high_pct": dist_high20,
        "ema21_dist_pct": ema21_dist,
        "ema50_dist_pct": ema50_dist,
    }


# ── MREV indicators ──────────────────────────────────────────────────────────


def enrich_mrev_indicators(row: dict, close: Optional[float] = None) -> dict:
    """Extrae indicadores MREV. Naming distinto al RFTM (rsi_14 vs rsi14)
    pero unificamos las claves de salida con sufijos canónicos."""
    if close is None:
        close = _to_float(row.get("close"))

    rsi14 = _to_float(row.get("rsi_14") or row.get("rsi14"))
    atr14 = _to_float(row.get("atr_14") or row.get("atr14"))
    atr14_pct = _to_float(row.get("atr_14_pct") or row.get("atr14_pct"))
    if atr14_pct is None and atr14 and close:
        atr14_pct = atr14 / close
    bb_upper = _to_float(row.get("bb_upper"))
    bb_lower = _to_float(row.get("bb_lower"))
    sma_20 = _to_float(row.get("sma_20"))
    vol = _to_float(row.get("volume"))
    vol_ma20 = _to_float(row.get("volume_ma_20") or row.get("vol_ma20"))

    vol_ratio = None
    if vol and vol_ma20 and vol_ma20 > 0:
        vol_ratio = vol / vol_ma20

    bb_pct = None
    if close and bb_upper and bb_lower and (bb_upper - bb_lower) > 0:
        # 0 = banda inferior, 1 = banda superior
        bb_pct = (close - bb_lower) / (bb_upper - bb_lower)

    sma_dist = None
    if close and sma_20 and sma_20 > 0:
        sma_dist = (close - sma_20) / sma_20

    return {
        "rsi14": rsi14,
        "atr14": atr14,
        "atr14_pct": atr14_pct,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_pct": bb_pct,
        "sma_20": sma_20,
        "sma_20_dist_pct": sma_dist,
        "vol_ratio_20d": vol_ratio,
    }


# ── Régimen de mercado (SPY trend, VIX) ──────────────────────────────────────


def enrich_market_regime(
    *,
    alpaca_request_fn=None,
    use_cache: bool = True,
) -> dict:
    """Devuelve {spy_above_sma200, spy_pct_change_5d, vix_level, vix_regime}.

    Si `alpaca_request_fn` es None o falla, devuelve dict con Nones.
    Cachea el resultado por TTL de 1h (los regímenes no cambian
    intraday significativamente).

    `vix_regime` es la categoría heurística:
    - "calm"     si VIX < 15
    - "normal"   si 15 ≤ VIX < 20
    - "elevated" si 20 ≤ VIX < 30
    - "panic"    si VIX ≥ 30
    """
    cache_key = "regime"
    if use_cache and cache_key in _REGIME_CACHE:
        ts, value = _REGIME_CACHE[cache_key]
        # 1h TTL
        if (datetime.now(timezone.utc) - ts).total_seconds() < 3600:
            return value

    result = {
        "spy_above_sma200": None,
        "spy_pct_change_5d": None,
        "vix_level": None,
        "vix_regime": None,
    }

    if alpaca_request_fn is None:
        return result

    try:
        # Fetch SPY bars (último año aprox para SMA200)
        spy_data = _fetch_recent_closes(alpaca_request_fn, "SPY", days=220)
        if spy_data and len(spy_data) >= 200:
            closes = spy_data
            sma200 = sum(closes[-200:]) / 200
            result["spy_above_sma200"] = bool(closes[-1] > sma200)
            if len(closes) >= 6:
                result["spy_pct_change_5d"] = (closes[-1] - closes[-6]) / closes[-6]
    except Exception as e:
        print(f"[kaizen_enrichment] SPY fetch fail: {e}", file=sys.stderr)

    try:
        # VIX como ETN proxy (VIXY) o directamente "VIX" via Alpaca
        # NOTA: Alpaca paper no expone VIX directo — usamos VIXY como proxy
        # Si el fetch falla, dejamos en None.
        vix_data = _fetch_recent_closes(alpaca_request_fn, "VIXY", days=2)
        if vix_data:
            vix = vix_data[-1]
            result["vix_level"] = vix
            if vix < 15:
                result["vix_regime"] = "calm"
            elif vix < 20:
                result["vix_regime"] = "normal"
            elif vix < 30:
                result["vix_regime"] = "elevated"
            else:
                result["vix_regime"] = "panic"
    except Exception:
        pass

    _REGIME_CACHE[cache_key] = (datetime.now(timezone.utc), result)
    return result


def _fetch_recent_closes(alpaca_request_fn, symbol: str, days: int = 220) -> list:
    """Fetch closes diarios desde Alpaca data API. Devuelve lista o []."""
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days * 1.5)  # margen por fines de semana

    res = alpaca_request_fn(
        "GET",
        f"/v2/stocks/{symbol}/bars?timeframe=1Day"
        f"&start={start.strftime('%Y-%m-%d')}"
        f"&end={end.strftime('%Y-%m-%d')}"
        f"&limit={days + 10}",
        None,
    )
    if not isinstance(res, dict):
        return []
    bars = res.get("bars", [])
    if not bars:
        return []
    return [float(b.get("c", 0)) for b in bars if b.get("c")]


# ── Ejecución (slippage, tiempo en posición) ─────────────────────────────────


def enrich_execution(
    *,
    fill_price: float,
    target_price: Optional[float] = None,
    entry_dt_iso: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Métricas de ejecución para un evento.

    - slippage_pct: (fill - target) / target — positivo = peor (vendiste
      más bajo que target) para SELLs, o pagaste más caro para BUYs.
    - time_in_position_hours: horas desde entry_dt hasta now.
    """
    now = now or datetime.now(tz=timezone.utc)
    slippage = None
    if target_price and target_price > 0:
        slippage = (fill_price - target_price) / target_price

    time_in_position = None
    if entry_dt_iso:
        try:
            entry_dt = datetime.fromisoformat(entry_dt_iso)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            time_in_position = (now - entry_dt).total_seconds() / 3600.0
        except Exception:
            pass

    return {
        "slippage_pct": slippage,
        "time_in_position_hours": time_in_position,
    }


# ── Constructor combinado ────────────────────────────────────────────────────


def build_enriched_extra(
    *,
    bot: str,
    market_row: Optional[dict] = None,
    close: Optional[float] = None,
    fill_price: Optional[float] = None,
    target_price: Optional[float] = None,
    entry_dt_iso: Optional[str] = None,
    alpaca_request_fn=None,
    include_regime: bool = True,
    extra_kv: Optional[dict] = None,
) -> dict:
    """Builder all-in-one. Devuelve el dict que se pasa al
    `log_trade_event(..., extra=...)`.

    Componentes (todos opcionales, mergeados al resultado):
    - indicators_*: indicadores del bot (rfm o mrev) según `bot`.
    - regime_*: régimen de mercado vía Alpaca (cached).
    - execution_*: slippage + tiempo en posición.
    - extra_kv: cualquier kv adicional del caller (ej. flags F1).
    """
    out: dict = {}

    if market_row:
        bot_u = (bot or "").upper()
        if bot_u == "RFTM":
            inds = enrich_rftm_indicators(market_row, close=close)
        else:
            inds = enrich_mrev_indicators(market_row, close=close)
        out.update({f"ind_{k}": v for k, v in inds.items()})

    if include_regime and alpaca_request_fn is not None:
        reg = enrich_market_regime(alpaca_request_fn=alpaca_request_fn)
        out.update({f"reg_{k}": v for k, v in reg.items()})

    if fill_price is not None or entry_dt_iso is not None:
        exe = enrich_execution(
            fill_price=fill_price if fill_price is not None else 0.0,
            target_price=target_price,
            entry_dt_iso=entry_dt_iso,
        )
        out.update({f"exe_{k}": v for k, v in exe.items()})

    if extra_kv:
        out.update(extra_kv)

    return out
