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
for arg in "$@"; do
  case "$arg" in
    --diagnose) DIAGNOSE_ONLY=1 ;;
    --help|-h)
      cat <<EOF
用法：bash install-hermes.sh [options]
  （无参数）       完整安装（patch config / 写 .env / 复制 plugin / 启 adapter / enable plugin）
  --diagnose    自检模式：跑 12 项检查输出 ✓/✗，不做任何修改
  --reset-deliver 清空 state.json::deliver.target，强制重新探测 IM（搭配安装用）
  -h, --help    显示本帮助
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
    MILOCO_VER="$("$PY" -c 'import subprocess; r=subprocess.run(["miloco-cli","--version"],capture_output=True,text=True,timeout=5); print(r.stdout.strip()[:60])' 2>/dev/null || echo unknown)"
    diag "miloco-cli 在 PATH" 1 "$MILOCO_VER"
  else
    diag "miloco-cli 在 PATH" 0 "上游装：curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare"
  fi

  # 4. miloco backend 在跑
  if command -v miloco-cli >/dev/null 2>&1; then
    ML_OUT="$(miloco-cli service status 2>&1 || true)"
    if echo "$ML_OUT" | grep -qiE "running|active|ok|started"; then
      diag "miloco backend 在跑" 1
    else
      diag "miloco backend 在跑" 0 "miloco-cli service start"
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
    if hermes plugins list 2>/dev/null | grep -E "miloco.*enabled" >/dev/null 2>&1; then
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
      diag "adapter (launchd)" 0 "未加载 → bash plugins/hermes/scripts/miloco-adapter.sh start"
    fi
  elif get_pid_by_port "$ADAPTER_PORT" >/dev/null 2>&1 && [ -n "$(get_pid_by_port "$ADAPTER_PORT" | tr -d ' \r\n')" ]; then
    ADAPTER_PID_VAL="$(get_pid_by_port "$ADAPTER_PORT" | tr -d ' \r\n' | head -1)"
    diag "adapter 进程 (端口 $ADAPTER_PORT)" 1 "PID=$ADAPTER_PID_VAL"
  else
    diag "adapter 进程" 0 "bash plugins/hermes/scripts/miloco-adapter.sh start"
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
  err "找不到 ${MILOCO_HOME}/config.json，请确认 MILOCO_HOME 正确（或 export MILOCO_HOME=...）"; exit 1
fi
mark_done 1

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
mark_done 4

# --- 4.5 自动探测 Hermes 已配置的 IM 平台，写入插件 state.json ---
step 4.5 "探测 IM 平台 → 写 plugin state.json::deliver.target"
# 让 miloco_im_push 在 cron 场景下也能直接投递，不需要 LLM 在 cron session 里
# 完成"两段式 bind"（cron 没人可对话，原方案不可用）。
# 探测顺序：
#   1) ~/.hermes/auth.json 的 providers（真连接凭据，首选）
#   2) ~/.hermes/config.yaml 里哪些 platform 有 bot_token/token（fallback）
# 候选 platform 顺序：国内用户优先 weixin / feishu / wecom / dingtalk / qqbot
DETECTED_TARGETS_JSON="$(
  "$PYTHON" - "$HERMES_HOME" <<'PY'
import json, sys
from pathlib import Path
try:
    import yaml
except ImportError:
    yaml = None

home = Path(sys.argv[1])
# 候选顺序：国内用户优先（weixin/feishu/wecom/dingtalk/qqbot），然后海外主流
CANDIDATES = (
    "weixin", "feishu", "wecom", "dingtalk", "qqbot",
    "telegram", "discord", "slack",
    "whatsapp", "signal", "mattermost", "bluebubbles", "matrix",
)
# 各平台判定"已配置"的 token 字段名（config.yaml 段）
TOKEN_KEYS = {
    "telegram":  ("bot_token", "token"),
    "discord":   ("bot_token", "token"),
    "slack":     ("bot_token", "app_token"),
    "feishu":    ("app_id", "app_secret", "verification_token"),
    "wecom":     ("corp_id", "corp_secret", "agent_id"),
    "whatsapp":  ("phone_number", "access_token"),
    "signal":    ("phone_number",),
    "mattermost":("url", "token"),
    "dingtalk":  ("app_key", "app_secret"),
    "bluebubbles": ("server_url", "password"),
    "matrix":    ("homeserver", "access_token"),
    "qqbot":     ("app_id", "client_secret"),
    "weixin":    ("app_id", "app_secret", "token", "encoding_aes_key"),
}


def build_target(plat, sec):
    """把 platform 段解析成 send_message 的 target 串。"""
    hc = sec.get("home_channel") or {}
    chat_id = (hc.get("chat_id") if isinstance(hc, dict) else None) or ""
    thread_id = (hc.get("thread_id") if isinstance(hc, dict) else None) or ""
    if chat_id:
        return f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else "")
    return plat


found = []  # 保留所有候选，让 state.json.candidates 可见

# 1) auth.json providers（真实连接凭据；最权威）
auth_path = home / "auth.json"
auth_cfg = {}
if auth_path.is_file():
    try:
        auth_cfg = json.loads(auth_path.read_text(encoding="utf-8")) or {}
    except Exception:
        auth_cfg = {}
providers = auth_cfg.get("providers") if isinstance(auth_cfg, dict) else None
if isinstance(providers, dict):
    for plat in CANDIDATES:
        p = providers.get(plat)
        if isinstance(p, dict) and any(p.get(k) for k in ("connected", "status", "token", "bot_token", "app_id")):
            if p.get("connected") is True or p.get("status") == "connected":
                chat_id = p.get("chat_id") or p.get("home_chat_id") or ""
                thread_id = p.get("thread_id") or ""
                if chat_id:
                    found.append(f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else ""))
                else:
                    found.append(plat)

# 2) config.yaml 段（fallback；可能只是声明但未连）
if not found:
    cfg_path = home / "config.yaml"
    if cfg_path.is_file() and yaml is not None:
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
        for plat in CANDIDATES:
            sec = cfg.get(plat)
            if not isinstance(sec, dict):
                continue
            keys = TOKEN_KEYS.get(plat, ())
            if any(sec.get(k) for k in keys):
                found.append(build_target(plat, sec))

print(json.dumps(found, ensure_ascii=False), end="")
PY
)" || DETECTED_TARGETS_JSON='[]'

DETECTED_TARGET="$("$PYTHON" - "$DETECTED_TARGETS_JSON" <<'PY'
import json, sys
arr = json.loads(sys.argv[1])
print(arr[0] if arr else "", end="")
PY
)"

# state.json 必须写到 plugin 自己的目录里，因为 tools_notify.py::_state_path(ctx)
# 用 ctx.manifest.path / "state.json" 解析（manifest.path 指向 plugin dir）。
# 写到外面的话 plugin 永远读不到 → miloco_im_push 永远报 no deliver target。
PLUGIN_STATE="$HERMES_PLUGINS_DIR/miloco-plugin/state.json"
CANDIDATES_COUNT=$("$PYTHON" - "$DETECTED_TARGETS_JSON" <<'PY'
import json, sys
print(len(json.loads(sys.argv[1])), end="")
PY
)

# --- 4.6 升级保留旧 deliver.target（除非 --reset-deliver）---
RESET_DELIVER=0
for arg in "$@"; do
  case "$arg" in
    --reset-deliver) RESET_DELIVER=1 ;;
  esac
done
PRESERVED_TARGET=""
if [ "$RESET_DELIVER" -eq 0 ] && [ -f "$PLUGIN_STATE" ]; then
  PRESERVED_TARGET="$("$PYTHON" - "$PLUGIN_STATE" <<'PY'
import json, sys
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    t = (d.get("deliver") or {}).get("target")
    print(t or "", end="")
except Exception:
    print("", end="")
PY
)"
fi

"$PYTHON" - "$PLUGIN_STATE" "$DETECTED_TARGETS_JSON" "$PRESERVED_TARGET" <<'PY'
import json, sys, datetime
from pathlib import Path
path, candidates_json, preserved = sys.argv[1], sys.argv[2], sys.argv[3]
candidates = json.loads(candidates_json)
try:
    state = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
except Exception:
    state = {}
deliver = state.get("deliver") or {}
# 优先级：探测到的第一个 → 旧 state.json 的 target（用户手动改过）
target = (candidates[0] if candidates else None) or preserved or None
state["deliver"] = {
    "target": target,
    "auto_configured": bool(candidates) and target == (candidates[0] if candidates else None),
    "configured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source": "install-hermes.sh auto-detect (auth.json → config.yaml)",
    "candidates": candidates,
}
Path(path).parent.mkdir(parents=True, exist_ok=True)
Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"  → state.json deliver.target = {target}  (candidates: {len(candidates)})")
PY

if [ -n "$DETECTED_TARGET" ]; then
  info "通知投递已自动配置：target=$DETECTED_TARGET（候选 ${CANDIDATES_COUNT} 个，写入 state.json.candidates）"
elif [ -n "$PRESERVED_TARGET" ]; then
  info "未检测到新 IM 平台，复用旧 deliver.target=$PRESERVED_TARGET"
else
  warn "未检测到 Hermes 已配置的 IM 平台（auth.json / config.yaml 都空）"
  warn "  → miloco 主动通知将无法送达（miloco_im_push 会返回 no deliver target）。"
  warn "  → 装完请二选一："
  warn "     a) 在 Hermes 里连一个 IM（hermes config set telegram.bot_token ...）后重跑 install-hermes.sh"
  warn "     b) 手动编辑 ${PLUGIN_STATE}，加 deliver.target 字段，形如："
  warn "        {\"deliver\": {\"target\": \"telegram\"}}"
fi
mark_done 4.5

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
  if hermes plugins list 2>/dev/null | grep -E "miloco.*enabled" >/dev/null 2>&1; then
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

# --- 9. 记录版本到 state.json（升级一致性检查用） ---
step 9 "记录版本到 plugin state.json"
HERMES_VER="$(command -v hermes >/dev/null 2>&1 && hermes --version 2>&1 | head -1 || echo unknown)"
MILOCO_VER="$(command -v miloco-cli >/dev/null 2>&1 && miloco-cli --version 2>&1 | head -1 || echo unknown)"
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
    bash $HERE/scripts/miloco-adapter.sh status    # 看 PID / 端口
    bash $HERE/scripts/miloco-adapter.sh logs      # tail 日志
    bash $HERE/scripts/miloco-adapter.sh restart   # 重启
    bash $HERE/scripts/miloco-adapter.sh stop      # 停

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
