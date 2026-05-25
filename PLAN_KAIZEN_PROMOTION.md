# Plan de promoción del stack kaizen — local → origin/main

**Fecha:** 2026-05-24
**Autor:** Sesión de auditoría post-fix de micro-pérdidas
**Base:** `3537efc` (HEAD = origin/main)
**Branch de trabajo:** `main` directo (no se crea feature branch — los chunks son pequeños y reversibles vía `git revert`)

---

## Por qué este plan existe

Tu working tree local tiene 18 archivos modificados + 57 untracked que `CLAUDE.md` describe como productivos pero **no están en `origin/main`**. La performance histórica de +11% en 2 meses se generó SIN ese stack. Antes de tocar V2 (4H + universo + filtro de régimen) hay que llevar lo que ya existe al estado descrito.

Este plan promueve el stack en chunks chicos, cada uno con tests, observación y criterio de rollback. **No hace cambios estratégicos nuevos** salvo dos espejos del fix MREV de hoy (`4409888`/`3537efc`): el equivalente RFTM del simplified `check_exit`.

---

## Estado actual (snapshot)

- **Sincronización git:** local en `3537efc`, igual a `origin/main`. Cero divergencia.
- **Cambios en disco:** 18 modificados + 57 untracked.
- **Bots productivos:** corren la versión vieja desde GHA (`origin/main`). Sin kaizen, sin cooldowns, sin watchdog health, sin trade_logger.
- **Bug fix de hoy:** ya pusheado, funcionando (caso AVAX 14:42/14:43 = primer trade con stop fijo correcto).
- **Tests sin commitear:** 4 modificados (actualizan assertions del TP2 stop-raise) + 12 nuevos (cubren los 10 módulos kaizen).

---

## Decisiones tomadas en la sesión 2026-05-24

1. **Refactor RFTM ("fix 2026-05-21"):** se aplica completo, igual que la versión MREV de hoy. Elimina E5 trailing, E6 time stop, E7 take-profit. Quedan E3 (hard stop) + cascade TPs + final TP +10%.
2. **F2 state/db push:** se activa en el chunk normal de workflows. Push de DB+JSONL a una branch dedicada, con artifact upload como fallback.

---

## Ritual de seguridad antes de CADA chunk

```bash
cd ~/Desktop/trading-system

# 1. Confirmar repo sincronizado
git fetch origin
git status -sb  # esperar "## main...origin/main" sin "ahead/behind"

# 2. Confirmar lock file no existe
rm -f .git/index.lock 2>/dev/null
```

Si el `git status` muestra `ahead` o `behind`, parar y revisar.

---

## Chunk 0 — Higiene + documentación

**Por qué:** cambios sin impacto en bots. Limpiar el ruido del `git status` para que los chunks siguientes muestren solo cambios reales.

**Archivos:**
- `.gitignore` (2 líneas nuevas: `.state_db_last_sync`, `*.local-bak`)
- `Makefile` (targets `sync-db` y `sync-db-force` — solo definiciones, los scripts vienen en Chunk 3)
- `.env.example` (documentación de todas las env vars nuevas: F1, F3.1, F3.2, F5.0, sheets service account)
- `RUNBOOK.md` (modificado — solo docs)
- `scripts/sheets/SETUP.md` (modificado — setup del service account, sin código)
- `scripts/STATE_DB.md` (nuevo — docs)
- `PLAN_V2.md` (este es el plan V2 original; documentación)
- `PLAN_MEJORAS_KAIZEN_v2.md` (documentación)
- `PLAN_MIGRACION_LIVE.md` (documentación)
- `PLAN_OPTIMIZACION_EMAILS.md` (documentación)
- `AUDITORIA_PRE_GOLIVE.md` (documentación)
- `PROMPT_AUDITORIA_GO_LIVE.md` (documentación)
- `PROMPT_FASE_1_VISIBILIDAD.md` (documentación)
- `PROMPT_FIX_VENTAS_Y_EMAILS.md` (documentación)
- `PROMPT_WATCHDOGS_Y_FIXES.md` (documentación)
- `PROMPT_fix_mrev_watchdog_y_emails.md` (documentación)
- `PROMPT_fix_terminal_escape_noise.md` (documentación)
- `Trading_Bots_Analisis_Exhaustivo.docx` (binario)
- `Plan_Agresivo_8_10_Trading_Bots.docx` (binario)
- `PLAN_KAIZEN_PROMOTION.md` (este mismo archivo)

**NO incluir:**
- `CLAUDE.md` — va al final (Chunk 8), cuando lo que describe efectivamente existe.
- `.DS_Store` — basura de macOS, agregar a `.gitignore` si todavía no está y NUNCA commitear.

**Tests pre-commit:**
```bash
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py rftm_watchdog.py mrev_watchdog.py
# Sanidad de que el código en disco compila — los .md no afectan, esto es para descartar accidentes.
```

**Commit + push:**
```bash
# Agregar archivos uno por uno o por glob (NO usar `git add .` por las dudas)
git add .gitignore Makefile .env.example RUNBOOK.md scripts/sheets/SETUP.md
git add scripts/STATE_DB.md
git add PLAN_V2.md PLAN_MEJORAS_KAIZEN_v2.md PLAN_MIGRACION_LIVE.md
git add PLAN_OPTIMIZACION_EMAILS.md PLAN_KAIZEN_PROMOTION.md
git add AUDITORIA_PRE_GOLIVE.md
git add PROMPT_*.md
git add Trading_Bots_Analisis_Exhaustivo.docx Plan_Agresivo_8_10_Trading_Bots.docx

git status -sb  # verificar que solo eso está staged

git commit -m "chore: higiene + documentación pendiente sin impacto en bots

- .gitignore: agrega .state_db_last_sync y *.local-bak (F2 sentinels)
- Makefile: targets sync-db (los scripts vienen en Chunk 3)
- .env.example: documenta env vars F1/F3.1/F3.2/F5.0/sheets
- Adds: PLAN_V2, PLAN_KAIZEN_PROMOTION, PLAN_MEJORAS_KAIZEN_v2,
  PLAN_MIGRACION_LIVE, AUDITORIA_PRE_GOLIVE, PROMPTs históricos,
  scripts/STATE_DB.md (F2 docs), 2 .docx de análisis

Sin impacto operativo. CLAUDE.md se posterga al Chunk 8."

git push origin main
```

**Observación post-push:** ninguna. Es documentación. Solo confirmar que el siguiente `git fetch && git status` muestra cero divergencia.

**Rollback:** `git revert HEAD --no-edit && git push`.

---

## Chunk 1 — Módulos kaizen inertes + tests

**Por qué:** dejar los 10 módulos kaizen disponibles en el repo sin wirearlos todavía. Los bots productivos NO los importan en este punto (la importación viene en Chunk 2). Tests verdes en CI confirman que los módulos están sanos.

**Archivos (módulos, 10):**
- `_trade_logger.py` (250L)
- `_cooldowns.py` (268L)
- `_kaizen_missed.py` (115L)
- `_kaizen_enrichment.py` (339L)
- `_kaizen_rules.py` (171L)
- `_kaizen_overrides.py` (141L)
- `_shadow_trades.py` (366L)
- `_kaizen_monthly_metrics.py` (188L)
- `_watchdog_health.py` (257L)
- `_bracket_orders.py` (174L)

**Archivos (tests, 12):**
- `tests/test_trade_logger.py`
- `tests/test_cooldowns.py`
- `tests/test_kaizen_enrichment.py`
- `tests/test_kaizen_rules.py`
- `tests/test_kaizen_overrides.py`
- `tests/test_shadow_trades.py`
- `tests/test_kaizen_monthly_metrics.py`
- `tests/test_watchdog_health.py`
- `tests/test_bracket_orders.py`
- `tests/test_kaizen_review.py`
- `tests/test_stop_recalc.py`
- `tests/test_simple_exit_fix.py`

**Tests pre-commit:**
```bash
# Todos los módulos kaizen compilan
python3 -m py_compile _trade_logger.py _cooldowns.py _kaizen_missed.py \
  _kaizen_enrichment.py _kaizen_rules.py _kaizen_overrides.py \
  _shadow_trades.py _kaizen_monthly_metrics.py _watchdog_health.py \
  _bracket_orders.py

# La batería completa que CLAUDE.md describe (esperado 141/141)
python3 -m unittest tests.test_trade_logger tests.test_cooldowns \
  tests.test_stop_recalc tests.test_watchdog_health tests.test_bracket_orders \
  tests.test_kaizen_enrichment tests.test_kaizen_review tests.test_kaizen_rules \
  tests.test_kaizen_overrides tests.test_shadow_trades tests.test_kaizen_monthly_metrics

# Verificar que el bot RFTM productivo todavía compila (no debería romperse, los imports nuevos no existen aún en producción)
python3 -m py_compile standalone_paper_trader.py rftm_watchdog.py
```

Si alguno falla → STOP, revisar. No avanzar.

**Commit + push:**
```bash
git add _trade_logger.py _cooldowns.py _kaizen_missed.py \
  _kaizen_enrichment.py _kaizen_rules.py _kaizen_overrides.py \
  _shadow_trades.py _kaizen_monthly_metrics.py _watchdog_health.py \
  _bracket_orders.py

git add tests/test_trade_logger.py tests/test_cooldowns.py \
  tests/test_kaizen_enrichment.py tests/test_kaizen_rules.py \
  tests/test_kaizen_overrides.py tests/test_shadow_trades.py \
  tests/test_kaizen_monthly_metrics.py tests/test_watchdog_health.py \
  tests/test_bracket_orders.py tests/test_kaizen_review.py \
  tests/test_stop_recalc.py tests/test_simple_exit_fix.py

git status -sb  # verificar exactamente esos 22 archivos staged

git commit -m "feat(kaizen): land all kaizen modules + tests inert

Modules (10):
  _trade_logger      F5.0  JSONL + Sheets best-effort wrapper
  _cooldowns         F1    cooldown table + decision logic
  _kaizen_missed     F1    post-mortem JSONL for blocked moves
  _kaizen_enrichment F5.1  enriquece eventos con indicadores+régimen
  _kaizen_rules      F5.3  load/match/auto-activate rules
  _kaizen_overrides  F5.5  param overrides (manual approval)
  _shadow_trades     F6.1  simulación de trades bloqueados
  _kaizen_monthly_metrics F6.5  snapshot mensual de métricas
  _watchdog_health   F3.2  HealthReport + email + JSONL
  _bracket_orders    F3.1  safety SELL STOP (feature flag, default off)

Tests (12): cubren los 10 módulos + stop_recalc + simple_exit_fix.

Nada de esto está wired al código productivo en este commit — los
módulos quedan disponibles para que el Chunk 2 los importe. Si las
funciones se importan desde un módulo que no existe (este commit
deja todos), pasan. Sin riesgo operativo."

git push origin main
```

**Observación post-push:** confirmar en GitHub Actions que el próximo run del workflow `daily_trade` (RFTM entry, cron `35 13 * * 1-5`) y los watchdogs no rompen. **Los bots no deberían cambiar comportamiento porque nada en producción importa estos módulos todavía.**

**Rollback:** `git revert HEAD --no-edit && git push`. Bajo riesgo.

**Esperar antes de Chunk 2:** 1-2 horas. Confirmar 1 run verde del entry bot + 1 watchdog run.

---

## Chunk 2 — Producción: refactor RFTM + wiring kaizen

**Por qué:** este es el chunk más importante y más riesgoso. Aplica al RFTM el equivalente del fix MREV de hoy (simplified `check_exit` + size_position fixed 5% + cascade post-TP2 stop raise). EN EL MISMO COMMIT activa el wiring kaizen: cooldowns F1, capa C6 (KAIZEN rules), safety stops F3.1, trade_logger F5.0, enrichment F5.1, shadow trades F6.1, watchdog health F3.2.

Justificación de hacerlos juntos: el diff de `standalone_paper_trader.py` (316 líneas) y `rftm_watchdog.py` (~250 líneas) tiene estos dos cambios interleaved en los mismos bloques (ej. el buy block calcula stop fijo, envía safety stop, y loggea con _trade_logger — los tres juntos). Separarlos requiere edición quirúrgica del patch. Decidiste pushear igual que MREV → un commit.

**Archivos (productivo):**
- `standalone_paper_trader.py`
- `rftm_watchdog.py`

**Archivos (tests):**
- `tests/test_exit_logic.py` (test_tp2 assertion update)
- `tests/test_watchdog/test_mrev_watchdog.py` (test_tp2 assertion)
- `tests/test_watchdog/test_rftm_watchdog.py` (test_tp2 assertion)
- `tests/test_mrev/test_insert_enter.py` (anchor más robusto)

**Tests pre-commit:**
```bash
# Compila TODO el stack (incluye nuevos módulos del Chunk 1)
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
  _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py \
  _trade_logger.py _cooldowns.py _kaizen_missed.py _kaizen_enrichment.py \
  _kaizen_rules.py _kaizen_overrides.py _shadow_trades.py \
  _kaizen_monthly_metrics.py _watchdog_health.py _bracket_orders.py _exit_logic.py

# Unittest battery (141 esperado)
python3 -m unittest tests.test_trade_logger tests.test_cooldowns \
  tests.test_stop_recalc tests.test_watchdog_health tests.test_bracket_orders \
  tests.test_kaizen_enrichment tests.test_kaizen_review tests.test_kaizen_rules \
  tests.test_kaizen_overrides tests.test_shadow_trades tests.test_kaizen_monthly_metrics

# Pytest del resto (cobertura RFTM/MREV/watchdog/health/db)
python3 -m pytest tests/test_indicators.py tests/test_strategy.py \
  tests/test_health.py tests/test_mrev tests/test_watchdog \
  tests/test_exit_logic.py tests/test_db_health.py tests/test_db_schema.py \
  tests/test_universes_disjoint.py tests/test_mode_entry_only.py
```

**Si CUALQUIER test falla → STOP.** Este chunk modifica el bot productivo; tests rojos = no push.

**Commit + push:**
```bash
git add standalone_paper_trader.py rftm_watchdog.py
git add tests/test_exit_logic.py tests/test_watchdog/test_mrev_watchdog.py \
  tests/test_watchdog/test_rftm_watchdog.py tests/test_mrev/test_insert_enter.py

git status -sb  # confirmar SOLO esos 6 archivos staged

git commit -m "feat(rftm): mirror MREV fix + wire kaizen integration

Strategy refactor (mirror del fix MREV commits 4409888/3537efc):
  - check_exit: ahora solo E3 (hard stop). Removidos E5 trailing
    (3 fases), E6 time stop (20 bars), E7 take-profit (2:1 RR) por
    micro-pérdidas de slippage.
  - size_position: stop fijo entry × 0.95 (5%), sin ATR.
  - Cascade post-TPs: TP1 → stop=entry (breakeven), TP2 → stop=
    entry × (1+TP1_pct) = lock TP1. Mismo patrón que MREV.

Kaizen wiring:
  - F1 cooldowns (RFTM_COOLDOWN_DAYS=5, RFTM_REENTRY_MAX_RUNUP=0.10).
    Post-mortem JSONL via _kaizen_missed cuando cooldown de precio bloquea.
  - Capa C6 KAIZEN rules (F5.4) — filtro adicional post check_entry.
    Shadow trade creado vía _shadow_trades cuando una regla bloquea.
  - F3.1 safety stops broker-side (RFTM_BRACKET_ORDERS_ENABLED=0
    por default, OFF — se activa por env var).
  - F3.2 HealthReport del watchdog con email + JSONL.
  - F5.0 _trade_logger reemplaza _sheets_logger (drop-in, misma firma).
    Enrichment F5.1 vía build_enriched_extra en cada log_trade_event.

Tests actualizados: TP2 ahora sube stop a entry*(1+TP1) = 105 (era
sin cambio). Mismo cambio que MREV: ya en _exit_logic.py de origin/main.

NO se modifica check_entry. Las capas C6 y F1 son filtros ADICIONALES
después de check_entry — solo rechazan más cosas, nunca aflojan
(invariante CLAUDE.md #7).

Riesgo conocido: si los módulos del Chunk 1 no estuvieran en origin,
los imports fallarían y el código defaultea a 'allow entry, skip
kaizen feature' por los try/except defensivos. Pero Chunk 1 ya está."

git push origin main
```

**Observación post-push (CRÍTICA — 24-48h):**

Esperar a que corran:
1. **Entry RFTM** (cron `35 13 * * 1-5`) — próximo run a las 13:35 UTC weekday. Mirar logs en GHA:
   - `[init_db] cooldown setup` → debería estar OK (sin "failed").
   - `SKIP {symbol}: cooldown_*` → puede aparecer si hay un símbolo recién salido.
   - `SKIP {symbol}: K_*_blocked` → solo si hay reglas KAIZEN cargadas (improbable en este punto, kaizen_rules.json no existe aún).
   - `BOUGHT {qty} × {symbol} ... stop=$...` → confirmar que el stop es exactamente `entry × 0.95`.
   - `safety_stop {symbol} qty=... stop=$... id=...` → NO debería aparecer (feature flag off).
2. **RFTM watchdog** (cron pendiente, manual por ahora) — disparar manual con `workflow_dispatch`:
   - `RFTM Watchdog — ...` header normal.
   - `Watchdog evaluated N positions (expected N)` → mismo N de ambos lados.
   - Si hay TPs disparados: confirmar que el stop post-TP1 sube a `entry` y post-TP2 a `entry × 1.05`.
   - Buscar `safety_stop_order_id` en logs → debería decir "feature flag off" o no aparecer.

**Métricas a verificar a las 24h:**
- Equity ± normal vs baseline. Ningún drop > 2%.
- `trades` ejecutados similares en cantidad al promedio histórico.
- Cero errores `ERROR` en logs.
- JSONL `logs/trade_events_rftm.jsonl` está siendo escrito (lo verás en el artifact upload del Chunk 4, no aún).

**Rollback:** `git revert HEAD --no-edit && git push`. Bot vuelve a estrategia vieja y sin kaizen.

**Esperar antes de Chunk 3:** 24-48h. Mínimo 2-3 runs del entry bot + 5+ runs del watchdog si está habilitado, todos verdes.

---

## Chunk 3 — Scripts F2 (state/db sync)

**Por qué:** los scripts que el Chunk 4 (workflows) va a invocar. Sin ellos, los workflow steps `bash scripts/state_db_push.sh` van a fallar (pero el workflow tiene `continue-on-error: true`, así que no rompen nada). Aún así, los pusheamos en su propio chunk.

**Archivos:**
- `scripts/state_db_push.sh`
- `scripts/sync_db.sh`

**Tests pre-commit:**
```bash
# Sintaxis bash
bash -n scripts/state_db_push.sh
bash -n scripts/sync_db.sh

# Si tenés shellcheck instalado (no obligatorio):
# shellcheck scripts/state_db_push.sh scripts/sync_db.sh
```

**Commit + push:**
```bash
git add scripts/state_db_push.sh scripts/sync_db.sh

git commit -m "feat(f2): scripts de state/db sync entre CI y local

state_db_push.sh: pushea snapshots de DB + JSONL a la branch state/db.
Invocado al final de cada workflow (F2). Idempotente.
Backups rotativos .bak-1..7 preservan historial.

sync_db.sh: descarga state/db a local. Para que make sync-db (Chunk 0)
pueda traer el estado más reciente de CI a la Mac."

git push origin main
```

**Observación post-push:** ninguna en este chunk. Los scripts no se invocan hasta el Chunk 4.

**Rollback:** `git revert HEAD --no-edit && git push`.

**Esperar antes de Chunk 4:** 0 (puede ser inmediato).

---

## Chunk 4 — Workflows modificados (kaizen JSONL + F2 push)

**Por qué:** ahora que el código productivo escribe JSONL via `_trade_logger` (Chunk 2) y los scripts F2 existen (Chunk 3), activamos en los workflows: cache restore/save del JSONL, artifact upload como fallback, y push a `state/db`.

**Archivos:**
- `.github/workflows/daily_trade.yml`
- `.github/workflows/mrev_hourly.yml`
- `.github/workflows/mrev_watchdog.yml`
- `.github/workflows/rftm_watchdog.yml`

**Tests pre-commit:**
```bash
# Validar sintaxis YAML (si actionlint está instalado; sino el push fallará y vemos)
# brew install actionlint  # si querés tenerlo
# actionlint .github/workflows/daily_trade.yml .github/workflows/mrev_hourly.yml \
#   .github/workflows/mrev_watchdog.yml .github/workflows/rftm_watchdog.yml

# Sanity Python (no aplica)
```

**Commit + push:**
```bash
git add .github/workflows/daily_trade.yml .github/workflows/mrev_hourly.yml \
  .github/workflows/mrev_watchdog.yml .github/workflows/rftm_watchdog.yml

git commit -m "ci: agregar cache JSONL + artifact upload + F2 state/db push

Cuatro workflows (daily_trade, mrev_hourly, mrev_watchdog, rftm_watchdog):

- permissions: contents: write (necesario para state_db_push.sh).
- restore + save de logs/trade_events_{bot}.jsonl entre runs.
- Watchdogs además cache logs/kaizen_health.jsonl (F3.2).
- TRADE_EVENTS_JSONL_PATH apunta al JSONL por bot.
- upload-artifact (retention 7d) como fallback de visibilidad si
  state/db push falla.
- step final 'Push state to branch state/db' (continue-on-error)
  ejecuta scripts/state_db_push.sh.

Sin cambios en cron/triggers/horarios."

git push origin main
```

**Observación post-push (24h):**
1. Próximo run de cada workflow:
   - Step "Restore trade events JSONL" → OK (puede estar vacío la primera vez).
   - Step "Save trade events JSONL" → OK.
   - Step "Upload trade events JSONL + DB snapshot" → artifact aparece en la UI del workflow.
   - Step "Push state to branch state/db" → puede fallar la primera vez si la branch no existe; el script debería crearla.
2. En GitHub web → branches → confirmar que `state/db` aparece y tiene commits con DB + JSONL.
3. Verificar: `git fetch origin state/db && git log origin/state/db --oneline | head -5` desde local.

**Rollback:** `git revert HEAD --no-edit && git push`. Los workflows vuelven a no cachear JSONL ni pushear a state/db (mantienen el comportamiento previo).

**Esperar antes de Chunk 5:** 24h con runs de los 4 workflows ejecutados al menos una vez.

---

## Chunk 5 — Scripts kaizen + utilidades ops

**Por qué:** scripts que corren manualmente o vía workflows kaizen (Chunk 6). Sin caller en este chunk → bajo riesgo.

**Archivos (scripts kaizen):**
- `scripts/kaizen_review.py` (F5.2 — análisis semanal)
- `scripts/kaizen_decision.py` (F6.4 — approve/reject)
- `scripts/kaizen_decision_email.py` (F6.4 — email helpers)
- `scripts/kaizen_monthly_report.py` (F6.2 — snapshot+email mensual)
- `scripts/shadow_tick.py` (F6.1 — tick diario)
- `scripts/dump_trade_events.py` (utility)
- `scripts/audit_alpaca_orders.py` (utility audit)
- `scripts/audit_entry_snapshot.py` (utility audit)
- `scripts/audit/` (directorio, contenido a confirmar)
- `scripts/ops/restore_highest_since_entry.py` (utility ops)
- `check_alpaca_state.py` (diagnostic en root)

**Tests pre-commit:**
```bash
python3 -m py_compile scripts/kaizen_review.py scripts/kaizen_decision.py \
  scripts/kaizen_decision_email.py scripts/kaizen_monthly_report.py \
  scripts/shadow_tick.py scripts/dump_trade_events.py \
  scripts/audit_alpaca_orders.py scripts/audit_entry_snapshot.py \
  scripts/ops/restore_highest_since_entry.py check_alpaca_state.py

# Smoke test --help de cada script
python3 scripts/kaizen_review.py --help
python3 scripts/kaizen_decision.py --help 2>/dev/null || true
python3 scripts/shadow_tick.py --help
```

**Commit + push:**
```bash
git add scripts/kaizen_review.py scripts/kaizen_decision.py \
  scripts/kaizen_decision_email.py scripts/kaizen_monthly_report.py \
  scripts/shadow_tick.py scripts/dump_trade_events.py \
  scripts/audit_alpaca_orders.py scripts/audit_entry_snapshot.py \
  scripts/ops/restore_highest_since_entry.py check_alpaca_state.py

# Si scripts/audit/ tiene archivos, agregarlo:
[ -d scripts/audit ] && git add scripts/audit/

git commit -m "feat(scripts): kaizen pipeline scripts + ops utilities

Scripts kaizen (invocados por workflows del Chunk 6):
  kaizen_review.py        F5.2 — semanal, Claude detecta patrones
  kaizen_decision.py      F6.4 — apply approve/reject a rule/override
  kaizen_decision_email.py     helpers de email
  kaizen_monthly_report.py F6.2 — snapshot mensual + email
  shadow_tick.py          F6.1 — tick diario de shadows

Utilities ops:
  dump_trade_events.py        dump JSONL legible
  audit_alpaca_orders.py      forensics
  audit_entry_snapshot.py     forensics
  restore_highest_since_entry.py  recovery
  check_alpaca_state.py       diagnostic Alpaca vs DBs

Sin caller automático en este chunk — los workflows kaizen vienen en
el siguiente chunk."

git push origin main
```

**Observación post-push:** ninguna inmediata. Nada los invoca todavía.

**Rollback:** `git revert HEAD --no-edit && git push`.

**Esperar antes de Chunk 6:** 0 (inmediato si quisieras, pero recomiendo 24h).

---

## Chunk 6 — Workflows kaizen nuevos

**Por qué:** activar la pipeline kaizen completa. Cuatro workflows nuevos con sus propios cron.

**Archivos:**
- `.github/workflows/kaizen_weekly.yml` (cron domingos 23:00 UTC — F5.2)
- `.github/workflows/kaizen_monthly.yml` (cron día 1 12:00 UTC — F6.2)
- `.github/workflows/shadow_tick.yml` (cron diario 22:00 UTC L-V — F6.1)
- `.github/workflows/kaizen_decision.yml` (manual workflow_dispatch — F6.4)

**Pre-requisito en GitHub Secrets:**
- `ANTHROPIC_API_KEY` (para kaizen_weekly, llama a Claude API). Si no está, agregar en Settings → Secrets → Actions.

**Tests pre-commit:**
```bash
# Si actionlint instalado:
# actionlint .github/workflows/kaizen_*.yml .github/workflows/shadow_tick.yml
```

**Commit + push:**
```bash
git add .github/workflows/kaizen_weekly.yml \
  .github/workflows/kaizen_monthly.yml \
  .github/workflows/shadow_tick.yml \
  .github/workflows/kaizen_decision.yml

git commit -m "ci(kaizen): habilitar workflows del pipeline KAIZEN

- kaizen_weekly.yml: cron domingos 23:00 UTC. Corre kaizen_review.py
  con Claude API para detectar patrones, mergea kaizen_rules.json.
  REQUIERE secret ANTHROPIC_API_KEY.
- kaizen_monthly.yml: cron día 1 12:00 UTC. Snapshot de métricas +
  email mensual.
- shadow_tick.yml: cron diario 22:00 UTC L-V. Tickea shadow trades
  contra precios reales de Alpaca.
- kaizen_decision.yml: workflow_dispatch manual. Recibe inputs
  (target_id, type, decision) desde el email mensual.

Concurrencia configurada para evitar runs solapados de cada tipo."

git push origin main
```

**Observación post-push (1 semana):**
- **Primer scheduled run de cada workflow:**
  - `shadow_tick.yml` → mañana 22:00 UTC (M-V).
  - `kaizen_weekly.yml` → próximo domingo 23:00 UTC.
  - `kaizen_monthly.yml` → próximo día 1 del mes.
  - `kaizen_decision.yml` → cuando lo dispares manualmente.
- Verificar logs de cada uno. Errores comunes esperables y soluciones:
  - "ANTHROPIC_API_KEY not set" en weekly → setear el secret.
  - "kaizen_rules.json not found" → normal en primera corrida, el script lo crea.
  - "no shadow trades to tick" → normal hasta que el bot bloquee algo y cree shadows.

**Rollback:** `git revert HEAD --no-edit && git push`. Workflows se eliminan, sin efecto sobre los bots productivos.

**Esperar antes de Chunk 7:** 0 a 7 días. Chunk 7 no depende de este.

---

## Chunk 7 — Utility scripts (seed_missing_positions, mark_partial_tp_done)

**Por qué:** scripts manuales que vos corrés para reconciliar la DB con Alpaca. Importan `_exit_logic.recalc_stop_for_stage` y `_exit_logic.stage_implied_high_floor` (ya en origin/main, no cambios).

**Archivos:**
- `seed_missing_positions.py`
- `mark_partial_tp_done.py`

**Tests pre-commit:**
```bash
python3 -m py_compile seed_missing_positions.py mark_partial_tp_done.py

# Smoke test --dry-run (sin tocar la DB)
python3 seed_missing_positions.py --dry-run 2>/dev/null || true
python3 mark_partial_tp_done.py --dry-run 2>/dev/null || true
```

**Commit + push:**
```bash
git add seed_missing_positions.py mark_partial_tp_done.py

git commit -m "fix(seed,mark): stop ATR-aware + highest_since_entry floor stage-aware

seed_missing_positions.py:
- Stop al insertar nueva posición usa _exit_logic.recalc_stop_for_stage
  con fallback al 5% en vez de hardcodear entry*0.95. Si más adelante
  agregamos ATR al script, respeta el invariante.
- Raise stage-aware del highest_since_entry: si stage>=1, el high
  tuvo que llegar al floor implícito (entry × 1.05 stage=1, etc).
  Si quedó más bajo tras re-seed, lo subimos.

mark_partial_tp_done.py:
- Mismo fix de highest_since_entry stage-aware para que el trailing
  del watchdog calcule profit_atr correcto.
- Al insertar con stage=1 ya marcado: stop al breakeven (entry, no
  entry*0.95) y high seed al floor implícito.

Sin impacto sobre los bots automáticos — son scripts manuales."

git push origin main
```

**Observación post-push:** ninguna automática. La próxima vez que corras `seed_missing_positions.py` o `mark_partial_tp_done.py` verás el comportamiento nuevo.

**Rollback:** `git revert HEAD --no-edit && git push`.

**Esperar antes de Chunk 8:** 0.

---

## Chunk 8 — CLAUDE.md descriptivo

**Por qué:** ahora todo lo que CLAUDE.md describe efectivamente existe y corre. Actualizamos la doc para que sea descripción y no aspiración.

**Archivos:**
- `CLAUDE.md`

**Tests pre-commit:** ninguno (es markdown).

**Commit + push:**
```bash
git add CLAUDE.md

git commit -m "docs: CLAUDE.md actualizado — refleja stack kaizen ya productivo

Tras Chunks 0-7, todos los módulos, scripts, workflows y env vars
descritos en CLAUDE.md están efectivamente en origin/main y corriendo.

Cambios respecto a la versión previa:
- Tabla de módulos auxiliares (14 módulos)
- Sección Cooldowns post-exit (F1)
- Sección Trade event logging (F5.0)
- Lista de scripts (state_db_push, kaizen_review, etc.)
- Env vars nuevas (F1, F5.0, KAIZEN_MISSED_PATH, SHEETS_*)
- Rituales de seguridad ampliados (#8 capa C6, #9 param overrides,
  #10 stop solo sube)
- Banner que apunta a PLAN_V2.md para próximas mejoras

CLAUDE.md ahora es ground-truth, no roadmap."

git push origin main
```

**Observación post-push:** ninguna.

**Rollback:** `git revert HEAD --no-edit && git push` (vuelve a la versión vieja, queda inconsistente con el código pero no rompe nada).

---

## Resumen de tiempos estimados

| Chunk | Riesgo | Espera mínima antes del siguiente |
|---|---|---|
| 0 — Higiene | Nulo | 0 |
| 1 — Módulos + tests | Bajo | 1-2h (1 run verde) |
| 2 — RFTM refactor + wiring | **ALTO** | **24-48h, 2-3 runs RFTM verdes** |
| 3 — Scripts F2 | Nulo | 0 |
| 4 — Workflows modificados | Medio | 24h (4 workflows con ≥1 run cada uno) |
| 5 — Scripts kaizen | Bajo | 24h |
| 6 — Workflows kaizen nuevos | Bajo | 0 a 7 días (ver scheduled runs) |
| 7 — Utility scripts | Nulo | 0 |
| 8 — CLAUDE.md | Nulo | — (fin) |

**Total estimado calendario:** 3 a 7 días desde Chunk 0 a Chunk 8, con observación entre cada uno.

---

## Criterio global de rollback

Si en cualquier momento entre Chunk 2 y Chunk 6 detectás:
- Equity drawdown > 3% en 24h sin causa de mercado.
- Más de 3 errores en logs de bots o watchdogs.
- Comportamiento de TPs / stops distinto al esperado (ej. stop bajando, TPs vendiendo qty incorrecta, posiciones que no se cierran cuando deberían).
- Sells fantasma o sells duplicados en Alpaca.

→ **Rollback inmediato del chunk problemático**:
```bash
git revert HEAD --no-edit
git push origin main
```

Y reportar para diagnosticar antes de seguir.

---

## Lo que NO está en este plan

- **V2 — 4H, universo cripto nuevo, filtro de régimen.** Eso queda para después del Chunk 8, sobre el stack kaizen ya productivo. El `PLAN_V2.md` original sigue siendo la referencia, con las 6 enmiendas acordadas en la sesión 2026-05-24:
  1. Default `MREV_REGIME_FILTER_ENABLED=true`, 24h en modo shadow vía env.
  2. Thresholds laxos al inicio: BTC drawdown 7%, ADX max 30.
  3. Orden invertido: 4H → universo → régimen (no al revés).
  4. Gates numéricos del universo (spread <0.5%, corr 90d <0.75, fallback AAVE→LTC / MKR→BCH / UNI→DOT).
  5. Criterios cuantitativos Día 7 (max_dd > baseline×1.30 = rollback).
  6. Mini-runbook de rollback con manejo de posiciones V2, estado DB y workflows.

- **Migración Paper → Live.** El `PLAN_MIGRACION_LIVE.md` existente se mantiene; no se toca en esta promoción.

---

**Última actualización:** 2026-05-24 — fin de sesión de auditoría.
