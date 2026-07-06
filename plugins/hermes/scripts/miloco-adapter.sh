#!/usr/bin/env bash
# miloco-adapter.sh —— miloco 后台进程生命周期管理（适配器收敛到 backend）。
#
# 架构 #1+#2 后，入站适配器已从独立 aiohttp 进程收敛到 miloco backend 的
# AgentPlatformAdapter。本脚本不再管理独立的 "adapter" 进程，而是管理 supervisord
# 下的 miloco-backend 程序（backend 启停 / 健康状况 / 日志等）。
#
# 子命令：
#   start     确保 supervisord + miloco-backend 在跑
#   stop      停 miloco-backend / supervisord
#   restart   stop + start
#   status    查看 supervisord 状态 / miloco-backend 进程 / 端口
#   logs      tail -f 日志（miloco backend + 旧 adapter 日志合并）
#   env       显当前生效的环境变量（从 .env 读）
#
# 版本变迁：
#   v1      独立 aiohttp adapter 进程（python -m adapter）
#   v2      适配器收敛到 backend AgentPlatformAdapter（本版本）

set -euo pipefail

export LANG=C.UTF-8 LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
ADAPTER_PORT="${ADAPTER_PORT:-18789}"
ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_LOG="$HERMES_HOME/miloco-adapter.log"
ADAPTER_PID="$HERMES_HOME/miloco-adapter.pid"
SUPERVISORD_CONF="$MILOCO_HOME/supervisord.conf"
SUPERVISORD_SOCK="$MILOCO_HOME/supervisor.sock"
BACKEND_PROGRAM="miloco-backend"

IS_MACOS=0
[ "$(uname -s)" = "Darwin" ] && IS_MACOS=1

# 旧架构残留清理
LAUNCHD_LABEL="com.xiaomi.miloco.hermes.adapter"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
info() { echo -e "${G}[✓]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[✗]${N} $*" >&2; }

# -- supervisorctl wrapper ---------------------------------------------------

supervisorctl() {
  local cmd="supervisorctl"
  if [ -S "$SUPERVISORD_SOCK" ]; then
    SKIP_PLUGIN_CHECK=0 command "$cmd" -c "$SUPERVISORD_CONF" "$@" 2>/dev/null || true
  else
    return 1
  fi
}

_sv_running() {
  [ -S "$SUPERVISORD_SOCK" ] && pgrep -f "supervisord.*$SUPERVISORD_CONF" >/dev/null 2>&1
}

_backend_running() {
  local st; st="$(supervisorctl status "$BACKEND_PROGRAM" 2>/dev/null || echo "")"
  [[ "$st" == *"RUNNING"* ]]
}

_backend_ok() {
  local st; st="$(supervisorctl status "$BACKEND_PROGRAM" 2>/dev/null || echo "")"
  [[ "$st" == *"RUNNING"* || "$st" == *"STARTING"* || "$st" == *"BACKOFF"* ]]
}

# -- 旧架构清理 --------------------------------------------------------------

_cleanup_old_launchd() {
  # 从旧安装（独立 aiohttp adapter）清掉 launchd job + plist
  if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
    warn "清理旧 launchd job: $LAUNCHD_LABEL"
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
  fi
  # 杀旧端口占用
  local port_pid
  port_pid="$(lsof -nP -iTCP:"$ADAPTER_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [ -n "$port_pid" ] && [ "$port_pid" != "$(cat "$ADAPTER_PID" 2>/dev/null || echo '')" ]; then
    warn "清理旧 adapter 端口占用 PID=$port_pid"
    kill "$port_pid" 2>/dev/null || true
  fi
  if [ -f "$LAUNCHD_PLIST" ]; then rm -f "$LAUNCHD_PLIST"; fi
  if [ -f "$ADAPTER_PID" ]; then rm -f "$ADAPTER_PID"; fi
}

# -- load_env ----------------------------------------------------------------

load_env() {
  if [ ! -f "$HERMES_HOME/.env" ]; then return 0; fi
  while IFS='=' read -r k v; do
    case "$k" in
      ''|\#*) continue ;;
      API_SERVER_KEY|HERMES_API_URL|HERMES_API_KEY|ADAPTER_AUTH_BEARER|ADAPTER_PORT|ADAPTER_HOST)
        export "$k=$v" ;;
    esac
  done < "$HERMES_HOME/.env"
}

# -- cmd_start ---------------------------------------------------------------

cmd_start() {
  load_env
  _cleanup_old_launchd

  # 检查 auto_start 是否被禁用（用于跳过 install-hermes.sh 内的启动）
  if [ "${MILOCO_NO_AUTOSTART:-0}" -eq 1 ]; then
    info "auto_start 已禁用，跳过"
    return 0
  fi

  # 确保 supervisord 在跑
  if ! _sv_running; then
    info "启动 supervisord ..."
    supervisord -c "$SUPERVISORD_CONF" 2>/dev/null || true
    local i; for i in $(seq 1 10); do
      sleep 0.5
      if _sv_running; then break; fi
    done
    if ! _sv_running; then
      err "supervisord 启动失败"; exit 1
    fi
  fi

  # 启动 miloco-backend
  if _backend_ok; then
    if _backend_running; then
      info "miloco-backend 已在跑"
    else
      info "miloco-backend 状态=STARTING/BACKOFF，等待就绪..."
      local i; for i in $(seq 1 30); do
        sleep 1
        if _backend_running; then break; fi
      done
    fi
  else
    info "启动 miloco-backend ..."
    supervisorctl start "$BACKEND_PROGRAM"
    sleep 2
  fi

  if _backend_running; then
    info "adapter (miloco-backend) 已就绪"
  else
    err "miloco-backend 启动失败，看 supervisorctl status"
    supervisorctl status 2>/dev/null | tail -5 || true
    exit 1
  fi

  # 检查是否真的在监听 webhook 端口
  local port_pid
  port_pid="$(lsof -nP -iTCP:"$ADAPTER_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [ -n "$port_pid" ]; then
    info "webhook 端口 ${ADAPTER_PORT} 由 PID=${port_pid} 监听"
  else
    warn "port ${ADAPTER_PORT} 暂无进程监听（backend 可能未启 webhook）"
  fi
}

# -- cmd_stop ----------------------------------------------------------------

cmd_stop() {
  _cleanup_old_launchd

  if _sv_running; then
    info "停 miloco-backend ..."
    supervisorctl stop "$BACKEND_PROGRAM" 2>/dev/null || true
    sleep 1
    info "停 supervisord ..."
    supervisorctl shutdown 2>/dev/null || true
    sleep 1
  fi

  # 兜底杀
  pkill -f "supervisord.*$SUPERVISORD_CONF" 2>/dev/null || true
  pkill -f "miloco.main" 2>/dev/null || true
  info "已停"
}

# -- cmd_status --------------------------------------------------------------

cmd_status() {
  if _sv_running; then
    info "supervisord 在跑"
    supervisorctl status 2>/dev/null || warn "supervisorctl status 失败"
  else
    warn "supervisord 未在跑"
  fi

  # webhook 端口
  local port_pid
  port_pid="$(lsof -nP -iTCP:"$ADAPTER_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [ -n "$port_pid" ]; then
    info "webhook 端口 ${ADAPTER_PORT} 监听中 PID=${port_pid}"
  else
    warn "webhook 端口 ${ADAPTER_PORT} 未监听"
  fi

  echo "  MILOCO_HOME:  $MILOCO_HOME"
  echo "  supervisord:  $SUPERVISORD_CONF"
  echo "  adapter 端口: $ADAPTER_PORT"
}

# -- cmd_logs ----------------------------------------------------------------

cmd_logs() {
  local logs=(
    "$MILOCO_HOME/log/miloco-backend.log"
    "$MILOCO_HOME/log/miloco.log"
    "$MILOCO_HOME/log/supervisord.log"
    "$ADAPTER_LOG"
  )
  local first=""
  for f in "${logs[@]}"; do
    [ -f "$f" ] && { first="$f"; break; }
  done
  if [ -z "$first" ]; then
    warn "没找到日志文件" && return 1
  fi
  info "tail -f $first (其他日志可用 logs_<name>)"
  tail -n 200 -f "$first"
}

# -- cmd_env -----------------------------------------------------------------

cmd_env() {
  load_env
  echo "API_SERVER_KEY=${API_SERVER_KEY:-<unset>}"
  echo "ADAPTER_AUTH_BEARER=${ADAPTER_AUTH_BEARER:-<unset>}"
  echo "HERMES_API_URL=${HERMES_API_URL:-<unset>}"
  echo "HERMES_API_KEY=${HERMES_API_KEY:-<unset>}"
  echo "ADAPTER_PORT=$ADAPTER_PORT"
  echo "ADAPTER_HOST=$ADAPTER_HOST"
  echo "MILOCO_HOME=$MILOCO_HOME"
}

# -- cmd_restart -------------------------------------------------------------

cmd_restart() { cmd_stop; cmd_start; }

# -- 子命令路由 ---------------------------------------------------------------

usage() {
  cat <<EOF
用法: $(basename "$0") {start|stop|restart|status|logs|env}

架构 #1+#2 后适配器收敛到 miloco backend (AgentPlatformAdapter)。
本脚本管理 supervisord 下的 miloco-backend 程序。

默认配置：
  MILOCO_HOME=$MILOCO_HOME
  SUPERVISORD_CONF=$SUPERVISORD_CONF
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
