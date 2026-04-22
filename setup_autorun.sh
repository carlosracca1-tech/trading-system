#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  DEPRECATED 2026-04-22: este bot ahora corre vía GitHub Actions
#  (.github/workflows/daily_trade.yml). NO correrlo localmente —
#  causa doble ejecución y potencial doble-compra del mismo símbolo.
#
#  Para desinstalar un agente launchd ya cargado:
#      bash setup_autorun.sh --remove
#  O manual:
#      launchctl unload ~/Library/LaunchAgents/com.rftm.trader.plist
#      rm ~/Library/LaunchAgents/com.rftm.trader.plist
#
#  Este archivo se mantiene como referencia histórica.
# ══════════════════════════════════════════════════════════════════════════════
#  setup_autorun.sh — Configura el RFTM Bot para correr automáticamente en Mac
#
#  Usa launchd (el scheduler nativo de macOS) — más confiable que cron.
#  El bot correrá automáticamente de Lunes a Viernes.
#
#  Uso:
#    bash setup_autorun.sh          # Instala el agente automático
#    bash setup_autorun.sh --remove # Desinstala el agente
#    bash setup_autorun.sh --status # Ver si está activo
#    bash setup_autorun.sh --logs   # Ver los últimos logs
#    bash setup_autorun.sh --now    # Correr el bot ahora mismo (prueba)
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_SCRIPT="$SCRIPT_DIR/run_rftm_bot.sh"
LOG_DIR="$SCRIPT_DIR/logs"
PLIST_TEMPLATE="$SCRIPT_DIR/com.rftm.trader.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.rftm.trader.plist"
LABEL="com.rftm.trader"

# ──────────────────────────────────────────────────────────────────────────────
check_requirements() {
    if [[ "$(uname)" != "Darwin" ]]; then
        error "Este script es solo para macOS. En Linux usá cron: crontab -e"
    fi
    if [ ! -f "$RUNNER_SCRIPT" ]; then
        error "No encontré run_rftm_bot.sh en $SCRIPT_DIR"
    fi
    if [ ! -f "$SCRIPT_DIR/.env.paper" ]; then
        warn ".env.paper no encontrado. Asegurate de tener las API keys configuradas."
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
install_agent() {
    check_requirements
    mkdir -p "$LOG_DIR"
    mkdir -p "$HOME/Library/LaunchAgents"
    chmod +x "$RUNNER_SCRIPT"

    # Reemplaza los placeholders en el plist
    sed \
        -e "s|PLACEHOLDER_SCRIPT_PATH|$RUNNER_SCRIPT|g" \
        -e "s|PLACEHOLDER_HOME|$HOME|g" \
        -e "s|PLACEHOLDER_WORKING_DIR|$SCRIPT_DIR|g" \
        -e "s|PLACEHOLDER_LOG_PATH|$LOG_DIR|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DEST"

    # Desinstala versión anterior si existe
    launchctl unload "$PLIST_DEST" 2>/dev/null || true

    # Instala el nuevo agente
    launchctl load -w "$PLIST_DEST"

    echo ""
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${GREEN}  ✓ RFTM Bot configurado para correr automáticamente${NC}"
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${BOLD}Horario:${NC}    Lunes a Viernes a las 10:35 AM (hora de tu Mac)"
    echo -e "  ${BOLD}Mercado:${NC}    Equivale a ~9:35 AM ET (abre NYSE)"
    echo ""
    echo -e "  ${BOLD}Logs:${NC}       $LOG_DIR/rftm_FECHA.log"
    echo -e "  ${BOLD}Estado:${NC}     bash setup_autorun.sh --status"
    echo -e "  ${BOLD}Probar ya:${NC}  bash setup_autorun.sh --now"
    echo -e "  ${BOLD}Desinstalar:${NC} bash setup_autorun.sh --remove"
    echo ""
    warn "⚠ Tu Mac debe estar ENCENDIDA y con sesión iniciada a las 10:35 AM"
    echo "  Si la Mac está en standby (lid cerrado) el bot NO corre."
    echo "  Para evitar esto: Ajustes → Batería → desactivar 'Poner disco en reposo'."
    echo ""
}

# ──────────────────────────────────────────────────────────────────────────────
remove_agent() {
    if [ -f "$PLIST_DEST" ]; then
        launchctl unload "$PLIST_DEST" 2>/dev/null || true
        rm -f "$PLIST_DEST"
        success "Agente desinstalado. El bot ya no correrá automáticamente."
    else
        warn "El agente no estaba instalado."
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
show_status() {
    echo ""
    echo -e "${BOLD}Estado del RFTM Auto-Runner${NC}"
    echo "$(printf '─%.0s' {1..50})"
    echo ""

    if [ -f "$PLIST_DEST" ]; then
        success "Agente instalado en: $PLIST_DEST"

        # Ver si está cargado en launchctl
        if launchctl list | grep -q "$LABEL" 2>/dev/null; then
            success "launchd: ACTIVO (cargado)"
            LAST_EXIT=$(launchctl list | grep "$LABEL" | awk '{print $2}')
            echo -e "  Último código de salida: ${BOLD}$LAST_EXIT${NC} (0 = OK)"
        else
            warn "launchd: NO activo (plist existe pero no está cargado)"
            echo "  Para re-activar: bash setup_autorun.sh"
        fi
    else
        warn "Agente NO instalado. Corré: bash setup_autorun.sh"
    fi

    echo ""
    echo -e "${BOLD}Últimos logs:${NC}"
    LATEST_LOG=$(ls -t "$LOG_DIR"/rftm_*.log 2>/dev/null | head -1 || echo "")
    if [ -n "$LATEST_LOG" ]; then
        echo "  Archivo: $LATEST_LOG"
        echo "$(printf '─%.0s' {1..50})"
        tail -30 "$LATEST_LOG"
    else
        warn "No hay logs todavía. El bot aún no ha corrido."
    fi
    echo ""
}

# ──────────────────────────────────────────────────────────────────────────────
show_logs() {
    LATEST_LOG=$(ls -t "$LOG_DIR"/rftm_*.log 2>/dev/null | head -1 || echo "")
    if [ -z "$LATEST_LOG" ]; then
        warn "No hay logs todavía."
        exit 0
    fi
    echo -e "${BOLD}Log más reciente: $LATEST_LOG${NC}"
    echo "$(printf '─%.0s' {1..60})"
    cat "$LATEST_LOG"
}

# ──────────────────────────────────────────────────────────────────────────────
run_now() {
    check_requirements
    chmod +x "$RUNNER_SCRIPT"
    echo ""
    info "Ejecutando el bot AHORA (modo prueba)..."
    echo "$(printf '─%.0s' {1..50})"
    bash "$RUNNER_SCRIPT"
}

# ──────────────────────────────────────────────────────────────────────────────
# Main
case "${1:-install}" in
    --remove)   remove_agent ;;
    --status)   show_status  ;;
    --logs)     show_logs    ;;
    --now)      run_now      ;;
    install|"") install_agent ;;
    *)
        echo "Uso: bash setup_autorun.sh [--remove | --status | --logs | --now]"
        exit 1
        ;;
esac
