# PROMPT — Fase 1: Visibilidad operativa del bot

**Fecha:** 2026-04-28 (sesión siguiente a la que cerró Fase 0)
**Branch sugerida:** `feat/visibility-and-emails`
**Tiempo estimado:** ~3 horas en commits chicos.

---

## 0. Contexto que tenés que cargar

Esta sesión arranca DESPUÉS de Fase 0 mergeada. Antes de tocar nada, leer:

1. `CLAUDE.md` — notas de arquitectura, env vars, rituales.
2. `AUDITORIA_PRE_GOLIVE.md` — auditoría del 23-abr. Documenta los 2 bugs estructurales y el "no alpha".
3. `RUNBOOK_WATCHDOGS.md` (sección "Historial de operaciones") — qué se ejecutó en Fase 0 y Op 2.4 del 27-28 abr.
4. Esta sesión anterior: PR #2 (mergeado), commit `04a8d93` en main.

Hallazgos clave de la sesión anterior que cambian todo:

- **La DB local del Mac NO es la verdad operativa.** Está congelada al snapshot del 22-abr + reconcile del 28-abr. La verdad vive en GHA cache (`mrev-db-v2-main-{run_id}`, ~83 KiB). Cada run del bot persiste ahí.
- **Op 2.4 (28-abr 13:11 UTC) confirmó que el fix del DRY_RUN funciona.** Watchdog MREV vendió BTC/USD (qty 0.14757 @ $76152) y ETH/USD (qty 4.70022 @ $2266) — ambas con pérdida ~−1.8%, probablemente time_stop/trailing.
- **No llegó ningún email del Op 2.4.** Hay que debuggear. El verify-step de email secrets que se agregó en Fase 0 está en `daily_trade.yml` y `mrev_hourly.yml` pero NO en los watchdog ymls. Posibles causas: secrets faltantes, o `send_stage_event_email` con guard que salta callado.
- **AVAX/DOGE están abiertas en Alpaca** (compradas 25-27 abr) y no se vendieron en Op 2.4. En el próximo cron tick podrían venderse o no según thresholds.

Reglas innegociables (de CLAUDE.md):
- No tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS` sin preguntar.
- No tocar `check_entry`, `check_exit`, `_calc_take_profit`, `size_position` sin preguntar.
- `.env.paper` nunca se imprime ni se commitea.
- Cambios de schema solo `ALTER TABLE … ADD COLUMN` o `CREATE TABLE IF NOT EXISTS`, idempotentes.
- Errores de Alpaca → `warn`, no abortan el run.

Ritual antes de tocar código:
```bash
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
  _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py
python3 -m pytest tests/test_indicators.py tests/test_strategy.py \
  tests/test_health.py tests/test_mrev tests/test_watchdog \
  tests/test_exit_logic.py tests/test_db_health.py tests/test_db_schema.py \
  tests/test_universes_disjoint.py tests/test_mode_entry_only.py
python3 scripts/ops/preflight.py
```

---

## 1. Pre-requisitos antes de arrancar

Cosas que Charlie hace en su Mac/GitHub ANTES de la sesión, así no se traba:

1. **Verificar que los GitHub Secrets de email están seteados.**
   `Settings → Secrets and variables → Actions`. Que existan:
   - `EMAIL_FROM` (gmail address)
   - `EMAIL_PASSWORD` (App Password de Gmail, 16 caracteres)
   - `EMAIL_TO` (gmail destino, puede ser el mismo)

   Si falta alguno, agregarlo. Sin esto, ningún plan de emails funciona.

2. **Pegar el output del log del watchdog MREV de Op 2.4** al asistente al arrancar:
   ```bash
   cd ~/Desktop/trading-system
   MREV_ID=$(gh run list --workflow=mrev_watchdog.yml --event=workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')
   gh run view $MREV_ID --log | sed -n '/Run watchdog/,/Save MREV database/p' | head -120
   ```
   Sirve para investigar exactamente qué decidió el bot con BTC/ETH (Tarea 3).

3. **Confirmar que main está actualizada localmente:**
   ```bash
   git checkout main
   git pull
   git log --oneline -3
   # Esperás ver el merge de PR #2 (04a8d93) en el log
   ```

---

## 2. Plan de la sesión — 3 tareas en orden

Branch única: `feat/visibility-and-emails`. Una sub-branch / un commit por tarea. PR final único.

### Tarea 1 — Debuggear y arreglar emails (PRIORIDAD MÁXIMA, ~45 min)

**Por qué primero:** sin emails es imposible seguir las decisiones del bot en tiempo real. Es el bloqueador #1 de cualquier intento de tracking.

**Contexto técnico:**
- `_email_helpers.py` contiene `send_stage_event_email` (compartido RFTM/MREV).
- Los watchdogs llaman a esa función al final de un partial TP / exit.
- En Fase 0 agregamos verify-step a `daily_trade.yml` y `mrev_hourly.yml`. Falta agregarlo a `rftm_watchdog.yml` y `mrev_watchdog.yml`.

**Pasos:**

1. Leer `_email_helpers.py` entero. Mapear cómo se invoca el SMTP, qué guards tiene, dónde podría salir callado.
2. Agregar al inicio de `send_stage_event_email` el mismo `info()` de presencia que pusimos en `send_email_report` de RFTM:
   ```python
   info(
       "Stage event email check: "
       f"FROM={'set' if email_from else 'EMPTY'}({len(email_from)}) "
       f"PASSWORD={'set' if email_pwd else 'EMPTY'}({len(email_pwd)}) "
       f"TO={'set' if email_to else 'EMPTY'}({len(email_to)})"
   )
   ```
   Sin imprimir valores. Solo presencia + length.
3. Agregar el step `Verify email secrets` (espejo del de daily_trade.yml) a `rftm_watchdog.yml` y `mrev_watchdog.yml`. Si no están seteados, abortar early con mensaje claro.
4. Test manual: workflow_dispatch del RFTM watchdog con `dry_run=true` y mirar el log. Debería decir "Email check: FROM=set(...) PASSWORD=set(...) TO=set(...)". Si dice "EMPTY", agregar los secrets.
5. Si los secrets están bien y siguen sin llegar mails, el problema está en SMTP/login. Capturar la excepción exacta con `repr(e)` y diagnosticar (App Password vencido, 2FA, etc.).

**Commit:** `fix(emails): verify secrets in watchdog CI + log presence in send_stage_event_email`

**Acceptance:** disparar workflow_dispatch del watchdog RFTM (manual, dry_run=true), confirmar que en el log aparece el "Email check" con todo en `set`. Después correr con dry_run=false sobre alguna posición y confirmar que llega un mail al inbox de Charlie.

### Tarea 2 — Subir DBs como artifact en cada run (~30 min)

**Por qué segundo:** una vez que tenemos visibilidad por email, queremos también poder bajar la DB de producción cuando sea útil para debug (analytics, reconcile, auditoría histórica).

**Pasos:**

1. En `daily_trade.yml`, después del step "Save RFTM database" (que persiste al cache), agregar:
   ```yaml
   - name: Upload RFTM database as artifact
     if: always()
     uses: actions/upload-artifact@v4
     with:
       name: rftm-db-${{ github.run_id }}
       path: |
         trading_paper.db
         trading_paper.db-wal
         trading_paper.db-shm
       retention-days: 7
       if-no-files-found: warn
   ```
2. Mismo en `rftm_watchdog.yml`, `mrev_hourly.yml`, `mrev_watchdog.yml`.
3. Tests: NO afecta tests del repo, son cambios de workflow.
4. Documentar en `RUNBOOK_WATCHDOGS.md` cómo bajar la DB:
   ```bash
   # Bajar la última DB de RFTM
   RFTM_RUN=$(gh run list --workflow=rftm_watchdog.yml --limit 1 --json databaseId --jq '.[0].databaseId')
   gh run download $RFTM_RUN -n rftm-db-$RFTM_RUN -D /tmp/gha-rftm
   ls -la /tmp/gha-rftm/  # ahí está trading_paper.db de prod

   # Inspeccionar
   sqlite3 /tmp/gha-rftm/trading_paper.db "SELECT symbol, qty, partial_tp_taken FROM positions WHERE status='open'"
   ```

**Commit:** `feat(ops): upload trading DBs as artifacts for local inspection`

**Acceptance:** correr workflow_dispatch de cualquier workflow, verificar que el artifact aparece en la pestaña Artifacts, hacer `gh run download` y abrir la DB localmente.

### Tarea 3 — Auditar logs y entender qué pasó con BTC/ETH (~30 min)

**Por qué tercero:** una vez que vemos el log, decidimos si la lógica del bot está OK o hay un bug.

**Pasos:**

1. Charlie pega el output del `gh run view ... | sed -n '/Run watchdog/,/Save MREV database/p'` que generó al arranque de la sesión.
2. Buscar en el log las líneas que mencionan BTC/USD y ETH/USD. Identificar:
   - ¿Qué stage tenía cada una al evaluarse?
   - ¿Qué exit mechanism disparó? (los 4 candidatos: stop_loss, trailing_stop, time_stop, exit-en-banda SMA20+1.5*ATR)
   - ¿El cálculo del threshold tenía sentido o estaba basado en datos stale?
3. Si la decisión fue lógica (ej: time_stop después de 3-4 días sin reversión a SMA20) → no hacer nada, es performance esperada del MREV.
4. Si hay algo raro (ej: stop_loss recalculado con un avg_entry distinto al de Alpaca, lo que causaría salidas mal-priced) → fixear con commit chico.
5. Documentar findings en un comment en el commit o en `AUDITORIA_PRE_GOLIVE.md`.

**Posible commit (solo si encontramos bug):** `fix(mrev): <lo que sea>`

**Acceptance:** entender exactamente por qué el bot hizo lo que hizo. No-action es un resultado válido si la lógica es correcta.

---

## 3. Pos-sesión (lo que sigue después de esta Fase 1)

Ya con visibilidad operativa via email + artifact, se desbloquea:

- **Fase 1 original del PROMPT_FIX_VENTAS_Y_EMAILS.md** (trade-cards + position_events + emails consolidados con tarjetas progresivas). Ese plan asumía que la DB local era la verdad — ahora hay que adaptarlo para que el builder de cards lea la DB de GHA (vía artifact) o construya las cards sólo con datos de Alpaca + position_events nueva.
- **Fase 2 original** (email mensual RFTM + dashboard estático en gh-pages).

---

## 4. Decisiones que necesito de Charlie al arrancar

1. **¿Hacer todo en una sola PR o 3 PRs separados?** — Recomendación: 1 PR único (`feat/visibility-and-emails`) con 3 commits chicos. Más rápido de revisar/mergear que 3 PRs encadenados.
2. **¿Retention de los artifacts? 7 días default es OK o querés 14/30?** — 7 alcanza para debug agudo. Si querés histórico mensual, 30.
3. **Si en Tarea 3 encontramos que el watchdog vendió BTC/ETH por un bug real, ¿queremos pausarlo (DRY_RUN=true en cron) hasta fixearlo, o seguir live?** — Recomendación: pausar 5 min en cuanto se detecte, fixear, reactivar.

---

## 5. Mensajes de commit sugeridos

```
fix(emails): verify secrets in watchdog CI + log presence in send_stage_event_email
feat(ops): upload trading DBs as artifacts for local inspection
docs(runbook): document how to download GHA DB artifact
```

(El cuarto commit, sólo si Tarea 3 encuentra bug, lleva el nombre que corresponda.)

---

## 6. Resumen ultra-corto para arrancar

> "Sesión Fase 1 — Visibilidad. Branch feat/visibility-and-emails. Tres commits:
> (1) verify-email-secrets en watchdog ymls + log de presencia en send_stage_event_email,
> (2) upload-artifact de los .db en cada run, (3) leer logs del watchdog del 28-abr y
> entender por qué vendió BTC/ETH a -2%. Antes de empezar, leer CLAUDE.md +
> RUNBOOK_WATCHDOGS.md secciones nuevas + el PR #2 mergeado. Tests verdes 181/181
> es el baseline. No tocar lógica de check_entry/check_exit/sizing sin preguntar."
