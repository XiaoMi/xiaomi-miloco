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
#   --packages <list>    miloco-miot,miloco,miloco-cli,openclaw,web,hermes,launcher 任意子集
#                        launcher = macOS 签名启动器（部署到 darwin 时必需）
#
# 安装组件 (远端, 逗号分隔):
#   --install <list>     miloco | miloco-cli | openclaw | supervisor | launcher
#                        all (默认) | none
#                        miloco 自动带 miloco-miot wheel
#                        launcher = macOS 签名启动器 miloco.app（仅 darwin 生效）
#
# 其他:
#   -h, --help
#
# 默认远端路径: ~/miloco-plugin
# 注：backend 重启由 openclaw gateway restart 自动带起，本脚本不再单独重启 backend。

set -euo pipefail

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; }

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
    all)  INSTALL_LIST="miloco,miloco-cli,openclaw,supervisor,launcher" ;;
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

ssh "$HOST" \
    "REMOTE_PATH='$REMOTE_PATH' BUILD_MODE='$BUILD_MODE' \
     PACKAGES='$PACKAGES' INSTALL_LIST='$INSTALL_LIST' \
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

    # macOS: 落地签名启动器（miloco.app）到 miloco_home，让 backend 作为其子进程
    # 绕过 Local Network Privacy（见 cli service.py 的 launchd 分支）。启动器是 miloco
    # 在 mac 运行的硬依赖，故装 miloco 即隐式带上（也支持显式 --install launcher）。
    if [[ "$(uname -s)" == Darwin ]] && { want miloco || want launcher; }; then
        LAUNCHER_TGZ=$(ls "$DIST"/miloco-launcher-darwin*.tar.gz 2>/dev/null | head -1)
        if [[ -z "$LAUNCHER_TGZ" ]]; then
            echo "[remote] 缺 miloco-launcher-darwin tar（--packages 含 launcher?）" >&2; exit 1
        fi
        MH="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
        echo "[remote] 安装 macOS 签名启动器 → $MH/miloco.app"
        mkdir -p "$MH"
        rm -rf "$MH/miloco.app"
        tar -C "$MH" -xzf "$LAUNCHER_TGZ"          # 保留签名字节（cdhash 稳定）
        chmod +x "$MH/miloco.app/Contents/MacOS/miloco"
        codesign -v "$MH/miloco.app" 2>/dev/null \
            && echo "[remote]   签名校验 OK" \
            || echo "[remote]   WARN: 签名校验失败，LNP 授权可能需重新打勾"
    fi

    # macOS 用 launchd、不用 supervisord（见 cli service.py），darwin 跳过安装
    # （与 install.py 的 darwin 分支一致，避免装个用不上的 supervisor）。
    if want supervisor && [[ "$(uname -s)" != Darwin ]]; then
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
