#!/usr/bin/env bash
# adapter-launcher.sh —— launchd 调用入口（过渡兼容）
#
# 架构 #1+#2 后，适配器收敛到 miloco backend 的 AgentPlatformAdapter。
# 旧 launchd → python -m adapter 路径已不适用。
# 本脚本只做旧架构残留清理：卸载 launchd job + 删除 plist，然后退出。
#
# 新安装里 install-hermes.sh Step 7 已改叫 miloco-adapter.sh start，
# 后者直接管 supervisord 下的 miloco-backend。

set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
LAUNCHD_LABEL="com.xiaomi.miloco.hermes.adapter"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

echo "[adapter-launcher] 架构 #1+#2 后本脚本已被 miloco-adapter.sh 取代。"

# 卸载旧 launchd job（幂等）
if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL" 2>/dev/null; then
  echo "[adapter-launcher] 清理旧 launchd job: $LAUNCHD_LABEL"
  launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
fi
[ -f "$LAUNCHD_PLIST" ] && rm -f "$LAUNCHD_PLIST"
[ -f "$HERMES_HOME/miloco-adapter.pid" ] && rm -f "$HERMES_HOME/miloco-adapter.pid"

echo "[adapter-launcher] 完成。适配器现在由 supervisord 管理（miloco-backend）。"
