#!/usr/bin/env bash
# Install the QwenPaw webhook bridge as a supervisor-managed service.
#
# Usage:  bash scripts/install_qwenpaw_bridge.sh
#
# Requires: miloco-cli, python3, supervisor (via miloco's supervisord)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_PY="$SCRIPT_DIR/qwenpaw_webhook_bridge.py"
SUPERVISOR_CONF_TEMPLATE="$SCRIPT_DIR/qwenpaw_bridge_supervisor.conf"
SUPERVISORD_CONF="$HOME/.openclaw/miloco/supervisord.conf"
QWENPAW_BIN="${QWENPAW_BIN:-/app/venv/bin/qwenpaw}"

echo "[install] QwenPaw Miloco webhook bridge"

# --- validate -----------------------------------------------------------
if [[ ! -f "$BRIDGE_PY" ]]; then
    echo "[install] ERROR: bridge script not found: $BRIDGE_PY" >&2
    exit 1
fi
if [[ ! -f "$SUPERVISORD_CONF" ]]; then
    echo "[install] ERROR: miloco supervisord.conf not found (run miloco-cli first)" >&2
    exit 1
fi
if ! command -v python3 &>/dev/null; then
    echo "[install] ERROR: python3 not found" >&2
    exit 1
fi

# --- append bridge program to supervisor conf ---------------------------
BRIDGE_SECTION="[program:qwenpaw-miloco-bridge]"
if grep -qF "$BRIDGE_SECTION" "$SUPERVISORD_CONF" 2>/dev/null; then
    echo "[install] Bridge already registered — skipping supervisor config"
else
    echo "[install] Adding bridge section to supervisord.conf …"
    echo "" >> "$SUPERVISORD_CONF"
    sed \
        -e "s|command=.*|command=python3 $BRIDGE_PY|" \
        -e "s|environment=.*|environment=QWENPAW_BIN=\"$QWENPAW_BIN\",QWENPAW_BASE_URL=\"http://localhost:8088\"|" \
        "$SUPERVISOR_CONF_TEMPLATE" >> "$SUPERVISORD_CONF"
fi

# --- restart miloco so supervisor picks up the new program --------------
echo "[install] Restarting miloco service …"
if miloco-cli service status &>/dev/null; then
    miloco-cli service restart
else
    miloco-cli service start
fi

echo "[install] Done."
echo "  Status : miloco-cli service status"
echo "  Logs   : tail -f ~/.openclaw/miloco/log/qwenpaw_bridge.log"
