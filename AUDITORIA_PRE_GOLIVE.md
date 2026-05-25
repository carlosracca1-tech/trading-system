# Auditoría Forense Pre-Go-Live — Sistema Dual RFTM + MREV

**Fecha de auditoría:** 2026-04-23 UTC
**Ámbito:** Alpaca Paper, ~60 días de datos. Sin tocar código productivo.
**Fuentes de verdad:** Alpaca API (`/v2/account`, `/v2/positions`, `/v2/orders`,
`/v2/account/portfolio/history`), DBs locales SQLite (`trading_paper.db`,
`mrev_paper.db`), workflows GH Actions, git log, `gh run view --log`.
Scripts reproducibles en `scripts/audit/*.py`.

## Resumen ejecutivo

- **Veredicto de readiness: `RED` — no apto para plata real.** (Detalle §8.)
- **No hay alpha.** Portfolio +7.56% vs SPY buy-and-hold +8.47% desde la
  primera compra (2026-03-23). **Alpha = −0.91 pp · Information Ratio = −1.25.**
- **Dos bugs estructurales graves** causan que la DB y Alpaca se desincronicen
  en cada corrida, y que el bot MREV dispare parciales "desde cero" sobre
  posiciones que ya habían avanzado de etapa.
- **La "cascada" del 22/04 16:18 UTC no fue un fallo de lógica**: fue el bot
  MREV corriendo **código pre-split** (SHA `2e1c9c9`, commit `7271283` del
  split se pusheó 2h20m más tarde). MREV reclamó todos los ETFs por
  `sync_with_alpaca` como stage=0, y los vendió con sus propios TPs.
- **El "sell low buy higher" de LINK del 23/04** se explica por (a) `trailing_stop`
  disparado con `close=9.44 ≤ trail=9.46` pero **fill de mercado a $9.16**
  (slippage del 3%), (b) **no hay cooldown** entre exit y re-entry, (c) el
  rebuy **ni siquiera se persistió** en la DB (bug 12 vs 15 cols).
- **Ni RFTM ni MREV tienen bracket orders**: si se cae el runner, la posición
  queda desnuda en Alpaca.

---

## §1 · Reconciliación DB local ↔ Alpaca

Generado por `scripts/audit/02_reconcile.py`. Snapshot tomado
2026-04-23 ~16:30 UTC. Detalle por símbolo:

| symbol | bot | db_qty | alpaca_qty | db_entry | alpaca_avg_entry | diff_qty | diff_pct_entry | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ARGT | RFTM | 135 | 135 | 92.35 | 92.35 | 0 | 0.000% | IN_SYNC |
| AVAXUSD | MREV | 555.627 | 0 | 9.1194 | — | 555.627 | — | **ONLY_IN_DB** |
| DOGEUSD | MREV | 113372.858 | 0 | 0.096 | — | 113372.858 | — | **ONLY_IN_DB** |
| ECH | RFTM | 133 | 133 | 41.7223 | 41.7223 | 0 | 0.000% | IN_SYNC |
| EWJ | RFTM | 141 | 141 | 87.3811 | 87.3811 | 0 | 0.000% | IN_SYNC |
| FLBR | RFTM | 260 | 260 | 24.94 | 24.94 | 0 | 0.000% | IN_SYNC |
| GLD | RFTM | 15 | 15 | 434.88 | 434.88 | 0 | 0.000% | IN_SYNC |
| IWM | RFTM | 24 | 12 | 259.49 | 259.49 | +12 | 0.000% | **QTY_DRIFT** |
| LINKUSD | MREV | 2136.452 | 1195.224 | 9.3793 | 9.4212 | +941.228 | −0.446% | **QTY_DRIFT** |
| PAVE | RFTM | 234 | 234 | 53.8992 | 53.8992 | 0 | 0.000% | IN_SYNC |
| QQQ | RFTM | 11 | 3 | 605.00 | 605.00 | +8 | 0.000% | **QTY_DRIFT** |
| SLV | RFTM | 14 | 14 | 69.9207 | 69.9207 | 0 | 0.000% | IN_SYNC |
| SOLUSD | MREV | 230.841 | 0 | 84.879 | — | +230.841 | — | **ONLY_IN_DB** |
| SPY | RFTM | 10 | 5 | 674.71 | 674.71 | +5 | 0.000% | **QTY_DRIFT** |
| XLE | RFTM | 438 | 0 | 54.1273 | — | +438 | — | **ONLY_IN_DB** |
| XLK | RFTM | 1 | 1 | 141.77 | 141.77 | 0 | 0.000% | IN_SYNC |

Distribución: `IN_SYNC: 8 · QTY_DRIFT: 4 · ONLY_IN_DB: 4 · ONLY_IN_ALPACA: 0`.

### Por qué está desincronizada

**Confirmado empíricamente leyendo los workflows y el .gitignore:**

1. `.gitignore` contiene `*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`
   (commit `b904541`). **Ninguna DB viaja en git.**
2. **`daily_trade.yml` (RFTM) NO persiste la DB.** No hay `actions/cache`
   save ni restore; sólo un `actions/upload-artifact` al final que sube
   `trading_paper.db` como artefacto pero nunca se restaura. En
   consecuencia **cada corrida de RFTM parte con DB vacía** y reconstruye
   posiciones vía `sync_with_alpaca`.
3. `mrev_hourly.yml` sí usa `actions/cache@v4` con key
   `mrev-db-v2-${{ github.ref_name }}`. La DB sobrevive entre runs **mientras
   la cache no expire** (7 días en GH) y **mientras no haya cache miss**. Si
   se pierde, MREV reconstruye desde Alpaca igual que RFTM.
4. Mi DB local (`trading_paper.db`) está congelada al estado del commit
   `a0caea8 chore(db): reconcile positions via seed_missing_positions.py`
   del **2026-04-22 15:14 UTC** — pre-split, pre-cascada. Las posiciones
   que quedaron "ONLY_IN_DB" (AVAX/DOGE/SOL/XLE) se cerraron en Alpaca
   después de esa foto.

Consecuencia operativa: **la DB local es un museo**, no refleja estado real.
Lo único confiable es lo que corre en el runner de GH Actions, y sólo hasta
que la cache caduque.

---

## §2 · Equity curve, drawdowns, métricas

Generado por `scripts/audit/03_equity_curve.py` usando
`/v2/account/portfolio/history?period=90D&timeframe=1D`.

### Serie diaria (últimos 24 puntos)

| date | equity_eod | daily_ret_% | peak | dd_from_peak_% |
| --- | ---: | ---: | ---: | ---: |
| 2026-03-20 | 100,000.00 | +0.000 | 100,000.00 | 0.000 |
| 2026-03-21 | 100,000.00 | +0.000 | 100,000.00 | 0.000 |
| 2026-03-24 | 100,000.84 | +0.001 | 100,000.84 | 0.000 |
| 2026-03-25 | 100,001.89 | +0.001 | 100,001.89 | 0.000 |
| 2026-03-26 | 100,003.36 | +0.001 | 100,003.36 | 0.000 |
| 2026-03-27 | 100,001.88 | −0.001 | 100,003.36 | −0.001 |
| 2026-03-28 | 100,001.88 | +0.000 | 100,003.36 | −0.001 |
| 2026-03-31 | 100,001.88 | +0.000 | 100,003.36 | −0.001 |
| 2026-04-01 | 100,263.78 | +0.262 | 100,263.78 | 0.000 |
| 2026-04-02 | 100,234.08 | −0.030 | 100,263.78 | −0.030 |
| 2026-04-03 | 100,417.68 | +0.183 | 100,417.68 | 0.000 |
| 2026-04-07 | 100,160.91 | −0.256 | 100,417.68 | −0.256 |
| 2026-04-08 | 100,104.25 | −0.057 | 100,417.68 | −0.312 |
| 2026-04-09 | 100,625.58 | +0.521 | 100,625.58 | 0.000 |
| 2026-04-10 | 102,273.97 | +1.638 | 102,273.97 | 0.000 |
| 2026-04-11 | 102,989.13 | +0.699 | 102,989.13 | 0.000 |
| 2026-04-14 | 104,583.62 | +1.548 | 104,583.62 | 0.000 |
| 2026-04-15 | 106,506.46 | +1.839 | 106,506.46 | 0.000 |
| 2026-04-16 | 106,379.36 | −0.119 | 106,506.46 | −0.119 |
| 2026-04-17 | 106,585.82 | +0.194 | 106,585.82 | 0.000 |
| 2026-04-18 | 107,618.67 | +0.969 | 107,618.67 | 0.000 |
| 2026-04-21 | 107,498.15 | −0.112 | 107,618.67 | −0.112 |
| 2026-04-22 | 107,329.42 | −0.157 | 107,618.67 | −0.269 |
| 2026-04-23 | 107,934.16 | +0.563 | 107,934.16 | 0.000 |

### Días con `daily_return` < −1.5%

**Ninguno en la ventana de 90 días.** El peor cierre-a-cierre fue
**−0.256%** el 2026-04-07.

### El "−2% en un día" que recordás

**No existe como daily close-to-close.** Sí existe como *drawdown
intradiario* medido con granularidad 1H:

```
Max intraday DD (30d, 1H):  -1.828%
Desde:  2026-04-10 14:30 UTC  (equity ~102,304)
Hasta:  2026-04-13 13:30 UTC  (equity ~100,443)
```

Es decir, entre el 10 y el 13 de abril el equity cayó 1.83% desde un pico
intradiario, pero se recuperó antes del cierre.

### Métricas agregadas (ventana 90D, ~23 días de trading activo)

| métrica | valor |
| --- | ---: |
| días observados | 23 |
| return total | +7.934 % |
| **CAGR anualizado (crudo)** | **+130.8 %** |
| **Volatilidad anualizada** | **9.67 %** |
| **Sharpe (rf=0)** | **8.71** |
| Max drawdown (close-to-close) | −0.312 % |
| % días positivos | 56.5 % |
| Mean daily | +0.334 % |
| Median daily | +0.001 % |
| Worst day | −0.256 % |
| Best day | +1.839 % |

> ⚠️ **No leer Sharpe 8.7 ni CAGR 130% como ciertos.** N=23 días es
> estadísticamente insuficiente para cualquiera de esas estimaciones.
> Con N=23 el intervalo de confianza del 95% de la media incluye valores
> cercanos a cero.

---

## §3 · Trade-by-trade P&L (60 días)

Generado por `scripts/audit/04_trades.py`. Reconstrucción FIFO sobre
`/v2/orders?status=filled` (dump crudo en `scripts/audit/orders_60d.json`
y CSV en `scripts/audit/trades_closed.csv`).

- **Trades cerrados con entry+exit en la ventana: 36.**
- Entradas "viejas" (pre-60d) no aparecen como cerradas acá porque la
  ventana no contiene ambas patas.

### Stats por bot

| bot | trades | win_rate_% | avg_winner_% | avg_loser_% | expectancy_% | profit_factor | total_pnl_usd | median_hold_h | p90_hold_h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **RFTM** | 25 | **100.0** | +4.075 | 0.000 | +4.075 | ∞ | **+6,207.84** | 149.9 | 314.8 |
| **MREV** | 11 | 72.7 | +2.779 | −1.647 | +1.572 | 1.76 | **+357.33** | 85.1 | 112.1 |

> 100% de win-rate en RFTM es un artefacto del período corto + la dinámica
> "todo es partial TP a +5%/+7.5%, rara vez un stop". Con 25 trades no hay
> muestra para estimar la tasa real.

### Top 5 winners (%)

| symbol | bot | entry | exit | pnl_% | hold_h |
| --- | --- | ---: | ---: | ---: | ---: |
| QQQ | RFTM | 605.00 | 653.66 | +8.043 | 316.6 |
| QQQ | RFTM | 605.00 | 652.90 | +7.917 | 314.7 |
| SOL/USD | MREV | 81.82 | 87.29 | +6.684 | 400.9 |
| IWM | RFTM | 259.49 | 275.64 | +6.225 | 314.8 |
| XLK | RFTM | 141.77 | 149.95 | +5.772 | 149.9 |

### Top 5 losers (%)

| symbol | bot | entry | exit | pnl_% | hold_h | comentario |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| LINK/USD | MREV | 9.458 | 9.162 | **−3.132** | 112.1 | trailing stop + slippage (§5) |
| LINK/USD | MREV | 9.302 | 9.162 | **−1.503** | 105.2 | idem, segundo lote FIFO |
| AVAX/USD | MREV | 9.240 | 9.212 | −0.306 | 64.0 | pequeño |
| DBA | RFTM | 26.80 | 26.89 | +0.336 | 66.8 | positivo, cayó al final del top10 por la cantidad |
| PAVE | RFTM | 53.90 | 54.11 | +0.391 | 149.9 | positivo también |

### Por símbolo (P&L acumulado descendente)

| symbol | bot | trades | wr_% | total_pnl_usd | avg_% | best_% | worst_% |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| QQQ | RFTM | 4 | 100 | +1,328.06 | +6.586 | +8.043 | +5.190 |
| XLE | RFTM | 4 | 100 | +986.95 | +2.748 | +4.970 | +0.477 |
| ECH | RFTM | 2 | 100 | +925.01 | +5.585 | +5.585 | +5.585 |
| IWM | RFTM | 3 | 100 | +886.02 | +4.544 | +6.225 | +3.697 |
| SPY | RFTM | 3 | 100 | +853.53 | +4.215 | +5.202 | +3.718 |
| FLBR | RFTM | 3 | 100 | +736.51 | +3.796 | +3.809 | +3.769 |
| SOL/USD | MREV | 3 | 100 | +558.01 | +4.093 | +6.684 | +2.602 |
| EWJ | RFTM | 1 | 100 | +244.85 | +2.002 | +2.002 | +2.002 |
| DOGE/USD | MREV | 2 | 100 | +193.67 | +3.023 | +4.493 | +1.552 |
| ARGT | RFTM | 1 | 100 | +179.55 | +1.440 | +1.440 | +1.440 |
| AVAX/USD | MREV | 3 | 66.7 | +51.79 | +0.953 | +2.032 | −0.306 |
| PAVE | RFTM | 1 | 100 | +49.11 | +0.391 | +0.391 | +0.391 |
| BTC/USD | MREV | 1 | 100 | +18.82 | +0.740 | +0.740 | +0.740 |
| XLK | RFTM | 2 | 100 | +16.36 | +5.769 | +5.772 | +5.765 |
| DBA | RFTM | 1 | 100 | +1.89 | +0.336 | +0.336 | +0.336 |
| **LINK/USD** | MREV | 2 | **0** | **−464.96** | −2.317 | −1.503 | −3.132 |

### Holding time

Mediana 149.9h (~6.25 días), mean 154.8h, p90 314.8h, max 400.9h. Los
partial TPs están generando rotación cada 5–10 días para RFTM y ~4 días
para MREV (85h).

### ¿+7.5% es alpha o beta?

Script: `scripts/audit/05_spy_compare.py`.

```
Portfolio: $100,000.84 → $107,556.98   (+7.556%)
SPY close: $655.37    → $710.88        (+8.470%)
Alpha (port − SPY):   −0.914 pp
Information ratio:    −1.25  (17 trading days matched)
Tracking error:       1.35 %/día
Mean active return:   −0.11 %/día
```

**El portfolio rindió 0.91 pp menos que comprar SPY y sentarse.** El IR es
fuertemente negativo. La muestra es chica (17 días comparables) pero el
resultado es inequívocamente "no hay alpha en este período". El riesgo
extra de tener 12 posiciones rotantes no se está pagando.

---

## §4 · Forensic cronológico de la cascada 2026-04-22 16:18 UTC

Script: `scripts/audit/06_cascade.py`. Cross-checkeado con
`gh run view --log` de los runs relevantes.

### Qué pasó minuto a minuto

| ts_utc | symbol | side | qty | price | entry_avg | pnl_% | clasif. FIFO | bot origen |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 16:18:21.102Z | DOGE/USD | sell | 113372.86 | 0.09769 | 0.096 | +1.78 | EARLY_EXIT | MREV |
| 16:18:21.896Z | SPY | sell | 5.0 | 709.81 | 674.71 | +5.20 | TP1 | **MREV (pre-split)** |
| 16:18:25.307Z | QQQ | sell | 5.0 | 652.90 | 605.00 | +7.92 | TP2 | **MREV (pre-split)** |
| 16:18:27.494Z | IWM | sell | 12.0 | 275.64 | 259.49 | +6.23 | (entre TP1 y TP2) | **MREV (pre-split)** |
| 16:18:29.100Z | XLE | sell | 438.0 | 56.34 | 54.13 | +4.09 | mean-rev TP (X1) | **MREV (pre-split)** |
| 18:11:16.618Z | QQQ | sell | 3.0 | 653.66 | 605.00 | +8.04 | TP2 (re-fire) | **MREV (pre-split)** |
| 23:01:44.004Z | SOL/USD | sell | 230.84 | 87.29 | 84.87 | +2.85 | mean-rev TP (X1) | MREV |

### Quién disparó cada orden (confirmado con logs de GH)

- **Run `24789517390` (MREV) — start 16:17:42Z, fills 16:18:2x** — el log
  dice literalmente:

  ```
  ✓  Trading 15 symbols: BTC/USD, ETH/USD, SOL/USD, AVAX/USD, DOGE/USD,
                         LINK/USD, SPY, QQQ, IWM, XLE, XLF, GLD, SLV, BITO, ARKK
  ✓  SYNC MREV: insertada SPY qty=10.0 @ $674.7127 (stage=0, initial_qty=10.0)
  ✓  SYNC MREV: insertada QQQ qty=11.0 @ $605.0005 (stage=0, initial_qty=11.0)
  ✓  SYNC MREV: insertada IWM qty=24.0 @ $259.4865 (stage=0, initial_qty=24.0)
  ✓  SYNC MREV: insertada XLE qty=438.0 @ $54.1273 (stage=0, initial_qty=438.0)
  ✓  SYNC MREV: insertada DOGE/USD qty=113372.8575 @ $0.0960 (stage=0)
  ...
  EXIT  DOGE/USD  $ 0.10  P&L: $+234.92  (take_profit (close=0.10 ≥ sma+1.5atr=0.10))
  PTP1  SPY       $ 709.87  50%=5.0  (+5.21%)
  PTP1  QQQ       $ 653.07  50%=5.0  (+7.95%)
  PTP1  IWM       $ 275.77  50%=12.0 (+6.28%)
  EXIT  XLE       $ 56.35   P&L: $+973.55 (take_profit (close=56.35 ≥ sma+1.5atr=56.17))
  ```

- **El `headSha` del run era `2e1c9c9`** — **commit anterior al split**.
  El commit del split `7271283 feat: split MREV/RFTM universes — MREV
  crypto-only` se pusheó **2026-04-22 15:37:51 -03 (= 18:37:51 UTC)**.
  La corrida de MREV que tocó los ETFs fue a las **16:17:42 UTC**, dos
  horas y veinte minutos **antes** del push del split.

- **Run `24794693212` (MREV) — start 18:10:35Z, fill 18:11 (QQQ 3 extra)**:
  también con `2e1c9c9`. El segundo TP2 disparó porque la DB de cache
  venía con stage=1 para QQQ, y close=$653 dio +8% (>7.5%), vendió 50%
  del remanente (5 → 3, floor).

### Por qué cada exit

| exit | razón del código | interpretación |
| --- | --- | --- |
| DOGE/USD | `take_profit (close=0.10 ≥ sma+1.5atr=0.10)` | X1 mean-reversion TP (MREV). Corría contra SMA20 + 1.5·ATR. |
| SPY +5.20% | `partial_tp1_5.0pct:+5.21%` | MREV TP1 (50% del qty). |
| QQQ +7.92% | `partial_tp2_7.5pct:+7.95%` (stage 1→2) | MREV TP2 al vender 50% del remanente. |
| IWM +6.23% | `partial_tp1_5.0pct:+6.28%` | MREV TP1 (stage=0 recién sincronizada). |
| XLE +4.09% | `take_profit (close=56.35 ≥ sma+1.5atr=56.17)` | X1 mean-reversion. Explica el **full exit** a +4% (no era TP1 ni TP2). |
| QQQ +8.04% | `partial_tp2_7.5pct` (segunda vuelta) | idem QQQ +7.92. |
| SOL +2.85% | `take_profit ... sma+1.5atr` | X1 MREV. |

### `migrate_legacy_etf_positions` — ¿vendió algo?

**No.** Solo actualiza `status='CLOSED', exit_reason='migrated_etf_out_of_mrev'`
en la DB local. No manda orders a Alpaca (inspección directa del código
en `standalone_mrev_trader.py:480-516`).

### Conclusión del §4

El comportamiento observado es **exactamente lo que esperabas de MREV bajo
la versión previa al split** (universo mixto cripto + ETFs). La cascada se
habría evitado si el split se hubiera deployado antes de la corrida de las
16:17. El RFTM bot corrió aparte a las 14:59 UTC y no mandó **ninguna
venta** (`EXIT signals: 0`, ver log del run `24785642072`).

---

## §5 · El caso LINK/USD — "sell low, buy higher" del 23/04

### Cronología con fuentes

| 23/04 UTC | evento | fuente |
| --- | --- | --- |
| 09:00:44 | MREV run `24826492790` arranca (SHA `7271283`, post-split, 6 symbols) | `gh run view` |
| 09:01:04 | MREV **close LINK** por `trailing_stop (close=9.44 ≤ trail=9.46)` — P&L logueado +$128.14 | log del run |
| 09:01:04.482 | Fill Alpaca: **sell 2136.45 @ $9.1618** (3.02% de slippage bajo el close usado por el bot) | `/v2/orders` |
| 15:20:43 | MREV run `24843452196` arranca (SHA `7271283`) | `gh run view` |
| 15:21:22 | MREV detecta entry: `ENTER LINK/USD $ 9.29 qty=1198.22 stop=$9.17 RSI=34.3` | log del run |
| 15:21:22 | **Buy enviado y filleado a $9.4212** (1.43% de slippage sobre el close del bot) | `/v2/orders` |
| 15:21:22 | El bot intenta persistir en DB y falla: `Failed to buy LINK/USD: table mrev_positions has 15 columns but 12 values were supplied` | log del run |

### Diagnóstico preciso

1. **El exit fue `trailing_stop` (X5 de `check_exit`)**, no un stop loss ni
   un TP. El trigger fue marginal: `close=9.44 ≤ trail=9.46`, 2 centavos.
   El bot pensó que vendía con ganancia (+$128).
2. **Slippage del 3% en la venta** — Alpaca llenó el market order a $9.16
   en una ventana de alta volatilidad de LINK. La P&L real del
   bot fue **−$465** (ver `trades_closed.csv` y stats §3).
3. **Slippage del 1.4% en la re-compra** — close del bot $9.29 vs fill
   $9.42.
4. **No hay cooldown post-stop.** Entre 09:01 (exit) y 15:21 (rebuy) el bot
   corrió 5 veces (6:15, 9:00, 10:43, 12:06, 15:20). En cuanto LINK volvió
   a cumplir `RSI≤45 AND close≤bb_lower`, el bot volvió a comprar. El
   valor absoluto del re-entry price ($9.29) es superior al exit price
   ($9.44 close usado / $9.16 fill), lo cual es consistente con la tesis
   mean-reversion ("la caída extra abrió una nueva entrada válida"),
   pero el PNL neto del round trip es **negativo por slippage**.
5. **El bug crítico está en la persistencia**: el INSERT de
   `standalone_mrev_trader.py:1701` tiene **12 placeholders** para una
   tabla de **15 columnas** (`id, run_id, symbol, qty, entry_price,
   stop_loss, entry_dt, status, exit_price, exit_dt, pnl, exit_reason,
   highest_since_entry, partial_tp_taken, initial_qty`). La `exception`
   se captura silenciosamente y el bot sigue: la compra queda en Alpaca
   sin registro en mrev_positions. En la próxima corrida
   `sync_with_alpaca` la re-inserta con `stage=0, highest_since_entry=entry,
   initial_qty=qty_actual` — perdiendo la historia de trailing y
   re-arrancando el ciclo de parciales desde cero.

### Sizing del rebuy: 1198 vs 2136 originales

El qty bajó ~56% por **la combinación de riesgo por ATR y cap de cash**
(no por un downsize elegido). Tras el stop loss realizado, el cash
disponible quedó en ~$25K (MREV capital nominal), pero el `size_position`
del bot:

```
qty_risk = equity * 0.05 / (2 × ATR)   # ATR de LINK en 1H ≈ 0.20 aprox.
qty_cap  = equity * 0.40 / close       # cap notional
```

Con LINK más volátil después del drop, el ATR subió, achicando `qty_risk`.
Además el equity efectivo había bajado por la pérdida realizada.

### ¿Bug, feature o mala señal?

- **Bug**: persistencia (INSERT rota) + falta de cooldown + no hay límite
  de slippage.
- **Feature (intencional)**: la estrategia *sí* permite re-entrar después
  de exit si la nueva vela cierra oversold. Eso es mean-reversion.
- **Mala señal**: el trigger del trail stop fue marginal (2 centavos) y
  el slippage borró el margen.

### Propuesta (NO implementada)

- **Cooldown**: 24h desde un stop/trailing exit sobre el mismo símbolo
  antes de permitir un nuevo entry.
- **Límite de slippage**: convertir los market orders de cripto a
  marketable-limit (close ± 50 bps) y cancelar si no se llena.
- **Arreglar el INSERT** (§6 bug B). Tapar también el `except Exception`
  silencioso.

---

## §6 · Revisión del código sensible

No modifiqué nada. Observaciones por función.

### 6.1 RFTM — `check_entry` (`standalone_paper_trader.py:394-440`)

Pseudocódigo:
```
C1: close > ema21  AND  close > ema50
C2: 55 ≤ rsi14 ≤ 70
C3: close > high20  (breakout)
C4: volume ≥ 0.8 × vol_ma20  (si vol_ma válido)
C5: 0.3% ≤ atr14_pct ≤ 8%
```
- Sano. Los bailouts usan `_is_valid_number`; devuelve `False` sin excepción.
- Filtro estricto: en escáneos reales "solo ~10% de candidatos pasan" —
  buen selectivity para un trend-follow.

### 6.2 RFTM — `check_exit` (`standalone_paper_trader.py:442-496`)

```
E3: close ≤ stop_loss                                      → STOP
E7: stage ≥ 2 AND close ≥ entry + 2·(entry-stop)          → +10% TP (stage 2)
E5 phase 3 (agresivo): profit_atr ≥ 1.5 AND close ≤ high - 1.0·atr
E5 phase 2 (breakeven): profit_atr ≥ 0.5 AND close ≤ entry
E6: bars_since_last_high ≥ 20
```
- E1/E2/E4 comentados (el bot mata exits prematuros). OK.
- **Edge case:** si `atr=0` o `highest_since_entry=0`, E5 no dispara. OK.
- **Edge case:** si `stop_loss ≥ entry` (por ejemplo post-TP1 con stop en
  breakeven), E7 queda desactivado por `stop_loss < entry_price`. OK.
- **Edge case:** no hay manejo explícito de gap-down abriendo debajo del
  stop; como son ETFs y el bot corre al abrir, es aceptable. En cripto
  (24/7) este código no se llama — MREV tiene su propio check.

### 6.3 RFTM — `_partial_take_profit` inline (`standalone_paper_trader.py:1510-1572`)

```
tp_stage = 0 AND close/entry - 1 ≥ 0.05 AND qty ≥ 2  → vende 50%, stage→1
tp_stage = 1 AND close/entry - 1 ≥ 0.075 AND qty ≥ 2 → vende 50% del remanente, stage→2
```
- `sell_qty = floor(cur_qty * 0.50)`. Con qty=5, vende 2 (no 3 — `floor`).
- Chequea `notional ≥ PARTIAL_MIN_NOTIONAL_USD ($10)` para no mandar
  órdenes dust.
- **Bug latente**: `cur_qty = int(pos["qty"])` — si viene como string o
  decimal (LINK tiene qty=0.0001), lo trunca. Pero RFTM solo opera ETFs
  donde qty es entera, así que no aplica.

### 6.4 RFTM — `size_position` (`standalone_paper_trader.py:501-510`)

```
shares_risk = floor(portfolio_value × 0.05 / (1.5 × atr))
shares_cap  = floor(portfolio_value × 0.25 / close)
shares      = min(shares_risk, shares_cap)
```
- **`ATR_MULT=1.5` está HARDCODEADO**, no viene de env (ver declaración en
  la cabecera). No es crítico, pero si ajustás el stop distance por
  config habría que moverlo a env var.
- Sin validación de `close=0` — trivial divide-by-zero si se alimentara
  data incompleta. En condiciones normales el data fetcher bloquea.

### 6.5 RFTM — `sync_with_alpaca` (`standalone_paper_trader.py:836-950`)

Lee Alpaca, compara, hace tres cosas:

1. **Cerrar DB local si Alpaca no tiene el symbol** — set `status='closed',
   close_reason='synced_from_alpaca'`.
2. **Arreglar entry_price/qty si difieren** — sobre-escribe lo local sin
   tocar `partial_tp_taken`.
3. **Insertar positions que Alpaca tiene y DB no**, **con `partial_tp_taken=0`
   y `highest_since_entry=real_entry`** (línea 935). **Esta es la raíz del
   "re-firing" de TP1**: cada corrida limpia de DB, MREV/RFTM re-agregan
   todas las posiciones con stage=0 y vuelven a dispararse los parciales.

**Edge case preocupante**: pre-split, los ETFs caían también en el loop
del MREV `sync_with_alpaca` porque `ALL_SYMBOLS` incluía ETF_SYMBOLS.
Fue lo que gatilló la cascada §4. Post-split eso está cerrado.

**Riesgo residual**: la función trunca qty a `int(float(qty))`
(línea 927), lo cual **destruye fracciones de cripto** si RFTM llegara a
reclamarlas. El filtro de `_is_crypto` en línea 917-920 lo evita, pero
es defensivo por accidente — si alguien agrega un ETF que termina en
"USD" (e.g. "USDY"), esto podría volverse problema.

### 6.6 MREV — `check_entry` (`standalone_mrev_trader.py:317-339`)

```
requires not-NaN: sma_20, bb_lower, rsi_14, atr_14_pct, volume_ma_20
rsi ≤ 45
close ≤ bb_lower
volume ≥ 0.5 × volume_ma_20
0.2% ≤ atr_pct ≤ 15%
```
- Sano. Devuelve motivo al rechazar.

### 6.7 MREV — `check_exit` (`standalone_mrev_trader.py:342-377`)

```
X1: close ≥ sma_20 + 1.5 × atr_14          (mean-rev TP)
X2: close ≤ entry - 2.0 × atr_14           (stop loss)
X4: hours_held ≥ 120                        (time stop)
X5: close ≤ highest_since_entry - 1.0×atr  (trailing stop)
```
- **No existe X3** (comentado). Sano.
- **X4 depende de `entry_dt`** que, tras la cadena sync → insert, se fija
  a `datetime.now()` — **reseteando el reloj** en cada corrida fresca de DB.
  Positivo si querés evitar time-stops espurios; negativo si querés que el
  time-stop proteja realmente.

### 6.8 MREV — partial-TP inline (`standalone_mrev_trader.py:1432-1488`)

Mismo esquema 5% → 50% / 7.5% → 50% del remanente. Usa `_round_qty` que
respeta la precisión cripto (BTC 0.0001, DOGE 1.0, etc).

### 6.9 MREV — `size_position` (`standalone_mrev_trader.py:384-408`)

- `stop_dist = 2 × ATR`.
- `qty_risk = equity × 0.05 / stop_dist`.
- `qty_cap = equity × 0.40 / close` (max 40% notional).
- Para cripto: `qty = floor(raw_qty / min_qty) × min_qty`. Para ETF: floor
  entero.
- **Mínimo $10 notional**. Bajo ese umbral, qty=0.

### 6.10 MREV — `sync_with_alpaca` (`standalone_mrev_trader.py:526-601`)

- Resuelve el alias Alpaca `AVAXUSD` ↔ `AVAX/USD`. OK.
- Filtra por `ALL_SYMBOLS` antes de insertar — post-split ALL_SYMBOLS =
  CRYPTO_SYMBOLS, así no toca ETFs.
- Setea `highest_since_entry = entry_price` (línea 598) — **igual que
  RFTM pierde el high real** si la DB se cayó.

### 6.11 MREV — INSERT roto en BOUGHT (`standalone_mrev_trader.py:1701-1703`)

```python
conn.execute("INSERT INTO mrev_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    (str(uuid.uuid4())[:8], run_id, b["symbol"], b["qty"], b["price"],
     b["stop"], now.isoformat(), "OPEN", None, None, None, None))
```

Tabla tiene **15 columnas**; el `INSERT VALUES` asume orden posicional y
pasa solo **12 valores**. Todo ENTER MREV tira `OperationalError` atrapado
por el `except Exception` en la línea 1709, log dice
`Failed to buy {symbol}: table mrev_positions has 15 columns but 12
values were supplied` y el bot **continúa como si nada**. La orden de
compra ya se mandó a Alpaca en la línea 1695 antes del INSERT — por eso
la discrepancia DB/Alpaca.

**Este es un bug ciego; afecta las entradas nuevas de MREV desde que se
agregaron columnas a la tabla**. Funciona "de casualidad" porque
`sync_with_alpaca` repara desde Alpaca, pero pierde stage, highest,
initial_qty correctos.

---

## §7 · Workflows de GitHub Actions

### 7.1 `.github/workflows/daily_trade.yml` (RFTM)

- **Cron:** `35 13 * * 1-5` — 13:35 UTC de L a V, 5 min después de NYSE
  open en EDT (en EST tocaría mover a 14:35 UTC; la nota en el YAML lo dice).
- **Concurrency:** **NO HAY.** Dos manual dispatches rápidos pueden
  correr en paralelo y duplicar órdenes. Riesgo bajo porque el bot es
  determinístico y hace pocas operaciones, pero nominal.
- **Env:** ALPACA_API_KEY/SECRET vienen de GitHub Secrets; crea `.env.paper`
  inline. **No pasa** `PARTIAL_TP*`, `MAX_DRAWDOWN`, `ALPACA_BP_SAFETY` —
  usan defaults hardcodeados del código (listados §10).
- **Persistencia DB:** **NULA.** No hay `actions/cache` save ni restore.
  Solo `actions/upload-artifact` con `trading_paper.db` (log y DB) —
  guarda por 30 días pero **nunca se vuelve a montar**. Cada run parte
  del git checkout (que no incluye la DB por `.gitignore`).
- **Notificación de falla:** No hay email ni Slack. El bot tira exit 1 y
  eso queda en la UI de Actions. Si el cron corre un viernes tarde y
  nadie mira, el lunes se opera con un estado inconsistente.
- **Impacto:** cada corrida de RFTM (una por día hábil) rearma la DB de
  cero. Ver §6.5 + §3: este es el driver #1 de que los parciales
  "renazcan" y que la DB local nunca coincida con Alpaca.

### 7.2 `.github/workflows/mrev_hourly.yml` (MREV)

- **Cron:** `5 * * * *` — cada hora al minuto 5. En la práctica GH mueve
  el reloj ±15 min.
- **Concurrency:** `group: mrev-hourly-${{ github.ref }}, cancel-in-progress: false`.
  OK, los dos runs no pueden solapar.
- **Env:** ídem RFTM, además `MREV_INITIAL_CAPITAL=25000, MREV_MAX_POSITIONS=6,
  MREV_RISK_PER_TRADE=0.05, EMAIL_HOURS_UTC=12, EMAIL_MONTHLY_DAY=1,
  ACCOUNT_TOTAL_CAPITAL=100000, DAILY_BOT_CAPITAL=75000`.
- **Persistencia DB:** usa `actions/cache@v4` con
  `key: mrev-db-v2-${{ github.ref_name }}`, `restore-keys: mrev-db-v2-`.
  Sobrevive **entre runs del mismo branch** siempre que no haya cache
  miss ni expiración (GH limpia entradas no usadas >7 días).
- **WAL checkpoint** antes del save — correcto.
- **Notificación de falla:** no hay. Idem RFTM.
- **Artifacts:** `mrev_output.txt + mrev_paper.db` con `retention-days: 14`.

### 7.3 Problemas comunes

- **No hay healthcheck de DB.** Si la DB arrancó nueva (cache miss), el
  bot no lo señaliza — simplemente corre fresh.
- **No se valida que el commit SHA de producción sea el esperado.** Como
  mostró la cascada del 22/04, un bot puede correr con un SHA viejo si
  el push del fix está en flight.
- **`.env.paper` se construye desde secrets usando `cat > .env.paper <<EOF`**
  con `${{ secrets.X }}` interpolado — si uno de los secretos tiene un
  `$` bien puesto, expande mal. Bajo riesgo, vale registrarlo.

---

## §8 · Veredicto

```
GO-LIVE READINESS: RED

BLOQUEANTES (deben arreglarse antes de plata real):

  1. [RFTM] daily_trade.yml no persiste la DB. Cada run parte en blanco
     y sync_with_alpaca re-inserta todas las posiciones con stage=0.
     Los parciales (TP1/TP2) se re-disparan cada corrida sobre el mismo
     precio de referencia hasta que el TP finalmente se agote.
     (Arreglo: actions/cache igual que MREV, o commit de DB, o migrar
     a Postgres/SQLite persistente en un volumen externo.)

  2. [MREV] INSERT INTO mrev_positions usa 12 placeholders sobre una
     tabla de 15 columnas. Cada ENTER MREV falla silenciosamente en la
     persistencia. El Alpaca order se envía. El bot continúa porque el
     except Exception lo traga. Líneas exactas:
     standalone_mrev_trader.py:1701-1703.

  3. [Arquitectura] No hay bracket orders. Si el runner muere post-buy
     y no hay otro run, la posición queda desnuda sin stop. Con plata
     real esto puede ser catastrófico.

  4. [Risk] No hay alpha sobre SPY buy-and-hold en el período. El
     portfolio underperforma 0.91 pp con IR = −1.25. La muestra es
     chica (~17 días comparables) pero el signo es consistente con
     "el bot cobra comisiones y slippage en lugar de pagar por tiempo
     de mercado". Ir a plata real en este estado es entregar a
     broker + slippage un % del equity esperado por diseño.

  5. [MREV] El sell-then-rebuy de LINK del 23/04 combinó un trailing
     stop marginal (2 centavos de gap) con 3% de slippage en la venta
     por order-type = market. No hay ni cooldown post-exit ni control
     de slippage, y el bug (2) hace que cada rebuy se trate como nueva
     posición stage 0.

  6. [DB] La DB local (mi máquina) y Alpaca tienen 4 QTY_DRIFT y 4
     ONLY_IN_DB. Para pasar a plata real hace falta un proceso de
     reconciliación automático y auditable, idealmente con alertas si
     el drift supera un umbral.

RIESGOS ACEPTABLES PERO A MONITOREAR:

  - El kill-switch MAX_DRAWDOWN (20%) sólo frena ENTRADAS nuevas — no
    cierra posiciones abiertas. Documentarlo explícito y considerar un
    parachute "hard close at 30%" opcional.
  - Win-rate RFTM 100% (25/25) es ruido de sample chico. Va a degradarse
    a lo que sea el WR real (probablemente 55-70% en trend-follow).
  - MREV depende de la cache de GH Actions — si expira (>7d sin runs) o
    si cambiás el branch, la DB nace fresh.
  - Universes disjuntos OK post-split, pero no hay test automatizado que
    verifique la invariante. Un commit futuro podría re-introducir el
    overlap sin fallo visible hasta la siguiente cascada.
  - El cron RFTM cron es 13:35 UTC — en EST (Nov-Mar) son las 8:35 ET,
    una hora antes del open. Anotado en el YAML pero no automatizado.

EVIDENCIA DE RENTABILIDAD:

  - Es alpha real: NO (inconclusivo pero negativo).
    Razón: Portfolio +7.56% vs SPY +8.47% en ventana idéntica
    (2026-03-23 → 2026-04-23). IR −1.25. Se pagó tracking error para
    perder contra el benchmark.
  - Win rate por bot:
        RFTM 25/25 = 100%  (artefacto: solo hay partial TPs en la ventana)
        MREV 8/11  = 72.7% (más realista; 2 losers LINK de $465 total)
  - Sharpe (rf=0):  8.71 (artefacto de 23 días)
  - CAGR anualizado: 130.8% (artefacto)
  - Max DD close-to-close: 0.31% (artefacto — muestra sin stress)
  - vs SPY B&H:     −0.91 pp (underperform)
  - Muestra suficiente: NO. 36 trades cerrados, 23 días de trading.
    Para declarar alpha con p<0.05 sobre un edge esperado de ~3-5%/año
    hacen falta del orden de 200+ trades o 6+ meses de operación
    multi-activo.
```

### ¿Qué tiene que pasar para semáforo YELLOW?

- Resolver bloqueantes 1, 2, 3 (persistencia DB, INSERT bug, bracket
  orders o alertas de "bot caído").
- Extender el período de paper a **al menos 90 días sin intervenciones
  de seed_missing_positions**, con DB persistente.
- Medir alpha vs SPY en esa ventana con IR > 0.

### ¿Y para GREEN?

- YELLOW + evidencia estadística de alpha (IR > 0 sustentado, al
  menos 100 trades cerrados, drawdown real observado en un mes rojo).
- Runbook de failure modes: bot caído, DB perdida, gap intradiario,
  expiration de cache, bracket orders indisponibles, secrets rotados.
- Reconciliación automatizada post-run con alerta si hay drift > N%.

---

## §9 · Recomendaciones priorizadas

| # | Prioridad | Tarea | Archivo · Función · Línea | Evidencia | Esfuerzo | Requiere aprobación |
| - | --- | --- | --- | --- | --- | --- |
| 1 | **P0** | Arreglar INSERT MREV: 15 cols vs 12 placeholders. Usar columnas explícitas como en `sync_with_alpaca`. | `standalone_mrev_trader.py:1701-1703` | §5, §6.11. Log del run 24843452196: "15 columns but 12 values" | S | Sí (toca ENTER path) |
| 2 | **P0** | Persistir `trading_paper.db` en `daily_trade.yml` igual que `mrev_hourly.yml` (actions/cache + WAL checkpoint). | `.github/workflows/daily_trade.yml` | §1, §7.1, §4 ("SYNC: added missing position SPY..." en log run 24785642072) | S | No (infra) |
| 3 | **P0** | No tragarse excepciones de INSERT: cambiar `except Exception as e: err(...)` (1709) por un comportamiento que **cancele la orden** si la DB no puede persistir, o al menos abortar el run. | `standalone_mrev_trader.py:1709-1710` | §5. Cualquier error futuro queda oculto. | S | Sí |
| 4 | **P1** | Cooldown post-stop/trailing. Un nuevo entry del mismo símbolo requiere ≥ N barras (propuesta: N=6 para MREV 1H = 6h, N=5 días para RFTM). | `standalone_mrev_trader.py` alrededor de `check_entry`; `standalone_paper_trader.py` idem. Nueva tabla `last_exit_by_symbol`. | §5 | M | Sí (cambia entry path) |
| 5 | **P1** | Convertir market orders de cripto a marketable-limit (`limit = close + 50 bps` buy, `close - 50 bps` sell) con cancelación si no llenan en N segundos. | `alpaca_submit_order` en ambos bots. | §5 slippage LINK 3%. | M | Sí |
| 6 | **P1** | Alertas de falla (Email/Telegram) en los dos workflows. Si `exit code != 0`, si la cache de DB no se restauró, si el SHA del run != HEAD de main. | ambos yml + hook shell. | §7 | S/M | No (infra) |
| 7 | **P1** | Bracket orders en Alpaca al momento del entry (stop + take-profit del lado broker). Mitiga "bot caído". | `alpaca_submit_order` (body `stop_loss`, `take_profit`). | §8 | M | Sí |
| 8 | **P2** | Asserts/tests de invariante: "ningún símbolo puede estar open simultáneamente en RFTM y MREV". | Nuevo test `tests/test_universe_disjoint.py`. | CLAUDE.md convención, §4 cascada. | S | No |
| 9 | **P2** | `sync_with_alpaca`: al re-insertar un symbol existente, cross-checkear si el último exit fue `synced_from_alpaca` reciente y **no resetear** `partial_tp_taken` a 0 sin evidencia. | `standalone_paper_trader.py:910-940`, `standalone_mrev_trader.py:572-600` | §3 win-rate inflado por TP re-firings. | M | Sí |
| 10 | **P2** | Script de reconciliación auditable (nuevo `scripts/reconcile.py`) que corra en cada PR y publique un diff DB↔Alpaca. | nuevo | §1 | S | No |
| 11 | **P3** | Mover `ATR_MULT=1.5` (hardcoded) a env var en RFTM, y documentarlo. | `standalone_paper_trader.py` header | §6.4 | S | Sí |
| 12 | **P3** | Medir slippage sistemáticamente: guardar close-at-signal vs filled_avg_price en la tabla de orders/signals. | ambos bots | §5 | S/M | No |
| 13 | **P3** | Switch automático de cron EDT ↔ EST en noviembre (workflow_dispatch manual por ahora). | `.github/workflows/daily_trade.yml` | §7.1 | S | No |

**Ítems marcados "Requiere aprobación explícita"** están protegidos por
CLAUDE.md (tocan `check_entry`, `check_exit`, `_calc_take_profit`,
`size_position`, universos, o el flujo de ejecución de órdenes).

---

## §10 · Anexos

### Env vars efectivas (defaults / código)

| var | default | usado por |
| --- | --- | --- |
| ALPACA_BP_SAFETY | 0.90 | RFTM + MREV |
| MAX_DRAWDOWN | 0.20 | RFTM + MREV (kill switch) |
| INITIAL_CAPITAL | 75,000 (código) / 100,000 (workflow RFTM) | RFTM |
| MAX_POSITIONS | 10 | RFTM |
| MAX_POSITION_PCT | 0.25 | RFTM |
| RISK_PER_TRADE | 0.05 | RFTM |
| ATR_MULT | 1.5 (hardcoded) | RFTM |
| MIN_SHARES | 1 (hardcoded) | RFTM |
| PARTIAL_TP1_PCT · RATIO | 0.05 · 0.50 | RFTM + MREV |
| PARTIAL_TP2_PCT · RATIO | 0.075 · 0.50 | RFTM + MREV |
| PARTIAL_MIN_NOTIONAL_USD | 10.0 | ambos |
| MREV_INITIAL_CAPITAL | 25,000 | MREV |
| MREV_MAX_POSITIONS | 6 | MREV |
| MREV_RISK_PER_TRADE | 0.05 | MREV |
| ACCOUNT_TOTAL_CAPITAL | 100,000 | MREV reportes |
| DAILY_BOT_CAPITAL | 75,000 | MREV reportes |
| EMAIL_HOURS_UTC | 12 | MREV |
| EMAIL_MONTHLY_DAY | 1 | MREV |

### Dumps generados por esta auditoría

- `scripts/audit/_alpaca.py` — helper de API.
- `scripts/audit/01_snapshot.py` — pull del estado vivo (account,
  positions, orders, portfolio_history).
- `scripts/audit/02_reconcile.py` — DB vs Alpaca.
- `scripts/audit/03_equity_curve.py` — serie diaria + métricas.
- `scripts/audit/04_trades.py` — FIFO + stats por bot/símbolo.
- `scripts/audit/05_spy_compare.py` — comparación vs SPY B&H.
- `scripts/audit/06_cascade.py` — forensic de 04-21/22/23.
- Outputs: `scripts/audit/account.json`, `positions.json`,
  `orders_60d.json`, `portfolio_history_90d.json`,
  `portfolio_history_30d_1h.json`, `activities_fills_60d.json`,
  `reconcile_db_vs_alpaca.csv`, `equity_curve.csv`, `equity_metrics.json`,
  `trades_closed.csv`, `*_output.txt`.

### Fuentes primarias citadas

- Alpaca: `/v2/account` (equity 107,557 · cash 29,607 · buying_power 126,019).
- Alpaca: `/v2/positions` (12 symbols).
- Alpaca: `/v2/orders?status=filled` (60d, 56 orders).
- Alpaca: `/v2/account/portfolio/history` (90D/1D y 30D/1H).
- Alpaca Data: `/v2/stocks/SPY/bars?feed=iex` (B&H reference).
- Commits: `5a6ee9e` (HEAD), `7271283` (split), `2e1c9c9` (pre-split),
  `a0caea8` (seed real run).
- GH Actions runs: `24785642072` (RFTM 04-22), `24789517390` (MREV 04-22
  cascada), `24794693212` (MREV 04-22 18:11 QQQ3), `24826492790` (MREV
  04-23 LINK exit), `24843452196` (MREV 04-23 LINK rebuy).

### Cosas que NO pude verificar

- Logs retroactivos de GH Actions **>90 días** — no hay retención.
  Conclusiones sobre drift de parciales anteriores se limitan al
  horizonte de runs todavía visibles.
- Estado del volumen-ma y bb_lower en el momento exacto del re-entry
  LINK del 23/04 15:21 — habría que re-computar con las velas horarias
  congeladas (no las actuales). No lo hice porque el log del run ya
  confirma la señal; re-deriving no aporta nada nuevo.
- El kill-switch 20% MAX_DRAWDOWN **nunca se ejecutó en la ventana**
  (máximo drawdown real ~0.3% close-to-close). No podemos validar que
  ese path de código funcione en vivo.

### Notas de credenciales / permisos

Tuve acceso a:

- `.env.paper` (API keys Alpaca Paper).
- `gh` CLI autenticado para `carlosracca1-tech/trading-system` (bajé
  logs con `gh run view --log`).
- DBs locales (`trading_paper.db`, `mrev_paper.db`).

No fue necesario pedir nada más. Todas las consultas a Alpaca fueron
**read-only** (`GET` sobre `/v2/account*`, `/v2/positions`, `/v2/orders`,
`/v2/stocks/*/bars`). Ninguna modificó estado.
