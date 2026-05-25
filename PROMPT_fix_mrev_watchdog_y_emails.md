# Prompt para Claude Code — Fix MREV Watchdog + MREV-1H + spam de emails

> Pegar tal cual en una sesión nueva de Claude Code abierta sobre `/Users/charlie/Desktop/trading-system`.
> El repo tiene `CLAUDE.md` con la arquitectura — leelo primero, no te lo voy a repetir acá.

---

## Contrato

Trabajás en modo agente autónomo. **No me pidas confirmación paso a paso.** Investigá, hipotetizá, arreglá, testeá, pusheá, mirá el CI, iterá hasta que los dos workflows MREV estén en verde **dos runs consecutivos**. Recién ahí me reportás. Si en algún momento lo único que tenés para decirme es "encontré X, ¿procedo?", la respuesta es siempre **sí, procedé**.

Solo frená y preguntame si:

1. El fix requiere tocar `ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS`, `check_entry`, `check_exit`, `_calc_take_profit`, o `size_position` (lista negra de `CLAUDE.md`).
2. El fix requiere cambiar la semántica de `partial_tp_taken` (stage counter 0/1/2) o la regla "Alpaca = verdad operativa, DB = estrategia".
3. Tenés que rotar credenciales, borrar la DB de producción, o cerrar posiciones reales que están abiertas en Alpaca.
4. El bug está en una dependencia externa (Alpaca SDK, GitHub Actions runner) y la única solución es esperar a que el upstream arregle.

Cualquier otra cosa — typos, env vars faltantes, schema drift, cache corrupto, imports rotos, tests rotos, lógica de notificaciones, refactors menores, agregar guardas, agregar tests, cambiar el cron, agregar `if: failure()` con condición de streak — la decidís y la ejecutás vos.

## Síntomas

Desde hace varios días, dos workflows fallan consistentemente y mandan email cada run:

- `MREV Watchdog (Exits)` — job `MREV Watchdog — check exits` — falla en **~18s**.
- `MREV-1H Hourly Bot` — job `Run MREV-1H Strategy` — falla en **~19s**.

Mismo SHA reportado en los emails (`04a8d93`). Fallar tan rápido y siempre en el mismo tiempo = error temprano: import, env var, `assert_db_health`, o cache de DB corrupto.

`daily_trade.yml` (RFTM) parece estar OK. El problema es el lado MREV.

## Plan de ataque (ejecutá end-to-end, sin parar)

### 1. Diagnóstico (esperado: <10 min)

```bash
gh run list --workflow=mrev_watchdog.yml --limit 20 --json conclusion,headSha,createdAt,databaseId
gh run list --workflow=mrev_hourly.yml   --limit 20 --json conclusion,headSha,createdAt,databaseId
gh run view <id_ultimo_fail_watchdog> --log-failed
gh run view <id_ultimo_fail_hourly>   --log-failed
```

Identificá:

- El primer SHA donde empezó a fallar cada workflow (bisect manual contra el JSON).
- El primer `Error:` / traceback / `SystemExit(N)` en cada log.
- Si ambos fallan por la misma causa o por causas distintas.

`git log --oneline <sha_ultimo_verde>..<sha_primer_rojo> -- standalone_mrev_trader.py mrev_watchdog.py _db_health.py _email_helpers.py _exit_logic.py .github/workflows/mrev_*.yml` para ver qué cambió en la ventana sospechosa.

### 2. Hipótesis ordenadas por probabilidad

Probalas en este orden, descartá rápido y seguí:

1. **`assert_db_health` abortando**: chequear si falta una columna (drift de schema), si hay >1 run con status `RUNNING` huérfano, o si `integrity_check` falla. Mirá `_db_health.py` y comparalo contra el `CREATE TABLE` real en `standalone_mrev_trader.py` y contra la DB cacheada.
2. **Cache de `actions/cache` corrupto o con key vieja**: si la DB que restauró el runner es de un schema previo, los `INSERT` revientan. Solución: invalidar la cache key (bump del nombre o agregar `${{ hashFiles('standalone_mrev_trader.py') }}` al sufijo) y dejar que el bot la recree.
3. **Env var faltante en el workflow**: `grep -E "os\.(getenv|environ)" standalone_mrev_trader.py mrev_watchdog.py` y comparar contra `env:` en los dos `.yml`. Falta especialmente sospechosa: `MREV_DB_PATH`, `EMAIL_*`, `ALPACA_*`.
4. **Import error / módulo no committeado**: `python3 -m py_compile` local sobre los archivos del ritual. Si rompe local, rompe en CI.
5. **Tests rotos que el workflow corre antes del bot** (si los corre): mirá si el `.yml` ejecuta pytest. Si sí, qué test rompió.
6. **`SystemExit(2)` del fix P0-B** (buy-loop MREV con `sqlite3.Error`): si reapareció el bug de schema, este path se dispara.
7. **Migración aplicada local pero no en CI**: alguna `ALTER TABLE` que corrió en tu máquina pero no está en el path del bot — la DB de CI nunca la vio.

### 3. Reproducción local

Antes de pushear, reproducí el error localmente con la DB en `$TMPDIR` para no tocar la real:

```bash
export RFTM_DB_PATH="$TMPDIR/rftm_trader/trading_paper.db"
export MREV_DB_PATH="$TMPDIR/mrev_trader/mrev_paper.db"
mkdir -p "$(dirname "$RFTM_DB_PATH")" "$(dirname "$MREV_DB_PATH")"

# Cargar credenciales paper sin imprimirlas
set -a; source .env.paper; set +a

# Probar el watchdog y el entry bot en el mismo modo que CI
MODE=entry_only python3 standalone_mrev_trader.py
python3 mrev_watchdog.py
```

Si reproduce: arreglalo local, volvé a correr, validá que ahora pasa.
Si **no** reproduce: el problema es de CI puro (env, cache, runner). Atacá directo el `.yml`.

### 4. Fix + ritual de seguridad (obligatorio antes de pushear)

```bash
python3 -m py_compile standalone_paper_trader.py standalone_mrev_trader.py \
    _email_helpers.py seed_missing_positions.py rftm_watchdog.py mrev_watchdog.py

python3 -m pytest \
    tests/test_indicators.py tests/test_strategy.py tests/test_health.py \
    tests/test_mrev tests/test_watchdog tests/test_exit_logic.py \
    tests/test_db_health.py tests/test_db_schema.py \
    tests/test_universes_disjoint.py tests/test_mode_entry_only.py

python3 scripts/ops/preflight.py
```

Los tres tienen que pasar (preflight con exit 0). Si rompiste algún test con tu fix, **agregá la cobertura nueva o arreglá el test** — no lo skipees, no lo borres.

Si la causa raíz es schema drift / health-check, **agregá un test de regresión** en `tests/test_db_schema.py` o `tests/test_db_health.py` que falle sin tu fix. Esto es no-negociable: si no agregás el test, vuelve a romperse.

### 5. Push y esperar verde

```bash
git checkout -b fix/mrev-watchdog-<descripcion-corta>
git add -A
git commit -m "fix(mrev): <una línea precisa de qué arreglaste y por qué>"
git push -u origin HEAD
gh pr create --fill --base main
```

Después:

```bash
gh run watch  # el run del PR
```

- Si verde en el PR: `gh pr merge --squash --auto` (o merge directo si preferís).
- Si rojo: leé el log nuevo, iterá. **No declares victoria con un solo run verde.** Esperá a que después del merge corran al menos un run del watchdog y uno del hourly bot en `main` y los dos terminen verdes. Recién ahí pasa a fase 2.

### 6. Ruido de emails (recién después de tener CI estable)

Tres palancas, hacé las tres:

1. **Workflow-level** (la que más mueve la aguja): agregá un job de notificación con gate de **streak**. Solo manda email si **3 runs consecutivos** del workflow están en `failure`. Implementación sugerida — un step adicional al final del job que, en `if: failure()`, consulte `gh api /repos/${{ github.repository }}/actions/workflows/<id>/runs?per_page=3&status=completed` y solo deje fallar el job (= dispara el email default) si los 3 últimos son `failure`. Si los anteriores fueron verdes, el step termina con éxito y no se manda email a pesar de que el bot falló. **Importante**: el bot real sigue marcando failure en el step propio — solo el "notification gate" cambia. No usar `continue-on-error: true` en el step del bot, eso esconde el problema.

   Alternativa más simple si lo de arriba se complica: agregar un step de "summary" con `if: failure() && github.run_attempt == 1` y dejar que el retry automático absorba los flakes. Decidilo vos según lo que veas.

2. **GitHub settings** (manuales, no las podés tocar — solo dejame instrucciones claras al final): ir a `https://github.com/settings/notifications` → Actions → ajustar para que solo notifique en `default branch`. Listámelo como TODO en el reporte final.

3. **Filtro Gmail** (manual también — instrucciones en el reporte final): regla que catch `from:notifications@github.com subject:"Run failed: MREV"` → label `bot/ci-failures`, skip inbox, mark as read. Sirve como red de seguridad.

## Reglas duras

- `.env.paper` **no** se imprime, **no** se commitea, **no** aparece en logs. Si lo necesitás, `set -a; source .env.paper; set +a` y listo.
- Errores de Alpaca: log `warn`, **no abortar el run** (regla de `CLAUDE.md`).
- Cambios de schema solo con `ALTER TABLE ... ADD COLUMN` envueltos en try/except idempotente.
- Logging via `ok()/info()/warn()/err()/hdr()` — no metas loggers nuevos.
- Backward compat de env vars: cualquier env nueva que agregues tiene default hardcodeado.
- Si el problema es la DB cacheada: la solución correcta es **invalidar la key**, no `rm -rf` la DB en el runner.

## Reporte final (lo único que quiero leer cuando termines)

Cuando los dos workflows estén verdes en `main` por al menos 1 run cada uno **después** de tu merge, mandame en este formato exacto:

```
## Diagnóstico
- Causa raíz watchdog: <una línea>
- Causa raíz hourly: <una línea, o "misma que watchdog">
- SHA donde se rompió: <abc1234>
- Por qué no lo agarró el CI antes: <una línea>

## Fix
- Archivos tocados: <lista>
- Test de regresión agregado: <path::test_name>
- PR: <url>
- Run verde watchdog: <url>
- Run verde hourly: <url>

## Email noise
- Cambio en workflow: <descripción + commit>
- TODO manual para mí (Charlie):
  1. <paso en GitHub settings>
  2. <regla en Gmail>

## Riesgos / cosas raras que vi
- <lista corta, o "ninguna">
```

Sin postamble, sin "espero que esto te sirva", sin emojis. Reporte y listo.
