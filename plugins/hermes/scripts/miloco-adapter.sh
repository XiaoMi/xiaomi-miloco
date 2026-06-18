#!/usr/bin/env bash
# miloco-adapter.sh —— 入站 adapter 进程生命周期管理。
#
# 子命令：
#   start     后台启 adapter
#   stop      按 pid 文件停 adapter
#   restart   stop + start
#   status    显 PID / 端口 / 健康
#   logs      tail -f 日志
#   env       显当前生效的环境变量（从 .env 读）
#
# 装/启参数来自 install-hermes.sh 写的 ~/.hermes/miloco-adapter.{pid,log} 和 .env。
# 如果 ~/.hermes/miloco-adapter.pid 不存在（不是 install-hermes.sh 装的），
# start 会从 .env 拿环境变量 + HERMES_PLUGINS_DIR/adapter 启动。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
ADAPTER_PORT="${ADAPTER_PORT:-18789}"
ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_LOG="$HERMES_HOME/miloco-adapter.log"
ADAPTER_PID="$HERMES_HOME/miloco-adapter.pid"
HERMES_PLUGINS_DIR="$HERMES_HOME/plugins/miloco"
ADAPTER_DIR="$HERMES_PLUGINS_DIR/adapter"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
info() { echo -e "${G}[✓]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[✗]${N} $*" >&2; }

# 跨平台查占用某端口的进程 PID（pipeline + || true 兜底，与 install-hermes.sh 同源）
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

# 跨平台杀进程
kill_pid() {
  local pid="$1"
  [ -z "$pid" ] && return 0
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //PID "$pid" //F >/dev/null 2>&1 || true
  else
    kill -9 "$pid" 2>/dev/null || true
  fi
}

# 杀 adapter：按 PID + 按端口双兜底
kill_adapter() {
  local pid="$1" port="$2"
  kill_pid "$pid"
  sleep 1
  if [ -n "$port" ]; then
    local p; p="$(get_pid_by_port "$port" | tr -d '\r\n ')"
    if [ -n "$p" ] && [ "$p" != "$pid" ]; then
      warn "端口 $port 还被 PID=$p 占着，taskkill 兜底"
      kill_pid "$p"
    fi
  fi
}

# 从 ~/.hermes/.env 抽环境变量（不 source，避免污染 shell）
load_env() {
  if [ ! -f "$HERMES_HOME/.env" ]; then return 0; fi
  # 只抽 MILOCO_ADAPTER_* / HERMES_API_* / API_SERVER_KEY / ADAPTER_*
  while IFS='=' read -r k v; do
    case "$k" in
      ''|\#*) continue ;;
      API_SERVER_KEY|HERMES_API_URL|HERMES_API_KEY|ADAPTER_AUTH_BEARER|ADAPTER_PORT|ADAPTER_HOST)
        export "$k=$v" ;;
    esac
  done < "$HERMES_HOME/.env"
}

cmd_start() {
  load_env
  # Bearer 优先级：ADAPTER_AUTH_BEARER env > API_SERVER_KEY（adapter 默认与之共用）
  local bearer="${ADAPTER_AUTH_BEARER:-${API_SERVER_KEY:-}}"
  if [ -z "$bearer" ]; then
    err "找不到 Bearer，.env 里需要有 API_SERVER_KEY 或 ADAPTER_AUTH_BEARER"
    err "（先跑 install-hermes.sh）"
    exit 1
  fi
  if [ ! -d "$ADAPTER_DIR" ]; then
    err "找不到 $ADAPTER_DIR，先跑 install-hermes.sh"; exit 1
  fi

  # 已在跑就跳过
  if [ -f "$ADAPTER_PID" ] && kill -0 "$(cat "$ADAPTER_PID" 2>/dev/null)" 2>/dev/null; then
    warn "adapter 已在跑，PID=$(cat "$ADAPTER_PID")"
    return 0
  fi
  # stale pid 文件（旧进程已死但 pid 文件还在），清掉
  [ -f "$ADAPTER_PID" ] && rm -f "$ADAPTER_PID"

  info "启动 adapter（端口 $ADAPTER_PORT）..."
  ( cd "$HERMES_PLUGINS_DIR" \
    && PYTHONUTF8=1 \
       ADAPTER_AUTH_BEARER="$bearer" \
       HERMES_API_URL="${HERMES_API_URL:-http://127.0.0.1:8642}" \
       HERMES_API_KEY="${HERMES_API_KEY:-$bearer}" \
       ADAPTER_HOST="$ADAPTER_HOST" \
       ADAPTER_PORT="$ADAPTER_PORT" \
       nohup "$(command -v python3 || command -v python)" -m adapter \
         > "$ADAPTER_LOG" 2>&1 & echo $! > "$ADAPTER_PID" )

  sleep 2
  # 关键：按端口反查真实 Windows native PID 覆盖写入 pid 文件（|| echo "" 兜底 set -e）
  local pid
  pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
  if [ -n "$pid" ]; then
    echo "$pid" > "$ADAPTER_PID"
    info "adapter 已起，PID=$pid（按端口反查），日志=$ADAPTER_LOG"
  else
    err "adapter 启动失败，端口 $ADAPTER_PORT 未监听，看 $ADAPTER_LOG 末尾："
    tail -20 "$ADAPTER_LOG" >&2 || true
    exit 1
  fi
}

cmd_stop() {
  if [ ! -f "$ADAPTER_PID" ]; then
    warn "pid 文件不存在，adapter 可能没在跑"; return 0
  fi
  local pid; pid="$(cat "$ADAPTER_PID" 2>/dev/null || echo '')"
  # 检查端口是否还有占用（grep 失败时 $() 走 || echo "" 兜底，避免 set -e 退出）
  local port_pid
  port_pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
  if [ -z "$pid" ] && [ -z "$port_pid" ]; then
    warn "adapter 进程 $pid 不在跑，清掉 pid 文件"
    rm -f "$ADAPTER_PID"
    return 0
  fi
  info "停 adapter PID=$pid（按 PID + 按端口双兜底）"
  kill_adapter "$pid" "$ADAPTER_PORT"
  rm -f "$ADAPTER_PID"
  info "已停"
}

cmd_status() {
  local pid=""
  if [ -f "$ADAPTER_PID" ]; then pid="$(cat "$ADAPTER_PID" 2>/dev/null || echo '')"; fi
  # 优先以端口反查为准（Git Bash PID 不可靠）；用 || echo "" 兜底 set -e
  local port_pid
  port_pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
  if [ -n "$port_pid" ]; then
    info "adapter 在跑，端口 PID=$port_pid（pid 文件记录的=$pid）"
    [ "$port_pid" != "$pid" ] && [ -n "$port_pid" ] && echo "$port_pid" > "$ADAPTER_PID"
  elif [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    info "adapter 在跑，PID=$pid"
  else
    warn "adapter 未在跑"
    [ -f "$ADAPTER_PID" ] && warn "（stale pid 文件：$pid）"
  fi
  # 健康检查
  local url="http://127.0.0.1:${ADAPTER_PORT}/health"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      info "health OK: $url"
    else
      warn "health 检查失败: $url"
    fi
  fi
  echo "  pid 文件: $ADAPTER_PID"
  echo "  日志文件: $ADAPTER_LOG"
  echo "  监听端口: $ADAPTER_PORT"
}

cmd_logs() { tail -n 200 -f "$ADAPTER_LOG"; }

cmd_env() {
  load_env
  echo "API_SERVER_KEY=${API_SERVER_KEY:-<unset>}"
  echo "ADAPTER_AUTH_BEARER=${ADAPTER_AUTH_BEARER:-<unset>}"
  echo "HERMES_API_URL=${HERMES_API_URL:-<unset>}"
  echo "HERMES_API_KEY=${HERMES_API_KEY:-<unset>}"
  echo "ADAPTER_PORT=$ADAPTER_PORT"
  echo "ADAPTER_HOST=$ADAPTER_HOST"
}

cmd_restart() { cmd_stop; cmd_start; }

usage() {
  cat <<EOF
用法: $(basename "$0") {start|stop|restart|status|logs|env}

默认配置文件：
  HERMES_HOME=$HERMES_HOME
  MILOCO_HOME=$MILOCO_HOME
  ADAPTER_PORT=$ADAPTER_PORT
EOF
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  env)     cmd_env ;;
  -h|--help|help|"") usage ;;
  *) err "未知子命令: $1"; usage; exit 1 ;;
esac
