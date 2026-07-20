#!/usr/bin/env bash
# miloco-status.sh — 直调 miloco_plugin.tools_status 跑自检。
#
# 与 `hermes -z "miloco_status"` 的区别：
# - oneshot 路径把字面量 prompt 发给 LLM，依赖 LLM 推断调哪个 tool（非确定性）
# - 本脚本直接 import tool handler 跑（确定性，无 LLM 中间环节）
#
# 用法：
#   miloco-status.sh                  # 跑 gather_status（需要 plugin ctx；不可用时报清晰错误）
#   miloco-status.sh plugin_self      # 跑单个子项（ctx-free）
#   miloco-status.sh state_json
#   miloco-status.sh adapter_health
#   ...
#
# 子项列表来自 tools_status.gather_status() 的迭代 tuple。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/../miloco-plugin" && pwd)"
SUBCMD="${1:-gather}"
export MILOCO_STATUS_PLUGIN_DIR="$PLUGIN_DIR"
export MILOCO_STATUS_SUBCMD="$SUBCMD"

python3 - <<'PYEOF'
import importlib.util
import json
import os
import sys
from pathlib import Path

plugin_dir = Path(os.environ['MILOCO_STATUS_PLUGIN_DIR'])
subcmd = os.environ['MILOCO_STATUS_SUBCMD']

spec = importlib.util.spec_from_file_location(
    'miloco_plugin',
    plugin_dir / '__init__.py',
    submodule_search_locations=[str(plugin_dir)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules['miloco_plugin'] = mod
spec.loader.exec_module(mod)
from miloco_plugin import tools_status

if subcmd == 'gather':
    print(json.dumps({
        'ok': False,
        'error': 'gather_status requires Hermes plugin ctx; use a subcommand like `plugin_self` or `adapter_health` for ctx-free checks. Or run the full check from inside Hermes: hermes -z "miloco_status" (LLM-mediated).',
        'available_subcommands': [
            'plugin_self', 'state_json', 'hermes_plugin_enabled',
            'adapter_health', 'cron_jobs', 'miloco_backend',
            'skills_installed', 'versions', 'trace_hooks',
        ],
    }, indent=2, ensure_ascii=False))
    sys.exit(2)

check_map = {
    'plugin_self': tools_status._check_plugin_self,
    'hermes_plugin_enabled': tools_status._check_hermes_plugin_enabled,
    'adapter_health': tools_status._check_adapter_health,
    'cron_jobs': tools_status._check_cron_jobs,
    'miloco_backend': tools_status._check_miloco_backend,
    'skills_installed': tools_status._check_skills_installed,
    'trace_hooks': tools_status._check_trace_hooks,
    # B2 wrapper 收敛(Zirconi 6/25 review): 加 state_json + versions subcommand,
    # 这样 UPGRADE.md / install-guide-hermes.md 引用的 `miloco-status.sh versions/state_json`
    # 才有对应实现可用。
    'state_json': lambda: tools_status._check_state_json(None),
    'versions': lambda: tools_status._check_versions(None),
}
if subcmd not in check_map:
    print(json.dumps({
        'ok': False,
        'error': f'unknown subcommand: {subcmd}',
        'available': sorted(check_map.keys()),
    }, indent=2, ensure_ascii=False))
    sys.exit(2)

result = check_map[subcmd]()
print(json.dumps(result, indent=2, ensure_ascii=False))
PYEOF
