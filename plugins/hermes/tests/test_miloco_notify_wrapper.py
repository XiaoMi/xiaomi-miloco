"""miloco-notify CLI wrapper 测试。

覆盖:
- list 返回 candidates + current
- switch 错格式(没冒号)→ ok=false
- switch 正确格式 → state.json::deliver.target 更新
- switch 写在 state.json 不破坏 candidates
- reset 重新跑 detect_im_platforms.py

不依赖 hermes runtime(直接调 CLI subprocess),用临时 HERMES_HOME 隔离。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "plugins" / "hermes" / "scripts" / "miloco-notify.py"
DETECT_SCRIPT = REPO_ROOT / "plugins" / "hermes" / "scripts" / "detect_im_platforms.py"


def _setup_hermes_home(tmp_path: Path, deliver: dict | None) -> Path:
    """构造隔离的 HERMES_HOME + state.json,返 state.json 路径。"""
    hermes = tmp_path / ".hermes"
    plugin = hermes / "plugins" / "miloco" / "miloco-plugin"
    plugin.mkdir(parents=True)
    state = {
        "deliver": deliver or {
            "target": "weixin:o9cqsss629QGu22aknaIChWNAxYI@im.wechat",
            "auto_configured": True,
            "configured_at": "2026-07-05T11:51:03Z",
            "source": "install-hermes.sh auto-detect",
            "candidates": [
                "weixin:o9cqsss629QGu22aknaIChWNAxYI@im.wechat",
                "feishu:oc_806ed7124bae73745846704be33ae2b3",
            ],
        },
        "versions": {"plugin": "0.2.0"},
    }
    state_path = plugin / "state.json"
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return state_path


def _run_wrapper(args: list[str], hermes_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    return subprocess.run(
        ["python3", str(WRAPPER), *args],
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_list_returns_candidates_and_current(tmp_path):
    """list 命令:返 candidates + current target + state path。"""
    state_path = _setup_hermes_home(tmp_path, None)
    proc = _run_wrapper(["list"], tmp_path / ".hermes")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert "current" in out
    assert "candidates" in out
    assert "candidates_count" in out
    assert "state_path" in out
    assert str(state_path) == out["state_path"]
    assert out["candidates_count"] == 2


def test_switch_rejects_bad_format_no_colon(tmp_path):
    """switch target 缺冒号且不是 'all' → ok=false, exit 2。"""
    _setup_hermes_home(tmp_path, None)
    proc = _run_wrapper(["switch", "wrong-no-colon"], tmp_path / ".hermes")
    assert proc.returncode == 2
    out = json.loads(proc.stderr)
    assert out["ok"] is False


def test_switch_all_is_fanout_sentinel(tmp_path):
    """switch target='all' → 1:1 写入 state.json(走 fanout 语义)。"""
    state_path = _setup_hermes_home(tmp_path, None)
    proc = _run_wrapper(["switch", "all"], tmp_path / ".hermes")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["target"] == "all"
    # 1:1 保留
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["deliver"]["target"] == "all"
    # candidates 还在
    assert len(state["deliver"]["candidates"]) == 2


def test_switch_rejects_empty_target(tmp_path):
    """switch target 空字符串 → ok=false。"""
    _setup_hermes_home(tmp_path, None)
    proc = _run_wrapper(["switch", ""], tmp_path / ".hermes")
    assert proc.returncode == 2
    out = json.loads(proc.stderr)
    assert out["ok"] is False


def test_switch_writes_target_atomically(tmp_path):
    """switch 正确格式 → state.json::deliver.target 更新, candidates 保留。"""
    state_path = _setup_hermes_home(tmp_path, None)
    new_target = "feishu:oc_806ed7124bae73745846704be33ae2b3"
    proc = _run_wrapper(["switch", new_target], tmp_path / ".hermes")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["target"] == new_target
    assert "warning" not in out  # 在 candidates 里,不该 warn

    # 验证 state.json 真的改了
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["deliver"]["target"] == new_target
    # candidates 必须保留(不能被 switch 清掉)
    assert len(state["deliver"]["candidates"]) == 2
    assert state["deliver"]["auto_configured"] is False
    assert "miloco-notify CLI" in state["deliver"]["source"]


def test_switch_warns_for_unknown_target(tmp_path):
    """switch target 不在 candidates → 仍写入但带 warning。"""
    state_path = _setup_hermes_home(tmp_path, None)
    proc = _run_wrapper(["switch", "feishu:oc_UNKNOWN"], tmp_path / ".hermes")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert "warning" in out
    assert "不在 candidates 里" in out["warning"]

    # state.json 确实改了(target 是 oc_UNKNOWN)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["deliver"]["target"] == "feishu:oc_UNKNOWN"


def test_switch_does_not_corrupt_state_on_error(tmp_path):
    """switch 失败时 state.json 不被破坏(原子写:tmp → rename)。"""
    state_path = _setup_hermes_home(tmp_path, None)
    original = state_path.read_text(encoding="utf-8")

    # 错格式 → 失败,但 state.json 应该不变
    proc = _run_wrapper(["switch", "no-colon-here"], tmp_path / ".hermes")
    assert proc.returncode != 0

    after = state_path.read_text(encoding="utf-8")
    assert after == original, "state.json 在 switch 失败时不应被改"

    # 确认没有 tmp 残留
    assert not state_path.with_suffix(".json.tmp").exists()


def test_does_not_silently_fix_malformed_oc_id(tmp_path):
    """核心回归测试:LLM 之前会把 oc_806ed7124bae73745846704be33ae2b3
    改成 oc_806ed7124bae73745846704be33ae33ae2b3(多 33ae)。

    wrapper 必须 1:1 保留 user 传的 target 字符串,不做任何"修正"。
    """
    state_path = _setup_hermes_home(tmp_path, None)
    # user 故意传 LLM 改坏的 oc id(模拟 LLM 犯的错)
    bad_target = "feishu:oc_806ed7124bae73745846704be33ae33ae2b3"
    proc = _run_wrapper(["switch", bad_target], tmp_path / ".hermes")
    assert proc.returncode == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    # 必须是 1:1,不能 wrapper 自己"修正"
    assert state["deliver"]["target"] == bad_target


@pytest.mark.skipif(not DETECT_SCRIPT.exists(), reason="detect_im_platforms.py 不存在")
def test_reset_runs_detect_script(tmp_path, monkeypatch):
    """reset 命令:跑 detect_im_platforms.py 重新探测 → 写新 target。"""
    state_path = _setup_hermes_home(tmp_path, None)
    # monkeypatch:wrapper 内 subprocess.run 找的是相对路径
    # detect_im_platforms.py 真存在,我们就跑真的
    proc = _run_wrapper(["reset"], tmp_path / ".hermes")
    # 不管 returncode(取决于本机有没有 IM auth),主要看 state.json 被 reset 了
    if proc.returncode == 0:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["deliver"]["auto_configured"] is True
        assert "auto-detect" in state["deliver"]["source"]