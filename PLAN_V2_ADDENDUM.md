# PLAN_V2 — Addendum (enmiendas + reset de baseline)

**Fecha:** sesión Cowork 2026-05-24
**Aplica sobre:** `PLAN_V2.md`
**Estado:** las decisiones de este addendum SOBRESCRIBEN las del PLAN_V2 original donde hay conflicto.

---

## Por qué este addendum existe

El `PLAN_V2.md` fue escrito antes de promover el stack kaizen. En la sesión Cowork del 2026-05-24 revisamos el plan y acordamos 6 enmiendas (contradicciones internas + gates numéricos + criterios cuantitativos). Además, ahora que kaizen está productivo, el "baseline V1" cambió: ya no es `3537efc`, es el HEAD post-Chunk 8 del `PLAN_KAIZEN_PROMOTION.md`.

---

## Reset de baseline (PRE-WORK V2 actualizado)

`PLAN_V2.md` §2.4 hablaba de crear `v1-stable-pre-v2` apuntando al estado pre-kaizen. **Ya no aplica.** El baseline correcto ahora es:

```bash
cd ~/Desktop/trading-system
git fetch origin

# Confirmar que estás en HEAD post-Chunk 8 (CLAUDE.md descriptivo + todo kaizen pusheado)
git log --oneline -3
git status -sb   # esperado: "## main...origin/main", sin cambios

# Tag del baseline real
git tag -a v1-kaizen-pre-v2 -m "Baseline V2: kaizen productivo, pre-cambios estructurales MREV"
git push origin v1-kaizen-pre-v2
```

**Snapshot de performance baseline (post-kaizen):**

```bash
set -a; source .env.paper; set +a
curl -s "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=30D&timeframe=1D" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  > baseline_v1_kaizen_$(date +%Y%m%d).json

git add baseline_v1_kaizen_*.json
git commit -m "chore: baseline performance snapshot post-kaizen, pre-V2"
git push origin main
```

Este JSON es la referencia contra la que vamos a medir el éxito de V2.

---

## Las 6 enmiendas

### Enmienda 1 — Feature flag default y modo shadow

**`PLAN_V2.md` §5.4 vs §6 Día 1 paso 4** se contradicen sobre el default del feature flag. Resolución:

- **En código:** default `true` (`os.environ.get("MREV_REGIME_FILTER_ENABLED", "true")`).
- **En el primer merge del régimen:** setear `MREV_REGIME_FILTER_ENABLED=false` en el workflow env (`mrev_hourly.yml` o `mrev_4h.yml`) durante 24h. El filtro evalúa pero NO bloquea — modo shadow, vemos qué hubiera bloqueado sin afectar trades reales.
- **Después de 24h validando shadow:** flippeás a `true` cambiando solo el workflow env, sin redeploy de código.

Beneficio: pruebas el filtro en producción sin riesgo, y la activación es un commit de 1 línea reversible.

### Enmienda 2 — Thresholds laxos al inicio

`PLAN_V2.md` §5.2 dice "BTC drawdown 5%, ADX max 25". §7 mitigación dice "empezar laxo: 7% y 30". Usar **7% y 30**, NO 5%/25.

```bash
MREV_REGIME_BTC_DRAWDOWN_PCT=0.07
MREV_REGIME_ADX_MAX=30
```

Razón: 5% contra SMA50 de BTC es muy estricto — BTC pasa semanas enteras 5% bajo su media en consolidaciones normales, bloquearíamos casi todo. Apretamos sólo si la data del shadow muestra que el filtro a 7%/30 dejó pasar trades genuinamente malos.

### Enmienda 3 — Orden invertido

`PLAN_V2.md` propone: régimen → universo → 4H. **Invertir a: 4H → universo → régimen.**

Razones:
- **4H es el menos opinable:** solo cambia frecuencia de datos, no la lógica. Permite ver cuánto baja el flujo de trades en limpio, sin contaminar la señal con el filtro.
- **Universo después,** cuando ya tenés baseline 4H.
- **Régimen al final,** cuando podés medir su efecto incremental sobre los dos cambios previos. Si lo metés primero, te queda confundido qué mejoró: ¿el filtro o el menor ruido del 4H?

### Enmienda 4 — Universo cripto: gates numéricos y fallback

`PLAN_V2.md` §4.4 dice "si spread > 0.5% reemplazar" pero no define la cadena de fallback. Concretarlo:

| Métrica | Ideal | Tolerable | Reemplazar |
|---|---|---|---|
| Spread bid/ask | <0.30% | <0.50% | ≥0.50% |
| Correlación 90d vs BTC | <0.65 | <0.75 | ≥0.75 |

**Cadena de fallback en orden:**
- AAVE/USD → LTC/USD
- MKR/USD → BCH/USD
- UNI/USD → DOT/USD

**`CRYPTO_MIN_QTY` para los nuevos símbolos:** fetcheado de `/v2/assets/{symbol}` de Alpaca, NO estimado. Comando:

```bash
for sym in AAVE LTC MKR BCH UNI DOT; do
  curl -s "https://paper-api.alpaca.markets/v2/assets/${sym}USD" \
    -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
    -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f'{d[\"symbol\"]}: min_order_size={d.get(\"min_order_size\", \"N/A\")}, min_trade_increment={d.get(\"min_trade_increment\", \"N/A\")}')"
done
```

Si AAVE/MKR/UNI fallan alguno de los dos gates (spread o correlación), reemplazar UNO por su fallback antes de pushear. NO pushear sin chequear ambos gates.

### Enmienda 5 — Criterios Día 7 cuantitativos (no cualitativos)

`PLAN_V2.md` §6 Día 7 dice "equity curve más suave o más errático". Subjetivo, abre la puerta a confirmation bias. Reemplazar por:

- **Rollback inmediato** si `max_drawdown_7d_v2 > max_drawdown_7d_v1 × 1.30`.
- **Rollback** si `trades_count_7d < 5` → no hay data significativa; extender observación a 14 días antes de decidir.
- **Promoción** si `sharpe_7d_v2 >= sharpe_7d_v1 - 0.2` **Y** `pnl_realized_7d_v2 >= pnl_realized_7d_v1 × 0.7`.
- **Zona gris** (cualquier cosa intermedia): extender a 14 días, re-evaluar.

Estas métricas se calculan con:

```bash
curl -s "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=7D&timeframe=1H" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  > performance_v2_day7.json
```

Y procesando con un script de comparación contra `baseline_v1_kaizen_*.json`.

### Enmienda 6 — Mini-runbook de rollback

`PLAN_V2.md` menciona "rollback al tag" pero no especifica cómo manejar:

**(a) Posiciones abiertas que entraron con lógica V2:**
- **NO liquidar manualmente.** Dejarlas correr hasta TP cascade o stop natural. Liquidar manual mete ruido en las métricas y oculta si V2 era el problema o no.
- Excepción: si el bot está claramente roto (vendiendo mal, dejando posiciones desnudas, etc.), entonces sí: pausar workflows (cambiar `cron` a `# 0 0 30 2 *`), cerrar manual desde Alpaca dashboard, y recién después revertir.

**(b) Estado en `mrev_paper.db`:**
- Si V2 hizo `ALTER TABLE ADD COLUMN` (probable para el régimen logging), el revert de código NO revierte schema. Pero los `ADD COLUMN` son backward-compatible — el código viejo simplemente ignora las columnas nuevas. Sin acción requerida.
- Si V2 cambió tipos de columna o renombró tablas (no debería, pero verificar antes del revert), entonces hay que escribir migración inversa.

**(c) Workflows:**
- Si V2 modificó workflows (probable: cambio cron del MREV de 1h a 4h en chunk 4H), el revert los desactiva temporariamente.
- **Antes de hacer `git revert`** del commit de workflows, confirmá en GitHub Actions que ningún workflow está en ejecución. Si lo está, esperá a que termine (5-10 min) antes de revertir.

**Comando de rollback estándar:**

```bash
# Rollback de UN chunk específico de V2
git log --oneline -10                              # identificar el commit a revertir
git revert <commit-sha> --no-edit
git push origin main

# Rollback de TODO V2 (vuelta al baseline post-kaizen)
git revert v1-kaizen-pre-v2..HEAD --no-edit
git push origin main
```

---

## Plan de chunks V2 actualizado

Mantengo la estructura general del `PLAN_V2.md` original pero con el orden invertido (enmienda 3) y los criterios cuantitativos (enmienda 5). Cada chunk hace UN cambio, se observa, después el siguiente.

### Chunk V2-A — Migración 4H

**Por qué primero:** cambio menos opinable. Solo cambia frecuencia de datos.

**Archivos a modificar (resumido — el detalle está en `PLAN_V2.md` §3.2):**
- `standalone_mrev_trader.py`: `timeframe=1Hour` → `timeframe=4Hour`. Headers de logs "1H" → "4H".
- `mrev_watchdog.py`: `fetch_crypto_atr` → 4H. **Confirmado en sesión Cowork:** este cambio es cosmético hoy porque el ATR del watchdog es dead code post-fix (`check_exit` usa stop fijo). Donde sí importa el 4H ATR: filtro de entry `atr_14_pct ∈ [0.002, 0.15]` y TP dinámico SMA20+1.5×ATR del 25% remanente — ambos en el trader, no en el watchdog.
- Nuevo workflow `.github/workflows/mrev_4h.yml` (cron `5 1,5,9,13,17,21 * * *`), **crear primero**, validar 1 run verde, **recién después** borrar `mrev_hourly.yml`. Cero downtime.
- Tests: renombrar fixtures `test_indicators_1h.py` → `test_indicators_4h.py`. Adaptar `test_pipeline_mrev.py`.
- Agregar test nuevo: `(now_utc - last_bar_close) < timedelta(hours=2)` por GHA cron drift (ver `PLAN_V2.md` §3.3 ALERT).

**Observación post-push (48h):**
- 6 runs/día (no 24 como antes). En 48h esperás ~12 runs.
- Confirmar que la cantidad de bars fetched = 250 cubre ~42 días, no menos.
- Cero errores en logs por timeframe mismatch.

**Criterio rollback:** los criterios Día 7 de enmienda 5, evaluados a las 72h en lugar de 7 días (menos data acumulada, pero suficiente para detectar bug claro).

### Chunk V2-B — Nuevo universo cripto

**Por qué después de 4H:** ya tenés baseline 4H limpio, ahora cambiás composición.

**Pre-requisito obligatorio:** correr el script de gates de la enmienda 4 ANTES de modificar `CRYPTO_SYMBOLS`. Si AAVE/MKR/UNI fallan, usar la cadena de fallback.

**Archivos:**
- `standalone_mrev_trader.py`: `CRYPTO_SYMBOLS = [...]` (6 nombres definitivos), `CRYPTO_MIN_QTY` actualizado con los valores reales de Alpaca, `migrate_legacy_etf_positions` con tuple `crypto_roots` actualizada.
- Tests con símbolos hardcodeados: actualizar.

**Gestión de posiciones legacy (SOL/AVAX/DOGE):** opción B del `PLAN_V2.md` §4.6 (ya elegida en sesión Cowork) → dejar que el watchdog las cierre por TPs/stops naturales. No abrir nuevas porque ya no están en `CRYPTO_SYMBOLS`.

**Observación post-push (72h):** confirmar que los nuevos símbolos generan ≥1 evaluación cada uno. Si alguno no evalúa nunca, revisar el filtro de entry (probablemente `atr_14_pct` no entra al rango).

### Chunk V2-C — Filtro de régimen (capa C7)

**Por qué al final:** ahora podés medir su efecto incremental sobre 4H + universo nuevo.

**Archivos:**
- Crear `_regime_filter.py` (ver código completo en `PLAN_V2.md` §5.4 — los thresholds del snippet ahí están MAL según enmienda 2, usar 7% y 30 en el default).
- Integrar capa C7 en `standalone_mrev_trader.py` después de `check_entry`, después del cooldown F1, después de las KAIZEN rules F5.4, ANTES del sizing (ver patrón en `PLAN_V2.md` §5.3).
- Crear `tests/test_regime_filter.py` con los 10 tests del `PLAN_V2.md` §5.5.
- Agregar en `.env.example`:
  ```
  MREV_REGIME_FILTER_ENABLED=true
  MREV_REGIME_BTC_DRAWDOWN_PCT=0.07
  MREV_REGIME_BTC_EUPHORIA_PCT=0.10
  MREV_REGIME_ADX_MAX=30
  MREV_REGIME_ADX_PERIOD=14
  ```
- **En el workflow** (`mrev_4h.yml`): setear `MREV_REGIME_FILTER_ENABLED: 'false'` para el primer push (modo shadow). Documentar en el commit message que después de 24h se flippea a `true` mediante un commit de 1 línea.

**Observación post-push fase shadow (24h):**
- En los logs del bot debería aparecer `[REGIME SHADOW]` cuando el filtro hubiera bloqueado.
- Si en 24h ningún bloqueo aparece → filtro demasiado laxo, considerar bajar BTC drawdown a 5% (volver a la enmienda 2 con más data).
- Si todos los entries son bloqueados → filtro demasiado estricto, subir umbrales.
- Si el ratio bloqueado/permitido es 30-50% → arrancar.

**Activación post-shadow:** PR de 1 línea cambiando `false` a `true` en el workflow env. Push y observar 7 días con criterios cuantitativos de enmienda 5.

---

## Criterio global de éxito V2

A los 7 días post-activación del régimen (= post-Chunk V2-C):
- Aplicar enmienda 5 contra el baseline `baseline_v1_kaizen_*.json`.
- **Promoción:** mantener V2.
- **Rollback:** `git revert v1-kaizen-pre-v2..HEAD` y volver al baseline.
- **Zona gris:** extender 14 días más y re-evaluar.

---

**Última actualización:** 2026-05-24 fin de sesión Cowork.
