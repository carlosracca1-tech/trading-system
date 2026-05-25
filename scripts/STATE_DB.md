# F2 — DB sync con branch `state/db`

## Por qué

Las DBs (`trading_paper.db`, `mrev_paper.db`) y los JSONLs de eventos
viven en los runners de GitHub Actions. Entre runs se preservan via
cache, pero **no son visibles desde tu Mac**. Si querés ver el estado
real (qué posiciones tiene el bot, qué eventos loggeó), tenés que ir
a la UI de GHA o descargar el artifact.

F2 resuelve esto: cada workflow al terminar pushea sus archivos a una
branch dedicada `state/db`. Vos hacés `make sync-db` y tenés todo
copiado localmente.

## Modelo

```
   ┌─────────────────────┐                    ┌──────────────────────┐
   │  GitHub Actions     │                    │  Tu Mac (local)      │
   │  (fuente de verdad) │ ──── git push ───▶ │                      │
   │                     │     state/db       │  ┌────────────────┐  │
   │  trading_paper.db   │                    │  │ make sync-db   │  │
   │  mrev_paper.db      │                    │  │ git fetch +    │  │
   │  trade_events_*.jsonl│ ◀── git fetch ──── │  │ git show       │  │
   │  kaizen_missed.jsonl│                    │  └────────────────┘  │
   └─────────────────────┘                    └──────────────────────┘
```

Garantías:

1. **GHA es siempre la fuente de verdad.** Si modificás la DB local
   y corre un workflow después, lo de GHA gana.
2. **Local solo lee.** El script local NO empuja nada a `state/db`.
3. **Backups rotativos.** Cada commit en `state/db` preserva los 7
   snapshots anteriores como `${file}.bak-1` ... `${file}.bak-7`.
4. **Lock de seguridad por mtime.** Si modificaste la DB local
   manualmente después del último sync, `make sync-db` aborta para
   evitar pisar los cambios. Sobrescribir con `make sync-db-force`.
5. **Resiliencia.** Si el push falla (ej. permission denied), el
   workflow NO falla — el step usa `continue-on-error: true`.

## Uso

```bash
# Pull todo
make sync-db

# Forzar pisado (ignora lock de mtime)
make sync-db-force

# Pull selectivo
bash scripts/sync_db.sh --only trading_paper.db
```

Después del pull, podés correr análisis sobre la DB sin tocar nada:

```bash
sqlite3 trading_paper.db "SELECT symbol, qty, entry_price, stop_loss FROM positions WHERE status='open'"
```

O abrir el JSONL de eventos:

```bash
tail -20 logs/trade_events_rftm.jsonl | jq .
```

## Si algo se corrompe

Las 7 backups rotativas viven en la misma branch `state/db`. Cada
commit:

- `trading_paper.db` ← el archivo actual
- `trading_paper.db.bak-1` ← snapshot del run anterior
- `trading_paper.db.bak-2` ← snapshot 2 runs atrás
- ...
- `trading_paper.db.bak-7` ← snapshot 7 runs atrás

Para recuperar:

```bash
# Ver qué hay
git fetch origin state/db
git ls-tree origin/state/db | head

# Bajar el bak-3 de hace unos runs
git show origin/state/db:trading_paper.db.bak-3 > trading_paper.db.recovered
```

Después se puede commitear esa versión recuperada a `state/db` desde
la UI de GitHub si querés que GHA arranque desde ahí, o copiarla en
local y trabajar offline.

## Detalles internos

### El script CI: `scripts/state_db_push.sh`

Lo invoca cada workflow al final con `if: always()` y
`continue-on-error: true`. Variables que espera:

| Env var | Qué es | Quién la setea |
|---|---|---|
| `STATE_DB_FILES` | Archivos space-separated a pushear | el step del workflow |
| `BOT_TAG` | Etiqueta del bot (`rftm`/`mrev`/`rftm-wd`/`mrev-wd`) — sale en commit msg | el step |
| `GITHUB_TOKEN` | Token con `contents: write` | automático por `permissions:` del workflow |
| `GITHUB_REPOSITORY` | `owner/repo` | automático |
| `GITHUB_RUN_NUMBER` | Run number — sale en commit msg | automático |

Flujo:

1. Fetch del branch `state/db` (si existe).
2. Checkout en un worktree temporal.
3. Rotar backups: `bak-6→bak-7`, ..., `bak-1→bak-2`, current → `bak-1`.
4. Copiar los archivos nuevos desde el workspace del runner.
5. `git add -A`, `commit`, `git push -f`.

### El script local: `scripts/sync_db.sh`

| Opción | Default |
|---|---|
| Archivos | `trading_paper.db`, `mrev_paper.db`, `logs/trade_events_{rftm,mrev}.jsonl`, `logs/kaizen_missed_moves.jsonl` |
| Sentinel | `.state_db_last_sync` (mtime del último pull exitoso) |
| `--only <file>` | Pull selectivo |
| `--force` | Ignora lock de mtime |

El sentinel `.state_db_last_sync` está en `.gitignore` — es local de
tu Mac. Si lo borrás, el lock no aplica en el próximo sync.

## Permisos

Los workflows tienen `permissions: contents: write` para que
`github.token` pueda pushear. No requiere PAT ni secrets adicionales.

Si tu org tiene policy de "Restrict workflow write permissions",
chequear Settings → Actions → General → Workflow permissions. Tiene
que estar en "Read and write".
