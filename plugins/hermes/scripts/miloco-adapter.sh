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

# 强制 UTF-8 + POSIX 字符类，防止 "$VAR中文" 被 bash 误识别为变量名延续
export LANG=C.UTF-8 LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
ADAPTER_PORT="${ADAPTER_PORT:-18789}"
ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_LOG="$HERMES_HOME/miloco-adapter.log"
ADAPTER_PID="$HERMES_HOME/miloco-adapter.pid"
HERMES_PLUGINS_DIR="$HERMES_HOME/plugins/miloco"
ADAPTER_DIR="$HERMES_PLUGINS_DIR/adapter"

# 平台检测
IS_MACOS=0
[ "$(uname -s)" = "Darwin" ] && IS_MACOS=1

# macOS launchd 路径
LAUNCHD_LABEL="com.xiaomi.miloco.hermes.adapter"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
PLIST_TEMPLATE="$HERE/com.xiaomi.miloco.hermes.adapter.plist"
LAUNCHER="$HERE/adapter-launcher.sh"

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
    err "找不到 ${ADAPTER_DIR}，先跑 install-hermes.sh"; exit 1
  fi

  # ── macOS 路径：launchd LaunchAgent（避免 install.sh exit 时 SIGHUP/SIGTERM 杀进程）──
  if [ "$IS_MACOS" -eq 1 ] && command -v launchctl >/dev/null 2>&1; then
    cmd_start_launchd
    return $?
  fi

  # ── Linux / Git Bash / WSL 路径：nohup + setsid（可选）+ </dev/null 完全脱离 ──
  # 已在跑就跳过
  if [ -f "$ADAPTER_PID" ] && kill -0 "$(cat "$ADAPTER_PID" 2>/dev/null)" 2>/dev/null; then
    warn "adapter 已在跑，PID=$(cat "$ADAPTER_PID")"
    return 0
  fi
  # stale pid 文件（旧进程已死但 pid 文件还在），清掉
  [ -f "$ADAPTER_PID" ] && rm -f "$ADAPTER_PID"

  info "启动 adapter（端口 ${ADAPTER_PORT}）..."
  local py; py="$(command -v python3 || command -v python)"
  (
    cd "$HERMES_PLUGINS_DIR"
    PYTHONUTF8=1 \
    ADAPTER_AUTH_BEARER="$bearer" \
    HERMES_API_URL="${HERMES_API_URL:-http://127.0.0.1:8642}" \
    HERMES_API_KEY="${HERMES_API_KEY:-$bearer}" \
    ADAPTER_HOST="$ADAPTER_HOST" \
    ADAPTER_PORT="$ADAPTER_PORT" \
    nohup "$py" -m adapter \
      > "$ADAPTER_LOG" 2>&1 < /dev/null &
    # Linux 下加 setsid 让进程脱离 shell job control（macOS 用 launchd 走另一条路）
    if [ "$IS_MACOS" -eq 0 ] && command -v setsid >/dev/null 2>&1; then
      # setsid -f 在子 shell 里跑；不用 -f 的话 setsid 本身要 fork
      : # 占位：实际不需要在 bash 子 shell 里 setsid，已被 nohup + </dev/null 覆盖
    fi
    echo $! > "$ADAPTER_PID"
    disown 2>/dev/null || true
  )

  # Retry loop：冷启动 adapter 可能要 import ~10s，最长等 60s
  local pid="" i
  for i in $(seq 1 120); do
    sleep 0.5
    pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
    if [ -n "$pid" ]; then break; fi
    if [ $((i % 10)) -eq 0 ]; then
      printf "  ...等待端口 (${i}/120, ${ADAPTER_PORT})\n"
    fi
  done
  if [ -n "$pid" ]; then
    [ "$pid" != "$(cat "$ADAPTER_PID" 2>/dev/null)" ] && echo "$pid" > "$ADAPTER_PID"
    info "adapter 已起，PID=${pid}（按端口反查），日志=${ADAPTER_LOG}"
  else
    err "adapter 启动失败，端口 $ADAPTER_PORT 未监听（等了 60s）。看 $ADAPTER_LOG 末尾："
    tail -20 "$ADAPTER_LOG" >&2 || true
    rm -f "$ADAPTER_PID"
    exit 1
  fi
}

# macOS launchd 路径的 start：写 plist + launchctl load
cmd_start_launchd() {
  if [ ! -f "$PLIST_TEMPLATE" ]; then
    err "找不到 plist 模板 ${PLIST_TEMPLATE}（fork 不完整？重 git pull）"; exit 1
  fi
  if [ ! -x "$LAUNCHER" ]; then
    chmod +x "$LAUNCHER" 2>/dev/null || true
  fi

  # 已加载就跳过
  if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
    warn "launchd 已加载 ${LAUNCHD_LABEL}，跳过（restart 会先 unload）"
    cmd_status_launchd
    return 0
  fi

  # 替换占位符写 plist
  mkdir -p "$(dirname "$LAUNCHD_PLIST")"
  sed -e "s|__LABEL__|$LAUNCHD_LABEL|g" \
      -e "s|__ADAPTER_DIR__|$HERMES_PLUGINS_DIR|g" \
      -e "s|__LAUNCHER__|$LAUNCHER|g" \
      -e "s|__LOG_PATH__|$ADAPTER_LOG|g" \
      -e "s|__ADAPTER_PORT__|$ADAPTER_PORT|g" \
      "$PLIST_TEMPLATE" > "$LAUNCHD_PLIST"

  # launchctl load -w：写入 + 立即拉起
  if launchctl load -w "$LAUNCHD_PLIST" 2>/dev/null; then
    info "launchd 已加载 ${LAUNCHD_LABEL}，plist=${LAUNCHD_PLIST}"
  else
    err "launchctl load 失败，看 /var/log/com.apple.xpc.launchd/launchd.log"
    cat "$LAUNCHD_PLIST" >&2
    exit 1
  fi

  # 等三件事齐全才算"起来了"：
  #   1) PID 文件被 wrapper 写出（launchd 拉起 wrapper → wrapper echo $$ > pid）
  #   2) 那个 PID 进程真活着（kill -0 成功）
  #   3) /health 返 200（端口真的绑了）
  #
  # 之前用 lsof 反查端口在 launchd 子进程上不可靠（macOS 上 lsof 偶发拿不到
  # launchd-managed 子进程的 socket → 误判失败 → 走 unload bootout 把刚拉起的
  # adapter 干掉了）。改成 PID 文件 + health 双确认，绕开 lsof。
  local pid="" i
  for i in $(seq 1 120); do
    sleep 0.5
    if [ -f "$ADAPTER_PID" ]; then
      pid="$(cat "$ADAPTER_PID" 2>/dev/null | tr -d ' \r\n' || echo '')"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        # PID 文件 + 进程活，最后用 /health 确认端口真的起接了
        if command -v curl >/dev/null 2>&1 \
           && curl -fsS --max-time 2 "http://127.0.0.1:${ADAPTER_PORT}/health" >/dev/null 2>&1; then
          break
        fi
      fi
    fi
  done
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    info "adapter 已起，PID=${pid}（launchd 路径，PID 文件确认），日志=${ADAPTER_LOG}"
  else
    err "launchd 加载成功但 60s 内 PID 文件未写出 / 进程未活 / /health 不通"
    err "看 ${ADAPTER_LOG} 末尾："
    tail -20 "$ADAPTER_LOG" >&2 || true
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    rm -f "$ADAPTER_PID"
    exit 1
  fi
}

cmd_stop() {
  # ── macOS launchd 路径 ──
  if [ "$IS_MACOS" -eq 1 ] && [ -f "$LAUNCHD_PLIST" ] && command -v launchctl >/dev/null 2>&1; then
    cmd_stop_launchd
    return $?
  fi
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
  info "停 adapter PID=${pid}（按 PID + 按端口双兜底）"
  kill_adapter "$pid" "$ADAPTER_PORT"
  rm -f "$ADAPTER_PID"
  info "已停"
}

# macOS launchd 路径的 stop
cmd_stop_launchd() {
  if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
    info "launchctl unload $LAUNCHD_LABEL"
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
  fi
  # 端口兜底杀
  local port_pid
  port_pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
  if [ -n "$port_pid" ]; then
    warn "launchd unload 后端口仍被 PID=$port_pid 占，kill -9 兜底"
    kill_pid "$port_pid"
  fi
  rm -f "$ADAPTER_PID"
  info "已停（launchd 路径）"
}

cmd_status() {
  # ── macOS launchd 路径 ──
  if [ "$IS_MACOS" -eq 1 ] && command -v launchctl >/dev/null 2>&1; then
    cmd_status_launchd
    return $?
  fi
  local pid=""
  if [ -f "$ADAPTER_PID" ]; then pid="$(cat "$ADAPTER_PID" 2>/dev/null || echo '')"; fi
  # 优先以端口反查为准（Git Bash PID 不可靠）；用 || echo "" 兜底 set -e
  local port_pid
  port_pid="$(get_pid_by_port "$ADAPTER_PORT" | tr -d '\r\n ' || echo '')"
  if [ -n "$port_pid" ]; then
    info "adapter 在跑，端口 PID=${port_pid}（pid 文件记录的=${pid}）"
    [ "$port_pid" != "$pid" ] && [ -n "$port_pid" ] && echo "$port_pid" > "$ADAPTER_PID"
  elif [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    info "adapter 在跑，PID=$pid"
  else
    warn "adapter 未在跑"
    [ -f "$ADAPTER_PID" ] && warn "（stale pid 文件：${pid}）"
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

# macOS launchd 路径的 status
cmd_status_launchd() {
  local label_status
  label_status="$(launchctl list 2>/dev/null | grep "$LAUNCHD_LABEL" || echo '')"
  if [ -n "$label_status" ]; then
    info "launchd 已加载: $label_status"
  else
    warn "launchd 未加载 $LAUNCHD_LABEL"
  fi
  echo "  plist:     $LAUNCHD_PLIST"
  echo "  launcher:  $LAUNCHER"
  echo "  日志文件: $ADAPTER_LOG"
  echo "  监听端口: $ADAPTER_PORT"
  # 健康检查（同非 macOS 路径）
  local url="http://127.0.0.1:${ADAPTER_PORT}/health"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      info "health OK: $url"
    else
      warn "health 检查失败: $url"
    fi
  fi
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
