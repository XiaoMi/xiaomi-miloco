#!/usr/bin/env bash
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
#
# Miloco Installer — Shell Bootstrap
# Ensures uv + Python are available, then delegates to install.py.
#
# Usage: bash scripts/install.sh [options]
# Options are forwarded to install.py (--dev, --lang, --omni-api-key, --uninstall, -h)
set -euo pipefail

# ── Colors (bootstrap phase only) ─────────────────────────
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m'
    C_YELLOW=$'\033[1;33m' C_NC=$'\033[0m'
else
    C_RED='' C_GREEN='' C_YELLOW='' C_NC=''
fi

info()  { printf "%s[INFO]%s  %s\n" "$C_GREEN"  "$C_NC" "$*"; }
warn()  { printf "%s[WARN]%s  %s\n" "$C_YELLOW" "$C_NC" "$*" >&2; }
fail()  { printf "%s[FAIL]%s  %s\n" "$C_RED"    "$C_NC" "$*" >&2; exit 1; }

# ── Step 1: Ensure uv is available ────────────────────────
ensure_uv() {
    for p in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if command -v "$p" >/dev/null 2>&1 || [[ -x "$p" ]]; then
            UV_CMD="$p"; return
        fi
    done
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv installation failed"
    UV_CMD="uv"
}

# ── Step 2: Ensure Python >=3.11 (prefer 3.14) ───────────
ensure_python() {
    local py_path
    for ver in 3.14 3.13 3.12 3.11; do
        py_path=$("$UV_CMD" python find "$ver" 2>/dev/null) && {
            info "Python $ver found: $py_path"
            return
        }
    done
    info "Installing Python 3.14 via uv..."
    "$UV_CMD" python install 3.14
    "$UV_CMD" python find 3.14 >/dev/null 2>&1 || fail "Python installation failed"
}

# ── Step 3: Ensure ~/.local/bin is on PATH ────────────────
ensure_user_local_bin_on_path() {
    local target="$HOME/.local/bin"
    mkdir -p "$target"
    export PATH="$target:$PATH"

    # shellcheck disable=SC2016
    local path_line='export PATH="$HOME/.local/bin:$PATH"'
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$rc" ]] && ! grep -q ".local/bin" "$rc"; then
            echo "$path_line" >> "$rc"
        fi
    done
}

# ── Run ──────────────────────────────────────────────────
# __SELF_CONTAINED__ — build.sh inserts resource extraction here
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ensure_uv
ensure_python
ensure_user_local_bin_on_path
exec "$UV_CMD" run "$SCRIPT_DIR/install.py" "$@"
