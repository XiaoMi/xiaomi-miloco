#!/usr/bin/env bash
# install-hermes.sh —— 一键把 miloco 装到 Hermes Agent。
#
# 干 7 件事：
#   1. 前置检查（hermes、miloco-cli、python、$MILOCO_HOME、$MILOCO_HOME/config.json）
#   2. 跑 scripts/sync-skills.py 生成 16 个 skill，复制到 ~/.hermes/skills/
#   3. 复制 miloco 插件到 ~/.hermes/plugins/miloco/，复制 adapter 到同目录
#   4. 自动 patch ${MILOCO_HOME}/config.json 的 agent 段（webhook_url + auth_bearer，备份原文件）
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

# 强制 UTF-8 + POSIX 字符类，防止 "$VAR中文" 被 bash 误识别为变量名延续
export LANG=C.UTF-8 LC_ALL=C.UTF-8

# --- CLI 参数解析（--diagnose / --reset-deliver） ---
DIAGNOSE_ONLY=0
NO_START_BACKEND=0
for arg in "$@"; do
  case "$arg" in
    --diagnose) DIAGNOSE_ONLY=1 ;;
    --no-start-backend) NO_START_BACKEND=1 ;;
    --help|-h)
      cat <<EOF
用法：bash install-hermes.sh [options]
  （无参数）       完整安装（patch config / 写 .env / 复制 plugin / 启 adapter / enable plugin）
  --diagnose         自检模式：跑 12 项检查输出 ✓/✗，不做任何修改
  --no-start-backend 跳过自动 miloco-cli service start（upstream install 退出时 atexit 杀掉的）
  --reset-deliver    清空 state.json::deliver.target，强制重新探测 IM（搭配安装用）
  -h, --help         显示本帮助
EOF
      exit 0
      ;;
  esac
done

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
step() { echo -e "${G}[${1}/${TOTAL_STEPS}]${N} ${2}"; }

# 跟踪已生效步骤，失败时 trap 打印（给 agent / 用户明确当前状态）
DONE_STEPS=()
mark_done() { DONE_STEPS+=("$1"); }
TOTAL_STEPS=9

# 用 EXIT trap 而不是 ERR trap，因为脚本里很多 `err ...; exit 1` 显式退出，
# ERR trap 在显式 exit 时不触发，EXIT trap 任何时候都触发
on_exit() {
  local rc=$?
  if [ $rc -ne 0 ]; then
    err "脚本退出码=$rc"
    echo
    echo -e "${Y}已生效步骤:${N} ${DONE_STEPS[*]:-无}"
    echo
    echo "可能状态：半装（plugin 复制了 / config patch 了 / adapter 没起）"
    echo "修复：重跑 bash $HERE/install-hermes.sh（幂等，自动 recover）"
  fi
}
trap on_exit EXIT

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

# --- 0. --diagnose 模式：跑 12 项检查输出 ✓/✗ + 汇总报告，不做任何修改 ---
if [ "$DIAGNOSE_ONLY" -eq 1 ]; then
  echo
  echo "═══════════════════════════════════════════════════════════════"
  echo " miloco × Hermes 链路自检（仅诊断，不修改任何文件）"
  echo "═══════════════════════════════════════════════════════════════"
  echo

  DIAG_OK=0
  DIAG_FAIL=0
  diag() {
    local name="$1" ok="$2"
    local detail="${3-}"  # set -u 安全：参数可能没传
    if [ "$ok" = "1" ]; then
      printf "  %b[✓]%b %s\n" "$G" "$N" "$name${detail:+ — $detail}"
      DIAG_OK=$((DIAG_OK + 1))
    else
      printf "  %b[✗]%b %s\n" "$R" "$N" "$name${detail:+ — $detail}"
      DIAG_FAIL=$((DIAG_FAIL + 1))
    fi
  }

  # 1. python
  if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    diag "python 可用" 1 "$(command -v python3 || command -v python)"
  else
    diag "python 可用" 0 "请装 python3"
  fi

  # 2. python 依赖（aiohttp / httpx / croniter）
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
  MISSING_DEPS="$("$PY" -c "import aiohttp, httpx, croniter" 2>&1 | head -1 || true)"
  if [ -z "$MISSING_DEPS" ]; then
    diag "python 依赖 (aiohttp/httpx/croniter)" 1
  else
    diag "python 依赖 (aiohttp/httpx/croniter)" 0 "缺模块 — pip install aiohttp httpx croniter"
  fi

  # 3. miloco-cli
  if command -v miloco-cli >/dev/null 2>&1; then
    MILOCO_VER="$("$PY" -c 'import subprocess,json; r=subprocess.run(["miloco-cli","version"],capture_output=True,text=True,timeout=5); v=(json.loads(r.stdout).get("version") if r.stdout.strip().startswith("{") else r.stdout.strip()); print(v)' 2>/dev/null || echo unknown)"
    diag "miloco-cli 在 PATH" 1 "$MILOCO_VER"
  else
    diag "miloco-cli 在 PATH" 0 "上游装：curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare"
  fi

  # 4. miloco backend 在跑
  if command -v miloco-cli >/dev/null 2>&1; then
    ML_OUT="$(miloco-cli service status 2>&1 || true)"
    if echo "$ML_OUT" | grep -qiE "running|active|ok|started"; then
      # 提取 PID + 端口（如果有）
      ML_PID="$(echo "$ML_OUT" | grep -oE 'pid[=:]?[ ]*[0-9]+' | grep -oE '[0-9]+' | head -1 || echo '')"
      ML_PORT="$(echo "$ML_OUT" | grep -oE 'port[=:]?[ ]*[0-9]+' | grep -oE '[0-9]+' | head -1 || echo '1810')"
      [ -n "$ML_PID" ] && diag "miloco backend 在跑" 1 "PID=$ML_PID 端口=$ML_PORT" || diag "miloco backend 在跑" 1
    else
      diag "miloco backend 在跑" 0 "upstream install 退出时 atexit 杀了 → miloco-cli service start（install-hermes.sh 会自动拉起，传 --no-start-backend 跳过）"
    fi
  else
    diag "miloco backend 在跑" 0 "miloco-cli 不在 PATH"
  fi

  # 5. Hermes 目录
  if [ -d "$HERMES_HOME" ]; then
    diag "Hermes 目录存在" 1 "$HERMES_HOME"
  else
    diag "Hermes 目录存在" 0 "请装 Hermes Agent"
  fi

  # 6. miloco config.json
  if [ -f "$MILOCO_HOME/config.json" ]; then
    AGENT_URL="$("$PY" -c "import json; print(json.load(open(r'$MILOCO_HOME/config.json',encoding='utf-8')).get('agent',{}).get('webhook_url',''))" 2>/dev/null || echo "")"
    diag "miloco config.json::agent.webhook_url" 1 "$AGENT_URL"
  else
    diag "miloco config.json" 0 "$MILOCO_HOME/config.json 不存在"
  fi

  # 7. Hermes .env 有 API_SERVER_KEY
  if [ -f "$HERMES_HOME/.env" ] && grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
    KEY_COUNT="$(grep -c '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null || echo 0)"
    if [ "$KEY_COUNT" = "1" ]; then
      diag "Hermes .env::API_SERVER_KEY" 1
    else
      diag "Hermes .env::API_SERVER_KEY" 0 "发现 $KEY_COUNT 行重复，应为 1 行 — 编辑清理"
    fi
  else
    diag "Hermes .env::API_SERVER_KEY" 0 "未设置 — 重跑 install-hermes.sh"
  fi

  # 8. plugin 装好
  if [ -d "$HERMES_PLUGINS_DIR/miloco-plugin" ] && [ -f "$HERMES_PLUGINS_DIR/miloco-plugin/plugin.yaml" ]; then
    diag "plugin 已装到 ~/.hermes/plugins/miloco/" 1
  else
    diag "plugin 已装" 0 "重跑 install-hermes.sh"
  fi

  # 9. plugin enabled
  if command -v hermes >/dev/null 2>&1; then
    # 同 step 8：严格匹配 status 列，避免 "not enabled" 假阳性
    if hermes plugins list --plain --no-bundled 2>/dev/null | grep -E "^enabled.*miloco$" >/dev/null 2>&1; then
      diag "plugin enabled (hermes plugins list)" 1
    else
      diag "plugin enabled" 0 "hermes plugins enable miloco"
    fi
  else
    diag "plugin enabled" 0 "找不到 hermes CLI"
  fi

  # 10. adapter 在跑
  if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    # macOS launchd 路径：直接看 launchctl list
    if launchctl list 2>/dev/null | grep -q "com.xiaomi.miloco.hermes.adapter"; then
      diag "adapter (launchd)" 1 "已加载 com.xiaomi.miloco.hermes.adapter"
    else
      diag "adapter (launchd)" 0 "未加载 → bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh start"
    fi
  elif get_pid_by_port "$ADAPTER_PORT" >/dev/null 2>&1 && [ -n "$(get_pid_by_port "$ADAPTER_PORT" | tr -d ' \r\n')" ]; then
    ADAPTER_PID_VAL="$(get_pid_by_port "$ADAPTER_PORT" | tr -d ' \r\n' | head -1)"
    diag "adapter 进程 (端口 $ADAPTER_PORT)" 1 "PID=$ADAPTER_PID_VAL"
  else
    diag "adapter 进程" 0 "bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh start"
  fi

  # 11. adapter /health
  ADAPTER_HEALTH=""
  if command -v curl >/dev/null 2>&1; then
    ADAPTER_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$ADAPTER_PORT/health" 2>/dev/null || echo "")"
  fi
  if [ "$ADAPTER_HEALTH" = "200" ]; then
    diag "adapter /health" 1 "HTTP 200"
  else
    diag "adapter /health" 0 "HTTP ${ADAPTER_HEALTH:-no-response} — 看 $HERMES_HOME/miloco-adapter.log 末尾"
  fi

  # 12. state.json::deliver.target
  if [ -f "$HERMES_PLUGINS_DIR/miloco-plugin/state.json" ]; then
    DELIVER_TARGET="$("$PY" -c "import json; d=json.load(open(r'$HERMES_PLUGINS_DIR/miloco-plugin/state.json',encoding='utf-8')); print((d.get('deliver') or {}).get('target') or '(null)')" 2>/dev/null || echo "(parse-fail)")"
    if [ "$DELIVER_TARGET" = "(null)" ] || [ "$DELIVER_TARGET" = "(parse-fail)" ] || [ -z "$DELIVER_TARGET" ]; then
      diag "state.json::deliver.target" 0 "null — Hermes 没配 IM 或装时没读到，调 miloco_notify_bind(action='switch', target='feishu') 或重跑 install-hermes.sh"
    else
      diag "state.json::deliver.target" 1 "$DELIVER_TARGET"
    fi
  else
    diag "state.json::deliver.target" 0 "state.json 不存在 — 重跑 install-hermes.sh"
  fi

  # 13. 16 个 skill
  SKILL_COUNT="$(ls -d "$HERMES_HOME/skills/miloco-"* 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$SKILL_COUNT" = "16" ]; then
    diag "16 个 miloco-* skill" 1
  else
    diag "16 个 miloco-* skill" 0 "只装到 $SKILL_COUNT 个 — 重跑 install-hermes.sh"
  fi

  # 14. 4 个 cron job（hermes cron list）
  if command -v hermes >/dev/null 2>&1; then
    CRON_MILOCO="$(hermes cron list 2>/dev/null | grep -ci 'miloco' || echo 0)"
    CRON_MILOCO="$(echo "$CRON_MILOCO" | tr -d ' \r\n')"
    if [ "$CRON_MILOCO" -ge 4 ] 2>/dev/null; then
      diag "4 个受管 cron job" 1 "$CRON_MILOCO 个"
    else
      diag "4 个受管 cron job" 0 "只看到 $CRON_MILOCO 个 — 重跑 install-hermes.sh 让 reconcile 跑"
    fi
  else
    diag "4 个受管 cron job" 0 "hermes CLI 不可用"
  fi

  echo
  echo "═══════════════════════════════════════════════════════════════"
  if [ "$DIAG_FAIL" -eq 0 ]; then
    printf " %b全部 %d 项通过%b — 推送链路完整\n" "$G" "$DIAG_OK" "$N"
    echo "═══════════════════════════════════════════════════════════════"
    exit 0
  else
    printf " %b通过 %d / 失败 %d%b\n" "$R" "$DIAG_OK" "$DIAG_FAIL" "$N"
    echo "═══════════════════════════════════════════════════════════════"
    echo " 修法：按上面 ✗ 项的提示操作；不确定先看 $HERE/INSTALL_KNOWN_ISSUES.md"
    exit 1
  fi
fi

# --- 1. 前置检查 ---
step 1 "前置检查 (python / miloco-cli / Hermes / config.json)"
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 \
  || { err "找不到 python，请先装 python"; exit 1; }
PYTHON="$(command -v python3 || command -v python)"

if ! command -v miloco-cli >/dev/null 2>&1; then
  err "找不到 miloco-cli，请先装好 miloco 后端并确认 miloco-cli 在 PATH"; exit 1
fi
if [ ! -d "$HERMES_HOME" ]; then
  err "找不到 Hermes 目录 ${HERMES_HOME}，请先装 Hermes Agent"; exit 1
fi
if [ ! -f "$MILOCO_HOME/config.json" ]; then
  # 第一次装：miloco 后端可能没初始化 config.json。两种情况处理：
  #   1) miloco service start 会自动 init（upstream behavior）
  #   2) 如果 start 后还是没 config.json，给用户明确指引而不是直接退出
  info "${MILOCO_HOME}/config.json 不存在，尝试 miloco service start 自动初始化..."
  if ! miloco-cli service start 2>&1 | tail -3; then
    err "miloco-cli service start 失败，config.json 还是没生成"
    err "请手动跑：miloco-cli init 或 export MILOCO_HOME=$HOME/.openclaw/miloco 后重跑 install"
    exit 1
  fi
  if [ ! -f "$MILOCO_HOME/config.json" ]; then
    err "miloco service start 后 ${MILOCO_HOME}/config.json 还是不存在"
    err "可能 miloco backend 的 Python venv 缺包。看 $(miloco-cli service logs 2>&1 | tail -5)"
    exit 1
  fi
  info "config.json 自动初始化成功"
fi

# 1.5 自动拉起 miloco backend（upstream install.py 注册了 atexit._stop_service，
# 装完会停 backend；fork 集成必须自己再 service start，否则 Step 2 OAuth 会 502 假错误）
# 用 --no-start-backend flag 可跳过（用户在外部管理 backend 时）
if [ "$NO_START_BACKEND" -eq 0 ]; then
  # 注意：miloco-cli service status 输出 JSON 形如 {"running": true/false,...}。
  # 老版本用 grep -qiE "running|active|ok|started" 会把 {"running": false} 也当成"在跑"，
  # 假阳性导致本该 start 的 backend 没起，Step 2 OAuth 必 502。改成 jq 解析 running 字段。
  # 兼容：没装 jq（alpine / minimal Docker / 部分 Windows Git Bash）时退化用 grep。
  _ML_STATUS_JSON="$(miloco-cli service status 2>/dev/null || echo '{"running": false}')"
  if command -v jq >/dev/null 2>&1; then
    _ML_RUNNING="$(jq -r '.running // false' <<< "$_ML_STATUS_JSON" 2>/dev/null || echo false)"
  else
    # 没 jq：用严格 grep 匹配 "running": true，排除 "running": false / "running":null
    if echo "$_ML_STATUS_JSON" | grep -qE '"running"[[:space:]]*:[[:space:]]*true'; then
      _ML_RUNNING="true"
    else
      _ML_RUNNING="false"
    fi
  fi
  if [ "$_ML_RUNNING" = "true" ]; then
    info "miloco backend 已在跑"
  else
    info "miloco backend 未跑（upstream install 退出时 atexit 杀的），自动 service start"
    if miloco-cli service start 2>&1 | tail -5; then
      info "service start 成功，等 backend /health..."
      for i in $(seq 1 30); do
        sleep 1
        if curl -fsS --max-time 2 http://127.0.0.1:1810/health >/dev/null 2>&1; then
          info "backend 就绪（等了 ${i}s）"
          break
        fi
        if [ "$i" = "30" ]; then
          warn "30s 内 backend /health 未 200 — 继续装但后续 Step 可能挂（看 miloco-cli service logs）"
        fi
      done
    else
      warn "miloco-cli service start 失败 — 继续装但后续 Step 2 OAuth 一定会 502"
      warn "手动修：miloco-cli service start  或  重跑上游：curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare"
    fi
  fi
fi

# --- 1.6 半装残留检测 + 清理（upstream --agent-prepare 异常退出时可能留下） ---
# 现象：supervisord 进程在跑但 supervisord.conf 已被删（半装态）。
# 后果：miloco service status 永远说"在跑"，但实际接不上 / 行为异常。
# 修法：检测到这种状态就 warn + 提示用户怎么清理，**不**擅自 kill supervisord（它可能管着别的服务）。
if [ -d "$MILOCO_HOME" ]; then
  SUPERVISORD_CONF="$MILOCO_HOME/supervisord.conf"
  SUPERVISORD_PID="$MILOCO_HOME/supervisord.pid"
  SUPERVISORD_SOCK="$MILOCO_HOME/supervisord.sock"
  # 情况 1: supervisord.conf 缺失但 PID 还在
  if [ ! -f "$SUPERVISORD_CONF" ] && [ -f "$SUPERVISORD_SOCK" ]; then
    warn "半装残留：supervisord.sock 存在但 supervisord.conf 缺失"
    warn "  这通常是上次 --agent-prepare 异常退出留下的"
    warn "  修复：miloco-cli service stop"
    warn "        或手动：supervisorctl shutdown（shutdown 是 supervisorctl 子命令，不是 supervisord 的）"
  fi
  # 情况 2: PID 文件存在但进程已死
  if [ -f "$SUPERVISORD_PID" ]; then
    OLD_SPID="$(cat "$SUPERVISORD_PID" 2>/dev/null | tr -d ' \r\n' || echo '')"
    if [ -n "$OLD_SPID" ] && ! kill -0 "$OLD_SPID" 2>/dev/null; then
      warn "半装残留：supervisord.pid 指向已死进程 $OLD_SPID"
      rm -f "$SUPERVISORD_PID"
      info "  已清理 stale pid 文件"
    fi
  fi
  # 情况 3: config.json 缺失（upstream --agent-prepare 没成功或被误删）
  if [ ! -f "$MILOCO_HOME/config.json" ]; then
    warn "半装残留：config.json 缺失（upstream --agent-prepare 似乎没成功）"
    warn "  修复：curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare"
  fi
fi

# --- 1.7 MILOCO_HOME 持久化（写进 shell rc，下次新 shell 不用再 export） ---
# 用户的 shell rc 文件（macOS = ~/.zshrc，Linux = ~/.bashrc，WSL Git Bash = ~/.bashrc）
SHELL_RC=""
case "${SHELL:-}" in
  */zsh)  SHELL_RC="$HOME/.zshrc" ;;
  */bash) SHELL_RC="$HOME/.bashrc" ;;
  *)      SHELL_RC="$HOME/.bashrc" ;;  # 兜底 bash
esac
if [ -n "$MILOCO_HOME" ] && [ "$MILOCO_HOME" != "$HOME/.openclaw/miloco" ]; then
  # 用户用了非默认路径，持久化
  if [ -n "$SHELL_RC" ] && [ -f "$SHELL_RC" ] && ! grep -q "export MILOCO_HOME=" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# miloco Hermes 兼容层" >> "$SHELL_RC"
    echo "export MILOCO_HOME=\"$MILOCO_HOME\"" >> "$SHELL_RC"
    info "MILOCO_HOME 已持久化到 $SHELL_RC"
  fi
fi

# --- 1.8 config.json::server.python_bin auto-fix ---
# 现象：miloco 用 uv 装时 backend 装在 ~/.local/share/uv/tools/miloco/bin/python，
# 但 miloco service start 用的是 system python3，找不到 miloco 模块 → backend 装包失败。
# 修法：扫 uv venv + pyenv venv，找到 miloco 包所在 python，patch 进 config.json。
if [ -f "$MILOCO_HOME/config.json" ]; then
  CUR_PY_BIN="$("$PYTHON" -c "
import json
try:
    d = json.load(open('$MILOCO_HOME/config.json'))
    print(d.get('server', {}).get('python_bin', '') or '')
except Exception:
    print('')
" 2>/dev/null || true)"

  # 测试当前配置的 python_bin 能不能 import miloco
  NEEDS_FIX=0
  if [ -n "$CUR_PY_BIN" ] && [ -x "$CUR_PY_BIN" ]; then
    if ! "$CUR_PY_BIN" -c 'import miloco' >/dev/null 2>&1; then
      NEEDS_FIX=1
    fi
  else
    # 当前没配或配的 python 不存在，扫常见 venv 路径
    NEEDS_FIX=1
  fi

  if [ "$NEEDS_FIX" -eq 1 ]; then
    # 扫 uv 装的 miloco venv
    FOUND_PY=""
    for cand in \
      "$HOME/.local/share/uv/tools/miloco/bin/python" \
      "$HOME/.local/share/uv/tools/miloco/bin/python3" \
      "$HOME/.venvs/miloco/bin/python" \
      "$HOME/.venvs/miloco/bin/python3"
    do
      if [ -x "$cand" ] && "$cand" -c 'import miloco' >/dev/null 2>&1; then
        FOUND_PY="$cand"
        break
      fi
    done
    if [ -z "$FOUND_PY" ]; then
      # fallback: 用当前 python 试装 miloco 包（如果 pip 可用）
      if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        info "config.json::server.python_bin 找不到能 import miloco 的 python，尝试 pip install miloco..."
        if "$PYTHON" -m pip install --quiet miloco 2>&1 | tail -3; then
          FOUND_PY="$PYTHON"
        fi
      fi
    fi

    if [ -n "$FOUND_PY" ]; then
      info "auto-fix: config.json::server.python_bin = $FOUND_PY"
      "$PYTHON" - "$MILOCO_HOME" "$FOUND_PY" <<'PY'
import json, sys
from pathlib import Path
home, py_bin = sys.argv[1], sys.argv[2]
p = Path(home) / "config.json"
try:
    cfg = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    cfg = {}
cfg.setdefault("server", {})["python_bin"] = py_bin
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
PY
    else
      warn "config.json::server.python_bin auto-fix 失败：找不到能 import miloco 的 python"
      warn "手动修：${MILOCO_HOME}/config.json::server.python_bin = <装 miloco 包的 python 路径>"
    fi
  fi
fi

mark_done 1

# --- 1.9 注入 web 静态目录 (cp dist → backend static_dir) ---
# 为什么: 上游 miloco-cli wheel 自带 main 时期的 web bundle,Hermes fork 改的
# 前端代码(per-modality perception-toggles 等)永远装不进来。
#
# 设计原则（对齐上游 release model）:
# - 上游走 GitHub Release: build.sh CI 跑一次 + release.yml 上传 prebuilt 归档
#   + install.py _fetch_release_bundle 下载校验后解压。**用户装的时候不 build**。
# - fork 没有 release 流水线:用 git 替代 — `web/dist/` 直接 prebuilt 入仓,
#   install-hermes.sh 从 <fork>/web/dist/ 直接 cp 到 backend static_dir。
# - **不需要 Node.js / npm / 任何 build 工具**。改 web/ source 的开发者本地
#   `npm run build` 后 `git add web/dist` 跟代码一起提交即可。
#
# 跳过条件: 后端 miloco.config.settings 解析失败(venv 配错),只 warn 不 exit,
# 此时后端还 serve wheel 自带 bundle(fork 改动不生效但功能不挂)。

step 1.9 "注入 web 静态目录 (cp prebuilt dist → backend static_dir)"

WEB_SRC="$(dirname "$HERE")/../web"   # <fork>/web
WEB_DIST="$WEB_SRC/dist"

if [ ! -d "$WEB_SRC" ]; then
  warn "找不到 web 源码目录: $WEB_SRC"
  warn "(clone 的 repo 结构异常? 跳过前端注入)"
elif [ ! -d "$WEB_DIST" ]; then
  warn "找不到 prebuilt web bundle: $WEB_DIST"
  warn "(开发者本地需 \`npm run build\` 后重跑 install-hermes.sh)"
else
  # 找 backend 的 static_dir。
  # $PYTHON = system python3(大概率不能 import miloco)—— miloco backend
  # 实际跑在 uv tool venv(由 miloco-cli 管理),需要找到那个 venv 的 python。
  # 优先探测 uv tool 路径(~/.local/share/uv/tools/miloco/bin/python),
  # 找不到再用 $PYTHON 拼一把(某些非 uv 装法能用)。
  MILOCO_PY="$PYTHON"
  for _cand in \
    "$HOME/.local/share/uv/tools/miloco/bin/python" \
    "$HOME/.local/share/uv/tools/miloco/bin/python3"; do
    if [ -x "$_cand" ]; then
      MILOCO_PY="$_cand"
      break
    fi
  done
  STATIC_DIR="$("$MILOCO_PY" -c "
try:
    from miloco.config.settings import get_settings
    print(get_settings().directories.static_dir)
except Exception as e:
    pass
" 2>/dev/null)"
  if [ -z "$STATIC_DIR" ]; then
    warn "找不到 backend static_dir(import miloco.config.settings 失败)"
    warn "(尝试的 python: $MILOCO_PY)"
    warn "(venv 配错的话 wheel 自带 bundle 仍生效,fork 改动暂时不挂)"
  else
    info "  STATIC_DIR=$STATIC_DIR"
    cp -r "$WEB_DIST/." "$STATIC_DIR/"
    info "  web/dist/* → \$STATIC_DIR/ ✓"
    warn "提示: web 内容改了,需要重启 backend 让 spa_handler 重解析"
    warn "      跑 \`hermes gateway restart\` 后 dashboard 才有新 UI"
  fi
fi
mark_done 1.9

# --- 1.10 patch backend(v2 per-modality toggles) ---
# 为什么: 上游 release 是 v1 schema(CameraToggleItem 只有 did+in_use),
# 前端 v3 感知设备列表发 video_enabled/audio_enabled 被 v1 backend strip 掉,
# 4 个场景(单独关视频/单独关音频/批量视频开/批量音频开)全部失效。
# 修法: fork 仓库里 v2 backend 代码(perception-toggles 分支改的)直接
# cp 到 miloco wheel 安装目录,覆盖 v1。
#
# 覆盖哪些:
#   miloco/miot/{schema,router,service,filter}.py
#   miloco/perception/collect/camera_adapter.py
#   miloco/database/kv_repo.py
#
# 跳过条件: 找不到 fork 源码目录(可能 clone 了非 integration branch)
#  或 import miloco 失败(venv 未配)

step 1.10 "patch backend (v2 per-modality toggles)"

FORK_SRC="$(dirname "$(dirname "$HERE")")/backend/miloco/src/miloco"
if [ ! -d "$FORK_SRC/miot" ]; then
  warn "找不到 fork backend 源码: $FORK_SRC"
  warn "(clone 的 repo 不含 backend 目录? 跳过 backend patch)"
else
  # 拿 miloco package 安装目录(static_dir 往上 1 级)
  MILOCO_PKG="$("$MILOCO_PY" -c "
try:
    from pathlib import Path
    from miloco.config.settings import get_settings
    d = Path(get_settings().directories.static_dir).parent
    print(d)
except Exception:
    pass
" 2>/dev/null)"
  if [ -z "$MILOCO_PKG" ] || [ ! -d "$MILOCO_PKG/miot" ]; then
    warn "找不到 miloco package 安装目录(import miloco 失败)"
    warn "(venv 配错,跳过 backend patch)"
  else
    # miot/
    for _f in schema.py router.py service.py filter.py; do
      _src="$FORK_SRC/miot/$_f"
      _dst="$MILOCO_PKG/miot/$_f"
      if [ -f "$_src" ]; then
        cp "$_src" "$_dst"
        info "  miot/$_f ✓"
      fi
    done
    # perception/collect/camera_adapter.py
    _src="$FORK_SRC/perception/collect/camera_adapter.py"
    _dst="$MILOCO_PKG/perception/collect/camera_adapter.py"
    if [ -f "$_src" ]; then
      mkdir -p "$(dirname "$_dst")"
      cp "$_src" "$_dst"
      info "  perception/collect/camera_adapter.py ✓"
    fi
    # database/kv_repo.py
    _src="$FORK_SRC/database/kv_repo.py"
    _dst="$MILOCO_PKG/database/kv_repo.py"
    if [ -f "$_src" ]; then
      mkdir -p "$(dirname "$_dst")"
      cp "$_src" "$_dst"
      info "  database/kv_repo.py ✓"
    fi
    warn "提示: backend 文件已更新(v2 per-modality),需要重启 backend"
    warn "      miloco-cli service restart 后单路开关才生效"
  fi
fi
mark_done 1.10

# --- 2. 拿/复用 Bearer ---
step 2 "拿/复用 adapter Bearer"
# 优先级：.env 已有的 API_SERVER_KEY > 旧 adapter pid 存在则重新生成 > 新生成
if [ -f "$HERMES_HOME/.env" ] && grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
  BEARER="$(grep '^API_SERVER_KEY=' "$HERMES_HOME/.env" | head -1 | cut -d= -f2-)"
  info "复用 .env 已有的 API_SERVER_KEY（${BEARER:0:8}...）"
else
  BEARER="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  info "新生成 adapter Bearer: ${BEARER:0:8}..."
fi
mark_done 2

# --- 3. 同步 skills ---
step 3 "同步 16 个 miloco-* skill → ${HERMES_HOME}/skills/"
"$PYTHON" "$HERE/scripts/sync-skills.py"
mkdir -p "$HERMES_HOME/skills"
cp -r "$HERE/skills"/miloco-* "$HERMES_HOME/skills/"
mark_done 3

# --- 4. 复制插件 + adapter ---
step 4 "复制 Hermes 插件 + adapter → ${HERMES_PLUGINS_DIR}/"
mkdir -p "$HERMES_PLUGINS_DIR"
info "  复制 miloco-plugin/"
rm -rf "$HERMES_PLUGINS_DIR/miloco-plugin"
cp -r "$HERE/miloco-plugin" "$HERMES_PLUGINS_DIR/miloco-plugin"
info "  复制 adapter/"
rm -rf "$HERMES_PLUGINS_DIR/adapter"
cp -r "$HERE/adapter" "$HERMES_PLUGINS_DIR/adapter"
# 清 pycache + 预编译（首次启动少 ~2s）
find "$HERMES_PLUGINS_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
"$PYTHON" -m compileall -q "$HERMES_PLUGINS_DIR/miloco-plugin" "$HERMES_PLUGINS_DIR/adapter" 2>/dev/null || true

# adapter-launcher.sh 要可执行（macOS launchd plist 调它）
chmod +x "$HERE/scripts/adapter-launcher.sh" 2>/dev/null || true

# 把 adapter 启停脚本复制到 plugins/ 下（agent / 自检工具按固定路径找）
# 之前只 chmod 不复制，导致 miloco_status fix 提示的 `bash plugins/hermes/scripts/miloco-adapter.sh start`
# 在用户 cwd 不是 fork 根目录时找不到。复到 ~/.hermes/plugins/miloco/ 下后绝对路径稳定。
info "  复制 miloco-adapter.sh（adapter 启停 wrapper）"
mkdir -p "$HERMES_PLUGINS_DIR/scripts"
cp -f "$HERE/scripts/miloco-adapter.sh" "$HERMES_PLUGINS_DIR/scripts/miloco-adapter.sh"
chmod +x "$HERMES_PLUGINS_DIR/scripts/miloco-adapter.sh"
mark_done 4

# --- 4.5 自动探测 Hermes 已配置的 IM 平台，写入插件 state.json ---
step 4.5 "探测 IM 平台 → 写 plugin state.json::deliver.target"
# 让 miloco_im_push 在 cron 场景下也能直接投递，不需要 LLM 在 cron session 里
# 完成"两段式 bind"（cron 没人可对话，原方案不可用）。
#
# 实现挪到外部 Python 脚本（scripts/detect_im_platforms.py），避免在 bash
# heredoc 内嵌大段 Python + (fallback) 等括号 → macOS bash 3.2 解析挂。
DETECTED_TARGETS_JSON="$("$PYTHON" "$HERE/scripts/detect_im_platforms.py" "$HERMES_HOME" 2>/dev/null || echo '{"targets": [], "source": "detection script failed"}')"

# 一次性拆 DETECTED_TARGETS_JSON 为 3 个标量：target / count / source
# 走 jq（macOS 自带）+ python3 -c，避开 bash 3.2 heredoc 嵌套括号 bug
DETECTED_TARGET="$(jq -r '.targets[0] // ""' <<< "$DETECTED_TARGETS_JSON")"
CANDIDATES_COUNT="$(jq -r '.targets | length' <<< "$DETECTED_TARGETS_JSON")"
DETECT_SOURCE="$(jq -r '.source // "unknown"' <<< "$DETECTED_TARGETS_JSON")"

# state.json 必须写到 plugin 自己的目录里，因为 tools_notify.py::_state_path(ctx)
# 用 ctx.manifest.path / "state.json" 解析（manifest.path 指向 plugin dir）。
# 写到外面的话 plugin 永远读不到 → miloco_im_push 永远报 no deliver target。
PLUGIN_STATE="$HERMES_PLUGINS_DIR/miloco-plugin/state.json"

# --- 4.6 升级保留旧 deliver.target（除非 --reset-deliver）---
RESET_DELIVER=0
for arg in "$@"; do
  case "$arg" in
    --reset-deliver) RESET_DELIVER=1 ;;
  esac
done
PRESERVED_TARGET=""
if [ "$RESET_DELIVER" -eq 0 ] && [ -f "$PLUGIN_STATE" ]; then
  PRESERVED_TARGET="$(jq -r '.deliver.target // ""' "$PLUGIN_STATE" 2>/dev/null || echo "")"
fi

"$PYTHON" "$HERE/scripts/write_state_json.py" "$PLUGIN_STATE" "$DETECTED_TARGETS_JSON" "$PRESERVED_TARGET"

if [ -n "$DETECTED_TARGET" ]; then
  info "通知投递已自动配置 target=${DETECTED_TARGET} 候选 ${CANDIDATES_COUNT} 个 写入 state.json.candidates"
elif [ -n "$PRESERVED_TARGET" ]; then
  info "未检测到新 IM 平台 复用旧 deliver.target=$PRESERVED_TARGET"
else
  warn "未检测到 Hermes 已配置的 IM 平台 auth.json / config.yaml 都空"
  warn "miloco 主动通知将无法送达 miloco_im_push 会返回 no deliver target"
  warn "装完请二选一"
  warn "a 在 Hermes 里连一个 IM hermes config set telegram.bot_token 后重跑 install-hermes.sh"
  warn "b 手动编辑 ${PLUGIN_STATE} 加 deliver.target 字段 形如"
  warn "        {\"deliver\": {\"target\": \"telegram\"}}"
fi
mark_done 4.5

# --- 4.7 同步本地感知 ONNX 模型到 MILOCO_HOME/models/ ---
# 对齐上游 install.sh --agent-finish 的"下载感知模型"步骤（见
# upstream install-guide.md 第 131 行"下载感知模型"）。
#
# 上游 install.sh 会从自己的 release assets 下模型到 ~/.openclaw/miloco/models/，
# hermes fork 走的是"plugin in fork 仓库"路线，不能复用 upstream 下载逻辑，
# 但 fork 仓库的 backend/miloco/src/miloco/perception/models/ 目录里其实打包了
# 同一份模型 — 直接 cp 即可（避免再下 80MB+）。
#
# 跳过条件：MILOCO_HOME/models/det_4C.onnx 已存在（用户已装）。
step 4.7 "同步本地感知 ONNX 模型 → ${MILOCO_HOME}/models/"
MODEL_SRC="$HERE/../../backend/miloco/src/miloco/perception/models"
if [ ! -d "$MODEL_SRC" ]; then
  warn "fork 仓库里找不到 ONNX 模型源目录：$MODEL_SRC"
  warn "感知引擎可能跑不起来（perceive query 报 models_missing）"
  warn "修法：手动 git pull 拉新，或从 upstream release 下载到 $MILOCO_HOME/models/"
else
  mkdir -p "$MILOCO_HOME/models"
  # 同步 .onnx + .json（bge tokenizer）；已存在的不覆盖（保留用户手动调整）
  synced=0
  skipped=0
  for f in "$MODEL_SRC"/*.onnx "$MODEL_SRC"/*.json; do
    [ -f "$f" ] || continue
    bn="$(basename "$f")"
    if [ -f "$MILOCO_HOME/models/$bn" ]; then
      skipped=$((skipped + 1))
    else
      cp "$f" "$MILOCO_HOME/models/$bn"
      synced=$((synced + 1))
    fi
  done
  info "  同步 ONNX 模型：新增 $synced 个、跳过已存在 $skipped 个"
  info "  模型目录：$MILOCO_HOME/models/"

  # 在 config.json 写 models 字段（settings.models_dir 默认读这里）
  if [ -f "$MILOCO_HOME/config.json" ]; then
    "$PYTHON" - "$MILOCO_HOME" <<'PY' || true
import json, sys
home = sys.argv[1]
p = f"{home}/config.json"
try:
    cfg = json.load(open(p, encoding="utf-8"))
except Exception:
    cfg = {}
cfg["models"] = f"{home}/models"
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"  config.json::models = {home}/models")
PY
  fi
fi
mark_done 4.7

# --- 5. patch ${MILOCO_HOME}/config.json ---
step 5 "patch ${MILOCO_HOME}/config.json 的 agent 段"
# backup 文件名加 PID + 纳秒，避免 30s 内 reinstall 撞名
TS="$(date +%Y%m%d-%H%M%S)-pid$$-nsec$(date +%N)"
cp "$MILOCO_HOME/config.json" "${MILOCO_HOME}/config.json.bak-${TS}"

# 清理老备份：保留最新 3 份，避免 config.json.bak-* 累积（重装 N 次 → N 份）
# 用 ls -1t 按时间倒序，tail -n +4 跳过前 3 行（即保留最新 3），其余删
old_baks="$(ls -1t "${MILOCO_HOME}"/config.json.bak-* 2>/dev/null | tail -n +4 || true)"
if [ -n "$old_baks" ]; then
  rm -f $old_baks
  info "  清理老 config.json.bak：保留最新 3 份"
fi
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
mark_done 5

# --- 6. patch ~/.hermes/.env（仅当缺失时追加）---
step 6 "确保 ${HERMES_HOME}/.env 有 API_SERVER_KEY"
touch "$HERMES_HOME/.env"
chmod 600 "$HERMES_HOME/.env"
if ! grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
  echo "API_SERVER_KEY=$BEARER" >> "$HERMES_HOME/.env"
  info "已追加 API_SERVER_KEY 到 .env"
else
  warn ".env 已有 API_SERVER_KEY，保持原值"
fi
mark_done 6

# --- 7. 重启 adapter ---
# 委托给 scripts/miloco-adapter.sh start，由它根据平台选择路径：
#   - macOS → launchd LaunchAgent（plist + launchctl load），adapter 完全脱离 install.sh 进程组
#   - Linux / Git Bash / WSL → nohup + </dev/null + 60s retry loop
# 这样 install.sh exit 1 时，adapter 不会被 SIGHUP/SIGTERM 误杀（macOS 上是关键）
step 7 "重启 adapter (端口 ${ADAPTER_PORT})"
info "  委托给 scripts/miloco-adapter.sh（macOS 走 launchd，其他走 nohup）"
if ! bash "$HERE/scripts/miloco-adapter.sh" start; then
  err "adapter 启动失败"
  exit 1
fi
mark_done 7

# --- 8. enable plugin（Hermes 是 opt-in，不 enable 就不会加载工具）---
step 8 "enable Hermes 插件 miloco"
# plugin.yaml 里的 name 字段是 'miloco'，enable 时用它
if command -v hermes >/dev/null 2>&1; then
  # 已 enabled 跳过；未 enabled 才 enable
  # 注意：hermes plugins list 表格里 not enabled 和 enabled 都有 'enabled' 子串，
  # 老版 grep "miloco.*enabled" 会把 not enabled 误判成 enabled 导致跳过 enable。
  # --plain --no-bundled + "^enabled.*miloco$" 是 column-agnostic 的严格匹配，
  # 不依赖终端 Unicode 框线字符渲染。对齐 install-guide-hermes.md 的写法。
  if hermes plugins list --plain --no-bundled 2>/dev/null | grep -E "^enabled.*miloco$" >/dev/null 2>&1; then
    info "  已是 enabled，跳过"
  else
    if hermes plugins enable miloco >/dev/null 2>&1; then
      info "  已 enable"
    else
      warn "  hermes plugins enable miloco 失败（可能是 hermes gateway 未启动或 CLI 版本不一致）"
      warn "  → 装完手动跑：hermes plugins enable miloco"
    fi
  fi
  # 可见性证据：echo 当前 enabled 行
  echo "  当前插件状态："
  hermes plugins list 2>/dev/null | sed 's/^/    /' || true
else
  warn "找不到 hermes CLI，跳过 enable（装完手动跑 hermes plugins enable miloco）"
fi
mark_done 8

# --- 8.5 兜底清掉 hermes namespace disable 漏写 ---
# upstream hermes plugins enable 用 manifest.name="miloco" discard disabled 集合，
# 但 nested plugin key="miloco/miloco-plugin" 不会被清 → install 显示成功但 runtime 仍 disabled。
# 这里手动从 ~/.hermes/config.yaml 删掉 miloco* 残留（幂等，no-op if 没残留）。
# 边界：
#   - 文件不存在 → 跳过（hermes 还没初始化）
#   - PyYAML 没装 → 跳过（不影响主流程）
#   - 解析失败 → 跳过（不致命，靠 step 9 versions 自检帮我们看到）
"$PYTHON" - <<'PY' 2>/dev/null || true
import sys
try:
    import yaml  # noqa
except ImportError:
    sys.exit(0)
from pathlib import Path
p = Path.home() / ".hermes" / "config.yaml"
if not p.exists():
    sys.exit(0)
try:
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
except Exception:
    sys.exit(0)
plugins = data.get("plugins") or {}
disabled = plugins.get("disabled") or []
new_disabled = [d for d in disabled if not str(d).startswith("miloco")]
if len(new_disabled) != len(disabled):
    plugins["disabled"] = new_disabled
    data["plugins"] = plugins
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"  ✓ 兜底清理 plugins.disabled：{disabled} → {new_disabled}")
else:
    print(f"  · plugins.disabled 无 miloco 残留，无需清理")
PY
mark_done 8.5

# --- 9. 记录版本到 state.json（升级一致性检查用） ---
step 9 "记录版本到 plugin state.json"
HERMES_VER="$(command -v hermes >/dev/null 2>&1 && hermes --version 2>&1 | head -1 || echo unknown)"
MILOCO_VER="$(command -v miloco-cli >/dev/null 2>&1 && (miloco-cli version 2>/dev/null | python3 -c 'import sys,json; line=sys.stdin.read().strip(); print(json.loads(line).get("version","") if line.startswith("{") else line)' 2>/dev/null) || echo unknown)"
PLUGIN_VER="$(grep '^version:' "$HERMES_PLUGINS_DIR/miloco-plugin/plugin.yaml" 2>/dev/null | awk '{print $2}' || echo unknown)"
GIT_COMMIT="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"

"$PYTHON" - "$PLUGIN_STATE" "$HERMES_VER" "$MILOCO_VER" "$PLUGIN_VER" "$GIT_COMMIT" <<'PY' || true
import json, sys, datetime
from pathlib import Path
path, hermes_v, miloco_v, plugin_v, git_c = sys.argv[1:6]
try:
    state = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
except Exception:
    state = {}
old_versions = state.get("versions") or {}
state["versions"] = {
    "hermes": hermes_v,
    "miloco_cli": miloco_v,
    "plugin": plugin_v,
    "git_commit": git_c,
    "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"  hermes={hermes_v}  miloco-cli={miloco_v}  plugin={plugin_v}  commit={git_c}")
# 检查升级变化
old_plugin = old_versions.get("plugin") or ""
old_commit = old_versions.get("git_commit") or ""
if old_plugin and old_plugin != plugin_v:
    print(f"  [升级] plugin: {old_plugin} → {plugin_v}")
elif old_commit and old_commit != git_c:
    print(f"  [升级] git_commit: {old_commit} → {git_c}")
PY
mark_done 9

# --- 终态 ---
cat <<EOF

============================================================
 ✅ 安装完成（可重复执行，幂等）
============================================================

EOF

# ⚠️ 醒目 banner：必须由用户自己跑 gateway restart（Hermes anti-restart-loop）
echo -e "${Y}============================================================${N}"
echo -e "${Y} ⚠️  现在请你自己终端跑（不要让 agent 代跑）：${N}"
echo -e "${Y}     hermes gateway restart${N}"
echo -e "${Y}     （或 hermes gateway stop && hermes gateway start）${N}"
echo -e "${Y} 原因：Hermes anti-restart-loop 会拒绝在 gateway 进程内重启${N}"
echo -e "${Y}============================================================${N}"
echo

cat <<EOF
[插件状态]
    上面 hermes plugins list 输出会确认 miloco 是 enabled

[试一下]
    hermes chat -q "把客厅灯打开" -Q

[adapter 状态]
    bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh status    # 看 PID / 端口
    bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh logs      # tail 日志
    bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh restart   # 重启
    bash ~/.hermes/plugins/miloco/scripts/miloco-adapter.sh stop      # 停

[配置文件位置]
    $MILOCO_HOME/config.json   # miloco 后端配置（已 patch）
    $HERMES_HOME/.env          # Hermes 环境（已追加 API_SERVER_KEY）
    $PLUGIN_STATE              # 插件 deliver.target
    $ADAPTER_PID               # adapter PID
    $ADAPTER_LOG               # adapter 日志

[想还原]
    ${MILOCO_HOME}/config.json.bak-${TS}  是 patch 前的备份
    $HERMES_HOME/.env 里去掉 API_SERVER_KEY 即可
    卸插件：rm -rf $HERMES_PLUGINS_DIR $HERMES_HOME/skills/miloco-*
    disable 插件：hermes plugins disable miloco

[详细文档] $HERE/README.md
============================================================
EOF
