#!/usr/bin/env bash
# Miloco for Hermes Agent —— 手动 / 高级安装脚本。
#
# 普通用户请用 ../install-hermes.sh（一键：复制 + patch + 启 adapter），
# 这个脚本只在你**想自己一步步做**时用：只复制 skill 和插件，**不** patch
# miloco config.json、**不**补 .env、**不**启 adapter。
#
# 它做三件事：
#   1. 把 16 个 miloco-* skill 同步到 ~/.hermes/skills/（由 sync-skills.py 生成）
#   2. 把 Hermes 插件 miloco-plugin/ 复制到 ~/.hermes/plugins/miloco/
#   3. （可选）注册 4 个受管 cron —— 由插件启动时自 reconcile，此处仅提示
#   4. 打印 miloco 后端 config.json 需要改的字段 + adapter 进程启动方式
#
# 前置：hermes 已安装（~/.hermes 存在）、miloco-cli 在 PATH。
# 不触碰 miloco 后端代码，只读 $MILOCO_HOME/config.json 提示用户手改。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$HERE/../miloco-plugin"
SKILLS_SRC="$HERE/../skills"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_SKILLS_DIR="$HERMES_HOME/skills"
HERMES_PLUGINS_DIR="$HERMES_HOME/plugins/miloco"

err()  { echo "[miloco-hermes] [错误] $*" >&2; }
info() { echo "[miloco-hermes] $*"; }

# --- 前置检查 ---
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || { err "找不到 python"; exit 1; }
PYTHON="$(command -v python3 || command -v python)"

if ! command -v miloco-cli >/dev/null 2>&1; then
  err "找不到 miloco-cli，请先装好 miloco 后端并确保 miloco-cli 在 PATH"
  exit 1
fi

if [ ! -d "$HERMES_HOME" ]; then
  err "找不到 Hermes 目录 $HERMES_HOME，请先安装 Hermes Agent"
  exit 1
fi

mkdir -p "$HERMES_SKILLS_DIR" "$HERMES_PLUGINS_DIR"

# --- 1. 生成并同步 skills ---
info "生成 miloco skills（从 plugins/skills 同步并适配 frontmatter）..."
"$PYTHON" "$HERE/sync-skills.py"
if [ ! -d "$SKILLS_SRC" ]; then
  err "sync-skills.py 未生成 $SKILLS_SRC"; exit 1
fi
info "复制 skills 到 $HERMES_SKILLS_DIR/"
cp -r "$SKILLS_SRC"/miloco-* "$HERMES_SKILLS_DIR"/

# --- 2. 复制插件 ---
info "复制 Hermes 插件到 $HERMES_PLUGINS_DIR/"
rm -rf "$HERMES_PLUGINS_DIR"
cp -r "$PLUGIN_SRC" "$HERMES_PLUGINS_DIR"
# 清掉可能带进去的 __pycache__
find "$HERMES_PLUGINS_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# --- 3. cron ---
info "受管 cron 由插件启动时自动 reconcile（标签 [miloco:home-profile]），无需手动注册。"
info "启动 Hermes 后可用 'hermes cron list' 确认 4 个 miloco 任务。"

# --- 4. 后续配置提示 ---
MILOCO_HOME="${MILOCO_HOME:-$HOME/.openclaw/miloco}"
ADAPTER_PORT="${ADAPTER_PORT:-18789}"
BEARER="${ADAPTER_AUTH_BEARER:-请自行生成一个随机串}"

cat <<EOF

==========================================================
 安装完成。还需手动完成以下配置：
==========================================================

[1] miloco 后端 config.json（$MILOCO_HOME/config.json）的 agent 段改成：
    "agent": {
      "webhook_url": "http://127.0.0.1:$ADAPTER_PORT/miloco/webhook",
      "auth_bearer": "<你的 Bearer，需与 adapter 启动时的 ADAPTER_AUTH_BEARER 一致>"
    }

[2] 确保 Hermes 已启用 api_server 平台并设了 API_SERVER_KEY：
    在 ~/.hermes/.env 里：API_SERVER_KEY=<某个密钥>

[3] 启动入站适配进程（把 miloco 的回调翻译给 Hermes）：
    ADAPTER_AUTH_BEARER=<与上一步同一个 Bearer> \\
    HERMES_API_URL=http://127.0.0.1:8642 \\
    HERMES_API_KEY=<同 API_SERVER_KEY> \\
    ADAPTER_PORT=$ADAPTER_PORT \\
    python -m plugins.hermes.adapter
    （生产环境建议用 systemd / nohup 常驻）

[4] 启动 / 重启 Hermes gateway 与 miloco 后端，对话试：
    hermes -z "把客厅灯打开"

文档：plugins/hermes/README.md
==========================================================
EOF
