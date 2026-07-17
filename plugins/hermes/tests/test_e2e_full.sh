#!/usr/bin/env bash
# 完整端到端验证脚本：从干净环境安装 → 逐项验证
# 用法: bash plugins/hermes/tests/test_e2e_full.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0
ok() { printf "  ${GREEN}[✓]${NC} %s\n" "$1"; PASS=$((PASS+1)); }
no() { printf "  ${RED}[✗]${NC} %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  ${YELLOW}[!]${NC} %s\n" "$1"; WARN=$((WARN+1)); }
section() { printf "\n── %s ──\n" "$1"; }

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.hermes/miloco}"
HERMES_BIN="$HOME/.local/bin/hermes"
MILOCO_CLI="$HOME/.local/bin/miloco-cli"
BACKEND_URL=$(python3 -c "
import json, os
mh = os.environ.get('MILOCO_HOME', os.path.expanduser('~/.openclaw/miloco'))
try:
    cfg = json.load(open(os.path.join(mh, 'config.json')))
    print(cfg.get('server', {}).get('url', 'http://127.0.0.1:1810'))
except: print('http://127.0.0.1:1810')
" 2>/dev/null)

cd "$(dirname "$0")/../../.."  # 切到 repo 根 (tests → hermes → plugins → root)

# ═══════════════════════════════════════════════════════════════════════
section "Phase 0: 干净环境"
# ═══════════════════════════════════════════════════════════════════════

echo "  停止 backend..."
"$MILOCO_CLI" service stop 2>/dev/null || true
supervisorctl -c "$MILOCO_HOME/supervisord.conf" shutdown 2>/dev/null || true
pkill -f "python.*miloco.main" 2>/dev/null || true
sleep 2

echo "  清理 miloco 残留..."
rm -rf "$HERMES_HOME/plugins/miloco"
rm -rf "$HERMES_HOME/skills/miloco-"*
rm -rf "$MILOCO_HOME/agent_platform"
rm -rf "$MILOCO_HOME/trace"
rm -f "$MILOCO_HOME/supervisord.pid" "$MILOCO_HOME/supervisord.sock"

# 清 cron
if python3 -c "import json,os; p=os.path.expanduser('$HERMES_HOME/jobs.json'); json.dump([j for j in json.load(open(p)) if 'miloco' not in str(j.get('name','')).lower()], open(p,'w'),indent=2) if os.path.exists(p) else None" 2>/dev/null; then :; fi

echo "  确认无残留..."
[ ! -d "$HERMES_HOME/plugins/miloco" ] && ok "插件已清除" || no "插件残留"
[ -z "$(ls "$HERMES_HOME/skills/miloco-"* 2>/dev/null)" ] && ok "skills 已清除" || no "skills 残留"
[ ! -d "$MILOCO_HOME/agent_platform" ] && ok "agent_platform 已清除" || no "agent_platform 残留"
lsof -i :1810 -sTCP:LISTEN 2>/dev/null | grep -q . && no "端口 1810 仍被占用" || ok "端口 1810 空闲"

# ═══════════════════════════════════════════════════════════════════════
section "Phase 1: 全新安装"
# ═══════════════════════════════════════════════════════════════════════

echo "  运行 install-hermes.sh ..."
export MILOCO_HOME HERMES_HOME
INSTALL_EXIT=0
bash plugins/hermes/install-hermes.sh > /tmp/install-e2e-$(date +%H%M%S).log 2>&1 || INSTALL_EXIT=$?
[ "$INSTALL_EXIT" -eq 0 ] && ok "install-hermes.sh exit=0" || no "install-hermes.sh exit=$INSTALL_EXIT"

sleep 3

# ═══════════════════════════════════════════════════════════════════════
section "Phase 2: Backend + 感知引擎"
# ═══════════════════════════════════════════════════════════════════════

HEALTH=$(python3 -c "import urllib.request; print(urllib.request.urlopen('$BACKEND_URL/health',timeout=5).read().decode())" 2>/dev/null || echo "")
[ "$HEALTH" = '{"status":"ok"}' ] && ok "backend /health" || no "backend /health: $HEALTH"

TOKEN=$(python3 -c "import json;print(json.load(open('$MILOCO_HOME/config.json'))['server']['token'])")
ENGINE_OK=$(python3 -c "
import urllib.request, json
r = urllib.request.urlopen(urllib.request.Request('$BACKEND_URL/api/perception/engine/status', headers={'Authorization':'Bearer $TOKEN'}))
d = json.loads(r.read())
print('OK' if d['data']['engine']['ready'] and d['data']['running'] else 'FAIL')
print(f\"  cameras={len(d['data']['active_sources'])} inferences={d['data']['today_inference_count']}\")
" 2>/dev/null)
echo "$ENGINE_OK" | grep -q ^OK && ok "感知引擎 ready=True" || no "感知引擎未就绪"

# ═══════════════════════════════════════════════════════════════════════
section "Phase 3: 文件完整性"
# ═══════════════════════════════════════════════════════════════════════

S=$(ls "$HERMES_HOME/skills/miloco-"* 2>/dev/null | wc -l | tr -d ' ')
[ "$S" -ge 16 ] && ok "$S skills" || no "skills: $S (<16)"

for f in __init__.py adapter.py context_injection.py catalog.py paths.py tools_habit.py; do
  [ -f "$MILOCO_HOME/agent_platform/hermes/$f" ] && ok "agent_platform/$f" || no "agent_platform/$f 缺失"
done

"$HERMES_BIN" plugins list --plain --no-bundled 2>/dev/null | grep -q miloco && ok "moco 插件已加载" || no "moco 插件未加载"

# ═══════════════════════════════════════════════════════════════════════
section "Phase 4: Cron jobs"
# ═══════════════════════════════════════════════════════════════════════

CRON_OUT=$("$HERMES_BIN" cron list 2>&1)
C=$(echo "$CRON_OUT" | grep -c miloco || echo 0)
[ "$C" -ge 4 ] && ok "$C cron jobs (>=4)" || warn "cron: $C 个 (需 gateway restart？)"

# 检查 deliver 字段不是 None
DELIVER_CHECK=$(echo "$CRON_OUT" | grep -A5 miloco | grep "Deliver:" | head -5 || echo "")
if echo "$DELIVER_CHECK" | grep -q "local" 2>/dev/null; then
  ok "deliver=local (非 null)"
else
  echo "$DELIVER_CHECK" | grep -q . && warn "deliver 值: $(echo $DELIVER_CHECK | head -1)" || warn "cron 未显示 deliver 字段"
fi

# ═══════════════════════════════════════════════════════════════════════
section "Phase 5: IM 探测 + owner session"
# ═══════════════════════════════════════════════════════════════════════

python3 -c "
import sys; sys.path.insert(0,'$HERMES_HOME/plugins/miloco/miloco-plugin')
from tools_notify import _detect_im_platforms_simple
r = _detect_im_platforms_simple()
print(r)
assert len(r) > 0, 'no IM found'
" 2>/dev/null | grep -v objc
python3 -c "import sys;sys.path.insert(0,'$HERMES_HOME/plugins/miloco/miloco-plugin');from tools_notify import _detect_im_platforms_simple;assert len(_detect_im_platforms_simple())>0" 2>/dev/null && ok "IM 探测有结果" || no "IM 探测空"

python3 -c "
import sys; sys.path.insert(0,'$MILOCO_HOME/agent_platform/hermes')
from adapter import _resolve_owner_session
s,p = _resolve_owner_session()
assert s is not None and p is not None
print(f'  session={s} platform={p}')
" 2>/dev/null | grep -v objc
python3 -c "import sys;sys.path.insert(0,'$MILOCO_HOME/agent_platform/hermes');from adapter import _resolve_owner_session;s,p=_resolve_owner_session();assert s is not None" 2>/dev/null && ok "owner session 解析成功" || no "owner session 失败"

# ═══════════════════════════════════════════════════════════════════════
section "Phase 6: 全量 pytest"
# ═══════════════════════════════════════════════════════════════════════

echo "  运行 199 tests..."
PYTEST_OUT=$(cd "$(dirname "$0")/../../.." && uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q 2>&1) && PYTEST_OK=1 || PYTEST_OK=0
echo "$PYTEST_OUT" | tail -3
[ "$PYTEST_OK" -eq 1 ] && ok "pytest 全绿" || no "pytest 有失败"

# ═══════════════════════════════════════════════════════════════════════
section "汇总"
# ═══════════════════════════════════════════════════════════════════════

printf "\n${GREEN}PASS: %d${NC}  ${RED}FAIL: %d${NC}  ${YELLOW}WARN: %d${NC}\n\n" $PASS $FAIL $WARN
if [ "$FAIL" -eq 0 ] && [ "$WARN" -le 1 ]; then
  echo "✅ 全部通过"
  exit 0
elif [ "$FAIL" -eq 0 ]; then
  echo "⚠️  通过但有警告"
  exit 0
else
  echo "❌ 有失败项"
  exit 1
fi
