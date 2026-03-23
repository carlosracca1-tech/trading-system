#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  run_rftm_bot.sh — RFTM Auto-Runner (called by launchd or cron)
#
#  DO NOT run this manually. Use standalone_paper_trader.py directly instead.
#  This script is designed to be called automatically by the system scheduler.
# ══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/rftm_$(date '+%Y-%m-%d').log"
LOCK_FILE="/tmp/rftm_bot.lock"

# ── Colores ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

mkdir -p "$LOG_DIR"

# ── Evita correr dos veces al mismo tiempo ───────────────────────────────────
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ Bot ya está corriendo (PID $LOCK_PID). Saltando." | tee -a "$LOG_FILE"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ── Verifica que sea día hábil (Lun-Vie) ─────────────────────────────────────
DAY_OF_WEEK=$(date '+%u')  # 1=Lunes … 7=Domingo
if [ "$DAY_OF_WEEK" -ge 6 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 📅 Fin de semana — no hay mercado. Bot no corre." | tee -a "$LOG_FILE"
    exit 0
fi

# ── Verifica que python3 y las dependencias existan ──────────────────────────
PYTHON=$(command -v python3 || command -v python || echo "")
if [ -z "$PYTHON" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ python3 no encontrado. Instalá Python." | tee -a "$LOG_FILE"
    exit 1
fi

# ── Verifica que las API keys estén configuradas ─────────────────────────────
ENV_FILE="$SCRIPT_DIR/.env.paper"
if [ ! -f "$ENV_FILE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ .env.paper no encontrado en $SCRIPT_DIR" | tee -a "$LOG_FILE"
    exit 1
fi

# Carga las env vars
set -a
source "$ENV_FILE"
set +a

if [ -z "${ALPACA_API_KEY:-}" ] || [[ "$ALPACA_API_KEY" == "your_"* ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ ALPACA_API_KEY no configurada en .env.paper" | tee -a "$LOG_FILE"
    exit 1
fi

# ── Ejecuta el bot ────────────────────────────────────────────────────────────
echo "" | tee -a "$LOG_FILE"
echo "════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🤖 Iniciando RFTM Bot" | tee -a "$LOG_FILE"
echo "════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"

cd "$SCRIPT_DIR"

"$PYTHON" standalone_paper_trader.py 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ Bot terminó correctamente." | tee -a "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ Bot terminó con error (código $EXIT_CODE)." | tee -a "$LOG_FILE"
fi

echo "════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# ── Limpia logs viejos (>30 días) ────────────────────────────────────────────
find "$LOG_DIR" -name "rftm_*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
