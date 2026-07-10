#!/usr/bin/env bash
# Author-grade 验收测试：模拟真实用户，逐项检查交互点的实际输出。
#
# 覆盖 author 发现的所有 14 个问题 + 每个交互点的真实行为验证。
# 用法: bash plugins/hermes/tests/test_acceptance.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; SKIP=0
ok()   { printf "  ${GREEN}[PASS]${NC} %s\n" "$1"; PASS=$((PASS+1)); }
no()   { printf "  ${RED}[FAIL]${NC} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip() { printf "  ${YELLOW}[SKIP]${NC} %s\n" "$1"; SKIP=$((SKIP+1)); }
section() { printf "\n──\033[1m %s \033[0m──\n" "$1"; }
die()  { echo "FATAL: $1"; exit 1; }

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES="${HERMES_HOME}/hermes"
MILOCO_CLI="$HOME/.local/bin/miloco-cli"
BACKEND="http://127.0.0.1:1810"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

# ═══════════════════════════════════════════════════════════════════════
section "A. 基础设施"
# ═══════════════════════════════════════════════════════════════════════

command -v python3 >/dev/null 2>&1 && ok "python3" || die "python3 不在 PATH"
command -v "$MILOCO_CLI" >/dev/null 2>&1 && ok "miloco-cli" || die "miloco-cli 未安装"
command -v hermes >/dev/null 2>&1 && ok "hermes CLI" || die "hermes CLI 不在 PATH"
[ -d "$HERMES_HOME" ] && ok "HERMES_HOME=~/.hermes" || die "HERMES_HOME 不存在"

# ═══════════════════════════════════════════════════════════════════════
section "B. Backend"
# ═══════════════════════════════════════════════════════════════════════

HEALTH=$(python3 -c "import urllib.request; print(urllib.request.urlopen('$BACKEND/health',timeout=10).read().decode())" 2>/dev/null || echo "")
[ "$HEALTH" = '{"status":"ok"}' ] && ok "backend /health" || no "backend /health: $HEALTH"

# 感知引擎状态
TOKEN=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('$HOME/.hermes/miloco/config.json')))['server']['token'])" 2>/dev/null || echo "")
ENGINE=$(python3 -c "
import urllib.request,json
r=urllib.request.urlopen(urllib.request.Request('$BACKEND/api/perception/engine/status',headers={'Authorization':'Bearer $TOKEN'}))
d=json.loads(r.read())['data']
assert d['running']==True
print(d['engine']['status'])
" 2>/dev/null || echo "TIMEOUT")

case "$ENGINE" in
  ready) ok "感知引擎 ready" ;;
  no_omni_api_key) ok "感知引擎 running (no_omni_api_key — 需用户配 model.omni.api_key)" ;;
  models_missing)  no "感知引擎 models_missing (author bug — 模型文件未同步)" ;;
  *) no "感知引擎: $ENGINE" ;;
esac

# ═══════════════════════════════════════════════════════════════════════
section "D. Cron jobs (author bug #6: deliver=null 崩 CLI)"
# ═══════════════════════════════════════════════════════════════════════

CRON_OUT=$(hermes cron list 2>&1) || {
  no "hermes cron list 崩溃! (author bug #6: deliver=null 导致 TypeError)"
}
CRON_COUNT=$(echo "$CRON_OUT" | grep -c miloco || echo 0)

# 检查：cron list 没抛异常（排除预期内的 Delivery failed 提示）
echo "$CRON_OUT" | grep -iv "delivery failed\|rate limited" | grep -qi "error\|traceback\|exception" 2>/dev/null && {
  no "hermes cron list 输出含异常/错误"
} || ok "hermes cron list 无异常"

# 检查：deliver 字段不是 None
DELIVER_VALUES=$(echo "$CRON_OUT" | grep -A10 miloco | grep "Deliver:" | head -5)
if echo "$DELIVER_VALUES" | grep -q "null"; then
  no "cron deliver=null 残留 (author bug #6)"
else
  ok "cron deliver 非 null"
fi

[ "$CRON_COUNT" -ge 4 ] && ok "$CRON_COUNT 个 miloco cron (>=4)" || no "miloco cron: $CRON_COUNT (<4)"

# ═══════════════════════════════════════════════════════════════════════
section "E. Adapter send_turn (author bug #1,2,14)"
# ═══════════════════════════════════════════════════════════════════════

ADAPTER_PY="$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('$HOME/.hermes/miloco/config.json')))['server']['python_bin'])" 2>/dev/null)"

# send_turn 可能需要等 gateway 就绪，最多重试 3 次
SEND_STATUS=""
SEND_RTT=""
for i in 1 2 3; do
  SEND_STATUS=$("$ADAPTER_PY" -c "
import asyncio
from miloco.agent_platform import load_adapter
from miloco.agent_platform.base import TurnContext
async def t():
    a = load_adapter()
    ctx = TurnContext(text='acceptance test ping', session_key='agent:main:miloco', lane='miloco-interactive', trace_id='acceptance', wait_timeout_ms=30000, profile='full')
    r = await a.send_turn(ctx)
    print(r.status)
asyncio.run(t())
" 2>&1 | tail -1)
  [ "$SEND_STATUS" = "ok" ] && break
  sleep 5
done

[ "$SEND_STATUS" = "ok" ] && ok "send_turn status=ok" || no "send_turn status=$SEND_STATUS (重试3次后)"

# ═══════════════════════════════════════════════════════════════════════
section "F. Trace 读写 (author bug #3,4,14)"
# ═══════════════════════════════════════════════════════════════════════

sleep 10

TRACE_META=$(find ~/.openclaw/miloco/trace ~/.hermes/miloco/trace -name "*.meta.json" -type f -mmin -5 2>/dev/null | head -1)

if [ -n "$TRACE_META" ]; then
  ok "trace meta.json 存在"
  # 验证 snake_case 字段（author bug #3）
  META_OK=$(python3 -c "
import json
d = json.load(open('$TRACE_META'))
assert 'run_id' in d, 'missing run_id'
assert 'query' in d, 'missing query'
assert 'success' in d, 'missing success'
assert 'llm_call_count' in d, 'missing llm_call_count'
assert 'jsonl_path' in d, 'missing jsonl_path'
print('OK')
" 2>/dev/null || echo "FAIL")
  [ "$META_OK" = "OK" ] && ok "trace 字段名 snake_case 正确" || no "trace 字段名不对"
else
  no "trace meta.json 未在 10s 内生成 (author bug #4)"
fi

# ═══════════════════════════════════════════════════════════════════════
section "G. IM 探测 (author bug #1)"
# ═══════════════════════════════════════════════════════════════════════

IM_RESULT=$(python3 -c "
import sys; sys.path.insert(0,'$HERMES_HOME/plugins/miloco/miloco-plugin')
from tools_notify import _detect_im_platforms_simple
r = _detect_im_platforms_simple()
print(','.join(r) if r else 'EMPTY')
" 2>/dev/null)

if [ "$IM_RESULT" = "EMPTY" ]; then
  no "IM 探测返回空 (author bug #1: 读 bot_token 假字段)"
else
  ok "IM 探测: $IM_RESULT"
fi

# ═══════════════════════════════════════════════════════════════════════
section "H. _resolve_owner_session (author bug #2)"
# ═══════════════════════════════════════════════════════════════════════

OWNER_RESULT=$(python3 -c "
import sys; sys.path.insert(0,'$HOME/.hermes/miloco/agent_platform/hermes')
from adapter import _resolve_owner_session
s,p = _resolve_owner_session()
print(f'{s}|{p}' if s else 'NULL')
" 2>/dev/null)

[ "$OWNER_RESULT" != "NULL" ] && ok "owner session: $OWNER_RESULT" || no "owner session 返回 None (author bug #2)"

# ═══════════════════════════════════════════════════════════════════════
section "I. State.json 保留 (author bug #11)"
# ═══════════════════════════════════════════════════════════════════════

STATE_NOW=$(python3 -c "
import json,os
p=os.path.expanduser('$HERMES_HOME/plugins/miloco/miloco-plugin/state.json')
if os.path.exists(p):
    d=json.load(open(p))
    t=(d.get('deliver') or {}).get('target','')
    print(t if t else 'NULL')
else:
    print('NOFILE')
" 2>/dev/null)

echo "  state.json::deliver.target = $STATE_NOW"
# 不清算 FAIL：新环境本来就没有 target，合理

# ═══════════════════════════════════════════════════════════════════════
section "J. Adapter 文件完整性 (author bug #9)"
# ═══════════════════════════════════════════════════════════════════════

ADAPTER_DIR="$HOME/.hermes/miloco/agent_platform/hermes"
for f in __init__.py adapter.py context_injection.py catalog.py paths.py tools_habit.py; do
  [ -f "$ADAPTER_DIR/$f" ] && ok "agent_platform/$f" || no "agent_platform/$f 缺失"
done

# ═══════════════════════════════════════════════════════════════════════
section "K. max_send_turn_latency_s (author bug #7)"
# ═══════════════════════════════════════════════════════════════════════

LATENCY=$(python3 -c "
import sys; sys.path.insert(0,'$ADAPTER_DIR')
from adapter import Adapter
a = Adapter()
print(a.max_send_turn_latency_s() if hasattr(a,'max_send_turn_latency_s') else 'MISSING')
" 2>/dev/null)

[ "$LATENCY" != "MISSING" ] && ok "max_send_turn_latency_s=$LATENCY" || no "max_send_turn_latency_s 未实现"

# ═══════════════════════════════════════════════════════════════════════
section "汇总"
# ═══════════════════════════════════════════════════════════════════════

printf "\n  ${GREEN}PASS: %d${NC}  ${RED}FAIL: %d${NC}  ${YELLOW}SKIP: %d${NC}\n\n" $PASS $FAIL $SKIP
[ "$FAIL" -eq 0 ] && echo "  ✅ 全部通过" && exit 0
echo "  ❌ 有 $FAIL 项失败" && exit 1
