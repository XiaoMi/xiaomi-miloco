#!/usr/bin/env bash
# adapter-launcher.sh —— launchd 调用入口
#
# launchd 不传 shell 环境变量，所以这里从 ~/.hermes/.env 读 ADAPTER_AUTH_BEARER /
# HERMES_API_URL / ADAPTER_PORT 等，写 PID 文件，然后 exec 真正的 adapter。
# 用 exec 让进程号不变，launchd 拿到的 PID 跟我们写的一致。
#
# 直接 install-hermes.sh 调用也安全（launchd 路径 + nohup 路径都用这个脚本作启动器）。

set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
PID_FILE="$HERMES_HOME/miloco-adapter.pid"

# 从 .env 抽变量（不 source，避免 set -e + 子 shell 行为差异）
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      ''|\#*) continue ;;
      API_SERVER_KEY|HERMES_API_URL|HERMES_API_KEY|ADAPTER_AUTH_BEARER|ADAPTER_PORT|ADAPTER_HOST)
        export "$k=$v" ;;
    esac
  done < "$ENV_FILE"
fi

# Bearer 优先级：ADAPTER_AUTH_BEARER > API_SERVER_KEY
export ADAPTER_AUTH_BEARER="${ADAPTER_AUTH_BEARER:-${API_SERVER_KEY:-}}"
if [ -z "$ADAPTER_AUTH_BEARER" ]; then
  echo "[adapter-launcher] FATAL: 找不到 Bearer（.env 里需要 API_SERVER_KEY 或 ADAPTER_AUTH_BEARER）" >&2
  exit 1
fi

# 找 python
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "[adapter-launcher] FATAL: 找不到 python3 / python" >&2
  exit 1
fi

# 写 PID 文件再 exec（$$ 是即将 exec 的 PID；exec 后 PID 不变）
echo $$ > "$PID_FILE"

# exec 真正适配器（PID 不变）
exec "$PY" -m adapter