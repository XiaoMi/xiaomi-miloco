#!/bin/bash
# Xiaomi Miloco macOS Metal Backend Setup Script
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
VENV_DIR="${PROJECT_ROOT}/.venv"
MODELS_DIR="${PROJECT_ROOT}/models"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_ok()   { echo -e "${GREEN}✅ $*${NC}"; }
print_warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
print_err()  { echo -e "${RED}❌ $*${NC}"; }
print_step() { echo -e "\n${GREEN}==> $*${NC}"; }

# ============================================================
# 1. System checks
# ============================================================
print_step "Checking system requirements"

# macOS
if [[ "$(uname)" != "Darwin" ]]; then
    print_err "This script is for macOS only."
    exit 1
fi
print_ok "macOS $(sw_vers -productVersion)"

# Apple Silicon
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
    print_err "Apple Silicon (arm64) required. Current: $ARCH"
    exit 1
fi
print_ok "Apple Silicon ($ARCH)"

# Xcode CLI tools
if ! xcode-select -p &>/dev/null; then
    print_warn "Xcode Command Line Tools not found. Installing..."
    xcode-select --install
    echo "Please re-run this script after installation completes."
    exit 1
fi
print_ok "Xcode Command Line Tools"

# Python 3.12
PYTHON=""
for candidate in python3.12 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver="$($candidate --version 2>&1 | grep -oE '3\.12\.[0-9]+')" || true
        if [[ -n "$ver" ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    print_err "Python 3.12.x required. Install via: brew install python@3.12"
    exit 1
fi
print_ok "$($PYTHON --version)"

# CMake
if ! command -v cmake &>/dev/null; then
    print_warn "cmake not found. Installing via brew..."
    brew install cmake
fi
print_ok "cmake $(cmake --version | head -1 | awk '{print $3}')"

# Node.js (for frontend)
if ! command -v node &>/dev/null; then
    print_warn "Node.js not found. Install via: brew install node"
    FRONTEND_OK=false
else
    print_ok "node $(node --version)"
    FRONTEND_OK=true
fi

# ============================================================
# 2. Python virtual environment
# ============================================================
print_step "Setting up Python virtual environment"

if [[ -d "$VENV_DIR" ]]; then
    print_ok "Virtual environment already exists at $VENV_DIR"
else
    $PYTHON -m venv "$VENV_DIR"
    print_ok "Created virtual environment"
fi

source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q 2>/dev/null

# ============================================================
# 3. Install Python dependencies
# ============================================================
print_step "Installing Python dependencies"

pip install -e "${PROJECT_ROOT}/miloco_ai_engine" -q 2>/dev/null
print_ok "miloco_ai_engine"

pip install -e "${PROJECT_ROOT}/miot_kit" -q 2>/dev/null
print_ok "miot_kit"

pip install -e "${PROJECT_ROOT}/miloco_server" -q 2>/dev/null
print_ok "miloco_server"

# ============================================================
# 4. Build AI engine with Metal
# ============================================================
print_step "Building AI engine (Metal backend)"

bash "${SCRIPT_DIR}/ai_engine_metal_build.sh"
print_ok "Metal backend built"

# ============================================================
# 5. Download models
# ============================================================
print_step "Downloading models"

MIMO_DIR="${MODELS_DIR}/MiMo-VL-Miloco-7B"
QWEN_DIR="${MODELS_DIR}/Qwen3-8B"
mkdir -p "$MIMO_DIR" "$QWEN_DIR"

# MiMo-VL main model
MIMO_MODEL="${MIMO_DIR}/MiMo-VL-Miloco-7B_Q4_0.gguf"
if [[ -f "$MIMO_MODEL" ]]; then
    print_ok "MiMo-VL-Miloco-7B_Q4_0.gguf already exists"
else
    echo "Downloading MiMo-VL-Miloco-7B_Q4_0.gguf (~4.5GB)..."
    curl -L --retry 3 -o "$MIMO_MODEL" \
        "https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/master/MiMo-VL-Miloco-7B_Q4_0.gguf"
    print_ok "MiMo-VL-Miloco-7B_Q4_0.gguf"
fi

# MiMo-VL mmproj
MIMO_MMPROJ="${MIMO_DIR}/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
if [[ -f "$MIMO_MMPROJ" ]]; then
    print_ok "mmproj-MiMo-VL-Miloco-7B_BF16.gguf already exists"
else
    echo "Downloading mmproj-MiMo-VL-Miloco-7B_BF16.gguf (~1.3GB)..."
    curl -L --retry 3 -o "$MIMO_MMPROJ" \
        "https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/master/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
    print_ok "mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
fi

# Qwen3-8B
QWEN_MODEL="${QWEN_DIR}/Qwen3-8B-Q4_K_M.gguf"
if [[ -f "$QWEN_MODEL" ]]; then
    print_ok "Qwen3-8B-Q4_K_M.gguf already exists"
else
    echo "Downloading Qwen3-8B-Q4_K_M.gguf (~5GB)..."
    curl -L --retry 3 -o "$QWEN_MODEL" \
        "https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/Qwen3-8B-Q4_K_M.gguf"
    print_ok "Qwen3-8B-Q4_K_M.gguf"
fi

# ============================================================
# 6. Update config for Metal
# ============================================================
print_step "Configuring for Metal"

CONFIG_FILE="${PROJECT_ROOT}/config/ai_engine_config.yaml"
# Update model paths (from /models/ to models/) and device (cuda to metal)
if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' \
        -e 's|model_path: "/models/|model_path: "models/|g' \
        -e 's|mmproj_path: "/models/|mmproj_path: "models/|g' \
        -e 's|device: "cuda"|device: "metal"|g' \
        "$CONFIG_FILE"
fi

# Update server config: 0.0.0.0 -> 127.0.0.1 for local_model host
SERVER_CONFIG="${PROJECT_ROOT}/config/server_config.yaml"
if grep -q 'host: "0.0.0.0"' "$SERVER_CONFIG" 2>/dev/null; then
    sed -i '' '/^local_model:/,/^[^ ]/s/host: "0.0.0.0"/host: "127.0.0.1"/' "$SERVER_CONFIG"
fi

print_ok "Config updated (device: metal, host: 127.0.0.1)"

# ============================================================
# 7. Build frontend
# ============================================================
if [[ "$FRONTEND_OK" == "true" ]]; then
    print_step "Building frontend"

    cd "${PROJECT_ROOT}/web_ui"
    npm install --include=dev -q 2>/dev/null
    ./node_modules/.bin/vite build 2>/dev/null
    mkdir -p "${PROJECT_ROOT}/miloco_server/static"
    cp -r dist/* "${PROJECT_ROOT}/miloco_server/static/"
    cd "$PROJECT_ROOT"
    print_ok "Frontend built"
fi

# ============================================================
# Done
# ============================================================
echo ""
echo "============================================================"
echo -e "${GREEN}  ✅ macOS Metal setup complete!${NC}"
echo "============================================================"
echo ""
echo "To start the services:"
echo ""
echo "  source .venv/bin/activate"
echo ""
echo "  # Terminal 1: AI Engine"
echo "  python scripts/start_ai_engine.py"
echo ""
echo "  # Terminal 2: Backend"
echo "  python scripts/start_server.py"
echo ""
echo "  # Terminal 3: Frontend"
echo "  cd web_ui && npx vite"
echo ""
echo "Access:"
echo "  Frontend:  https://127.0.0.1:5173"
echo "  Backend:   https://127.0.0.1:8000/docs"
echo "  AI Engine: http://127.0.0.1:8001/docs"
echo "============================================================"
