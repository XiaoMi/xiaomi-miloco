#!/usr/bin/env bash
# install-hermes.sh —— 一键把 miloco 装到 Hermes Agent。
#
# 干 7 件事：
#   1. 前置检查（hermes、miloco-cli、python、$MILOCO_HOME、$MILOCO_HOME/config.json）
#   2. 跑 scripts/sync-skills.py 生成 16 个 skill，复制到 ~/.hermes/skills/
#   3. 复制 miloco 插件到 ~/.hermes/plugins/miloco/（架构 #1+#2 后不再复制独立 adapter 进程,入站走 backend AgentPlatformAdapter）
#   4. 自动 patch ${MILOCO_HOME}/config.json 的 agent 段（webhook_url + auth_bearer，备份原文件）
#   5. 自动给 ~/.hermes/.env 补 API_SERVER_KEY（如缺失则生成；存在则复用）
#   6. 重启 miloco-backend（supervisord 管理），确保适配器收敛到 backend AgentPlatformAdapter
#   7. 打印终态：后端 PID / 日志路径 / 后续唯一要做的步骤
#
# 幂等：再跑一次不会破坏现有配置，会重启 backend 保留同一 Bearer。
# 还原：$MILOCO_HOME/config.json.bak-* 是 patch 前的备份，~/.hermes/.env 自行删 API_SERVER_KEY 即可。
#
# 高级/手动安装请用 scripts/install.sh（不做 patch、不启 backend）。
# backend 启停 / 日志请用 miloco-cli service {start,stop,restart,status,logs}。

set -euo pipefail

# 强制 UTF-8 + POSIX 字符类，防止 "$VAR中文" 被 bash 误识别为变量名延续
export LANG=C.UTF-8 LC_ALL=C.UTF-8

# --- CLI 参数解析（--diagnose / --no-start-backend / --post-install / -h） ---
DIAGNOSE_ONLY=0
NO_START_BACKEND=0
POST_INSTALL_ONLY=0

# 日志函数必须先定义（CLI 参数解析里会用到 warn）
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
info() { echo -e "${G}[✓]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[✗]${N} $*" >&2; }

for arg in "$@"; do
  case "$arg" in
    --diagnose) DIAGNOSE_ONLY=1 ;;
    --no-start-backend) NO_START_BACKEND=1 ;;
    --post-install) POST_INSTALL_ONLY=1 ;;
    --help|-h)
      cat <<EOF
用法：bash install-hermes.sh [options]
  （无参数）       完整安装（patch config / 写 .env / 复制 plugin / 启 backend / enable plugin）
  --diagnose         自检模式：跑 14 项检查输出 ✓/✗，不做任何修改
  --no-start-backend 跳过自动 miloco-cli service start（upstream install 退出时 atexit 杀掉的）
  -h, --help         显示本帮助
EOF
      exit 0
      ;;
    *)
      warn "未知参数: $arg（可用: --diagnose, --no-start-backend, -h）"
      ;;
  esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.hermes/miloco}"
HERMES_PLUGINS_DIR="$HERMES_HOME/plugins/miloco"

# 从 config.json 动态读取 backend 端口（不写死 1810）
_read_backend_port() {
  "$PYTHON" - "$MILOCO_HOME" <<'PY' 2>/dev/null || echo "1810"
import json, sys, os
p = os.path.join(sys.argv[1], 'config.json')
try:
    from urllib.parse import urlparse
    d = json.load(open(p))
    url = d.get('server',{}).get('url','') or 'http://127.0.0.1:1810'
    print(urlparse(url).port or 1810)
except Exception:
    print(1810)
PY
}

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
    # 同 step 8：对齐 install-guide-hermes.md:295 的严格模式
    # ^enabled.*miloco$ —— 行内 "enabled" 在前、"miloco" 在后，避免 not enabled 假阳性
    if hermes plugins list --plain --no-bundled 2>/dev/null | grep -E "^enabled.*miloco$" >/dev/null 2>&1; then
      diag "plugin enabled (hermes plugins list)" 1
    else
      diag "plugin enabled" 0 "hermes plugins enable miloco"
    fi
  else
    diag "plugin enabled" 0 "找不到 hermes CLI"
  fi

  # 10. miloco-backend 在跑（supervisord 管理）
  # 架构 #1+#2 后适配器收敛到 backend AgentPlatformAdapter，
  # 不再需要独立 adapter 进程。
  sv_running=0
  if pgrep -f "supervisord.*$MILOCO_HOME" >/dev/null 2>&1; then
    sv_running=1
  fi
  backend_running=0
  if [ "$sv_running" -eq 1 ] && command -v supervisorctl >/dev/null 2>&1; then
    st="$(SKIP_PLUGIN_CHECK=0 supervisorctl -c "$MILOCO_HOME/supervisord.conf" status miloco-backend 2>/dev/null || echo "")"
    if [[ "$st" == *RUNNING* ]]; then
      backend_running=1
    fi
  fi
  if [ "$backend_running" -eq 1 ]; then
    diag "miloco-backend (supervisord)" 1 "RUNNING"
  elif [ "$sv_running" -eq 1 ]; then
    diag "miloco-backend (supervisord)" 0 "supervisord 在跑但 miloco-backend 未启动 → 重跑 install-hermes.sh"
  else
    diag "miloco-backend (supervisord)" 0 "supervisord 未在跑 → 重跑 install-hermes.sh"
  fi

  # 11. 检查旧 launchd adapter 是否残留（旧架构遗留）

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

  # 13. 16+ 个 skill
  SKILL_COUNT="$(ls -d "$HERMES_HOME/skills/miloco-"* 2>/dev/null | wc -l | tr -d ' ')"; true
  if [ "$SKILL_COUNT" -ge 16 ]; then
    diag "$SKILL_COUNT 个 miloco-* skill" 1
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

# --post-install: install.py 调起。跳过 step 3 / step 4 前端部署主体（这两步依赖
# tarball 里没有的 scripts/sync-skills.py / skills/，必须整段跳）；
# step 5 (config set) / 6 (.env) / 7 (backend 重启) / 8 (enable plugin) 主体幂等，
# 会重跑一次以保证 config/enable/backend 状态收敛（step 7 会多一次 stop+sleep 3s+start）。
# 重点补齐的是 1.6/1.75/1.9 env 持久化 + 4.7 ONNX 模型 + 8.5 disable 残留清理 +
# 9 版本记录 + 10 cron reconcile + 收尾 banner。
if [ "$POST_INSTALL_ONLY" -eq 1 ]; then
  info "post-install 模式: 跳过 step 3/4 前端部署；step 5-8 幂等重跑；补 env / cron / 收尾"
  POST_INSTALL_SKIP=1
else
  POST_INSTALL_SKIP=0
fi

# --- 1. 前置检查 ---
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 1 "前置检查 (python / miloco-cli / Hermes / config.json)"
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
  # 第一次装：miloco 后端可能没初始化 config.json。
  # miloco-cli 没有 init 命令——先写最小 config.json 让 backend 能启动。
  info "${MILOCO_HOME}/config.json 不存在，写最小配置..."

  # 找能 import miloco 的 Python（系统 python3 通常没装 miloco 包）
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
  [ -z "$FOUND_PY" ] && FOUND_PY="$PYTHON"

  mkdir -p "$MILOCO_HOME"
  "$PYTHON" - "$MILOCO_HOME" "$FOUND_PY" <<'PY' || { err "无法创建 config.json"; exit 1; }
import json, sys
home = sys.argv[1]
py = sys.argv[2]
cfg = {
    "server": {"port": 1810, "url": "http://127.0.0.1:1810", "token": "", "python_bin": py},
    "model": {"omni": {"model": "", "base_url": "", "api_key": ""}},
    "agent": {"platform": "hermes"},
    "directories": {},
    "database": {"path": "miloco.db"},
}
path = f"{home}/config.json"
json.dump(cfg, open(path, 'w'), indent=2, ensure_ascii=False)
print(f"  config.json 已创建 (python_bin={py})")
PY
  # 启 backend 让它初始化 DB、填充 server.token
  if miloco-cli service start 2>&1 | tail -3; then
    info "  backend 初始化完成"
  elif miloco-cli service status 2>&1 | grep -q '"running":true'; then
    info "  backend 已在运行"
  else
    err "miloco-cli service start 失败，backend 无法启动"
    err "看日志: $(miloco-cli service logs 2>&1 | tail -5)"
    exit 1
  fi
fi

# 1.5 自动拉起 miloco backend（upstream install.py 注册了 atexit._stop_service，
# 装完会停 backend；fork 集成必须自己再 service start，否则 Step 2 OAuth 会 502 假错误）
# 用 --no-start-backend flag 可跳过（用户在外部管理 backend 时）。
# --post-install 场景下 install.py 主流程已经启动了 backend，跳过避免 miloco-cli
# service start 撞已在跑实例。
if [ "$NO_START_BACKEND" -eq 0 ] && [ "$POST_INSTALL_ONLY" -eq 0 ]; then
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
        if curl -fsS --max-time 2 "http://127.0.0.1:$(_read_backend_port)/health" >/dev/null 2>&1; then
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
    warn "        或手动：ps aux | grep supervisord，然后 kill <PID>"
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
  # 消费方：miloco-cli （cli/src/miloco_cli/config.py::miloco_home fallback = ~/.openclaw/miloco）。
  # 只有 MILOCO_HOME 恰好等于 CLI fallback 时才可省略 shell rc（新 shell 读不到 env
  # 也会 fallback 到同一路径）。其余情况（含默认 ~/.hermes/miloco）都必须持久化。
  if [ -n "$SHELL_RC" ] && [ -f "$SHELL_RC" ] && ! grep -q "export MILOCO_HOME=" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# miloco Hermes 兼容层" >> "$SHELL_RC"
    echo "export MILOCO_HOME=\"$MILOCO_HOME\"" >> "$SHELL_RC"
    info "MILOCO_HOME 已持久化到 $SHELL_RC"
  fi
fi

# --- 1.75 MILOCO_HOME 也写进 ~/.hermes/.env ---
# Hermes gateway 由 launchd plist 直接拉起，不 source shell rc，
# 但会通过 load_hermes_dotenv 加载 $HERMES_HOME/.env。
# 消费方：gateway 里的 miloco-plugin/paths.py fallback = ~/.hermes/miloco
# （见 plugins/hermes/miloco-plugin/paths.py::miloco_home）。只有 MILOCO_HOME 恰好
# 等于 plugin fallback 时才可省略 .env（gateway 读不到 env 也 fallback 到同一路径）。
# 注意：跟上面 1.7 的判断不同——两个消费方的 fallback 不同，判断也要各自对齐。
if [ -n "$MILOCO_HOME" ] && [ "$MILOCO_HOME" != "$HOME/.hermes/miloco" ]; then
  touch "$HERMES_HOME/.env"
  chmod 600 "$HERMES_HOME/.env"
  if grep -q '^MILOCO_HOME=' "$HERMES_HOME/.env" 2>/dev/null; then
    "$PYTHON" - "$HERMES_HOME/.env" "$MILOCO_HOME" <<'PY'
import sys
lines = open(sys.argv[1]).readlines()
with open(sys.argv[1], 'w') as f:
    for ln in lines:
        f.write(f'MILOCO_HOME={sys.argv[2]}\n' if ln.startswith('MILOCO_HOME=') else ln)
PY
  else
    echo "MILOCO_HOME=$MILOCO_HOME" >> "$HERMES_HOME/.env"
  fi
  info "MILOCO_HOME 已持久化到 $HERMES_HOME/.env"
fi

# --- 1.8 config.json::server.python_bin auto-fix ---
# 现象：miloco 用 uv 装时 backend 装在 ~/.local/share/uv/tools/miloco/bin/python，
# 但 miloco service start 用的是 system python3，找不到 miloco 模块 → backend 装包失败。
# 修法：扫 uv venv + pyenv venv，找到 miloco 包所在 python，patch 进 config.json。
if [ -f "$MILOCO_HOME/config.json" ]; then
  CUR_PY_BIN="$("$PYTHON" "$HERE/scripts/read_python_bin.py" "$MILOCO_HOME" 2>/dev/null || echo "")"

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

# --- 1.9 MILOCO_HOME 显式持久化 ---
# 架构：agent runtime 决定路径（hermes → ~/.hermes/miloco，openclaw → ~/.openclaw/miloco），
# env override（用户/CI 显式 export MILOCO_HOME）也支持并原样传递，不做 symlink / 数据迁移。
# 三个消费方拿到同一个 MILOCO_HOME 靠：
#   1. shell rc （~/.zshrc / ~/.bashrc） — 新 shell 里 miloco-cli / hermes 都能读到
#   2. supervisord.conf::environment — supervisord 拉起 backend 时的 env 兜底
#   3. supervisorctl reread + update — 下次 backend 重启应用
step 1.9 "MILOCO_HOME 显式持久化 → ${MILOCO_HOME}"
# 写用户 shell rc
SHELL_RC_LIST=()
[ -f "$HOME/.zshrc" ] && SHELL_RC_LIST+=("$HOME/.zshrc")
[ -f "$HOME/.bashrc" ] && SHELL_RC_LIST+=("$HOME/.bashrc")
if [ ${#SHELL_RC_LIST[@]} -gt 0 ]; then
  for _rc in "${SHELL_RC_LIST[@]}"; do
    if grep -q "^export MILOCO_HOME=" "$_rc" 2>/dev/null; then
      "$PYTHON" - "$_rc" "$MILOCO_HOME" <<'PY'
import re, sys
rc, new = sys.argv[1], sys.argv[2]
text = open(rc, encoding='utf-8').read()
text = re.sub(r'^export MILOCO_HOME=.*$', f'export MILOCO_HOME="{new}"', text, flags=re.MULTILINE)
open(rc, 'w', encoding='utf-8').write(text)
PY
      info "  $_rc: export MILOCO_HOME 已更新"
    else
      echo "" >> "$_rc"
      echo "# miloco (MILOCO_HOME=$MILOCO_HOME)" >> "$_rc"
      echo "export MILOCO_HOME=\"$MILOCO_HOME\"" >> "$_rc"
      info "  $_rc: export MILOCO_HOME 已追加"
    fi
  done
else
  warn "  ~/.zshrc / ~/.bashrc 都不存在,无法持久化 MILOCO_HOME"
  warn "  手动在 shell rc 加: export MILOCO_HOME=\"$MILOCO_HOME\""
fi
# 改 supervisor conf (miloco-cli service start 每次会重新生成,这里写只是防御,真生效靠 shell rc)
SUPERVISORD_CONF="$MILOCO_HOME/supervisord.conf"
if [ -f "$SUPERVISORD_CONF" ]; then
  if grep -q 'MILOCO_HOME=' "$SUPERVISORD_CONF"; then
    "$PYTHON" - "$SUPERVISORD_CONF" "$MILOCO_HOME" <<'PY'
import re, sys
path, new_home = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
text = re.sub(r'MILOCO_HOME="[^"]*"', f'MILOCO_HOME="{new_home}"', text)
open(path, 'w', encoding='utf-8').write(text)
print(f"  supervisord.conf::MILOCO_HOME = {new_home}")
PY
  fi
fi
# supervisor reread + update(让新 conf 暂存,等下次 restart 应用)
if command -v supervisorctl >/dev/null 2>&1 && [ -S "$MILOCO_HOME/supervisor.sock" ]; then
  supervisorctl -c "$SUPERVISORD_CONF" reread 2>&1 | head -3 || true
  supervisorctl -c "$SUPERVISORD_CONF" update 2>&1 | head -3 || true
fi
mark_done 1.9

# --- 2. 拿/复用 Bearer ---
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 2 "拿/复用 adapter Bearer"
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
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 3 "同步 16 个 miloco-* skill → ${HERMES_HOME}/skills/"
if [ "$POST_INSTALL_SKIP" -eq 0 ]; then
"$PYTHON" "$HERE/scripts/sync-skills.py"
mkdir -p "$HERMES_HOME/skills"
cp -r "$HERE/skills"/miloco-* "$HERMES_HOME/skills/"
fi
mark_done 3

# --- 4. 复制插件 ---
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 4 "复制 Hermes 插件 → ${HERMES_PLUGINS_DIR}/"
if [ "$POST_INSTALL_SKIP" -eq 0 ]; then
mkdir -p "$HERMES_PLUGINS_DIR"
info "  复制 miloco-plugin/"
# 备份用户 state.json（含 deliver.target 等手工配置），复制后还原
STATE_BAK="$("$PYTHON" -c "import tempfile,os; f=tempfile.mktemp(suffix='.json');print(f)" 2>/dev/null || echo "")"
if [ -n "$STATE_BAK" ] && [ -f "$HERMES_PLUGINS_DIR/miloco-plugin/state.json" ]; then
  cp "$HERMES_PLUGINS_DIR/miloco-plugin/state.json" "$STATE_BAK"
fi
rm -rf "$HERMES_PLUGINS_DIR/miloco-plugin"
cp -r "$HERE/miloco-plugin" "$HERMES_PLUGINS_DIR/miloco-plugin"
if [ -n "$STATE_BAK" ] && [ -f "$STATE_BAK" ]; then
  cp "$STATE_BAK" "$HERMES_PLUGINS_DIR/miloco-plugin/state.json"
  rm -f "$STATE_BAK"
  info "  已还原 state.json（保留用户配置的 deliver.target）"
fi
# 架构 #1+#2 收敛:pr-hermes 已删独立 aiohttp 进程 + plugins/hermes/adapter/ 整个目录。
# 入站 webhook 由 backend 侧 AgentPlatformAdapter 接管(plugins/hermes/miloco-plugin/hermes_adapter/adapter.py)。
# 此处只删旧 adapter/ 残留,不复制任何"老 adapter"。
rm -rf "$HERMES_PLUGINS_DIR/adapter"
# 清 pycache + 预编译（首次启动少 ~2s）
find "$HERMES_PLUGINS_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
"$PYTHON" -m compileall -q "$HERMES_PLUGINS_DIR/miloco-plugin" 2>/dev/null || true
fi
mark_done 4

# --- 4.x 部署 AgentPlatformAdapter 到 MILOCO_HOME ---
# backend loader (backend/miloco/src/miloco/agent_platform/loader.py) 按
# settings.agent.platform 从 $MILOCO_HOME/agent_platform/<name>/ 加载 adapter.py,
# submodule_search_locations 指向该目录,所以 adapter.py 内 from .xxx import 的
# 所有依赖文件(context_injection/catalog/paths) 必须也在同目录。
ADAPTER_DEST="$MILOCO_HOME/agent_platform/hermes"
mkdir -p "$ADAPTER_DEST"
for f in __init__.py adapter.py; do
  cp -f "$HERE/miloco-plugin/hermes_adapter/$f" "$ADAPTER_DEST/$f"
done
for f in context_injection.py catalog.py paths.py tools_habit.py; do
  cp -f "$HERE/miloco-plugin/$f" "$ADAPTER_DEST/$f"
done
# 清旧的 adapter/ 目录残留(独立 aiohttp 进程栈,架构 #1+#2 后不再用)
rm -rf "$MILOCO_HOME/agent_platform/adapter"
info "  部署 AgentPlatformAdapter → $ADAPTER_DEST/"

# 部署 web 前端
# [PR合并后] 可删除：上游 release 包自带 pre-built web，不需要此步骤
# 原因：fork 源码安装时 static/ 为空，需本地 npm build
WEB_DIR="$HERE/../../web"
STATIC_DST="$HERE/../../backend/miloco/src/miloco/static"
if [ ! -f "$STATIC_DST/index.html" ] && [ -f "$WEB_DIR/package.json" ]; then
  info "  构建 web 前端（首次安装，约 30s）..."
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && pnpm install --frozen-lockfile 2>&1 | tail -1 && pnpm build 2>&1 | tail -1) || true
  elif command -v npm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && npm install 2>&1 | tail -1 && npm run build 2>&1 | tail -1) || true
  fi
  if [ -f "$WEB_DIR/dist/index.html" ]; then
    mkdir -p "$STATIC_DST" && cp -r "$WEB_DIR/dist"/* "$STATIC_DST/"
    info "  ✓ web 前端已部署"
  else
    info "  · web 前端构建失败（跳过，不影响核心功能）"
  fi
fi

# 建 ~/.hermes/memory/(感知 cron skill 写感知摘要的目标目录)。首次跑 cron 时
# skill 会 `ls /Users/wkea/memory/<date>-miloco-perception.md`,目录不存在会报
# "No such file or directory"。这里是 cron 链路真 bug —— skill 写文件前必须
# 保证父目录存在。
mkdir -p "$HERMES_HOME/memory"

PLUGIN_STATE="$HERMES_PLUGINS_DIR/miloco-plugin/state.json"

# --- 4.7 同步本地感知 ONNX 模型到 MILOCO_HOME/models/ ---
# [PR合并后] 可简化：上游 --agent-finish 自动下载模型，不再需要从 fork 仓库 cp
# 原因：fork 走"plugin in fork 仓库"路线，不能复用 upstream 下载逻辑
# 对齐上游 install.sh --agent-finish 的"下载感知模型"步骤（见
# upstream install-guide.md 第 131 行"下载感知模型"）。
#
# hermes fork 走的是"plugin in fork 仓库"路线，不能复用 upstream 下载逻辑，
# 但 fork 仓库的 backend/miloco/src/miloco/perception/models/ 目录里其实打包了
# 同一份模型 — 直接 cp 即可（避免再下 80MB+）。
#
# 跳过条件：MILOCO_HOME/models/det_4C.onnx 已存在（用户已装）。
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 4.7 "同步本地感知 ONNX 模型 → ${MILOCO_HOME}/models/"

# Release 装机场景 install.py step 7「准备感知模型」已经从 miloco-models-*.tar.gz
# 解压 5 个 ONNX 到 $MILOCO_HOME/models/。本步只是 dev/fork 场景的兜底（从 git
# checkout 或 miloco 包内 cp），已经有模型就跳过 fork/pkg 搜索，避免误报
# 「找不到 ONNX 模型源目录」。
if compgen -G "$MILOCO_HOME/models/*.onnx" >/dev/null 2>&1; then
  onnx_count=$(ls "$MILOCO_HOME/models"/*.onnx 2>/dev/null | wc -l | tr -d ' ')
  info "  $MILOCO_HOME/models/ 已有 $onnx_count 个 ONNX 模型（install.py step 7 已解压 release tarball），跳过 fork/pkg 兜底"
  MODEL_SRC=""
else
  # 搜模型源目录：优先 fork 仓库（git checkout），其次 miloco Python 包内 models/
  # 安装到 ~/.hermes/plugins/miloco/ 后 $HERE 不再指向 git checkout，
  # 但 pip install -e 的 miloco 包内 models/ 仍可达，以此兜底。
  MODEL_SRC="$HERE/../../backend/miloco/src/miloco/perception/models"
  if [ ! -d "$MODEL_SRC" ]; then
    MODEL_SRC=$("$PYTHON" -c "from pathlib import Path; import miloco; print(Path(miloco.__file__).parent / 'perception' / 'models')" 2>/dev/null || true)
  fi
fi
if [ -n "$MODEL_SRC" ] && [ ! -d "$MODEL_SRC" ]; then
  warn "找不到 ONNX 模型源目录（fork 仓库 & miloco 包内均无）"
  warn "感知引擎可能跑不起来（perceive query 报 models_missing）"
  warn "修法：重新从 git checkout 目录运行本脚本，或从 upstream release 下载到 $MILOCO_HOME/models/"
elif [ -n "$MODEL_SRC" ]; then
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

# --- 5. patch ${MILOCO_HOME}/config.json (走 miloco-cli config set) ---
# Author #7 收敛:不再直接编辑 config.json 结构,改用 miloco-cli config set 写 agent.*
# (CLI 是 source of truth,插件不碰 config.json schema —— 未来 CLI 改键名不影响)
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 5 "miloco-cli config set 写 agent.webhook_url + agent.auth_bearer"
# 从 config.json 动态读取 backend 端口
BACKEND_PORT=$(_read_backend_port)
WEBHOOK_URL="http://127.0.0.1:${BACKEND_PORT}/miloco/webhook"

# 备份一次(防御:miloco-cli config set 若实现改了 schema,rollback 用)
TS="$(date +%Y%m%d-%H%M%S)-pid$$-nsec$(date +%N)"
if [ -f "$MILOCO_HOME/config.json" ]; then
  cp "$MILOCO_HOME/config.json" "${MILOCO_HOME}/config.json.bak-${TS}"
  # 清理老备份:保留最新 3 份
  old_baks="$(ls -1t "${MILOCO_HOME}"/config.json.bak-* 2>/dev/null | tail -n +4 || true)"
  if [ -n "$old_baks" ]; then
    rm -f $old_baks
    info "  清理老 config.json.bak:保留最新 3 份"
  fi
fi

# 走 CLI 写(Author #7:插件不碰 config.json 结构)。
# 三次 config set 都带 --no-restart，让 step 7 统一 restart 一次收敛——避免
# 每次 config set 都触发一次 supervisorctl restart（3 次冗余 restart，每次
# 带 _wait_for_health + sleep 3，纯拉长安装耗时）。参考 install.py:1623
# 批量 config set 的做法。
if miloco-cli config set agent.webhook_url "$WEBHOOK_URL" --no-restart 2>&1 | tail -3; then
  info "  webhook_url = $WEBHOOK_URL (via miloco-cli config set --no-restart)"
else
  err "miloco-cli config set agent.webhook_url 失败"
  exit 1
fi
if miloco-cli config set agent.auth_bearer "$BEARER" --no-restart 2>&1 | tail -3; then
  info "  auth_bearer = ${BEARER:0:8}... (via miloco-cli config set --no-restart)"
else
  err "miloco-cli config set agent.auth_bearer 失败"
  exit 1
fi
# 🔴#2 修复: 写 agent.platform=hermes 让 backend loader 加载 Adapter
# 不写则 loader 返回 None → dispatcher 静默丢弃所有入站 turn
if miloco-cli config set agent.platform hermes --no-restart 2>&1 | tail -3; then
  info "  agent.platform = hermes (via miloco-cli config set --no-restart)"
else
  # miloco-cli 可能不认识 agent.platform(旧版 CLI,PR 未合)
  # 降级: Python 直写 config.json
  warn "  miloco-cli config set agent.platform 失败,降级为 Python 直写 config.json"
  "$PYTHON" - "$MILOCO_HOME" <<'PY' && info "  agent.platform = hermes (via Python 直写)" || { err "agent.platform 写入失败"; exit 1; }
import json, sys
p = sys.argv[1] + '/config.json'
with open(p) as f:
    d = json.load(f)
d.setdefault('agent', {})['platform'] = 'hermes'
with open(p, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
print('agent.platform = hermes 已写入')
PY
fi
mark_done 5

# --- 6. patch ~/.hermes/.env（仅当缺失时追加）---
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 6 "确保 ${HERMES_HOME}/.env 有 API_SERVER_KEY"
touch "$HERMES_HOME/.env"
chmod 600 "$HERMES_HOME/.env"
if ! grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null; then
  echo "API_SERVER_KEY=$BEARER" >> "$HERMES_HOME/.env"
  info "已追加 API_SERVER_KEY 到 .env"
else
  warn ".env 已有 API_SERVER_KEY，保持原值"
fi
mark_done 6

# --- 7. 重启 backend ---
# Step 5 写了 agent.platform，必须重启 backend 才能加载新的 HermesAdapter（缓存刷新）。
# 否则旧进程缓存的是 WebhookAdapter fallback → onboarding 走 webhook → 405。
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 7 "重启 backend 加载 HermesAdapter（agent.platform 刚写入）"
info "  停止旧 backend"
miloco-cli service stop 2>/dev/null || true
sleep 3
info "  启动新 backend"
if ! START_OUTPUT=$(miloco-cli service start 2>&1); then
  err "backend 启动失败: $START_OUTPUT"
  exit 1
else
  if [ -n "$START_OUTPUT" ]; then
    info "  $START_OUTPUT"
  fi
  # 确认 /health
  for i in 1 2 3 4 5 6 7 8; do
    if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
      info "  backend /health OK (等了 ${i}s)"
      break
    fi
    sleep 1
  done
fi
mark_done 7

# --- 8. enable plugin（Hermes 是 opt-in，不 enable 就不会加载工具）---
[ "$POST_INSTALL_ONLY" -eq 1 ] || step 8 "enable Hermes 插件 miloco"
# plugin.yaml 里的 name 字段是 'miloco'，enable 时用它
if command -v hermes >/dev/null 2>&1; then
  # 已 enabled 跳过；未 enabled 才 enable
  # 对齐 install-guide-hermes.md:295 的严格模式：^enabled.*miloco$
  # 行内 "enabled" 在前、"miloco" 在后，避免 not enabled 假阳性
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

# --- 内联 cron reconcile（不依赖 gateway restart）---
# Hermes register() 只在 gateway 启动时跑一次。这里用 Hermes 的 venv Python
# 直接调 cron_setup.reconcile_cron_jobs()，与插件 register() 共享同一份任务定义，
# 消除双真源，且自动继承 L1 backend-readiness 守门。
HERMES_PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"
if [ -x "$HERMES_PYTHON" ]; then
  step 10 "创建/更新受管 cron job（调 cron_setup.reconcile_cron_jobs）"
  MILOCO_PLUGIN_DIR="$HERMES_PLUGINS_DIR/miloco-plugin"
  "$HERMES_PYTHON" - "$MILOCO_PLUGIN_DIR" "$MILOCO_HOME" <<'INNERPY' 2>&1 || true
import sys, os
sys.path.insert(0, sys.argv[1])
os.environ["MILOCO_HOME"] = sys.argv[2]
import cron_setup
result = cron_setup.reconcile_cron_jobs()
print(f"cron reconcile: created={result.get('created',0)} updated={result.get('updated',0)} "
      f"removed={result.get('removed',0)} active={result.get('active','N/A')} "
      f"skipped={result.get('skipped',False)}")
INNERPY
  mark_done 10
else
  warn "  找不到 Hermes Python ($HERMES_PYTHON)，跳过内联 cron reconcile"
  warn "  cron 将在下次 hermes gateway restart 时由插件 register() 兜底创建"
fi

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

[backend 状态]
    miloco-cli service status    # 看 supervisord / backend
    miloco-cli service logs      # tail 日志
    miloco-cli service restart   # 重启
    miloco-cli service stop      # 停

[配置文件位置]
    $MILOCO_HOME/config.json   # miloco 后端配置（已 patch）
    $HERMES_HOME/.env          # Hermes 环境（已追加 API_SERVER_KEY）
    $PLUGIN_STATE              # 插件 deliver.target
    $MILOCO_HOME/log/          # backend 日志

[想还原]
    ${MILOCO_HOME}/config.json.bak-${TS}  是 patch 前的备份
    $HERMES_HOME/.env 里去掉 API_SERVER_KEY 即可
    卸插件：rm -rf $HERMES_PLUGINS_DIR $HERMES_HOME/skills/miloco-*
    disable 插件：hermes plugins disable miloco

[详细文档] $HERE/README.md
============================================================
EOF
