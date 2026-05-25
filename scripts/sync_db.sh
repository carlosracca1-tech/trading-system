#!/usr/bin/env bash
# F2 — Pull de DB + JSONL desde el branch state/db (CI = fuente de verdad).
#
# Política:
# - GHA es la fuente de verdad. Esta máquina local solo LEE.
# - Lock de seguridad por mtime: si la DB local fue modificada DESPUÉS
#   del último pull, abortamos. Evita pisar cambios hechos a mano (ej.
#   un seed_missing_positions manual local).
# - Hace .bak local antes de pisar — si quedó algo importante, se puede
#   recuperar de inmediato.
# - El script NO empuja nada para arriba. Si necesitás modificar la DB
#   localmente y persistirlo, el path correcto es correr el bot en local
#   (que escribe a la misma DB) y comitearlo desde el workflow.
#
# Uso:
#   make sync-db                    # equivalente
#   bash scripts/sync_db.sh         # pull todo
#   bash scripts/sync_db.sh --force # ignorar el lock de mtime
#   bash scripts/sync_db.sh --only trading_paper.db
#
# Archivos por default:
#   trading_paper.db
#   mrev_paper.db
#   logs/trade_events_rftm.jsonl
#   logs/trade_events_mrev.jsonl
#   logs/kaizen_missed_moves.jsonl

set -euo pipefail

BRANCH="state/db"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DEFAULT_FILES=(
  "trading_paper.db"
  "mrev_paper.db"
  "logs/trade_events_rftm.jsonl"
  "logs/trade_events_mrev.jsonl"
  "logs/kaizen_missed_moves.jsonl"
)

FORCE=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    -h|--help)
      sed -n '1,30p' "$0"
      exit 0
      ;;
    *) echo "argumento desconocido: $1"; exit 2 ;;
  esac
done

if [ -n "$ONLY" ]; then
  FILES=("$ONLY")
else
  FILES=("${DEFAULT_FILES[@]}")
fi

# ── Helper: mtime cross-platform (Linux usa stat -c, macOS stat -f) ──────
# Linux `stat -f` devuelve info del filesystem y NO falla, así que no se
# puede usar como condición. Lo importante es probar GNU `stat -c` primero
# y caer a BSD `stat -f` sólo en macOS.
_mtime() {
  local f="$1"
  if stat -c "%Y" "$f" 2>/dev/null; then
    return 0
  fi
  stat -f "%m" "$f" 2>/dev/null
}

# ── 1. Sentinel del último pull ────────────────────────────────────────────
SENTINEL=".state_db_last_sync"
LAST_SYNC=0
if [ -f "$SENTINEL" ]; then
  LAST_SYNC=$(_mtime "$SENTINEL")
fi

# ── 2. Fetch del branch ────────────────────────────────────────────────────
if ! git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "✗ La branch '$BRANCH' no existe todavía en el remote."
  echo "  Esperá a que algún workflow (RFTM o MREV) termine y haga el primer push."
  exit 1
fi

echo "→ fetch origin/$BRANCH"
# --force / "+": la branch state/db se force-pushea con cada run del CI,
# así que el ref local de tracking se va a desincronizar si no se fuerza.
git fetch --depth=1 --force origin "+$BRANCH:refs/remotes/origin/$BRANCH" 2>&1 | sed 's/^/  /'

# ── 3. Por cada archivo: chequeo de mtime → backup → pull ─────────────────
ANY_PULLED=0
ANY_SKIPPED=0
for f in "${FILES[@]}"; do
  # ¿Existe en la branch?
  if ! git cat-file -e "refs/remotes/origin/${BRANCH}:${f}" 2>/dev/null; then
    echo "  • $f — no está en $BRANCH, skip"
    continue
  fi

  # ── Lock por mtime: si el archivo local fue modificado después del
  # último sync, abortar (a menos que --force).
  if [ -f "$f" ]; then
    LOCAL_MTIME=$(_mtime "$f")
    if [ "$FORCE" != "1" ] && [ -n "$LOCAL_MTIME" ] && [ -n "$LAST_SYNC" ] \
       && [ "$LAST_SYNC" -gt 0 ] && [ "$LOCAL_MTIME" -gt "$LAST_SYNC" ]; then
      echo "  ✗ $f — modificado localmente DESPUÉS del último sync."
      echo "    Local mtime  : $(date -d "@$LOCAL_MTIME" 2>/dev/null || date -r "$LOCAL_MTIME")"
      echo "    Último sync : $(date -d "@$LAST_SYNC"  2>/dev/null || date -r "$LAST_SYNC")"
      echo "    Re-correr con --force si querés pisar los cambios locales."
      ANY_SKIPPED=1
      continue
    fi
    # Backup local antes de pisar
    cp "$f" "$f.local-bak" 2>/dev/null || true
  fi

  mkdir -p "$(dirname "$f")"
  git show "refs/remotes/origin/${BRANCH}:${f}" > "$f"
  echo "  ✓ $f ($(wc -c < "$f" | tr -d ' ') bytes)"
  ANY_PULLED=1
done

# ── 4. Actualizar el sentinel ──────────────────────────────────────────────
if [ "$ANY_PULLED" = "1" ]; then
  touch "$SENTINEL"
  echo "→ sync OK (sentinel actualizado: $SENTINEL)"
fi

if [ "$ANY_SKIPPED" = "1" ]; then
  echo "⚠ Algunos archivos se skiparon por el lock de mtime."
  exit 3
fi
