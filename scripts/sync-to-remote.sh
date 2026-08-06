#!/usr/bin/env bash
# 把项目同步到部署机并按需安装。
#
# 用法:
#   scripts/sync-to-remote.sh [选项] user@host [remote_path]
#
# 构建模式 (互斥, 默认 --remote-build):
#   --remote-build       rsync 源码 (exclude dist/), 远端跑 scripts/build.sh
#   --local-build        本地跑 scripts/build.sh, 仅 rsync dist/ + scripts/
#   --install-only       不 rsync 不构建, 仅在远端触发安装 (复用远端已有 dist/)
#
# 构建包 (传给 build.sh, 不指定则全量):
#   --packages <list>    miloco-miot,miloco,miloco-cli,openclaw 任意子集
#
# 安装组件 (远端, 逗号分隔):
#   --install <list>     miloco | miloco-cli | openclaw | supervisor
#                        all (默认) | none
#                        miloco 自动带 miloco-miot wheel
#
# 其他:
#   -h, --help
#
# 默认远端路径: ~/miloco-plugin
# 注：backend 重启由 openclaw gateway restart 自动带起，本脚本不再单独重启 backend。
# 注：感知 ONNX 模型（~78MB）不参与 rsync，远端 build.sh 自己按 scripts/models.lock.json
#     拉。远端出网受限时，本地 export MILOCO_MODELS_BASE_URL=<内网源> 会一并透传过去。

set -euo pipefail

# 从第 2 行打到抬头注释块结束（第一个非 # 行），不写死结束行号：写死的话，往抬头
# 补一条说明就会把 --help 的尾巴无声截掉，而没人会为此跑一次 --help。
usage() { awk 'NR<2{next} !/^#/{exit} {sub(/^#[[:space:]]?/,""); print}' "${BASH_SOURCE[0]}"; }

# ─── 参数解析 ──────────────────────────────────────────────────────────────

BUILD_MODE="remote"
PACKAGES=""
INSTALL_LIST="all"
HOST=""
REMOTE_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote-build) BUILD_MODE="remote"; shift ;;
        --local-build)  BUILD_MODE="local";  shift ;;
        --install-only) BUILD_MODE="none";   shift ;;
        --packages)     PACKAGES="$2";       shift 2 ;;
        --install)      INSTALL_LIST="$2";   shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        --*)            echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
        *)
            if   [[ -z "$HOST" ]];        then HOST="$1"
            elif [[ -z "$REMOTE_PATH" ]]; then REMOTE_PATH="$1"
            else echo "多余参数: $1" >&2; exit 2
            fi
            shift
            ;;
    esac
done

[[ -n "$HOST" ]] || { echo "缺少 user@host 参数" >&2; usage >&2; exit 2; }
REMOTE_PATH="${REMOTE_PATH:-~/miloco-plugin}"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$INSTALL_LIST" in
    all)  INSTALL_LIST="miloco,miloco-cli,openclaw,supervisor" ;;
    none) INSTALL_LIST="" ;;
esac

# ─── 本地构建 ──────────────────────────────────────────────────────────────

if [[ "$BUILD_MODE" == "local" ]]; then
    echo "[sync] 本地构建..."
    local_args=()
    [[ -n "$PACKAGES" ]] && local_args+=(--packages "$PACKAGES")
    "$LOCAL_ROOT/scripts/build.sh" "${local_args[@]}"
fi

# ─── rsync ────────────────────────────────────────────────────────────────

COMMON_EXCLUDES=(
    --exclude '.git/'
    --exclude '.idea/'
    --exclude 'node_modules/'
    --exclude '.venv/'
    --exclude '__pycache__/'
    --exclude '*.pyc'
    --exclude '*.egg-info/'
    --exclude 'plugins/openclaw/skills/'
    --exclude '.pytest_cache/'
    --exclude '.ruff_cache/'
    --exclude '.DS_Store'
    # 感知 ONNX 不在 git 里（scripts/models.lock.json），本地这个目录合法地可能只剩
    # README。--remote-build 走的是 rsync --delete-after：不排除的话，本地空目录会把
    # 远端上一轮下好的 5 个模型删掉，逼远端 build.sh 的 --strict 重拉 ~78MB —— 而远端
    # 往往正是网络最差那台。远端模型自己按 lock 管理（build.sh 会 fetch），不归 rsync。
    --exclude 'backend/miloco/src/miloco/perception/models/'
)

case "$BUILD_MODE" in
    local)
        echo "[sync] -> $HOST:$REMOTE_PATH (dist/ + scripts/)"
        [[ -d "$LOCAL_ROOT/dist" ]] || { echo "本地 dist/ 不存在" >&2; exit 1; }
        ssh "$HOST" "mkdir -p $REMOTE_PATH/dist $REMOTE_PATH/scripts"
        rsync -az --delete-after --info=progress2 "${COMMON_EXCLUDES[@]}" \
            "$LOCAL_ROOT/dist/" "$HOST:$REMOTE_PATH/dist/"
        rsync -az --delete-after --info=progress2 "${COMMON_EXCLUDES[@]}" \
            "$LOCAL_ROOT/scripts/" "$HOST:$REMOTE_PATH/scripts/"
        ;;
    remote)
        echo "[sync] -> $HOST:$REMOTE_PATH (全量, exclude dist/)"
        rsync -az --delete-after --info=progress2 \
            "${COMMON_EXCLUDES[@]}" --exclude 'dist/' \
            "$LOCAL_ROOT/" "$HOST:$REMOTE_PATH/"
        ;;
    none)
        echo "[sync] --install-only, 跳过 rsync"
        ;;
esac

# ─── 远端构建 + 安装 ──────────────────────────────────────────────────────

if [[ "$BUILD_MODE" == "none" && -z "$INSTALL_LIST" ]]; then
    echo "[sync] 无构建无安装, 结束"
    exit 0
fi

echo "[sync] 远端: build=$BUILD_MODE install=[${INSTALL_LIST:-none}]"

# MILOCO_MODELS_BASE_URL 一并透传：远端 build.sh 要自己按 lock 拉 ~78MB 模型，
# 而远端常在内网 / 出口受限，换源逃生口不传过去就等于没有。空值也照传，远端
# fetch_models.py 对空串按"未设置"处理（见其 _sources）。
ssh "$HOST" \
    "REMOTE_PATH='$REMOTE_PATH' BUILD_MODE='$BUILD_MODE' \
     PACKAGES='$PACKAGES' INSTALL_LIST='$INSTALL_LIST' \
     MILOCO_MODELS_BASE_URL='${MILOCO_MODELS_BASE_URL:-}' \
     bash -s" <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
# 从变量展开来的 ~ 不会被 shell 二次展开；显式替换为 $HOME。
REMOTE_PATH="${REMOTE_PATH/#\~/$HOME}"
cd "$REMOTE_PATH"

want() { [[ ",$INSTALL_LIST," == *",$1,"* ]]; }

# ── 远端构建 ─────────────────────────────────
if [[ "$BUILD_MODE" == "remote" ]]; then
    echo "[remote] scripts/build.sh （默认 clean）"
    build_args=()
    [[ -n "$PACKAGES" ]] && build_args+=(--packages "$PACKAGES")
    bash scripts/build.sh "${build_args[@]}"
fi

DIST="$REMOTE_PATH/dist"

# ── backend 源码 venv sync (新依赖入库) ─────
# wheel 安装走 uv tool install, 源码模式跑 `python -m miloco.main` 时
# venv 需独立 uv sync 才能拉新依赖 (如 paho-mqtt 这种由 commit 引入的)。
if [[ -f "$REMOTE_PATH/backend/pyproject.toml" ]]; then
    echo "[remote] backend uv sync"
    (cd "$REMOTE_PATH/backend" && uv sync)
fi

# ── 平台 wheel tag ──────────────────────────
detect_wheel_tag() {
    local arch os
    arch=$(uname -m)
    os=$(uname -s | tr '[:upper:]' '[:lower:]')
    case "$os/$arch" in
        linux/x86_64)              echo manylinux_2_28_x86_64 ;;
        linux/aarch64|linux/arm64) echo manylinux_2_28_aarch64 ;;
        darwin/arm64)              echo macosx_11_0_arm64 ;;
        darwin/x86_64)             echo macosx_10_9_x86_64 ;;
        *) echo "" ;;
    esac
}

# ── 安装 ────────────────────────────────────
if [[ -n "$INSTALL_LIST" ]]; then
    [[ -d "$DIST" ]] || { echo "[remote] dist/ 不存在: $DIST" >&2; exit 1; }

    if want miloco; then
        TAG=$(detect_wheel_tag)
        [[ -n "$TAG" ]] || { echo "[remote] 不支持的平台: $(uname -s)/$(uname -m)" >&2; exit 1; }
        MIOT_WHEEL=$(ls "$DIST"/miloco_miot-*"$TAG"*.whl 2>/dev/null | head -1)
        MILOCO_WHEEL=$(ls "$DIST"/miloco-*.whl 2>/dev/null \
            | grep -Ev 'miloco_miot|miloco_cli' | head -1)
        [[ -n "$MIOT_WHEEL"   ]] || { echo "[remote] 缺 miloco_miot wheel ($TAG)" >&2; exit 1; }
        [[ -n "$MILOCO_WHEEL" ]] || { echo "[remote] 缺 miloco wheel" >&2; exit 1; }
        echo "[remote] uv tool install miloco --force (with $(basename "$MIOT_WHEEL"))"
        uv tool install "$MILOCO_WHEEL" --with "$MIOT_WHEEL" --force
    fi

    if want miloco-cli; then
        CLI_WHEEL=$(ls "$DIST"/miloco_cli-*.whl 2>/dev/null | head -1)
        [[ -n "$CLI_WHEEL" ]] || { echo "[remote] 缺 miloco_cli wheel" >&2; exit 1; }
        echo "[remote] uv tool install miloco-cli --force"
        uv tool install "$CLI_WHEEL" --force
    fi

    if want supervisor; then
        echo "[remote] uv tool install supervisor --force"
        uv tool install supervisor --force
    fi

    if want openclaw; then
        TGZ=$(ls "$DIST"/miloco-openclaw-plugin-*.tgz 2>/dev/null | head -1)
        [[ -n "$TGZ" ]] || { echo "[remote] 缺 openclaw plugin tgz" >&2; exit 1; }
        echo "[remote] openclaw plugins install --force $(basename "$TGZ")"
        openclaw plugins install --force "$TGZ"
        echo "[remote] register-skill-tools.sh （把 SKILL.md tool 加进 tools.alsoAllow）"
        bash "$REMOTE_PATH/scripts/register-skill-tools.sh"
        echo "[remote] openclaw plugins registry --refresh （清 plugin tool registry stale cache）"
        # 不清这层 cache 会导致新 plugin 部分 tool 报 'plugin tool runtime missing'，
        # 实证：normalize_time / terminate_current 跨 4 个 gateway 进程稳定复现，
        # 直到 refresh + gateway restart 才修复。详见 commit 说明。
        openclaw plugins registry --refresh
        echo "[remote] openclaw gateway restart"
        openclaw gateway restart
        echo "[remote] 等 30s 让 gateway 完成 plugin 加载 + tool registry 注册"
        sleep 30
    fi
fi

echo "[remote] done"
REMOTE

echo "[sync] 完成 — 远端日志: ssh $HOST 'miloco-cli service logs -f'"
