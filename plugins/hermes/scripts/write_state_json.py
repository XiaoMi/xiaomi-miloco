"""把 IM 探测结果写入 plugin state.json（deliver.target + candidates）。

用法:
    python3 write_state_json.py <PLUGIN_STATE> <CANDIDATES_JSON> <PRESERVED_TARGET>

外部化为一个 .py 文件，是因为原 bash heredoc 实现里 body 大量含 ``( )`` /
``{ }`` 嵌套，macOS 自带 bash 3.2 解析时偶发把内部 ``(`` 当 subshell 起点
报 syntax error。挪到外部脚本彻底消除 bash ↔ heredoc 嵌套。

写入规则:
    target 优先级: candidates[0] > preserved > None
    auto_configured:  候选非空 且 target == candidates[0]
    candidates 字段:  原样保留（给 miloco_notify_bind switch 用）

errors → 写 stderr + 抛 SystemExit(非 0)，bash `|| true` 兜底后不影响主流程
（state.json 写不进去是降级场景，不是 fatal）。
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: write_state_json.py <PLUGIN_STATE> <CANDIDATES_JSON> <PRESERVED>", file=sys.stderr)
        return 2
    path_str, candidates_json, preserved = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        parsed = json.loads(candidates_json)
    except Exception as e:
        print(f"candidates_json 解析失败: {e}", file=sys.stderr)
        parsed = {}

    # candidates_json 形态：detect_im_platforms.py 输出 {"targets": [...], "source": "..."}
    # 这里需要 list 形态；兼容 dict / 直接是 list
    if isinstance(parsed, dict):
        candidates = parsed.get("targets") or []
    elif isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = []
    if not isinstance(candidates, list):
        candidates = []

    path = Path(path_str)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        state = {}

    target = candidates[0] if candidates else (preserved or None)
    auto_cfg = bool(candidates) and target == candidates[0]
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    state["deliver"] = {
        "target": target,
        "auto_configured": auto_cfg,
        "configured_at": now_iso,
        "source": "install-hermes.sh auto-detect",
        "candidates": candidates,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"write {path} 失败: {e}", file=sys.stderr)
        return 1

    note = f"state.json deliver.target = {target}  candidates: {len(candidates)}"
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
