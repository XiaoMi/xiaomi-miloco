#!/usr/bin/env bash
# Miloco Hermes adapter service helper.
# Usage: ./miloco.sh {start|stop|restart|status}

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
BRIDGE_PORT="${MILOCO_HERMES_BRIDGE_PORT:-1811}"
BACKEND_URL="${MILOCO_BACKEND_URL:-http://127.0.0.1:1810}"

BRIDGE_PID_FILE="${MILOCO_HOME}/miloco-hermes-bridge.pid"
BRIDGE_LOG="${MILOCO_HOME}/log/miloco-hermes-bridge.log"
BRIDGE_SCRIPT="${SCRIPT_DIR}/miloco-bridge.py"
CONFIG_FILE="${MILOCO_HOME}/config.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[miloco-hermes]${NC} $*"; }
warn() { echo -e "${YELLOW}[miloco-hermes]${NC} $*"; }
err()  { echo -e "${RED}[miloco-hermes]${NC} $*"; }

is_bridge_running() {
  [ -f "$BRIDGE_PID_FILE" ] && kill -0 "$(cat "$BRIDGE_PID_FILE")" 2>/dev/null
}

is_backend_running() {
  curl -fsS --max-time 2 "${BACKEND_URL}/health" >/dev/null 2>&1
}

start_backend() {
  if is_backend_running; then
    log "miloco-backend already healthy (${BACKEND_URL})"
    return
  fi
  if ! command -v miloco-cli >/dev/null 2>&1; then
    err "miloco-cli not found; install Miloco CLI or start backend manually"
    return 1
  fi
  log "starting miloco-backend via miloco-cli service start"
  MILOCO_HOME="$MILOCO_HOME" miloco-cli service start
  for _ in $(seq 1 20); do
    if is_backend_running; then
      log "miloco-backend healthy (${BACKEND_URL})"
      return
    fi
    sleep 1
  done
  err "backend did not become healthy in time"
  return 1
}

stop_backend() {
  if ! command -v miloco-cli >/dev/null 2>&1; then
    warn "miloco-cli not found; skip backend stop"
    return
  fi
  MILOCO_HOME="$MILOCO_HOME" miloco-cli service stop || true
}

start_bridge() {
  mkdir -p "$(dirname "$BRIDGE_LOG")"
  if is_bridge_running; then
    warn "miloco-bridge already running (pid $(cat "$BRIDGE_PID_FILE"))"
    return
  fi
  log "starting miloco-bridge on port ${BRIDGE_PORT}"
  MILOCO_HOME="$MILOCO_HOME" python3 "$BRIDGE_SCRIPT" \
    --port "$BRIDGE_PORT" \
    --config "$CONFIG_FILE" \
    >> "$BRIDGE_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$BRIDGE_PID_FILE"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$BRIDGE_PID_FILE"
    err "miloco-bridge failed; see $BRIDGE_LOG"
    return 1
  fi
  log "miloco-bridge running (pid ${pid})"
}

stop_bridge() {
  if ! is_bridge_running; then
    warn "miloco-bridge not running"
    rm -f "$BRIDGE_PID_FILE"
    return
  fi
  local pid
  pid="$(cat "$BRIDGE_PID_FILE")"
  log "stopping miloco-bridge (pid ${pid})"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$BRIDGE_PID_FILE"
      return
    fi
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$BRIDGE_PID_FILE"
}

status() {
  echo ""
  echo "Miloco Hermes adapter"
  echo "  MILOCO_HOME: $MILOCO_HOME"
  echo "  backend:     $(is_backend_running && echo healthy || echo stopped)"
  echo "  bridge:      $(is_bridge_running && echo "running pid $(cat "$BRIDGE_PID_FILE")" || echo stopped)"
  echo "  bridge URL:  http://127.0.0.1:${BRIDGE_PORT}/miloco/webhook"
  echo "  bridge log:  $BRIDGE_LOG"
  echo ""
}

case "${1:-}" in
  start)
    mkdir -p "$MILOCO_HOME/log"
    start_backend
    start_bridge
    status
    ;;
  stop)
    stop_bridge
    stop_backend
    status
    ;;
  restart)
    stop_bridge
    start_backend
    start_bridge
    status
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
