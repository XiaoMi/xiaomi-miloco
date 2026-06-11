#!/bin/bash
# Xiaomi Miloco macOS One-Click Startup Script
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
VENV_DIR="${PROJECT_ROOT}/.venv"
LOG_DIR="${PROJECT_ROOT}/.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_ok()   { echo -e "${GREEN}✅ $*${NC}"; }
print_warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
print_err()  { echo -e "${RED}❌ $*${NC}"; }
print_step() { echo -e "${CYAN}▶ $*${NC}"; }

# ============================================================
# Pre-checks
# ============================================================
if [[ ! -d "$VENV_DIR" ]]; then
    print_err "Virtual environment not found. Run first: bash scripts/install_macos.sh"
    exit 1
fi

if [[ ! -f "${PROJECT_ROOT}/output/lib/libllama-mico.dylib" ]]; then
    print_err "Metal backend not built. Run first: bash scripts/ai_engine_metal_build.sh"
    exit 1
fi

mkdir -p "$LOG_DIR"

PYTHON="${VENV_DIR}/bin/python"
PID_FILE="${LOG_DIR}/.pids"

# ============================================================
# Cleanup on exit
# ============================================================
cleanup() {
    echo ""
    print_step "Shutting down services..."
    if [[ -f "$PID_FILE" ]]; then
        while read -r pid; do
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
            fi
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    wait 2>/dev/null
    print_ok "All services stopped"
    exit 0
}

trap cleanup SIGINT SIGTERM

# Clear old PID file
rm -f "$PID_FILE"

# ============================================================
# Start services
# ============================================================

# 1. AI Engine
print_step "Starting AI Engine (Metal)..."
cd "$PROJECT_ROOT"
$PYTHON scripts/start_ai_engine.py > "${LOG_DIR}/ai_engine.log" 2>&1 &
AI_PID=$!
echo "$AI_PID" >> "$PID_FILE"
print_ok "AI Engine starting (PID: $AI_PID)"

# Wait for AI engine to be ready
AI_ENGINE_READY=false
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:8001/models &>/dev/null; then
        AI_ENGINE_READY=true
        break
    fi
    sleep 1
done
if [[ "$AI_ENGINE_READY" == "true" ]]; then
    print_ok "AI Engine ready"
else
    print_warn "AI Engine may still be starting (check .log/ai_engine.log)"
fi

# 2. Backend
print_step "Starting Backend..."
cd "$PROJECT_ROOT"
$PYTHON scripts/start_server.py > "${LOG_DIR}/backend.log" 2>&1 &
BE_PID=$!
echo "$BE_PID" >> "$PID_FILE"
print_ok "Backend starting (PID: $BE_PID)"

# Wait for backend to be ready
BACKEND_READY=false
for i in $(seq 1 15); do
    if curl -sk https://127.0.0.1:8000/ &>/dev/null; then
        BACKEND_READY=true
        break
    fi
    sleep 1
done
if [[ "$BACKEND_READY" == "true" ]]; then
    print_ok "Backend ready"
else
    print_warn "Backend may still be starting (check .log/backend.log)"
fi

# 3. Frontend
print_step "Starting Frontend..."
if [[ -d "${PROJECT_ROOT}/web_ui/node_modules" ]]; then
    cd "${PROJECT_ROOT}/web_ui"
    ./node_modules/.bin/vite --host > "${LOG_DIR}/frontend.log" 2>&1 &
    FE_PID=$!
    echo "$FE_PID" >> "$PID_FILE"
    print_ok "Frontend starting (PID: $FE_PID)"
else
    print_warn "Frontend dependencies not installed, skipping"
    print_warn "Run: cd web_ui && npm install --include=dev"
fi

cd "$PROJECT_ROOT"

# ============================================================
# Show status
# ============================================================
sleep 2
echo ""
echo "============================================================"
echo -e "${GREEN}  🍎 Xiaomi Miloco (macOS Metal)${NC}"
echo "============================================================"
echo ""
echo -e "  Frontend:    ${CYAN}https://127.0.0.1:5173${NC}"
echo -e "  Backend API: ${CYAN}https://127.0.0.1:8000/docs${NC}"
echo -e "  AI Engine:   ${CYAN}http://127.0.0.1:8001/docs${NC}"
echo ""
echo "  Logs: .log/ai_engine.log, .log/backend.log, .log/frontend.log"
echo ""
echo -e "  Press ${YELLOW}Ctrl+C${NC} to stop all services"
echo "============================================================"
echo ""

# ============================================================
# Wait for any process to exit
# ============================================================
wait
