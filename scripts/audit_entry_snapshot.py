#!/usr/bin/env python3
"""
audit_entry_snapshot.py — Reconstruye los indicadores que tenía un
símbolo en una fecha dada, exactamente como los hubiera visto `check_entry`
del bot RFTM. Útil para entender por qué el bot abrió una posición
específica que después fue mal.

Calcula:
- close
- EMA21, EMA50, EMA200
- RSI14
- ATR14 y ATR14%
- high20 (máximo de las últimas 20 barras previas)
- vol_ma20

Y para cada uno reporta si pasó el filtro C1..C5 de check_entry.

Adicionalmente, evalúa el régimen general usando SPY: si SPY estaba
por encima de su EMA200 en esa fecha (filtro de "macro bullish" que
el bot NO chequea hoy en RFTM, pero es relevante saberlo).

Uso:
    python3 scripts/audit_entry_snapshot.py SLV 2026-05-11
    python3 scripts/audit_entry_snapshot.py FXI 2026-05-13
    python3 scripts/audit_entry_snapshot.py CPER 2026-05-11
    python3 scripts/audit_entry_snapshot.py SLV 2026-05-11 --bars 250

Read-only: solo GET /v2/stocks/{sym}/bars de Alpaca.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _load_env_paper() -> None:
    f = ROOT / ".env.paper"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_paper()
KEY = os.getenv("ALPACA_API_KEY") or ""
SEC = os.getenv("ALPACA_SECRET_KEY") or ""
DATA_URL = "https://data.alpaca.markets/v2"


def fetch_bars(symbol: str, from_date: str, to_date: str) -> pd.DataFrame | None:
    url = (
        f"{DATA_URL}/stocks/{symbol}/bars"
        f"?timeframe=1Day&start={from_date}&end={to_date}"
        f"&limit=10000&adjustment=all&feed=iex&sort=asc"
    )
    all_bars: list = []
    while url:
        try:
            req = urllib.request.Request(url)
            req.add_header("APCA-API-KEY-ID", KEY)
            req.add_header("APCA-API-SECRET-KEY", SEC)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"[ERROR] {symbol}: {e}", file=sys.stderr)
            return None
        for b in data.get("bars", []):
            all_bars.append({
                "date": b["t"][:10],
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": int(b["v"]),
            })
        nxt = data.get("next_page_token")
        if nxt:
            url = (
                f"{DATA_URL}/stocks/{symbol}/bars"
                f"?timeframe=1Day&start={from_date}&end={to_date}"
                f"&limit=10000&adjustment=all&feed=iex&sort=asc&page_token={nxt}"
            )
        else:
            url = None
    return pd.DataFrame(all_bars) if all_bars else None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Replica mínima de indicadores que usa check_entry."""
    out = df.copy()
    close = out["close"]

    # EMAs (Wilder/standard EMA con alpha = 2/(N+1))
    out["ema21"] = close.ewm(span=21, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["ema200"] = close.ewm(span=200, adjust=False).mean()

    # RSI14 (Wilder)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))

    # ATR14
    h = out["high"]
    lo = out["low"]
    prev_close = close.shift(1).fillna(close)
    tr = np.maximum.reduce([h - lo, (h - prev_close).abs(), (lo - prev_close).abs()])
    out["atr14"] = pd.Series(tr).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    out["atr14_pct"] = out["atr14"] / close

    # high20: máximo de las últimas 20 barras PREVIAS (no incluye hoy)
    out["high20"] = h.rolling(20).max().shift(1)

    # vol_ma20
    out["vol_ma20"] = out["volume"].rolling(20).mean()

    return out


def check_entry_replica(row: pd.Series) -> list[tuple[str, str, str]]:
    """Replica de check_entry — devuelve lista (filtro, valor, pass/fail)."""
    res = []
    close = float(row["close"])

    # C1a: close > EMA21
    e21 = row.get("ema21")
    if pd.notna(e21):
        ok = close > float(e21)
        res.append(("C1a close>EMA21", f"close={close:.2f} EMA21={float(e21):.2f}",
                     "✓" if ok else "✗"))
    # C1b: close > EMA50
    e50 = row.get("ema50")
    if pd.notna(e50):
        ok = close > float(e50)
        res.append(("C1b close>EMA50", f"close={close:.2f} EMA50={float(e50):.2f}",
                     "✓" if ok else "✗"))
    # C2: RSI 55-70
    rsi = float(row.get("rsi14", np.nan))
    if not np.isnan(rsi):
        ok = 55 <= rsi <= 70
        res.append(("C2 RSI 55-70", f"RSI={rsi:.1f}", "✓" if ok else "✗"))
    # C3: close > high20
    h20 = row.get("high20")
    if pd.notna(h20):
        ok = close > float(h20)
        res.append(("C3 close>high20", f"close={close:.2f} high20={float(h20):.2f}",
                     "✓" if ok else "✗"))
    # C4: volume ≥ 0.8 × vol_ma20
    vol = float(row.get("volume", 0) or 0)
    vm = row.get("vol_ma20")
    if pd.notna(vm) and float(vm) > 0:
        ok = vol >= 0.8 * float(vm)
        res.append(("C4 vol≥0.8×ma20",
                     f"vol={vol:,.0f} ma20={float(vm):,.0f} ratio={vol/float(vm):.2f}x",
                     "✓" if ok else "✗"))
    # C5: 0.003 ≤ ATR% ≤ 0.08
    ap = row.get("atr14_pct")
    if pd.notna(ap):
        ok = 0.003 <= float(ap) <= 0.08
        res.append(("C5 ATR% in [0.3%, 8%]",
                     f"ATR%={float(ap)*100:.2f}%", "✓" if ok else "✗"))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", help="Símbolo (ej. SLV)")
    ap.add_argument("date", help="Fecha YYYY-MM-DD del buy")
    ap.add_argument("--bars", type=int, default=260,
                    help="Cuántas barras previas pedir (default 260 para EMA200 limpia)")
    args = ap.parse_args()

    if not KEY or not SEC:
        print("ERROR: faltan ALPACA_API_KEY / ALPACA_SECRET_KEY (.env.paper)",
              file=sys.stderr)
        return 1

    target_date = datetime.strptime(args.date, "%Y-%m-%d")
    start = (target_date - timedelta(days=int(args.bars * 1.6))).strftime("%Y-%m-%d")
    # Tomamos hasta el día anterior — check_entry mira indicadores
    # calculados con las barras hasta el día previo. La compra usa el open
    # del día siguiente, pero usamos close del target_date como aproximación.
    end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"=== Auditando entry: {args.symbol} @ {args.date} ===")
    print(f"  Pidiendo {args.bars} barras previas a Alpaca...")

    df = fetch_bars(args.symbol, start, end)
    if df is None or df.empty:
        print(f"  Sin barras para {args.symbol}", file=sys.stderr)
        return 1
    df = compute_indicators(df)

    # Encontrar la fila del target_date (o la más cercana hacia atrás)
    target_str = args.date
    if target_str in df["date"].values:
        idx = df.index[df["date"] == target_str][0]
    else:
        # Tomamos la última barra antes del target
        prev = df[df["date"] < target_str]
        if prev.empty:
            print(f"  Sin data antes de {target_str}", file=sys.stderr)
            return 1
        idx = prev.index[-1]
        print(f"  ⚠ {target_str} no encontrado (¿feriado?), usando {df.loc[idx, 'date']}")

    row = df.loc[idx]
    print()
    print(f"Barra usada: {row['date']}")
    print(f"  close=${row['close']:.2f}  open=${row['open']:.2f}  "
          f"high=${row['high']:.2f}  low=${row['low']:.2f}  volume={row['volume']:,}")
    print()
    print("Indicadores:")
    print(f"  EMA21  = ${row['ema21']:.2f}  (close/EMA21 = {row['close']/row['ema21']:.4f})")
    print(f"  EMA50  = ${row['ema50']:.2f}  (close/EMA50 = {row['close']/row['ema50']:.4f})")
    print(f"  EMA200 = ${row['ema200']:.2f}  (close/EMA200= {row['close']/row['ema200']:.4f})")
    if pd.notna(row['rsi14']):
        print(f"  RSI14  = {row['rsi14']:.1f}")
    if pd.notna(row['atr14']):
        print(f"  ATR14  = ${row['atr14']:.4f}  ({row['atr14_pct']*100:.2f}% del precio)")
    if pd.notna(row['high20']):
        print(f"  high20 = ${row['high20']:.2f}  (close vs prev 20-d max)")
    if pd.notna(row['vol_ma20']):
        print(f"  vol_ma20 = {row['vol_ma20']:,.0f}  (today's ratio = "
              f"{row['volume']/row['vol_ma20']:.2f}x)")

    print()
    print("check_entry filters (replica):")
    filters = check_entry_replica(row)
    all_ok = True
    for name, val, ok in filters:
        print(f"  {ok} {name:<25} {val}")
        if ok == "✗":
            all_ok = False
    print()
    verdict = "✓ check_entry HABRÍA aprobado" if all_ok else "✗ check_entry HABRÍA rechazado"
    print(f"  ⇒ {verdict}")
    print()

    # Macro: SPY > EMA200?
    spy_df = fetch_bars("SPY", start, end)
    if spy_df is not None and not spy_df.empty:
        spy_df = compute_indicators(spy_df)
        if target_str in spy_df["date"].values:
            spy_row = spy_df[spy_df["date"] == target_str].iloc[0]
        else:
            prev = spy_df[spy_df["date"] < target_str]
            spy_row = prev.iloc[-1] if not prev.empty else None
        if spy_row is not None and pd.notna(spy_row.get("ema200")):
            spy_close = float(spy_row["close"])
            spy_ema200 = float(spy_row["ema200"])
            macro_ok = spy_close > spy_ema200
            print("Contexto macro (NO chequeado por RFTM):")
            print(f"  SPY close=${spy_close:.2f}  SPY EMA200=${spy_ema200:.2f}  "
                  f"ratio={spy_close/spy_ema200:.4f}")
            print(f"  {'✓ Macro bullish (SPY>EMA200)' if macro_ok else '✗ Macro BEARISH (SPY<EMA200)'}")
            # 50-day SMA de SPY como check secundario
            spy50 = spy_df["close"].rolling(50).mean()
            if not spy50.isna().iloc[-1]:
                close_above_50 = spy_close > spy50.iloc[-1]
                print(f"  SPY SMA50=${spy50.iloc[-1]:.2f}  "
                      f"{'✓ Above' if close_above_50 else '✗ Below'} SMA50")
    return 0


if __name__ == "__main__":
    sys.exit(main())
