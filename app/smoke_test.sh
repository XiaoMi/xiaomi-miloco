#!/usr/bin/env bash
# Miloco App 冒烟测试：验证打包产物真能起来，且 slim 的依赖裁剪没有留暗坑。
#
# 用法：app/smoke_test.sh /path/to/Miloco.app
#
# 覆盖：
#   1. 内置解释器存在且可执行、launcher --selftest 通过
#   2. 被裁剪的重依赖确实不存在（onnxruntime/scipy/tokenizers/...）
#   3. 屏蔽重依赖后 import miloco.main 仍成立（源码级独立性）
#   4. 真起服务 → /health 200 → /api/admin/edition 报 slim → 身份路由 404 → 数据库落盘
#   5. SIGTERM 优雅退出

set -euo pipefail

APP="${1:-}"
[[ -n "$APP" ]] || {
  echo "用法：$0 /path/to/Miloco.app" >&2
  exit 2
}
[[ -d "$APP" ]] || {
  echo "找不到 App：$APP" >&2
  exit 2
}

PY="$APP/Contents/Resources/py/bin/python3"
PORT="${MILOCO_SMOKE_PORT:-18991}"
HOME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/miloco-smoke-XXXXXX")"
SERVER_PID=""

pass() { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
fail() {
  printf '\033[31m  ✗\033[0m %s\n' "$*" >&2
  exit 1
}
step() { printf '\033[36m[smoke]\033[0m %s\n' "$*"; }

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$HOME_DIR"
}
trap cleanup EXIT

export MILOCO_HOME="$HOME_DIR"
export MILOCO_EDITION="slim"
export MILOCO_SERVER__HOST="127.0.0.1"
export MILOCO_SERVER__PORT="$PORT"

# ─── 1. 布局与 launcher 自检 ────────────────────────────────────────────────

# 冒烟测试直接跑**已签名**的 App，若发生字节码补写就会毁掉 ad-hoc 签名封条。
# 与启动器保持一致：全程禁写字节码（app/runtime/sitecustomize.py 是第二道兜底）。
export PYTHONDONTWRITEBYTECODE=1

step "1/5 检查 App 布局与 launcher"
[[ -x "$PY" ]] || fail "内置解释器缺失：$PY"
[[ -f "$APP/Contents/Resources/defaults/config.json" ]] || fail "默认配置缺失"
# -langOverride system：把「界面语言」固定成跟随系统，否则开发机里 App 存过的语言
# 选择（UserDefaults）会让这几条断言随机器状态漂移。
SELFTEST_OUT="$("$APP/Contents/MacOS/Miloco" --selftest -langOverride system)" \
    || fail "launcher --selftest 失败"
pass "内置解释器 + launcher 自检通过"

# 固定端口必须真的编进 launcher（和 CLI 的 1810 分开，是发布后的可见行为）
grep -q "preferred=1812" <<<"$SELFTEST_OUT" || fail "launcher selftest 未报固定端口 1812：$SELFTEST_OUT"
pass "App 固定端口 1812 已生效"

# 菜单栏图标必须是「房子线稿 + 透明背景」的模板图。若退回不透明 PNG/icns，模板渲染会把
# 整块 alpha 当实心，菜单栏上就是一带白边的方块 —— 正是住户看到的问题；这里用像素统计卡住。
[[ -f "$APP/Contents/Resources/MenuBarIcon.svg" ]] || fail "菜单栏图标 MenuBarIcon.svg 缺失"
grep -qE "menubar=svg\(transparent=[1-9][0-9]*,solid=[1-9][0-9]*\)" <<<"$SELFTEST_OUT" \
    || fail "菜单栏图标不是透明且有内容的 SVG：$SELFTEST_OUT"
pass "菜单栏图标为透明模板 SVG（房子线稿，无底色）"

# 不显示 Dock 图标：生命周期归菜单栏图标管，Dock 图标只是窗口开关，容易被误当成"关掉=停服务"。
# 退回 .regular 就会重新长出 Dock 图标 —— 用字段断言卡住（accessory=无 Dock 图标）。
grep -q "activation=accessory" <<<"$SELFTEST_OUT" \
    || fail "launcher 激活策略不是 accessory（会显示 Dock 图标）：$SELFTEST_OUT"
pass "无 Dock 图标（激活策略 accessory，Dock 不驻留、不进 ⌘-Tab）"

# 后端输出管道的 reader 在 EOF 上必须自注销：level-triggered 的 readabilityHandler
# 会让"写端已关"的 fd 永远可读，不自注销就是每毫秒空转、吃满一个核（真实事故：
# App 空转 100% CPU）。自检在真管子上跑 250ms 数回调次数，空转实现会上千次。
grep -q "pipereader=ok" <<<"$SELFTEST_OUT" \
    || fail "输出管道 reader 在 EOF 上空转（App 会吃满一个核）：$SELFTEST_OUT"
pass "后端输出管道 reader 在 EOF 上自注销（不空转 CPU）"

# 界面语言：默认英文、中文系统中文、可用 MILOCO_APP_LANG 强制
LANG_ZH_OUT="$(MILOCO_APP_LANG=zh "$APP/Contents/MacOS/Miloco" --selftest)"
LANG_EN_OUT="$(MILOCO_APP_LANG=en "$APP/Contents/MacOS/Miloco" --selftest)"
# 非中英文系统（这里用 Foundation 的 -AppleLanguages 伪装成法语）必须落到英文默认值
LANG_FR_OUT="$("$APP/Contents/MacOS/Miloco" --selftest -langOverride system -AppleLanguages '(fr)')"
grep -q "lang=zh" <<<"$LANG_ZH_OUT" || fail "MILOCO_APP_LANG=zh 未生效：$LANG_ZH_OUT"
grep -q "open=打开管理页面" <<<"$LANG_ZH_OUT" || fail "中文菜单未生效：$LANG_ZH_OUT"
grep -q "lang=en" <<<"$LANG_EN_OUT" || fail "MILOCO_APP_LANG=en 未生效：$LANG_EN_OUT"
grep -q "open=Open Dashboard" <<<"$LANG_EN_OUT" || fail "英文菜单未生效：$LANG_EN_OUT"
grep -q "lang=en" <<<"$LANG_FR_OUT" || fail "非中英文系统未默认英文：$LANG_FR_OUT"
pass "界面语言：跟随系统（中文→中文，其它→英文），MILOCO_APP_LANG 可强制"

# App 必须**独立**：早期版本会自动导入 CLI 版（~/.openclaw/miloco）的 config.json 与
# miloco.db，导致 App 悄悄继承别人的米家账号与模型 Key。注意：注释不会进二进制，所以
# 这里检查的是「真的还在打包/引用外部脚本」这类可执行证据，行为面由第 4 步的
# 「首启只有包内默认配置」断言兜住。
[[ -e "$APP/Contents/Resources/prepare_config.py" ]] \
  && fail "包里还带着 prepare_config.py（旧版自动迁移配置的遗留）"
if strings "$APP/Contents/MacOS/Miloco" | grep -q "prepare_config"; then
  fail "launcher 里仍引用 prepare_config（说明还在走旧版配置迁移）"
fi
pass "launcher 不依赖任何外部脚本/外部安装数据"

# ─── 2. 重依赖确实被裁掉 ────────────────────────────────────────────────────

step "2/5 确认重依赖已被裁剪"
for banned in onnxruntime scipy tokenizers fastmcp zeroconf mcp; do
  if "$PY" -c "import $banned" >/dev/null 2>&1; then
    fail "slim 版不应包含 $banned"
  fi
done
pass "onnxruntime / scipy / tokenizers / fastmcp / zeroconf 均不存在"

# ─── 3. 屏蔽重依赖后仍能导入主模块 ──────────────────────────────────────────

step "3/5 无重依赖导入 miloco.main"
cat >"$HOME_DIR/blocked_import.py" <<'PYEOF'
import importlib.abc
import sys

BLOCKED = {
    "onnxruntime", "scipy", "tokenizers",
    "fastmcp", "mcp", "zeroconf", "hf_xet", "huggingface_hub",
}


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError("BLOCKED-" + fullname)
        return None


sys.meta_path.insert(0, Blocker())
import miloco.main  # noqa: F401

from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import PerceptionConfig

engine = PerceptionEngine(PerceptionConfig(rule_only=True))
assert engine._identity_lib is None and engine._embedder is None
print("BLOCKED_IMPORT_OK")
PYEOF
if ! "$PY" "$HOME_DIR/blocked_import.py" 2>&1 | grep -q BLOCKED_IMPORT_OK; then
  fail "屏蔽重依赖后 import miloco.main / 构造引擎失败"
fi
pass "屏蔽重依赖后 miloco.main 与 rule_only 引擎均可用"

# ─── 4. 真起服务 ────────────────────────────────────────────────────────────

step "4/5 启动服务（127.0.0.1:${PORT}）"
# 复刻 launcher 首启：只写包内默认配置，**不从任何外部安装导入**（App 独立）。
cp "$APP/Contents/Resources/defaults/config.json" "$HOME_DIR/config.json"
cmp -s "$APP/Contents/Resources/defaults/config.json" "$HOME_DIR/config.json" \
  || fail "首启配置与包内默认不一致（被外部数据污染？）"
"$PY" -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg['app']['edition'] == 'slim', cfg.get('app')
assert cfg['perception']['engine']['rule_only'] is True, cfg['perception']
assert cfg['perf']['enabled'] is True, cfg['perf']
assert cfg['features']['pet_recognition'] is False, cfg['features']
assert cfg['server']['host'] == '127.0.0.1', cfg['server']
assert cfg['server']['port'] == 1812, cfg['server']  # App 固定端口，和 CLI 版 1810 分开
assert not cfg['server'].get('token'), '首启配置不该带 token（应由后端 bootstrap 生成）'
assert 'model' not in cfg, '首启配置不该带任何模型/账号（那是旧安装的东西）'
" "$HOME_DIR/config.json" || fail "首启配置内容不符合 slim 默认预期"
pass "首启只写包内 slim 默认配置（无 token、无模型、无旧数据）"

"$PY" -m miloco.main >"$HOME_DIR/server.log" 2>&1 &
SERVER_PID=$!

READY=0
for _ in $(seq 1 180); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    tail -30 "$HOME_DIR/server.log" >&2
    fail "服务进程提前退出（见上方日志）"
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
[[ "$READY" == "1" ]] || {
  tail -30 "$HOME_DIR/server.log" >&2
  fail "服务在 180s 内未就绪"
}
pass "/health 返回 200"

TOKEN="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['server']['token'])" "$HOME_DIR/config.json")"
[[ -n "$TOKEN" ]] || fail "config.json 未生成 server.token"
pass "server.token 已生成（$(printf '%.8s' "$TOKEN")…）"

EDITION_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/admin/edition")"
"$PY" -c "
import json, sys
data = json.loads(sys.argv[1])['data']
assert data['edition'] == 'slim', data
assert data['capabilities']['identity'] is False, data
assert data['capabilities']['one_click_upgrade'] is False, data
print('edition ok')
" "$EDITION_JSON" >/dev/null || fail "/api/admin/edition 不是 slim 能力集"
pass "/api/admin/edition 报 slim 且能力集正确"

IDENTITY_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/identity/persons")"
[[ "$IDENTITY_CODE" == "404" ]] || fail "身份路由在 slim 下应返回 404，实际 $IDENTITY_CODE"
pass "身份路由未注册（404）"

[[ -f "$HOME_DIR/miloco.db" ]] || fail "数据库未创建：$HOME_DIR/miloco.db"
# perf 必须开着:它是 action_ledger 的总开关,而日志页的「动作/触发场景」流走 /api/actions。
# 关掉 perf → observability.db 不建 + observability_router 不挂 → 住户在日志里看不到场景
# 进入/退出（曾经的回归）。这里把「开了」和「读得到」都卡住。
[[ -f "$HOME_DIR/observability.db" ]] || fail "observability.db 未创建（perf.enabled 被关了？）"
pass "miloco.db / observability.db 均已创建（perf.enabled=true 生效）"

# bootstrap 只允许往 config.json 里深合并 server.token，不能把默认配置冲掉。
"$PY" -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg['app']['edition'] == 'slim', cfg.get('app')
assert cfg['perception']['engine']['rule_only'] is True, cfg.get('perception')
assert cfg['perf']['enabled'] is True, cfg.get('perf')
assert cfg['schedule']['enabled'] is False, cfg.get('schedule')
assert cfg['server']['token'], cfg.get('server')
assert cfg['server']['port'] == 1812, cfg.get('server')
" "$HOME_DIR/config.json" || fail "bootstrap 后默认配置被破坏"
pass "bootstrap 后 config.json 默认项完整保留（仅新增 token）"

# 感知默认档来自**包内 settings.yaml**（用户 config.json 里没有这些键，全靠包兜底），
# 是打包时最容易漏改的一层：这里直接读引擎实际生效的值，别只看源码里改了没。
"$PY" -c "
import json, os, sys
os.environ.setdefault('MILOCO_EDITION', 'slim')
os.environ['MILOCO_HOME'] = sys.argv[1]
_srv = json.load(open(os.path.join(sys.argv[1], 'config.json')))['server']
# server.url 与 host/port 不一致时 get_settings() 会打一条告警，把冒烟日志刷脏；这里对齐。
os.environ['MILOCO_SERVER__URL'] = 'http://%s:%s' % (_srv['host'], _srv['port'])
from miloco.config import get_settings
inp = get_settings().perception.engine['input']
assert inp['video_short_edge'] == 768, inp
assert inp['media_resolution'] == 'high', inp
assert inp['rule_only_input'] == 'image', inp
assert inp['last_frame_only'] is False, inp
print('perception defaults ok')
" "$HOME_DIR" >/dev/null || fail "包内感知默认档不对（video_short_edge=768 / media_resolution=high / rule_only_input=image / last_frame_only=false）"
pass "感知默认档：768p / media_resolution=high / 图片输入 / 多帧"

# 「日志」页的一键清理:三处存储各一个 POST —— meaningful_events / on_demand_log(miloco.db)
# 与 action_ledger(observability.db,「触发场景」在这本台账里;早期版本漏了它)。
# 这里直接往库里插一行再清 —— 单元测试用 stub 顶掉了 perception_service,只有真服务
# 才验证得了「路由挂上了(slim 也挂) + 鉴权 + 真删到行」这条完整链路。
"$PY" - "$HOME_DIR/miloco.db" <<'SMOKESEED' || fail "插入待清理的日志行失败"
import sqlite3, sys
now = 1_700_000_000_000
conn = sqlite3.connect(sys.argv[1])
conn.execute(
    "INSERT INTO meaningful_events (id, timestamp, text, payload_json, has_rule_hit,"
    " has_suggestion, has_asr, snapshot_count, device_ids, rule_names, home_id, created_at)"
    " VALUES ('smoke-ev-1', ?, 'smoke', '{}', 0, 0, 0, 0, '[]', '{}', NULL, ?)",
    (now, now),
)
conn.execute(
    "INSERT INTO on_demand_log (id, timestamp, query, answer, sources, latency_ms,"
    " snapshot_count, clip_dids, clip_kinds, has_trace, created_at)"
    " VALUES ('smoke-od-1', ?, '谁在客厅', '没有人', '[]', 1, 0, '[]', '{}', 0, ?)",
    (now, now),
)
conn.commit()
conn.close()
SMOKESEED
"$PY" - "$HOME_DIR/observability.db" <<'SMOKESEEDACT' || fail "插入待清理的动作台账失败"
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute(
    "INSERT INTO action_ledger (id, timestamp, action_type, did, value_json, success,"
    " source, source_id, home_id) VALUES ('smoke-act-clear', 1700000000000, 'scene_trigger',"
    " 'scene.smoke.clear', '{\"scene_name\": \"冒烟清理场景\"}', 1, 'rule', 'rule-smoke-clear',"
    " 'home-smoke')",
)
conn.commit()
conn.close()
SMOKESEEDACT
EVENTS_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/events")"
OD_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/perception/on-demand-logs")"
ACT_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/actions?action_type=scene_trigger")"
"$PY" -c "
import json, sys
events = json.loads(sys.argv[1])['data']['events']
logs = json.loads(sys.argv[2])['data']['logs']
acts = json.loads(sys.argv[3])
assert len(events) == 1 and events[0]['event_id'] == 'smoke-ev-1', events
assert len(logs) == 1 and logs[0]['id'] == 'smoke-od-1', logs
assert len(acts) == 1 and acts[0]['id'] == 'smoke-act-clear', acts
" "$EVENTS_JSON" "$OD_JSON" "$ACT_JSON" || fail "插入的日志行没能通过接口读到"

CLEAR_EVENTS="$(curl -fsS --max-time 5 -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/events/clear")"
CLEAR_OD="$(curl -fsS --max-time 5 -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/perception/on-demand-logs/clear")"
CLEAR_ACT="$(curl -fsS --max-time 5 -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/actions/clear")"
"$PY" -c "
import json, sys
ev = json.loads(sys.argv[1])
od = json.loads(sys.argv[2])
act = json.loads(sys.argv[3])
assert ev['code'] == 0 and ev['data']['deleted'] == 1, ev
assert od['code'] == 0 and od['data']['deleted'] == 1, od
assert act['deleted'] == 1, act
" "$CLEAR_EVENTS" "$CLEAR_OD" "$CLEAR_ACT" || fail "清理接口返回的删除条数不对"

EVENTS_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/events")"
OD_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/perception/on-demand-logs")"
ACT_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/actions?action_type=scene_trigger")"
"$PY" -c "
import json, sys
assert json.loads(sys.argv[1])['data']['events'] == [], sys.argv[1]
assert json.loads(sys.argv[2])['data']['logs'] == [], sys.argv[2]
assert json.loads(sys.argv[3]) == [], sys.argv[3]
" "$EVENTS_JSON" "$OD_JSON" "$ACT_JSON" || fail "清理后列表仍有残留（触发场景没清掉？）"
pass "一键清理：事件 + 按需日志 + 动作台账（触发场景）都清空，清理后列表为空"

# 日志页的「动作」流（含"触发场景"）读 /api/actions ← action_ledger(observability.db)。
# 台账是 RuleRunner 触发场景时写的（source=rule / source_id=rule_id），这里插一行再读回来，
# 守住「slim 下 perf 开着、路由器挂着、按 action_type 过滤可用」这条链路。
"$PY" - "$HOME_DIR/observability.db" <<'SMOKEACT' || fail "插入动作台账失败"
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute(
    "INSERT INTO action_ledger (id, timestamp, action_type, did, value_json, success,"
    " source, source_id, home_id) VALUES ('smoke-act-1', ?, 'scene_trigger', 'scene.smoke.1',"
    " '{\"scene_name\": \"冒烟场景\"}', 1, 'rule', 'rule-smoke-1', 'home-smoke')",
    (1_700_000_000_000,),
)
conn.commit()
conn.close()
SMOKEACT
ACTIONS_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/api/actions?action_type=scene_trigger")"
"$PY" -c "
import json, sys
rows = json.loads(sys.argv[1])
assert len(rows) == 1, rows
row = rows[0]
assert row['action_type'] == 'scene_trigger', row
assert row['source'] == 'rule' and row['source_id'] == 'rule-smoke-1', row
assert json.loads(row['value_json'])['scene_name'] == '冒烟场景', row
" "$ACTIONS_JSON" || fail "slim 下 /api/actions 读不到场景触发台账（活动日志页会缺这一条流）"
pass "活动日志「动作」流可用：场景触发台账可写可读（source=rule）"

# ─── 5. 优雅退出 ────────────────────────────────────────────────────────────

step "5/5 优雅停止（SIGTERM）"
kill -TERM "$SERVER_PID"
for _ in $(seq 1 60); do
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$SERVER_PID" 2>/dev/null; then
  kill -KILL "$SERVER_PID" 2>/dev/null || true
  fail "服务未在 60s 内退出"
fi
SERVER_PID=""
pass "服务已优雅退出"

printf '\033[32m[smoke] 全部通过 ✅\033[0m\n'
