#!/usr/bin/env bash
# pr-hermes 真业务流 e2e(需 backend .env 配齐 model.omni.api_key + Xiaomi 账号已绑)
#
# 覆盖(按依赖顺序):
#   0. 前置(.env model.omni.api_key 配齐 + is_bound: true)
#   1. L1 守门激活:hermes gateway restart → 4 cron 应自动 active
#   2. 真实感知事件:miloco-cli perceive query → backend dispatch → adapter
#   3. trace meta 写盘:$MILOCO_HOME/trace/agent/<日期>/<run_id>.meta.json
#   4. backend poller 读盘:event_meta_v 或 agent_runs 表
#   5. 真实规则触发:miloco-cli rule list + 测试
#   6. 真实任务流:miloco-cli task list
#   7. hermes chat 调 skill(走 backend LLM 实际推理)
#   8. IM 投递真发(feishu target)
#   9. cron 自然触发(不主动 run,等定时器)

set -uo pipefail

HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
MILOCO_CLI_BIN="$(command -v miloco-cli 2>/dev/null || true)"
HERMES_ADAPTER_PY="/Users/wkea/.local/share/uv/tools/miloco/bin/python"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
PASS=0
FAIL=0
SKIP=0
ok() { echo "  [✓] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [✗] $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  [~] $1(skip)"; SKIP=$((SKIP + 1)); }
section() { echo ""; echo "═══ $1 ═══"; }

# === 0. 前置 ===
section "0. 前置检查 — 需 .env 配齐 + Xiaomi 账号"

[ -n "$HERMES_BIN" ] && ok "hermes CLI 在 PATH" || fail "hermes CLI 不在 PATH"
[ -n "$MILOCO_CLI_BIN" ] && ok "miloco-cli 在 PATH" || fail "miloco-cli 不在 PATH"

HEALTH=$(curl -sS http://127.0.0.1:1810/health 2>/dev/null || echo "")
[ "$HEALTH" = "{\"status\":\"ok\"}" ] && ok "backend /health 200" || fail "backend /health 失败: $HEALTH"

# 检查 model.omni.api_key(用 JSON 解析,避免 {"value": ""} 误判)
API_KEY=$("$MILOCO_CLI_BIN" config get model.omni.api_key 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); v=str(d.get('value','')); print('OK' if v.strip() else 'EMPTY')" \
  2>/dev/null || echo "EMPTY")
if [ "$API_KEY" = "OK" ]; then
  ok "model.omni.api_key 已配齐"
else
  fail "model.omni.api_key 未配!参考 backend/.env.example 或 install-guide §【可选】"
  echo "    修法: miloco-cli config set model.omni.api_key '<your-key>'"
fi

# 也检查 deliver target(顺手),不在的话多 fail 一个
DELIVER_TARGET=$("$MILOCO_CLI_BIN" account status 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('data',{}).get('is_bound'); print('YES' if v is True else 'NO')" \
  2>/dev/null || echo "NO")
[ "$DELIVER_TARGET" = "YES" ] || true  # 已在上面 fail,这里不再 fail

# 检查 Xiaomi 账号(数据嵌套 data.is_bound)
ACCT_BOUND=$("$MILOCO_CLI_BIN" account status 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('data',{}).get('is_bound'); print('YES' if v is True else 'NO')" \
  2>/dev/null || echo "NO")
if [ "$ACCT_BOUND" = "YES" ]; then
  ok "Xiaomi 账号已绑(is_bound: true)"
else
  fail "Xiaomi 账号未绑"
  echo "    修法: miloco-cli account bind → 跳链接 → agent 拿 base64 → miloco-cli account authorize '<base64>'"
fi

# === 1. L1 守门激活 ===
section "1. L1 守门激活 — backend 配齐后,reconcile 应自动 unpause 4 cron"

BEFORE=$(hermes cron list 2>&1 | grep -B1 "Name:.*\[miloco" | grep -oE "\[active\]|\[paused\]" | sort | uniq -c)
echo "  当前 miloco cron 状态: $BEFORE"

hermes gateway restart 2>&1 | tail -2
sleep 5

AFTER=$(hermes cron list 2>&1 | grep -B1 "Name:.*\[miloco" | grep -oE "\[active\]|\[paused\]" | sort | uniq -c)
echo "  重启后: $AFTER"
if echo "$AFTER" | grep -qE "[[:space:]]4[[:space:]]\[active\]"; then
  ok "L1 守门激活成功:4 个 cron 都 active"
else
  fail "L1 守门没激活,需手动 hermes cron resume <id>"
fi

# === 2. 感知事件 ===
section "2. 真实感知事件 — miloco-cli perceive query → backend dispatch → adapter"

if [ -d "$MILOCO_HOME/trace/agent" ]; then
  ok "trace 目录已存在($MILOCO_HOME/trace/agent)"
else
  skip "trace 目录不存在(没真实事件写过)"
fi

PERCEIVE=$("$MILOCO_CLI_BIN" perceive query --limit 5 2>&1 | head -20)
echo "  $PERCEIVE" | head -3
echo "$PERCEIVE" | grep -qE "events|perception|感知" && ok "perceive query 返事件" || skip "perceive query 没数据(可能没装摄像头)"

# === 3. trace meta 写盘 ===
section "3. trace meta 写盘(需要真实 turn 跑过)"

if [ -d "$MILOCO_HOME/trace/agent" ]; then
  META_FILES=$(find "$MILOCO_HOME/trace/agent" -name "*.meta.json" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$META_FILES" -gt 0 ]; then
    ok "$META_FILES 个 meta.json 已写"
  else
    skip "meta.json 还没写(需要真实 turn 跑过)"
  fi
else
  skip "trace 目录不存在"
fi

# === 4. backend poller 读盘 ===
section "4. backend poller 读盘(看 agent_runs 表有数据)"

POLLER_CHECK=$("$HERMES_ADAPTER_PY" -c "
import sqlite3
import os
db = os.path.expanduser('~/.openclaw/miloco/observability.db')
if not os.path.exists(db):
    print('NO_DB')
else:
    conn = sqlite3.connect(db)
    try:
        n = conn.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0]
        print(f'AGENT_RUNS={n}')
    except Exception as e:
        print(f'ERR={e}')
    finally:
        conn.close()
" 2>&1 | tail -1)
if [ "$POLLER_CHECK" = "NO_DB" ]; then
  skip "observability.db 不存在"
elif [[ "$POLLER_CHECK" == AGENT_RUNS=* ]]; then
  COUNT=${POLLER_CHECK#AGENT_RUNS=}
  if [ "$COUNT" -gt 0 ]; then
    ok "agent_runs 表有 $COUNT 条记录(poller 写成功)"
  else
    skip "agent_runs 表空(无真实 turn 跑过)"
  fi
else
  fail "agent_runs 查询失败: $POLLER_CHECK"
fi

# === 5. 规则列表 ===
section "5. 真实规则列表 — miloco-cli rule list"

RULE_LIST=$("$MILOCO_CLI_BIN" rule list 2>&1 | head -20)
RULE_COUNT=$(echo "$RULE_LIST" | grep -cE "^[a-f0-9-]{36}" || true)
if [ "$RULE_COUNT" -gt 0 ]; then
  ok "rule list 有 $RULE_COUNT 条规则"
else
  skip "rule list 为空(没绑小米账号/没设备/没规则)"
fi

# === 6. 任务列表 ===
section "6. 真实任务列表 — miloco-cli task list"

TASK_LIST=$("$MILOCO_CLI_BIN" task list 2>&1 | head -20)
TASK_COUNT=$(echo "$TASK_LIST" | grep -cE "^[a-f0-9-]{36}" || true)
if [ "$TASK_COUNT" -gt 0 ]; then
  ok "task list 有 $TASK_COUNT 条任务"
else
  skip "task list 为空(没任务)"
fi

# === 7. hermes chat 调 skill(走 backend LLM)===
section "7. hermes chat 实际调 skill 走 backend LLM"

# 简单测:让 hermes 调一个 skill,看是否走 LLM 推理
HERMES_RESPONSE=$("$HERMES_BIN" chat -q "加载 miloco-devices skill,告诉我设备列表" -Q 2>&1 | tail -5)
echo "  $HERMES_RESPONSE" | head -3
# 匹配多种设备类(灯/空调/热水壶/网关/did/设备)+ 排除 no_devices
if echo "$HERMES_RESPONSE" | grep -qE "灯|空调|热水|网关|扫地|音箱|did|设备|无.*设备"; then
  if echo "$HERMES_RESPONSE" | grep -qE "无设备|没绑.*账号"; then
    skip "hermes chat 调 miloco-devices 返无设备(没绑账号/没设备)"
  else
    ok "hermes chat 调 miloco-devices 返设备相关响应"
  fi
else
  fail "hermes chat 调 skill 没返设备响应: $(echo "$HERMES_RESPONSE" | head -1)"
fi

# === 8. IM 投递真发(用飞书) ===
section "8. IM 真发 — miloco_im_push 切飞书 + 真发 + 切回(可能需 60-90s)"

"$HERMES_BIN" chat -q "调miloco_notify_bind action=switch target=feishu:oc_806ed7124bae73745846704be33ae2b3" -Q >/dev/null 2>&1
echo "  (切到飞书,等新会话建立…)"
# 飞书新会话首次建立可能 60s+,给足够时间
PUSH=$("$HERMES_BIN" chat -q "调miloco_im_push发:【pr-hermes 真业务流测试 #$(date +%H%M%S)】model + account 都配好" -Q 2>&1 | tail -5)
echo "  $PUSH" | head -2
echo "$PUSH" | grep -qE "已送达|飞书.*ok|✅|搞定.*飞书" && ok "im_push 真发飞书" || fail "im_push 失败"
"$HERMES_BIN" chat -q "调miloco_notify_bind action=switch target=weixin:o9cq80y629QGu22aknaIChWNAxYI@im.wechat" -Q >/dev/null 2>&1

# === 9. 设备列表 ===
section "9. 真实设备列表 — miloco-cli device list"

DEVICE_LIST=$("$MILOCO_CLI_BIN" device list 2>&1 | head -20)
DEVICE_COUNT=$(echo "$DEVICE_LIST" | grep -cE "^[a-f0-9-]{36}|did=" || true)
if [ "$DEVICE_COUNT" -gt 0 ]; then
  ok "device list 有 $DEVICE_COUNT 个设备"
else
  skip "device list 空(没绑账号/没设备)"
fi

# === 汇总 ===
section "汇总"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "SKIP: $SKIP"
echo ""
[ "$FAIL" -eq 0 ] && echo "✅ 全部 PASS($PASS 项)" || echo "❌ $FAIL 项 FAIL"
[ "$SKIP" -gt 0 ] && echo "($SKIP 项 skip — 需真实业务事件/设备)"
exit $FAIL
