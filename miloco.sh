#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MILOCO_HOME="${MILOCO_HOME:-$ROOT_DIR/.local/miloco-home}"
CLI_BIN="$ROOT_DIR/cli/.venv/bin/miloco-cli"
SUPERVISORCTL="$ROOT_DIR/cli/.venv/bin/supervisorctl"
SUPERVISOR_CONF="$MILOCO_HOME/supervisord.conf"

export MILOCO_HOME
export PATH="$ROOT_DIR/cli/.venv/bin:$PATH"

usage() {
  cat <<EOF
Usage: ./miloco.sh start|stop|restart|status

Commands:
  start    Start miloco backend service
  stop     Stop miloco backend service
  restart  Restart miloco backend service
  status   Show backend supervisor status
EOF
}

require_cli() {
  if [[ ! -x "$CLI_BIN" ]]; then
    echo "miloco-cli not found: $CLI_BIN" >&2
    exit 1
  fi
}

require_supervisorctl() {
  if [[ ! -x "$SUPERVISORCTL" ]]; then
    echo "supervisorctl not found: $SUPERVISORCTL" >&2
    exit 1
  fi
}

case "${1:-}" in
  start)
    require_cli
    "$CLI_BIN" service start
    ;;
  stop)
    require_cli
    "$CLI_BIN" service stop
    ;;
  restart)
    require_cli
    "$CLI_BIN" service restart
    ;;
  status)
    require_supervisorctl
    if [[ -S "$MILOCO_HOME/supervisor.sock" && -f "$SUPERVISOR_CONF" ]]; then
      "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" status miloco-backend
    else
      require_cli
      "$CLI_BIN" service status
    fi
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac
