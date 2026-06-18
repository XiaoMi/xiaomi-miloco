#!/usr/bin/env bash
# install-hermes.sh —— 一键把 miloco 装到 Hermes Agent。
#
# 干 7 件事：
#   1. 前置检查（hermes、miloco-cli、python、$MILOCO_HOME、$MILOCO_HOME/config.json）
#   2. 跑 scripts/sync-skills.py 生成 16 个 skill，复制到 ~/.hermes/skills/
#   3. 复制 miloco 插件到 ~/.hermes/plugins/miloco/，复制 adapter 到同目录
#   4. 自动 patch $MILOCO_HOME/config.json 的 agent 段（webhook_url + auth_bearer，备份原文件）
#   5. 自动给 ~/.hermes/.env 补 API_SERVER_KEY（如缺失则生成；存在则复用）
#   6. 停掉旧 adapter（按 pid 文件），nohup 启新 adapter，PID 写到 ~/.hermes/miloco-adapter.pid
#   7. 打印终态：PID / 日志路径 / 后续唯一要做的步骤
#
# 幂等：再跑一次不会破坏现有配置，会重启 adapter 保留同一 Bearer。
# 还原：$MILOCO_HOME/config.json.bak-* 是 patch 前的备份，~/.hermes/.env 自行删 API_SERVER_KEY 即可。
#
# 高级/手动安装请用 scripts/install.sh（不做 patch、不启 adapter）。
# adapter 启停 / 日志请用 scripts/miloco-adapter.sh。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
ADAPTER_PORT="${ADAPTER_PORT:-18789}"
ADAPTER_LOG="$HERMES_HOME/miloco-adapter.log"
ADAPTER_PID="$HERMES_HOME/miloco-adapter.pid"
HERMES_PLUGINS_DIR="$HERMES_HOME/plugins/miloco"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
info() { echo -e "${G}[✓]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[✗]${N} $*" >&2; }

# 跨平台查占用某端口的进程 PID（Windows netstat / POSIX lsof/ss）
# 注意：函数内对每个 pipeline 加 || true 兜底，因为脚本 set -o pipefail，
# 跨调用方用 $(get_pid_by_port ... | tr ...) 拿值时，local 赋值在 pipeline 返回非零时
# 行为在某些 bash 版本下会触发 set -e 退出，函数内兜底最稳。
get_pid_by_port() {
  local port="$1"
  if command -v netstat >/dev/null 2>&1; then
    netstat -ano 2>/dev/null \
      | grep -E "[:.]$port[[:space:]]" 2>/dev/null \
      | grep LISTENING 2>/dev/null \
      | head -1 | awk '{print $NF}' \
      || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ":$port " 2>/dev/null | head -1 | grep -oP 'pid=\K[0-9]+' | head -1 || true
  fi
}

# 跨平台杀进程：taskkill 优先，POSIX kill -9 兜底
kill_pid() {
  local pid="$1"
  [ -z "$pid" ] && return 0
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //PID "$pid" //F >/dev/null 2>&1 || true
  else
    kill -9 "$pid" 2>/dev/null || true
  fi
}

# 杀 adapter 的两个兜底：先按 PID 杀（taskkill），再按端口反查 Windows PID 杀
# 因为 Git Bash 的 $! 在 Windows 下不一定是 Windows native PID
kill_adapter() {
  local pid="$1" port="$2"
  kill_pid "$pid"
  sleep 1
  if [ -n "$port" ]; then
    # 注意：pipeline + set -o pipefail 会让空匹配返回 1 触发 set -e，
    # 用 || echo "" 兜底
    local p
    p="$(get_pid_by_port "$port" | tr -d '\r\n ' || echo '')"
    if [ -n "$p" ] && [ "$p" != "$pid" ]; then
      warn "端口 $port 还被 Windows PID=$p 占着，taskkill 兜底"
      kill_pid "$p"
    fi
  fi
}

# --- 1. 前置检查 ---
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 \
  || { err "找不到 python，请先装 python"; exit 1; }
PYTHON="$(command -v python3 || command -v python)"

if ! command -v miloco-cli >/dev/null 2>&1; then
  err "找不到 miloco-cli，请先装好 miloco 后端并确认 miloco-cli 在 PATH"; exit 1
fi
if [ ! -d "$HERMES_HOME" ]; then
  err "找不到 Hermes 目录 $HERMES_HOME，请先装 Hermes Agent"; exit 1
fi
if [ ! -f "$MILOCO_HOME/config.json" ]; then
  err "找不到 $MILOCO_HOME/config.json，请确认 MILOCO_HOME 正确（或 export MILOCO_HOME=...）"; exit 1
fi

# --- 2. 拿/复用 Bearer ---
# 优先级：.env 已有的 API_SERVER_KEY > 旧 adapter pid 存在则重新生成 > 新生成
if [ -f "$HERMES_HOME/.env" ] && grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
  BEARER="$(grep '^API_SERVER_KEY=' "$HERMES_HOME/.env" | head -1 | cut -d= -f2-)"
  info "复用 .env 已有的 API_SERVER_KEY（${BEARER:0:8}...）"
else
  BEARER="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  info "新生成 adapter Bearer: ${BEARER:0:8}..."
fi

# --- 3. 同步 skills ---
info "生成并复制 16 个 miloco-* skill → $HERMES_HOME/skills/"
"$PYTHON" "$HERE/scripts/sync-skills.py"
mkdir -p "$HERMES_HOME/skills"
cp -r "$HERE/skills"/miloco-* "$HERMES_HOME/skills/"

# --- 4. 复制插件 + adapter ---
mkdir -p "$HERMES_PLUGINS_DIR"
info "复制 Hermes 插件 → $HERMES_PLUGINS_DIR/miloco-plugin/"
rm -rf "$HERMES_PLUGINS_DIR/miloco-plugin"
cp -r "$HERE/miloco-plugin" "$HERMES_PLUGINS_DIR/miloco-plugin"
info "复制 adapter → $HERMES_PLUGINS_DIR/adapter/"
rm -rf "$HERMES_PLUGINS_DIR/adapter"
cp -r "$HERE/adapter" "$HERMES_PLUGINS_DIR/adapter"
# 清 pycache
find "$HERMES_PLUGINS_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# --- 5. patch $MILOCO_HOME/config.json ---
info "patch $MILOCO_HOME/config.json 的 agent 段..."
TS="$(date +%Y%m%d-%H%M%S)"
cp "$MILOCO_HOME/config.json" "$MILOCO_HOME/config.json.bak-$TS"
"$PYTHON" - "$MILOCO_HOME" "$ADAPTER_PORT" "$BEARER" <<'PY'
import json, sys
home, port, bearer = sys.argv[1], sys.argv[2], sys.argv[3]
p = f"{home}/config.json"
cfg = json.load(open(p, encoding="utf-8"))
cfg.setdefault("agent", {})
cfg["agent"]["webhook_url"] = f"http://127.0.0.1:{port}/miloco/webhook"
cfg["agent"]["auth_bearer"] = bearer
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"  webhook_url = http://127.0.0.1:{port}/miloco/webhook")
print(f"  auth_bearer = {bearer[:8]}...")
PY

# --- 6. patch ~/.hermes/.env（仅当缺失时追加）---
info "确保 $HERMES_HOME/.env 有 API_SERVER_KEY..."
touch "$HERMES_HOME/.env"
chmod 600 "$HERMES_HOME/.env"
if ! grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
  echo "API_SERVER_KEY=$BEARER" >> "$HERMES_HOME/.env"
  info "已追加 API_SERVER_KEY 到 .env"
else
  warn ".env 已有 API_SERVER_KEY，保持原值"
fi

# --- 7. 重启 adapter ---
# 停旧的（Git Bash $! 在 Windows 下 ≠ Windows native PID，按端口反查兜底）
if [ -f "$ADAPTER_PID" ]; then
  OLD_PID="$(cat "$ADAPTER_PID" 2>/dev/null || echo '')"
  if [ -n "$OLD_PID" ] || [ -n "$ADAPTER_PORT" ]; then
    warn "旧 adapter 进程在跑，先停掉（按 PID + 按端口双兜底）"
    kill_adapter "$OLD_PID" "$ADAPTER_PORT"
  fi
  rm -f "$ADAPTER_PID"
fi

# 启新的
info "启动 adapter 进程（端口 $ADAPTER_PORT）..."
( cd "$HERMES_PLUGINS_DIR" \
  && PYTHONUTF8=1 \
     ADAPTER_AUTH_BEARER="$BEARER" \
     HERMES_API_URL="http://127.0.0.1:8642" \
     HERMES_API_KEY="$BEARER" \
     ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}" \
     ADAPTER_PORT="$ADAPTER_PORT" \
     nohup "$PYTHON" -m adapter \
       > "$ADAPTER_LOG" 2>&1 & echo $! > "$ADAPTER_PID" )

sleep 2
# 关键：按端口反查 Windows native PID 覆盖写入 pid 文件（Git Bash $! 在 Windows 不可靠）
WIN_PID="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
if [ -n "$WIN_PID" ]; then
  echo "$WIN_PID" > "$ADAPTER_PID"
  info "adapter 已起，PID=$WIN_PID（按端口反查）"
else
  err "adapter 启动失败，端口 $ADAPTER_PORT 未监听，看 $ADAPTER_LOG 末尾："
  tail -20 "$ADAPTER_LOG" >&2 || true
  exit 1
fi

# --- 8. 终态 ---
cat <<EOF

============================================================
 ✅ 安装完成（可重复执行，幂等）
============================================================

[后续你只要做这一件事] 重启 Hermes gateway 让插件和新配置生效：
    hermes gateway restart
    （或 hermes gateway stop && hermes gateway start）

[试一下]
    hermes chat -q "把客厅灯打开" -Q

[adapter 状态]
    bash $HERE/scripts/miloco-adapter.sh status    # 看 PID / 端口
    bash $HERE/scripts/miloco-adapter.sh logs      # tail 日志
    bash $HERE/scripts/miloco-adapter.sh restart   # 重启
    bash $HERE/scripts/miloco-adapter.sh stop      # 停

[配置文件位置]
    $MILOCO_HOME/config.json   # miloco 后端配置（已 patch）
    $HERMES_HOME/.env          # Hermes 环境（已追加 API_SERVER_KEY）
    $ADAPTER_PID               # adapter PID
    $ADAPTER_LOG               # adapter 日志

[想还原]
    $MILOCO_HOME/config.json.bak-$TS  是 patch 前的备份
    $HERMES_HOME/.env 里去掉 API_SERVER_KEY 即可
    卸插件：rm -rf $HERMES_PLUGINS_DIR $HERMES_HOME/skills/miloco-*

[详细文档] $HERE/README.md
============================================================
EOF
