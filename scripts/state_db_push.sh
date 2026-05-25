#!/usr/bin/env bash
# F2 — Push de DB + JSONL al branch state/db.
#
# Lo invoca cada workflow al final del job (con `if: always()`). Esto
# convierte a GitHub Actions en la fuente de verdad: la DB local del
# usuario sólo lee desde acá vía `scripts/sync_db.sh`.
#
# Política:
# - Una sola commit en state/db con el snapshot actual. Force-push
#   sobrescribe la commit anterior (mantenemos el historial mínimo).
# - Backups rotativos: antes de commitear el archivo nuevo, copiamos
#   el archivo viejo de la branch a `${file}.bak-${N}` donde N rota
#   en 1..7. Si algo se corrompe en un run, recuperable de la última
#   semana de runs.
# - Sólo pusha los archivos que existen en el workspace actual. Si el
#   workflow de RFTM corre, no toca los archivos de MREV en state/db.
#
# Args (env):
#   STATE_DB_FILES — lista separada por espacios de archivos a pushear.
#                    Ej: "trading_paper.db logs/trade_events_rftm.jsonl"
#   GITHUB_TOKEN   — token con permission contents:write (default que
#                    pasa actions/checkout@v4 si el workflow lo permite).
#   GITHUB_REPOSITORY — `<owner>/<repo>` (lo setea GitHub automáticamente).
#   BOT_TAG — etiqueta corta para el commit message (ej. "rftm", "mrev").
#
# Behavior:
# - Si la branch no existe, la crea como orphan (sin historial).
# - Si la branch existe, descarga el snapshot anterior para rotar bak.
# - El commit message incluye timestamp + run_number + bot_tag para
#   diagnóstico desde la UI de GitHub.

set -euo pipefail

if [ -z "${STATE_DB_FILES:-}" ]; then
  echo "[state_db] STATE_DB_FILES env var requerida (lista space-separated)"
  exit 0  # no fatal — solo skip
fi

if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
  echo "[state_db] GITHUB_TOKEN/GITHUB_REPOSITORY no seteados — skip push"
  exit 0
fi

BOT_TAG="${BOT_TAG:-bot}"
BRANCH="state/db"
MAX_BAK=7
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="${GITHUB_RUN_NUMBER:-manual}"

# Workspace del runner (siempre existe en GHA)
SRC="${GITHUB_WORKSPACE:-$PWD}"

# Filtrar archivos que existen — un watchdog que no tocó la DB no
# debería pushear archivos ausentes.
EXISTING=()
for f in $STATE_DB_FILES; do
  if [ -f "$SRC/$f" ]; then
    EXISTING+=("$f")
  else
    echo "[state_db] skip missing: $f"
  fi
done
if [ ${#EXISTING[@]} -eq 0 ]; then
  echo "[state_db] ningún archivo presente — nada para pushear"
  exit 0
fi
echo "[state_db] archivos a pushear: ${EXISTING[*]}"

# Trabajo en un worktree aparte para no contaminar el checkout principal.
TMPDIR_WT="$(mktemp -d)"
# Limpieza: hay que des-registrar el worktree (no alcanza con rm), si no
# git se queja en el próximo `git worktree add` de la misma branch.
cleanup_worktree() {
  cd "$SRC" 2>/dev/null || true
  if [ -d "$TMPDIR_WT/.git" ] || [ -f "$TMPDIR_WT/.git" ]; then
    git worktree remove --force "$TMPDIR_WT" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_WT"
}
trap cleanup_worktree EXIT

cd "$SRC"

# URL autenticada
REMOTE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"

git config user.email "bot@trading-system.local"
git config user.name "trading-system bot"

# ── 1. Fetch del branch state/db si existe ────────────────────────────────
if git ls-remote --exit-code --heads "$REMOTE_URL" "$BRANCH" >/dev/null 2>&1; then
  echo "[state_db] branch $BRANCH existe — fetch"
  # --force / "+" en el refspec: state/db cambia con cada force-push del CI,
  # así que el ref local "origin/state/db" puede estar adelantado o atrasado.
  git fetch --depth=1 --force "$REMOTE_URL" "+$BRANCH:refs/remotes/origin/$BRANCH" 2>&1 \
    || { echo "[state_db] fetch falló"; exit 0; }
  git worktree add -B "$BRANCH" "$TMPDIR_WT" "refs/remotes/origin/$BRANCH"
  BRANCH_EXISTED=1
else
  echo "[state_db] branch $BRANCH no existe — crear orphan"
  # worktree con branch huérfana
  git worktree add --no-checkout "$TMPDIR_WT" HEAD
  cd "$TMPDIR_WT"
  git checkout --orphan "$BRANCH"
  git rm -rf . >/dev/null 2>&1 || true
  cd "$SRC"
  BRANCH_EXISTED=0
fi

cd "$TMPDIR_WT"

# ── 2. Rotación de backups del archivo anterior ───────────────────────────
# Para cada archivo nuevo que vamos a sobrescribir, si ya hay una versión
# vieja en la branch, la copiamos a .bak-N rotando.
if [ "$BRANCH_EXISTED" = "1" ]; then
  for f in "${EXISTING[@]}"; do
    if [ -f "$f" ]; then
      # Desplazar bak-6 → bak-7, bak-5 → bak-6, ..., bak-1 → bak-2
      for i in $(seq $((MAX_BAK-1)) -1 1); do
        if [ -f "${f}.bak-${i}" ]; then
          mv "${f}.bak-${i}" "${f}.bak-$((i+1))"
        fi
      done
      # bak-7 antiguo (si existía pre-rotación) ya fue movido a bak-8 — borrar
      if [ -f "${f}.bak-$((MAX_BAK+1))" ]; then
        rm "${f}.bak-$((MAX_BAK+1))"
      fi
      # Snapshot actual → bak-1
      cp "$f" "${f}.bak-1"
    fi
  done
fi

# ── 3. Copiar los archivos nuevos desde el workspace ─────────────────────
for f in "${EXISTING[@]}"; do
  mkdir -p "$(dirname "$f")"
  cp "$SRC/$f" "$f"
done

# ── 4. README breve en la branch (solo si no existe) ─────────────────────
if [ ! -f README.md ]; then
  cat > README.md <<README_EOF
# state/db — snapshots persistentes de runtime

Esta branch NO es código. Es la fuente de verdad de los archivos de
estado que generan los bots:

- \`trading_paper.db\` — RFTM SQLite DB (positions, runs, snapshots).
- \`mrev_paper.db\` — MREV SQLite DB.
- \`logs/trade_events_rftm.jsonl\` — eventos RFTM (KAIZEN).
- \`logs/trade_events_mrev.jsonl\` — eventos MREV (KAIZEN).
- \`logs/kaizen_missed_moves.jsonl\` — rebotes perdidos por cooldown.

Cada workflow al terminar pushea sus archivos acá vía
\`scripts/state_db_push.sh\` (force push, una sola commit).

Backups rotativos: cada commit conserva los 7 snapshots anteriores
como \`{file}.bak-{N}\` (N=1..7). Si la DB de un run se corrompe,
recuperable desde la rotación.

Pull desde local: \`make sync-db\` (corre \`scripts/sync_db.sh\`).
README_EOF
fi

# ── 5. Commit + force push ────────────────────────────────────────────────
git add -A
if git diff --cached --quiet; then
  echo "[state_db] sin cambios — nada para commitear"
  exit 0
fi
git commit -m "state[${BOT_TAG}]: snapshot ${TS} run=${RUN}" >/dev/null
if git push -f "$REMOTE_URL" "HEAD:refs/heads/${BRANCH}" 2>&1; then
  echo "[state_db] ✓ push OK → ${BRANCH} (${TS}, run=${RUN}, bot=${BOT_TAG})"
else
  echo "[state_db] push falló (¿permissions: contents: write?)"
  exit 0  # no romper el job principal
fi
