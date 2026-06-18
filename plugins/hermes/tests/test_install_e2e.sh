#!/usr/bin/env bash
# test_install_e2e.sh —— install-hermes.sh + miloco-adapter.sh 端到端测试。
#
# 干这些事（用临时目录 mock $HERMES_HOME / $MILOCO_HOME，跑完自动清理）：
#   1. 校验 install-hermes.sh 7 步全过
#   2. 校验鉴权契约（POST 无 auth → 401，POST 有 auth 缺 action → code:1001）
#   3. 校验幂等性：再跑一次 install-hermes.sh 应不报错、复用 Bearer、PID 变
#   4. 校验 miloco-adapter.sh 完整生命周期：status / restart / stop
#   5. 校验 stop 后端口释放
#
# 用法：
#   bash plugins/hermes/tests/test_install_e2e.sh
#   或：REPO_ROOT=$(pwd) bash plugins/hermes/tests/test_install_e2e.sh
#
# 退出码：0=全过，1=有失败。
#
# 依赖：bash、python3/python（venv 即可）、curl、netstat 或 lsof 或 ss
#       本机已装好 miloco-cli 可更好（pre-flight 校验），缺也能跑通。

set -uo pipefail

# --- 定位仓库根 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
INSTALL_SH="$REPO_ROOT/plugins/hermes/install-hermes.sh"
ADAPTER_SH="$REPO_ROOT/plugins/hermes/scripts/miloco-adapter.sh"

[ -f "$INSTALL_SH" ] || { echo "找不到 $INSTALL_SH"; exit 1; }
[ -f "$ADAPTER_SH" ] || { echo "找不到 $ADAPTER_SH"; exit 1; }

# --- 找 python（优先 venv 的，没有就用 PATH 里的）---
if [ -f "$HOME/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" ]; then
  REAL_PY="$HOME/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  REAL_PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  REAL_PY="$(command -v python)"
else
  echo "找不到 python，跳过"
  exit 1
fi

# --- 准备 mock 环境 ---
TEST_ROOT="$(mktemp -d)"
FAKE_BIN="$TEST_ROOT/fake-bin"
mkdir -p "$FAKE_BIN" "$TEST_ROOT/miloco" "$TEST_ROOT/hermes"

# mock miloco-cli
cat > "$FAKE_BIN/miloco-cli" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$FAKE_BIN/miloco-cli"

# mock miloco config
cat > "$TEST_ROOT/miloco/config.json" <<'EOF'
{"server":{"port":8123,"token":"x"},"agent":{"webhook_url":"old","auth_bearer":"old"}}
EOF

# python wrapper（venv python 需要 pyvenv.cfg 同目录，wrapper 调它）
cat > "$FAKE_BIN/python3" <<EOF
#!/usr/bin/env bash
exec "$REAL_PY" "\$@"
EOF
chmod +x "$FAKE_BIN/python3"
cp "$FAKE_BIN/python3" "$FAKE_BIN/python"

# --- 测试用例计数 ---
PASS=0
FAIL=0
FAILS=()

assert() {
  local name="$1" cond="$2"
  if [ "$cond" = "1" ]; then
    PASS=$((PASS+1))
    echo "  ✓ $name"
  else
    FAIL=$((FAIL+1))
    FAILS+=("$name")
    echo "  ✗ $name"
  fi
}

# 端口号：用时间戳后 4 位避开冲突
PORT=2$(( $(date +%s) % 9000 + 1000 ))
export HERMES_HOME="$TEST_ROOT/hermes"
export MILOCO_HOME="$TEST_ROOT/miloco"
export ADAPTER_PORT="$PORT"
export PATH="$FAKE_BIN:$PATH"

cleanup() {
  for pid in $(tasklist //FI "IMAGENAME eq python.exe" //FO CSV //NH 2>/dev/null | awk -F'","' '{print $2}' | tr -d '"' 2>/dev/null); do
    taskkill //PID "$pid" //F >/dev/null 2>&1 || true
  done
  rm -rf "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT

echo "==== TEST 1: install-hermes.sh 首次安装 ===="
bash "$INSTALL_SH" > /tmp/i1.log 2>&1
RC1=$?
assert "install exit 0" "$([ $RC1 -eq 0 ] && echo 1 || echo 0)"
[ -d "$HERMES_HOME/plugins/miloco/miloco-plugin" ] && T=1 || T=0
assert "miloco-plugin 已复制" "$T"
[ -d "$HERMES_HOME/plugins/miloco/adapter" ] && T=1 || T=0
assert "adapter 已复制" "$T"
SKILL_DIRS=$(find "$HERMES_HOME/skills" -maxdepth 1 -name "miloco-*" -type d 2>/dev/null | wc -l)
assert "16 个 skill 目录" "$([ $SKILL_DIRS -eq 16 ] && echo 1 || echo 0)"
[ -f "$HERMES_HOME/miloco-adapter.pid" ] && T=1 || T=0
assert "pid 文件存在" "$T"
PID1=$(cat "$HERMES_HOME/miloco-adapter.pid" 2>/dev/null)
[ -n "$PID1" ] && T=1 || T=0
assert "pid 非空（$PID1）" "$T"
grep -q '^API_SERVER_KEY=' "$HERMES_HOME/.env" && T=1 || T=0
assert ".env 包含 API_SERVER_KEY" "$T"
grep -q '"webhook_url"' "$MILOCO_HOME/config.json" && T=1 || T=0
assert "miloco config.json 已 patch" "$T"
ls "$MILOCO_HOME"/config.json.bak-* >/dev/null 2>&1 && T=1 || T=0
assert "miloco config.json.bak-* 备份存在" "$T"

echo ""
echo "==== TEST 2: 鉴权契约 ===="
# 等一下端口就绪
sleep 1
HTTP_NO_AUTH=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 -X POST http://127.0.0.1:$PORT/miloco/webhook -H "Content-Type: application/json" -d '{}')
assert "POST 无 auth → 401" "$([ "$HTTP_NO_AUTH" = "401" ] && echo 1 || echo 0)"
BEARER=$(grep '^API_SERVER_KEY=' "$HERMES_HOME/.env" | cut -d= -f2-)
RESP=$(curl -sS --max-time 5 -X POST http://127.0.0.1:$PORT/miloco/webhook \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" -d '{}')
echo "$RESP" | grep -q '"code": 1001' && T=1 || T=0
assert "POST 有 auth 缺 action → code:1001" "$T"
HTTP_HEALTH=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:$PORT/health)
assert "/health → 200" "$([ "$HTTP_HEALTH" = "200" ] && echo 1 || echo 0)"

echo ""
echo "==== TEST 3: install-hermes.sh 幂等性 ===="
bash "$INSTALL_SH" > /tmp/i2.log 2>&1
RC2=$?
assert "幂等 exit 0" "$([ $RC2 -eq 0 ] && echo 1 || echo 0)"
PID2=$(cat "$HERMES_HOME/miloco-adapter.pid" 2>/dev/null)
[ "$PID1" != "$PID2" ] && [ -n "$PID2" ] && T=1 || T=0
assert "PID 变化（$PID1 → $PID2）" "$T"
BEARER2=$(grep '^API_SERVER_KEY=' "$HERMES_HOME/.env" | cut -d= -f2-)
[ "$BEARER" = "$BEARER2" ] && T=1 || T=0
assert "Bearer 复用" "$T"

echo ""
echo "==== TEST 4: miloco-adapter.sh 完整生命周期 ===="
bash "$ADAPTER_SH" status > /tmp/s1.log 2>&1
grep -q "adapter 在跑" /tmp/s1.log && T=1 || T=0
assert "status 显示在跑" "$T"
bash "$ADAPTER_SH" restart > /tmp/r.log 2>&1
RC3=$?
assert "restart exit 0" "$([ $RC3 -eq 0 ] && echo 1 || echo 0)"
PID3=$(cat "$HERMES_HOME/miloco-adapter.pid" 2>/dev/null)
[ "$PID2" != "$PID3" ] && [ -n "$PID3" ] && T=1 || T=0
assert "restart 后 PID 变化（$PID2 → $PID3）" "$T"
bash "$ADAPTER_SH" stop > /tmp/stop.log 2>&1
RC4=$?
assert "stop exit 0" "$([ $RC4 -eq 0 ] && echo 1 || echo 0)"
sleep 1
LISTEN=$(netstat -ano 2>/dev/null | grep ":$PORT " | grep LISTENING | head -1)
[ -z "$LISTEN" ] && T=1 || T=0
assert "stop 后端口释放" "$T"

echo ""
echo "==== TEST 5: install-hermes.sh（adapter 已停时）===="
bash "$INSTALL_SH" > /tmp/i3.log 2>&1
RC5=$?
assert "exit 0" "$([ $RC5 -eq 0 ] && echo 1 || echo 0)"
PID4=$(cat "$HERMES_HOME/miloco-adapter.pid" 2>/dev/null)
[ -n "$PID4" ] && T=1 || T=0
assert "新 adapter 已起（PID=$PID4）" "$T"

# --- 总结 ---
echo ""
echo "=========================================="
echo "PASS: $PASS, FAIL: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "失败用例："
  for f in "${FAILS[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "全部通过 ✓"
exit 0
