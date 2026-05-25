# Plan de mejoras RFTM + MREV — v2 (con feedback Charlie)

Documento de referencia para arrancar en próxima sesión.
Generado: 2026-05-15.

---

## F1 — Anti-whipsaw con post-mortem

**Bloqueo de re-entry (cooldown doble):**

- Tabla `rftm_cooldowns` espejo de `mrev_cooldowns`.
- Cooldown temporal: tras `E3_stop_loss` / `E5_*` / `E6_time_stop`, bloquear el símbolo N días hábiles. Env `RFTM_COOLDOWN_DAYS=5`.
- Cooldown de precio: aunque expire el temporal, no re-entrar si `entry_price > (1 + RFTM_REENTRY_MAX_RUNUP) × last_exit_price`. Env `RFTM_REENTRY_MAX_RUNUP=0.10` (10%).
- MREV: agregar el cooldown de precio (hoy solo tiene temporal).

**Post-mortem del rebote perdido (NUEVO — pedido Charlie):**

Cuando el cooldown de precio bloquea una entrada, generar análisis automático del símbolo:

- ¿Qué pasó entre el exit y el momento actual? (% de subida, días transcurridos, volumen relativo)
- Estado de indicadores hoy vs. estado en el último exit (RSI, ATR%, EMA21/50, distancia a 20d-high)
- ¿Hubo catalizador? (placeholder: chequear si volumen del día del rebote > 2× promedio — proxy de noticia)
- Log estructurado en `kaizen_missed_moves.jsonl` para que KAIZEN lo procese semanalmente.
- Email semanal opcional: "rebotes que perdimos esta semana y por qué".

Output: aprendemos qué subidas estamos dejando pasar y si valen la pena perseguir con otra estrategia.

---

## F2 — DB sincronizada bidireccional (sin pérdida de info)

**Mecanismo elegido**: branch dedicada `state/db` en el mismo repo.

- Workflow `daily_trade.yml` y los dos watchdogs, al final del job (incluso si parcialmente falló): `git commit` de `trading_paper.db` y `mrev_paper.db` a branch `state/db`, force-push con tag de timestamp.
- Pre-commit hace `.bak` del archivo anterior dentro de la misma branch (sufijo `-N` rotando 7 backups) — si algo se corrompe, recuperable.
- Local: `scripts/sync_db.sh` hace `git fetch origin state/db && git show origin/state/db:trading_paper.db > trading_paper.db` (idem MREV).
- Lock de seguridad: si la DB local fue modificada después del último pull (mtime), abortar el sync y avisar. Evita pisar cambios manuales.
- README + `make sync-db` para que sea un comando.

Garantías: GHA siempre es fuente de verdad; local solo lee; backups rotativos previenen pérdida; conflicto explícito.

---

## F3 — Blindaje de exits

### F3.1 Bracket orders en Alpaca

`alpaca_submit_order` para BUYs pasa a enviar `order_class=bracket` con `stop_loss.stop_price` y `take_profit.limit_price` calculados desde entry. Si el watchdog se cae, el broker ejecuta. Aplica solo a equity/ETF — Alpaca no soporta bracket en cripto, así que MREV sigue software-side (pero ver F3.2).

### F3.2 Watchdog healthcheck → email

Al final de cada run de watchdog:

- Si `evaluated < expected` (esperado = posiciones abiertas en Alpaca), warning.
- Si `assert_db_health` falló, error.
- Si una sell quedó `wait_for_fill` timeout y se canceló, info.
- Cualquier warning/error dispara email a Charlie.
- Métrica en `kaizen_health.jsonl`: timestamp, evaluated_count, errors, latency.

### F3.3 `sync_with_alpaca` recalcula stop_loss (respetando stage)

Hoy: cuando qty/entry cambia, no toca `stop_loss` → puede quedar obsoleto.
Cambio: recalcular según el **stage actual**, no por defecto a `entry × 0.95`.

| Stage | Stop loss correcto |
|---|---|
| 0 (sin parciales) | `entry − ATR_MULT × ATR14` |
| 1 (post-TP1) | `entry` (breakeven) — NO se baja nunca |
| 2 (post-TP2) | trailing activo, no se recalcula desde entry |

Si el stage no se puede determinar (ej. posición huérfana), default a stage 0 con stop ATR-based. Nunca usar el 5% genérico que hay hoy.

Regla invariante a respetar siempre: **stop_loss solo sube, nunca baja.** `new_stop = max(old_stop, calculated_stop)`.

---

## F4 — Riesgo (NO TOCAR)

Decisión Charlie: mantener `RISK_PCT=0.05`, `MAX_POS_PCT=0.25`, `ATR_MULT=1.5`. La mejora del comportamiento viene por F1+F3+F5, no por bajar tamaño.

KAIZEN sí puede proponer ajustes paramétricos si encuentra evidencia fuerte (ver F5).

---

## F5 — KAIZEN con auto-aplicación selectiva

### F5.0 PREREQ — Arreglar Google Sheets logging

Charlie reporta que no está logueando nada. Antes de armar KAIZEN, debug:

- `_sheets_logger.py`: verificar que `SHEETS_WEBHOOK_URL` esté en `.env.paper` y se levante en GHA secrets.
- Probar webhook con un payload manual.
- Si el webhook funciona pero el bot no logea: chequear excepciones tragadas en los `try/except` que envuelven `log_trade_event`.
- Si está completamente desconectado, fallback a logging local en JSONL hasta que se restaure.

Sin este step KAIZEN no tiene data para aprender.

### F5.1 Logging enriquecido por trade

Cada evento (BUY, SELL_TP1/2, SELL_STOP, SELL_TRAIL, SELL_TIME, SELL_FINAL_TP) registra:

- Indicadores en el momento: RSI, ATR%, vol_ratio, distancia a EMA21/50, %breakout sobre 20d-high
- Régimen de mercado: SPY trend (above/below SMA200), VIX nivel, sector relativo
- Tiempo en posición, P&L realizado, slippage vs precio target

### F5.2 Job semanal `kaizen_review.py`

Cron domingo noche. Toma trades de últimos 30 días, llama a Claude API con prompt estructurado:

> Acá están los trades cerrados de la semana con contexto completo. Identificá patrones recurrentes en los losers vs winners. Para cada patrón devolveme: id, descripción, condición (expresión booleana sobre indicadores), n_trades, win_rate, expectancy, confidence (low/medium/high).

Output JSON → `kaizen_rules.json` versionado.

### F5.3 Auto-aplicación SELECTIVA (ajuste Charlie)

Cada regla tiene un campo `auto_apply` que se decide así:

- **Auto-aplicar (active: true desde el inicio)** si todas:
  - `n_trades >= 10` (muestra suficiente)
  - `loss_rate >= 0.80` o `win_rate >= 0.80` (señal fuerte, no ambigua)
  - `confidence == "high"` (Claude reporta certeza)
  - Regla replicable por código (no requiere data externa rara)

- **Proponer (active: false)** si:
  - `n_trades < 10`, o
  - rate entre 60-80% (señal pero no contundente), o
  - confidence medium/low

- **Descartar** si `n_trades < 5` o señal débil — ni log.

Cada regla auto-aplicada manda email a Charlie con:
- Rationale
- Trades que la sustentan
- Botón/comando para desactivar (`make kaizen-disable K003`)

### F5.4 Consumo de reglas en `check_entry`

Capa C6 al final de los filtros existentes:

```python
for rule in load_active_kaizen_rules():
    if rule.matches(row):
        return False, f"K_{rule.id}_blocked"
```

C1-C5 siguen siendo el código base (Charlie aprueba cambios). KAIZEN solo agrega filtros adicionales, nunca afloja los existentes.

### F5.5 Auto-aprendizaje también para exits

No solo entries — KAIZEN puede sugerir ajustes a parámetros de exit si ve patrones:

- "Trades con ATR% > 5% en entry tienen 3× más probabilidad de hit stop antes de TP1 → bajar size en esos"
- "Trades que llegaron a +3% pero no a +5% (TP1) reversaron 70% de las veces → considerar TP1 a +3%"

Mismas reglas de confidence — auto-aplica solo si es revelador.

---

## F6 — Reporte mensual KAIZEN con shadow tracking

### F6.1 Shadow trades (counterfactual)

Cada vez que una regla KAIZEN bloquea una entrada, se crea un **trade fantasma** en tabla `kaizen_shadow_trades`:

- `rule_id` que lo bloqueó
- `symbol`, `entry_price` = close del día del bloqueo (como si hubiéramos entrado ese día)
- `stop_loss`, `tp1`, `tp2` calculados con los mismos parámetros del bot real
- `entry_dt`, `qty_simulated` (con `RISK_PCT` y `MAX_POS_PCT` del momento)
- Estado: `running`

Un cron diario simula la cascada completa contra precios reales de Alpaca:

- TP1 (+5%) → vende 50% simulado, sube stop a breakeven
- TP2 (+7.5%) → vende otro 25% simulado
- Stop / trailing / time stop (20 días max) → cierra remanente
- **Slippage simulado**: 0.05% en entry y exit (no inflar ahorros artificiales)

Al cerrar, calcula `pnl_simulated`. Si negativo → la regla nos ahorró plata. Si positivo → costo de oportunidad.

Mismo mecanismo para tuning de exits: cuando KAIZEN ajusta un parámetro (ej. TP1 5% → 3%), se mantiene un shadow con el parámetro viejo durante **3 meses**, comparando cada trade real con su versión shadow.

### F6.2 Email mensual (día 1 de cada mes)

Workflow nuevo `.github/workflows/kaizen_monthly.yml`, cron `0 12 1 * *`. Genera email HTML con:

- **Resumen ejecutivo**: net impact KAIZEN del mes (ahorro − costo de oportunidad) en USD
- **Equity curve** del mes con líneas verticales marcando activación de cada regla nueva
- **Por regla activa**: card con nombre, fecha de activación, trades bloqueados, fantasmas ganadores vs perdedores, net impact, win rate del fantasma
- **Top 3 reglas más rentables** del mes
- **Top 3 reglas problemáticas** (net impact negativo) — con sugerencia de desactivar
- **Reglas nuevas auto-aplicadas** ese mes con rationale
- **Reglas propuestas pendientes** de tu aprobación (con links de aprobación — ver F6.4)
- **Comparativa de métricas**: hit rate, expectancy, Sharpe — mes actual vs promedio últimos 3 meses
- **Insights de Claude**: resumen narrativo en 3-4 párrafos
- Versión markdown versionada en `kaizen_reports/YYYY-MM.md` para histórico

### F6.3 Auto-desactivación NO automática

Si una regla acumula net impact negativo durante 2 meses consecutivos y `n_blocks >= 10`:

- NO se desactiva sola.
- Email del mensual la marca con badge **"REQUIERE TU DECISIÓN"** en rojo.
- Subject del email lleva prefix `[ACCIÓN REQUERIDA]` si hay reglas en este estado.

### F6.4 Mecanismo de aprobación/rechazo por email

Cada regla pendiente (propuesta nueva o candidata a desactivar) tiene en el email dos botones grandes:

- **APROBAR K003** → link a `https://github.com/<user>/trading-system/actions/workflows/kaizen_decision.yml` con inputs prefillados: `rule_id=K003`, `decision=approve`
- **RECHAZAR K003** → mismo workflow con `decision=reject`

Workflow `kaizen_decision.yml`:

1. Recibe `rule_id` + `decision` vía `workflow_dispatch`.
2. Lee `kaizen_rules.json`, busca la regla, actualiza su `active` a true/false.
3. Si `decision=approve` y la regla era propuesta nueva: registra timestamp de activación.
4. Si `decision=reject`: marca `active=false` + `dismissed_dt` para que KAIZEN no la vuelva a proponer en 90 días.
5. Commit a main + mensaje "kaizen: approved K003 by Charlie via email-link".
6. Email de confirmación: "Listo, K003 está activa desde ahora".

Flujo Charlie:
- Abre email → click "APROBAR K003" → se abre GitHub en el navegador → click "Run workflow" → listo.
- Dos clicks reales, sin tocar código ni terminal.

Mobile-friendly: los botones funcionan desde el celular.

### F6.5 Métricas trackeadas

Tabla `kaizen_monthly_metrics` (una row por mes por regla):

- `month`, `rule_id`
- `n_blocks` (cuántas entradas bloqueó esta regla este mes)
- `n_shadows_closed`, `n_shadows_winners`, `n_shadows_losers`
- `gross_saved_usd`, `gross_missed_usd`, `net_impact_usd`
- `shadow_win_rate`, `shadow_expectancy`

Permite ver evolución regla por regla a lo largo de los meses.

---

## Orden de ejecución para próxima sesión

1. **F5.0** primero (arreglar Sheets logging — sin esto, KAIZEN no arranca nunca).
2. **F1** (anti-whipsaw — protege el capital ya).
3. **F2** (DB sync — visibilidad para Charlie).
4. **F3.3** + **F3.2** (recalc stop loss respetando stages + healthcheck emails).
5. **F3.1** (bracket orders — más invasivo, requiere testing cuidadoso).
6. **F5.1 → F5.5** (capa KAIZEN base, en orden).
7. **F6** (shadow tracking + reporte mensual + mecanismo de aprobación).

Steps 1-4 estimo 3-4 días. Step 5 (bracket orders) 1-2 días con testing. F5 base ~1 semana. F6 ~3-4 días encima de F5. Total: ~3 semanas si vamos en serie.

---

## Invariantes que NO se tocan sin preguntar

- `check_entry`, `check_exit`, `size_position`, `_calc_take_profit` — cambios requieren OK de Charlie (CLAUDE.md).
- `ETF_UNIVERSE`, `CRYPTO_SYMBOLS`, `ALL_SYMBOLS` — universos disjuntos, no mezclar.
- `RISK_PCT`, `MAX_POS_PCT`, `ATR_MULT` — Charlie quiere mantener riesgo agresivo.
- `partial_tp_taken` es stage counter (0/1/2), no booleano — respetar cascada.
- Stop loss solo sube, nunca baja.

---

## Métricas de éxito (a medir post-implementación)

- Reducción de whipsaws: trades stop-out seguidos de re-entry > +10% en 30 días → debería tender a 0.
- Cobertura watchdog: % de runs sin errores > 99%.
- Cobertura Sheets logging: 100% de eventos llegan al sheet.
- KAIZEN: al menos 1 regla auto-aplicada con n>=10 en primer mes.
- Sharpe / hit rate / expectancy mes a mes vs. baseline pre-cambios.
