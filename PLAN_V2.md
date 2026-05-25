# Plan V2 — MREV mejoras estructurales

**Documento de handoff para la próxima sesión de trabajo sobre `trading-system`.**

**Autor:** Sesión 2026-05-24 (post-fix de micro-pérdidas)
**Lectura obligatoria antes de tocar código:** `CLAUDE.md` + este documento + secciones marcadas como "PRE-WORK OBLIGATORIO".

---

## 0) Contexto: por qué este plan existe

El sistema viene de un fix crítico hecho hoy (2026-05-24, commits `4409888` y `3537efc`):

- **Bug eliminado:** check_exit usaba `stop = entry − 2×ATR` y trailing stop. Esto disparaba ventas dentro de los 60–90s de la compra, generando micro-pérdidas sistemáticas (caso AVAX: buy $9.31 → sell $9.13 a 84s, −$232).
- **Bug colateral arreglado:** mrev_watchdog filtraba posiciones por `run_id` propio, no veía posiciones del hourly bot, dejaba stops sin protección.

**Estado post-fix:**

- Performance histórica reportada por el usuario: **+11% en menos de 2 meses** (antes de los bugs introducidos).
- Sistema funcional. NO está roto. Cualquier cambio adicional es OPTIMIZACIÓN, no urgencia.
- Tests: 21/21 verdes el último push (`3537efc`).

**Análisis crítico de la estrategia (basado en research público) identificó 3 gaps estructurales:**

1. Timeframe 1H es subóptimo para mean reversion en cripto (consenso: 4H o daily).
2. Universo de 6 cripto altamente correlacionadas (>0.7 vs BTC) = falsa diversificación, en la práctica una sola apuesta apalancada a BTC.
3. Falta filtro de régimen. La literatura es unánime: mean reversion sin filtro de régimen pierde plata en mercados tendenciales. Mismo combo BB+RSI puede dar 35% win rate sin filtro vs 71% con filtro.

Sources de research están al final del documento.

---

## 1) Objetivos de esta sesión

Implementar las TRES mejoras estructurales identificadas, **sin romper la lógica core que ya funciona**:

| # | Cambio | Impacto esperado |
|---|---|---|
| 1 | Migrar MREV de timeframe 1H → 4H | Menos ruido, menos transactions, mejor R:R efectivo |
| 2 | Re-armar universo cripto con descorrelación real | Diversificación real, reduce drawdowns simultáneos |
| 3 | Agregar filtro de régimen (BTC trend + ADX) | Evitar fadear breakdowns sostenidos |

**Invariantes a respetar (de CLAUDE.md):**

- `check_entry` NO se toca. El filtro de régimen es una capa C7 que evalúa DESPUÉS de check_entry (igual patrón que el cooldown F1 y las reglas KAIZEN F5.4).
- Stop solo SUBE, nunca baja (enforced en `recalc_stop_for_stage`).
- Universos disjuntos: MREV solo cripto, RFTM solo ETFs. No tocar `ETF_UNIVERSE`.
- `.env.paper` nunca se imprime ni se commitea.

---

## 2) PRE-WORK OBLIGATORIO (no saltearse, esto preserva los $11%)

Antes de tocar UNA SOLA LÍNEA de código de estrategia, ejecutar y validar:

### 2.1 Verificar estado actual del repo

```bash
cd ~/Desktop/trading-system
git status                                    # debe estar limpio
git log --oneline -5                          # confirmar que estás en main con 3537efc o posterior
git fetch && git status                       # confirmar que main local == origin/main
```

### 2.2 Correr toda la batería de tests, todos verdes

```bash
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
  _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py \
  _trade_logger.py _cooldowns.py _kaizen_missed.py _kaizen_enrichment.py \
  _kaizen_rules.py _kaizen_overrides.py _shadow_trades.py _kaizen_monthly_metrics.py \
  _watchdog_health.py _bracket_orders.py _exit_logic.py

python3 -m unittest tests.test_trade_logger tests.test_cooldowns tests.test_stop_recalc \
  tests.test_watchdog_health tests.test_bracket_orders tests.test_kaizen_enrichment \
  tests.test_kaizen_review tests.test_kaizen_rules tests.test_kaizen_overrides \
  tests.test_shadow_trades tests.test_kaizen_monthly_metrics

python3 -m pytest tests/test_indicators.py tests/test_strategy.py tests/test_health.py \
  tests/test_mrev tests/test_watchdog tests/test_exit_logic.py tests/test_db_health.py \
  tests/test_db_schema.py tests/test_universes_disjoint.py tests/test_mode_entry_only.py
```

**Si UN solo test falla → STOP. No avanzar.** Reportar al usuario y diagnosticar antes de seguir.

### 2.3 Sanity de producción (verificar que los bots viven)

```bash
# 1. Confirmar que GitHub Actions de MREV Watchdog y MREV Hourly están corriendo verde
#    (browser → github.com/carlosracca1-tech/trading-system/actions)

# 2. Confirmar Alpaca BP disponible
set -a; source .env.paper; set +a
curl -s "https://paper-api.alpaca.markets/v2/account" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f'Equity: \${float(d[\"equity\"]):,.2f} | BP: \${float(d[\"buying_power\"]):,.2f} | Status: {d[\"status\"]}')"

# 3. Confirmar que no hay órdenes huérfanas
curl -s "https://paper-api.alpaca.markets/v2/orders?status=open&limit=100" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f'Open orders: {len(d)}')"
```

### 2.4 Crear baseline tag y branch

```bash
# Tag del estado actual estable
git tag -a v1-stable-pre-v2 -m "Baseline pre-V2 changes — last known-good state"
git push origin v1-stable-pre-v2

# Branch de trabajo (no tocar main directo hasta validar todo)
git checkout -b feature/v2-improvements
```

### 2.5 Snapshot de performance baseline

Antes de cualquier cambio, capturar la métrica que querés mejorar:

```bash
# Equity, posiciones, PnL realizado de los últimos 30 días
set -a; source .env.paper; set +a
curl -s "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=30D&timeframe=1D" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  > baseline_v1_$(date +%Y%m%d).json

# Guardalo en el repo — referencia para comparar post-V2
git add baseline_v1_*.json
git commit -m "chore: baseline performance snapshot pre-V2"
```

---

## 3) Cambio 1 — Migración de 1H a 4H

### 3.1 Justificación (research-backed)

> "As you cut short the timeframes, the charts become more polluted with false moves and noise. Most consistently profitable traders use the daily and 4-hour charts to find clean levels."

Los backtests fuertes que la literatura cita (Quantified Strategies: 50% CAGR Bitcoin con BB; profit factor 1.59 ETHUSD; QuantPedia 2024) son todos en timeframe **diario**. 4H es el sweet spot para mean reversion en cripto: suficientes señales pero ya sin el ruido microestructural del 1H.

### 3.2 Archivos a modificar

| Archivo | Qué cambiar |
|---|---|
| `.github/workflows/mrev_hourly.yml` | Renombrar a `mrev_4h.yml` + cambiar cron `5 * * * *` → `5 1,5,9,13,17,21 * * *` (cada 4h, offset para que esté después del cierre de la vela) |
| `standalone_mrev_trader.py` | Fetch bars: `timeframe=1Hour` → `timeframe=4Hour`. Bars a pedir: 250 sigue alcanzando (~42 días de 4H). Headers de logs y emails: "1H" → "4H" |
| `mrev_watchdog.py` | `fetch_crypto_atr`: cambiar `timeframe=1Hour` → `timeframe=4Hour` para que el ATR usado por evaluaciones esté alineado. Loop del watchdog mantener cada 90s (es price-based, no candle-based) |
| `_email_helpers.py` (si tiene refs a "1H") | Actualizar wording |
| `tests/test_mrev/test_indicators_1h.py` | Renombrar a `test_indicators_4h.py` + actualizar fixtures de bars 1H → 4H |
| `tests/test_mrev/test_pipeline_mrev.py` | Adaptar setups que asumen 1H |
| `CLAUDE.md` | Actualizar referencia "Horario" → "Cada 4h" en la sección de arquitectura |

### 3.3 Detalle del cambio en cron

El cron `5 1,5,9,13,17,21 * * *` corre a los minutos 5 de las horas 01, 05, 09, 13, 17, 21 UTC. La vela 4H cierra en 00, 04, 08, 12, 16, 20 UTC — el offset de 1h05 garantiza que la vela esté cerrada cuando el bot decide.

**ALERT:** GitHub Actions cron tiene drift de hasta 30 min. Si el bot corre muy tardío, podría agarrar la vela del próximo intervalo. Mitigación: en el código, antes de calcular indicadores, validar `(now_utc - last_bar_close) < timedelta(hours=2)` y abortar si no.

### 3.4 Cosas a NO tocar

- `MREV_RISK_PER_TRADE`, `MREV_MAX_POSITIONS`, `MREV_TP1_PCT`, `MREV_TP2_PCT`, `FINAL_TP_PCT`, `MREV_PARTIAL_MIN_NOTIONAL_USD` — la economía del trade no cambia.
- `check_entry` y `check_exit` — la lógica de señal sigue igual, solo cambia la frecuencia de los datos de input.
- `migrate_legacy_etf_positions`, `sync_with_alpaca` — quedaron blindadas en el fix de hoy.

### 3.5 Test de validación específico

```python
# Agregar en tests/test_mrev/
def test_4h_bars_fetched_correctly():
    """Confirmar que el fetch de Alpaca trae bars 4H."""
    # mock alpaca_request y assert que la URL incluye timeframe=4Hour
    ...

def test_indicators_on_4h_match_expected():
    """SMA20, BB(1.8σ), RSI14, ATR14 computados sobre 50 candles 4H sintéticas
    deben dar exactamente los mismos valores que la versión 1H aplicada a
    candles 1H sintéticas equivalentes (mismo close, distinto timeframe label)."""
    ...
```

---

## 4) Cambio 2 — Nuevo universo cripto con diversificación real

### 4.1 Justificación

Universo actual: `BTC, ETH, SOL, AVAX, DOGE, LINK` — correlación promedio entre sí > 0.75. Cuando BTC cae 7%, los 6 caen juntos. No hay diversificación real.

### 4.2 Cripto disponibles en Alpaca (validado 2026-05-24)

> AAVE, AVAX, BAT, BCH, BTC, CRV, DOGE, DOT, ETH, GRT, LINK, LTC, MKR, SHIB, SUSHI, UNI, USDC, USDT, XTZ, YFI

**Nota:** XRP **NO** está disponible en Alpaca (eso descarta una de las picks que la literatura citaba como descorrelada).

### 4.3 Nuevo universo propuesto

**Mantener:** `BTC, ETH, LINK`
**Quitar:** `SOL` (corr 0.72-0.78 con BTC/ETH, redundante), `AVAX` (mismo problema), `DOGE` (pura beta meme, edge negativo en research)
**Agregar:** `AAVE, MKR, UNI` (DeFi blue chips, ciclos diferentes a L1 puros)

| Símbolo | Sector | Por qué |
|---|---|---|
| BTC/USD | Anchor / Store of Value | Liquidez máxima, beta de referencia |
| ETH/USD | Smart contract L1 | Segunda liquidez, semi-independiente |
| LINK/USD | Oracle infrastructure | Beta diferente, ciclo determinado por adopción de protocolos |
| AAVE/USD | DeFi lending | Cycle DeFi distinto del cycle L1 |
| MKR/USD | DeFi stablecoin governance | Driver es DAI utilization, más independiente |
| UNI/USD | DeFi DEX | Driver es trading volume, más cíclico que L1 |

**Total: 6 nombres, ahora con representación real de 3 sectores distintos.**

### 4.4 Cosas a verificar antes de finalizar este universo

1. **Liquidez en Alpaca paper de AAVE/MKR/UNI.** Si el spread es mayor a 0.5%, mean reversion no funciona — el bot va a comerse el spread en cada round trip. Comando para chequear:
   ```bash
   curl -s "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols=AAVE/USD,MKR/USD,UNI/USD" \
     -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
     -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"
   ```
   Si bid/ask spread > 0.5%, considerar reemplazar con LTC, DOT, BCH (mayor liquidez típica).

2. **CRYPTO_MIN_QTY** para los nuevos símbolos. Está hardcoded en `standalone_mrev_trader.py`. Hay que agregar las entradas para AAVE, MKR, UNI. Buscar en Alpaca docs el `min_order_size` para cada uno.

3. **Correlación real con BTC en último año.** Validar antes de commitear con:
   ```bash
   # Pull de 90 días de candles diarias, compute correlación
   # Si AAVE/MKR/UNI tienen correlación > 0.8 con BTC, la "diversificación" es una ilusión
   ```

### 4.5 Archivos a modificar

| Archivo | Qué cambiar |
|---|---|
| `standalone_mrev_trader.py` | `CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "LINK/USD", "AAVE/USD", "MKR/USD", "UNI/USD"]` |
| | `CRYPTO_MIN_QTY` dict: agregar entries para AAVE, MKR, UNI |
| `migrate_legacy_etf_positions` (`crypto_roots` tuple) | Actualizar para incluir nuevos roots, dejar los viejos para que no cierre SOL/AVAX/DOGE legacy mal |
| Si hay posiciones abiertas de SOL/AVAX/DOGE en Alpaca actualmente | **NO cerrarlas automáticamente.** Esperar que el watchdog las cierre por TPs/stops naturales O liquidación manual del usuario antes del deploy |
| Tests que hardcodean símbolos viejos | Actualizar |

### 4.6 Gestión de transición (importante)

Si al momento del deploy hay posiciones abiertas en SOL/AVAX/DOGE:
- **Opción A (recomendada):** Liquidación manual desde Alpaca dashboard antes del push. Limpio.
- **Opción B:** Dejar que el watchdog las cierre por TPs/stops naturales. El watchdog ya las reconoce (están en `ALL_SYMBOLS` legacy). Posición se cierra sola eventualmente. NO se abren nuevas.
- **Opción C:** Forzar exit via script manual `scripts/ops/reconcile_position.py SOL/USD --apply` (etc).

---

## 5) Cambio 3 — Filtro de régimen (la mejora más importante)

### 5.1 Justificación (research-backed)

> "Mean reversion trades MUST be filtered by regime: only fade in choppy, non-trending market conditions. The biggest risk is fading a genuine trend."

> "ADX above 25 indicates a trending market... ADX below 20 indicates choppy/ranging markets where mean-reversion strategies work effectively."

> "Bollinger Band strategies combined with RSI filters have shown 71% win rates during ranging market conditions — but that performance applies specifically to trades taken with the trend filter active."

**Diferencia esperada:** mismo combo BB+RSI puede dar 35% win rate sin filtro vs 71% con filtro. Esto es probablemente el cambio que más impacto va a tener.

### 5.2 Diseño del filtro

**Doble check (igual patrón que F1 cooldowns):**

**Check 1 — BTC macro trend (proxy "el mercado cripto está colapsando"):**
- Pull de BTC 4H. Compute SMA(50) — eso son ~8 días de tendencia.
- Si `BTC_close < BTC_SMA50 × 0.95` (BTC más de 5% por debajo de su media móvil de 8 días) → **BLOQUEAR todas las entries**.
- Si `BTC_close > BTC_SMA50 × 1.10` (BTC más de 10% arriba — euforia, riesgo de mean reversion del propio BTC) → **opcional: bloquear o no, configurar via env**.

**Check 2 — ADX del símbolo individual:**
- Compute ADX(14) sobre 4H del símbolo a entrar.
- Si `ADX > 25` (símbolo en tendencia fuerte) → **BLOQUEAR esa entry específica**. Mean reversion contra trend es suicidio.
- Si `ADX < 20` (símbolo en rango) → **OK, condiciones ideales para mean reversion**.
- Si `20 ≤ ADX ≤ 25` (zona gris) → **permitir pero reducir size 50%**. (Opcional: arrancar simple sin esto y agregar después.)

### 5.3 Dónde meterlo en el código

**NO tocar `check_entry`** (invariante CLAUDE.md). Agregar como capa C7 (siguiendo el patrón existente del cooldown F1 y kaizen rules F5.4):

```python
# En standalone_mrev_trader.py, dentro del entry loop, DESPUÉS de check_entry,
# DESPUÉS del cooldown F1, DESPUÉS de kaizen rules F5.4, ANTES del sizing:

if should_enter:
    try:
        from _regime_filter import is_regime_favorable
        regime_ok, regime_reason = is_regime_favorable(
            symbol=sym,
            symbol_bars=all_data[sym],   # ya cargado
            btc_bars=all_data.get("BTC/USD"),  # garantizar que siempre se fetcha
        )
        if not regime_ok:
            info(f"SKIP {sym}: regime_filter_blocked ({regime_reason})")
            should_enter = False
            reason = f"regime_{regime_reason}"
    except Exception as _re:
        warn(f"regime filter eval failed (non-fatal, allowing entry): {_re}")
```

### 5.4 Nuevo módulo `_regime_filter.py`

Crear archivo nuevo con función pura y testeable:

```python
"""
_regime_filter — filtro de régimen para MREV (capa C7, post-2026-05-24).

Mean reversion solo tiene edge en mercados laterales. Este módulo evalúa
dos cosas antes de permitir un entry:

  1. BTC macro: si BTC 4H está más de X% debajo de su SMA50, todo
     fadeo es riesgoso (mercado en colapso, mean reversion no funciona).
  2. ADX símbolo: si el símbolo a entrar está en tendencia fuerte
     (ADX > umbral), la mean reversion va contra el flujo.

Feature flag: MREV_REGIME_FILTER_ENABLED (default 'true'). Permite
desactivar en emergencia sin redeploy.

Configurable via env:
  MREV_REGIME_BTC_DRAWDOWN_PCT   default 0.05  (5%)
  MREV_REGIME_BTC_EUPHORIA_PCT   default 0.10  (10%, 0 = disabled)
  MREV_REGIME_ADX_MAX             default 25
  MREV_REGIME_ADX_PERIOD          default 14
"""
from __future__ import annotations

import os
from typing import Optional
import pandas as pd
import numpy as np


def is_regime_filter_enabled() -> bool:
    return os.environ.get("MREV_REGIME_FILTER_ENABLED", "true").lower() in (
        "1", "true", "yes",
    )


def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """ADX clásico de Wilder. Devuelve el último valor."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    if adx.empty or pd.isna(adx.iloc[-1]):
        return float("nan")
    return float(adx.iloc[-1])


def is_regime_favorable(
    *,
    symbol: str,
    symbol_bars: pd.DataFrame,
    btc_bars: Optional[pd.DataFrame],
) -> tuple[bool, str]:
    """Devuelve (True, "ok") o (False, reason).

    reason es un string corto para logging y para el JSONL de KAIZEN.
    """
    if not is_regime_filter_enabled():
        return True, "disabled"

    # Check 1: BTC macro
    if btc_bars is not None and len(btc_bars) >= 50:
        btc_close = float(btc_bars["close"].iloc[-1])
        btc_sma50 = float(btc_bars["close"].rolling(50).mean().iloc[-1])

        drawdown_threshold = float(os.environ.get("MREV_REGIME_BTC_DRAWDOWN_PCT", "0.05"))
        if btc_close < btc_sma50 * (1.0 - drawdown_threshold):
            return False, f"btc_drawdown ({btc_close:.0f} < {btc_sma50 * (1.0 - drawdown_threshold):.0f})"

        euphoria_threshold = float(os.environ.get("MREV_REGIME_BTC_EUPHORIA_PCT", "0.10"))
        if euphoria_threshold > 0 and btc_close > btc_sma50 * (1.0 + euphoria_threshold):
            return False, f"btc_euphoria ({btc_close:.0f} > {btc_sma50 * (1.0 + euphoria_threshold):.0f})"

    # Check 2: ADX del símbolo
    adx_period = int(os.environ.get("MREV_REGIME_ADX_PERIOD", "14"))
    adx_max = float(os.environ.get("MREV_REGIME_ADX_MAX", "25"))

    if len(symbol_bars) >= adx_period * 2:
        adx_val = compute_adx(symbol_bars, period=adx_period)
        if not pd.isna(adx_val) and adx_val > adx_max:
            return False, f"adx_trending ({adx_val:.1f} > {adx_max})"

    return True, "ok"
```

### 5.5 Tests obligatorios

Crear `tests/test_regime_filter.py`:

```python
def test_regime_disabled_always_ok()
def test_btc_drawdown_5pct_blocks()
def test_btc_drawdown_4pct_allows()
def test_btc_euphoria_blocks_when_enabled()
def test_btc_euphoria_disabled_does_not_block()
def test_adx_above_25_blocks()
def test_adx_below_20_allows()
def test_adx_between_20_25_currently_allows()  # documenta comportamiento simple
def test_compute_adx_matches_reference_value()  # contra TradingView u otra ref
def test_missing_btc_bars_does_not_block()      # graceful degradation
```

### 5.6 Logging para KAIZEN

Cuando el filtro bloquea, hay que dejar un post-mortem en `logs/kaizen_missed_moves.jsonl` (ya existe la infra en `_kaizen_missed.py`). Esto permite ver qué trades NO se hicieron por el filtro y validar si el filtro está siendo demasiado restrictivo.

Patrón:
```python
if not regime_ok:
    try:
        from _kaizen_missed import log_missed_move
        log_missed_move(
            symbol=sym,
            reason=f"regime_{regime_reason}",
            close=float(row["close"]),
            rsi=rsi_val,
            extra={"filter": "regime_c7", "details": regime_reason},
        )
    except Exception:
        pass
```

---

## 6) Plan de rollout — orden y validación

**NO desplegar las 3 cosas juntas en un solo push.** Cada cambio en commit separado, validado en CI, observado al menos un día antes del siguiente.

### Día 1 — Filtro de régimen (el cambio más importante)

1. Branch `feature/v2-regime-filter` desde `feature/v2-improvements`.
2. Implementar `_regime_filter.py` + tests + integración.
3. Tests verdes localmente.
4. Push → CI verde → merge a `feature/v2-improvements` con flag `MREV_REGIME_FILTER_ENABLED=false` por DEFAULT.
5. Setear `MREV_REGIME_FILTER_ENABLED=true` en GitHub Secrets / env del workflow.
6. Observar 2-3 ciclos del hourly bot (todavía 1H en este punto). Validar logs: ¿bloquea entries cuando esperás? ¿permite cuando esperás?

### Día 2 — Universo nuevo (después de validar régimen)

1. Branch `feature/v2-new-universe`.
2. Liquidación manual de SOL/AVAX/DOGE en Alpaca (o esperar TPs).
3. Update `CRYPTO_SYMBOLS` y `CRYPTO_MIN_QTY`.
4. Tests verdes.
5. Push → observar al menos 1 ciclo donde se evalúen los nuevos símbolos.

### Día 3 — Migración a 4H

1. Branch `feature/v2-4h-timeframe`.
2. Cambios en workflow + código + tests.
3. **IMPORTANTE:** Crear el nuevo workflow `mrev_4h.yml` PRIMERO, validar que corre, recién después borrar `mrev_hourly.yml`. Cero downtime.
4. Push.
5. Observar 24h. La frecuencia es menor (6 runs/día vs 24) — esperás 4x menos trades.

### Día 7 — Validación de performance

Ejecutar:
```bash
curl -s "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=7D&timeframe=1H" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  > performance_v2_day7.json
```

Comparar con `baseline_v1_*.json`. Métricas a mirar:
- Equity curve: ¿más suave o más errático?
- Drawdown máximo intra-período
- Trades count (debería bajar ~4x con 4H + filtro)
- Win rate de trades cerrados
- PnL realizado total

Si la equity curve está peor que baseline en una semana entera → rollback al tag `v1-stable-pre-v2`. **No te enamores del cambio, la data manda.**

---

## 7) Casos borde y riesgos identificados

| Riesgo | Mitigación |
|---|---|
| **Filtro de régimen demasiado restrictivo → bot no opera nunca** | Empezar con thresholds laxos (BTC drawdown 7%, ADX max 30). Apretar gradualmente con data. |
| **AAVE/MKR/UNI con liquidez insuficiente en Alpaca paper** | Check de spread pre-deploy (ver 4.4). Si falla, fallback a LTC/DOT/BCH. |
| **4H reduce trades por debajo del mínimo estadístico significativo** | Aceptable trade-off. Backtests serios necesitan 100+ trades para conclusiones; con 6 cripto × 6 evaluaciones/día = 36 oportunidades/día máx, en 1 mes son 1000+ evaluaciones. Suficiente. |
| **BTC trend filter bloquea entradas justo cuando deberían dispararse (señal en el bottom)** | Esto es by design: la literatura es clara que mean reversion en falling knives pierde. Si el usuario quiere atrapar bottoms tiene que usar OTRA estrategia (acumulación / DCA). |
| **Watchdog continuo sigue iterando cada 90s pero ahora con 4H data** | Cero problema. El watchdog usa current_price para evaluar stops/TPs, no candles. Solo el `fetch_crypto_atr` cambia a 4H para que el cálculo de stop esté en escala consistente. |
| **Tests con fixtures 1H rompen** | Renombrar y adaptar. Mantener algunos como regression para confirmar que los cálculos básicos no cambiaron, solo la fuente de datos. |

---

## 8) Lo que explícitamente NO está en este plan

Cosas que quedaron en el tintero pero NO son parte de esta sesión:

- **Arreglar el bug del bot hourly `get_open_positions(conn, run_id)` línea 1583** — el bot mismo filtra por su run_id. Bajo prioridad: Alpaca BP actúa como safety net. Si querés cerrar también esa, es un fix de 5 líneas pero requiere actualizar cálculos de cash. Próxima iteración.
- **Arreglar el `[sheets] FAIL auth: JSONDecodeError`** que aparece en cada run. El service account JSON está malformateado en los secrets. No afecta trading, solo el espejo en Sheets. Bajo prioridad.
- **RFTM (bot de ETFs)** — merece su propio análisis estratégico igual de profundo. Trend following es un beast diferente. Próxima sesión.
- **Position sizing dinámico por volatilidad** — sería el siguiente nivel después de los cambios actuales. Asignar más capital a setups con mejor R:R según ATR del momento.

---

## 9) Sources del research que fundamenta este plan

- [Revisiting Trend-following and Mean-reversion Strategies in Bitcoin (QuantPedia, 2024)](https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/)
- [Bitcoin Bollinger Bands Trading Strategy Performance (Quantified Strategies)](https://www.quantifiedstrategies.com/bitcoin-bollinger-bands-trading-strategy-performance-backtest/)
- [Crypto Correlation Matrix — Live BTC/ETH/SOL (Sharpe Terminal)](https://www.sharpe.ai/learn/crypto-correlation-matrix)
- [Diversifying Crypto Portfolios with XRP and SOL (CME Group, 2025)](https://www.cmegroup.com/articles/2025/diversifying-crypto-portfolios-with-xrp-and-sol.html)
- [The Death of the Altseason: Why the 2025 Cycle Never Happened (Bitcoin News)](https://news.bitcoin.com/the-death-of-the-altseason-why-the-2025-cycle-never-happened/)
- [Market Regime: Trending vs Ranging vs Volatile (Trader's Second Brain)](https://traderssecondbrain.com/guides/market-regime-identification)
- [Mean Reversion Day Trading Strategy: Complete 2026 Guide (Tradewink)](https://www.tradewink.com/learn/mean-reversion-strategy)
- [Kelly Criterion + Risk Per Trade (BackTestBase)](https://www.backtestbase.com/education/how-much-risk-per-trade)
- [Best Time Frames for Crypto Trading (Finestel)](https://finestel.com/blog/best-time-frames-for-crypto-trading/)
- [Alpaca Supported Crypto Coins/Pairs](https://alpaca.markets/support/what-are-the-supported-coins-pairs)
- [Crypto Spot Trading Docs (Alpaca)](https://docs.alpaca.markets/docs/crypto-trading)
- [Dual-Regime Adaptive Trading System: RSI Mean-Reversion + Breakout (FMZ Quant)](https://medium.com/@FMZQuant/dual-regime-adaptive-trading-system-rsi-mean-reversion-and-breakout-combination-strategy-11621184e821)

---

## 10) Checklist final para la próxima sesión

Al arrancar la próxima sesión, agente nuevo o yo mismo, debe poder:

- [ ] Leer `CLAUDE.md` completo
- [ ] Leer este `PLAN_V2.md` completo
- [ ] Ejecutar todos los comandos de la sección 2 (PRE-WORK) y validar verde
- [ ] Confirmar al usuario el plan antes de tocar código (no asumir consenso)
- [ ] Implementar Cambio 1 (régimen) → push → observar
- [ ] Implementar Cambio 2 (universo) → push → observar
- [ ] Implementar Cambio 3 (4H) → push → observar
- [ ] Día 7: comparar performance vs baseline. Decidir si quedarse o rollback.

**Cualquier desvío del plan → preguntar al usuario primero.** Este sistema ya generó +11% en 2 meses con la versión anterior; cualquier cambio que rompa lo que funciona es regresión, no progreso.
