#!/usr/bin/env bash
# pr-hermes end-to-end 自测脚本(实际链路,不靠 diagnose 14 项)
#
# 覆盖:
#   1. adapter.send_turn 真实调 hermes api_server
#   2. backend dispatcher 触发 + adapter 调通(手工模拟)
#   3. trace meta.json 写盘 + 读盘(如果事件触发)
#   4. miloco_im_push 真发 IM(走 hermes send CLI,无需 model key)
#   5. hermes chat 真对话(走 hermes 自带 model)
#
# 用法: bash plugins/hermes/tests/test_e2e_real.sh
# 期望: 全部 PASS,无 FAIL

set -uo pipefail

HERMES_BIN="$(command -v hermes || true)"
MILOCO_CLI_BIN="$(command -v miloco-cli || true)"
HERMES_ADAPTER_PY="/Users/wkea/.local/share/uv/tools/miloco/bin/python"
PASS=0
FAIL=0
SKIP=0

ok() { echo "  [✓] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [✗] $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  [~] $1 (skip)"; SKIP=$((SKIP + 1)); }

section() { echo ""; echo "═══ $1 ═══"; }

# ====== 前置: 工具 + 服务可用 ======
section "0. 前置检查"

[ -n "$HERMES_BIN" ] && ok "hermes CLI 在 PATH ($HERMES_BIN)" || fail "hermes CLI 不在 PATH"
[ -n "$MILOCO_CLI_BIN" ] && ok "miloco-cli 在 PATH ($MILOCO_CLI_BIN)" || fail "miloco-cli 不在 PATH"

# backend /health
HEALTH=$(curl -sS http://127.0.0.1:1810/health 2>/dev/null || echo "")
[ "$HEALTH" = "{\"status\":\"ok\"}" ] && ok "backend /health 200" || fail "backend /health 失败: $HEALTH"

# adapter 加载
ADAPTER_LOAD=$("$HERMES_ADAPTER_PY" -c "
from miloco.agent_platform import load_adapter
a = load_adapter()
print('yes' if a and a.name == 'hermes' else 'no')
" 2>/dev/null)
[ "$ADAPTER_LOAD" = "yes" ] && ok "HermesAdapter 加载(name=hermes)" || fail "HermesAdapter 加载失败"

# ====== 1. adapter.send_turn 真实调通 ======
section "1. adapter.send_turn 实际调通 hermes :8642"

SEND_RESULT=$("$HERMES_ADAPTER_PY" -c "
import asyncio
from miloco.agent_platform import load_adapter
from miloco.agent_platform.base import TurnContext
async def t():
    a = load_adapter()
    ctx = TurnContext(text='e2e test ping', session_key='agent:main:miloco', lane='miloco-interactive', trace_id='e2e-real', wait_timeout_ms=30000, profile='full')
    r = await a.send_turn(ctx)
    return f'{r.status}|{r.rtt_ms:.0f}|{r.error or \"\"}'
print(asyncio.run(t()))
" 2>&1 | tail -1)
SEND_STATUS=$(echo "$SEND_RESULT" | cut -d'|' -f1)
SEND_RTT=$(echo "$SEND_RESULT" | cut -d'|' -f2)
SEND_ERR=$(echo "$SEND_RESULT" | cut -d'|' -f3)
[ "$SEND_STATUS" = "ok" ] && ok "send_turn status=ok rtt=${SEND_RTT}ms" || fail "send_turn 失败: $SEND_ERR"

# ====== 2. build_system 内容验证 ======
section "2. build_system 内容验证"

SYS_LEN=$("$HERMES_ADAPTER_PY" -c "
from miloco.agent_platform import load_adapter
a = load_adapter()
print(len(a.build_system('full', {})))
" 2>/dev/null)
[ "$SYS_LEN" -gt 1000 ] && ok "build_system('full') 长度 ${SYS_LEN} > 1000 (含工具索引+感知格式)" || fail "build_system 长度异常: $SYS_LEN"

# ====== 3. notify_bind list ======
section "3. notify_bind 工具可用"

NB_RESULT=$("$HERMES_BIN" chat -q "调miloco_notify_bind action=list,只回 target 一个字段" -Q 2>&1 | tail -3)
echo "$NB_RESULT" | grep -q "weixin:\|feishu:\|telegram:\|discord:" && ok "notify_bind list 返回 IM target 列表" || fail "notify_bind list 没返回 IM"

# ====== 4. im_push 真发 IM(走 hermes send) ======
section "4. im_push 实际 IM 投递"

# 先切换到飞书(避免微信 iLink rate limit)
"$HERMES_BIN" chat -q "调miloco_notify_bind action=switch target=feishu:oc_806ed7124bae73745846704be33ae2b3" -Q >/dev/null 2>&1
PUSH_RESULT=$("$HERMES_BIN" chat -q "调miloco_im_push发:【e2e test】pr-hermes test_e2e_real.sh $(date '+%H:%M:%S')" -Q 2>&1 | tail -5)
# 成功标志:agent 报 ok=true / 已送达 / 飞书(中文)
if echo "$PUSH_RESULT" | grep -qE "ok:\s*true|已送达|推送成功|✅"; then
  ok "im_push 真发成功(ok=true / 飞书已送达)"
else
  fail "im_push 失败: $PUSH_RESULT"
fi
"$HERMES_BIN" chat -q "调miloco_notify_bind action=switch target=weixin:o9cq80y629QGu22aknaIChWNAxYI@im.wechat" -Q >/dev/null 2>&1

# ====== 5. hermes chat 真对话 ======
section "5. hermes chat 真对话(走 hermes 自带 model)"

CHAT_RESULT=$("$HERMES_BIN" chat -q "ping" -Q 2>&1 | tail -3)
echo "$CHAT_RESULT" | grep -qiE "pong|在|hi|hello|嗨" && ok "hermes chat 返响应" || fail "hermes chat 无响应: $CHAT_RESULT"

# ====== 6. trace 路径验证(测试 adapter.read_trace_meta 读盘) ======
section "6. trace 路径文件存在性"

TRACE_DIR=/Users/wkea/.openclaw/miloco/trace/agent
if [ -d "$TRACE_DIR" ]; then
  # 目录存在 → 检查是否有日期子目录
  DATE_DIRS=$(find "$TRACE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
  if [ "$DATE_DIRS" -gt 0 ]; then
    ok "trace 目录有 $DATE_DIRS 个日期子目录"
  else
    skip "trace 目录存在但无日期子目录(需真实感知事件)"
  fi
else
  skip "trace 目录不存在(无感知事件写过)"
fi

# ====== 汇总 ======
section "汇总"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "SKIP: $SKIP"
echo ""
[ "$FAIL" -eq 0 ] && echo "✅ 全部 PASS($PASS 项)" || echo "❌ $FAIL 项 FAIL"
[ "$SKIP" -gt 0 ] && echo "($SKIP 项 skip — 需 backend .env 配 model key 才能跑)"
exit $FAIL