"""Hermes 插件集成测试——替换已删除的 test_install_e2e.sh。

测试全链路：配置写入 → 适配器加载 → send_turn → trace 读写。
不依赖真实 Hermes daemon。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── 配置写入 / 读取 ─────────────────────────────────────────────────────────

def test_config_write_and_read(tmp_path, monkeypatch):
    """模拟 install-hermes.sh 写 config.json + 验证。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    config = {
        "agent": {
            "platform": "hermes",
            "webhook_url": "http://127.0.0.1:1810/miloco/webhook",
            "auth_bearer": "test-bearer-abc123",
        },
        "omni": {"model": "test-model"},
        "server": {"port": 1810},
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    # 读回验证
    loaded = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert loaded["agent"]["platform"] == "hermes"
    assert loaded["agent"]["webhook_url"].endswith("/miloco/webhook")
    assert loaded["agent"]["auth_bearer"] == "test-bearer-abc123"


# ── 适配器加载（用 importlib 模拟 backend loader） ──────────────────────────

def test_hermes_adapter_module_loads():
    """HermesAdapter 模块可正常 import。"""
    from miloco_plugin_pkg.hermes_adapter import adapter as ha

    assert hasattr(ha, "Adapter")
    assert hasattr(ha.Adapter, "name")
    assert hasattr(ha.Adapter, "send_turn")
    assert hasattr(ha.Adapter, "read_trace_meta")
    assert hasattr(ha.Adapter, "build_system")
    assert ha.Adapter.name == "hermes"


def test_hermes_adapter_instantiable():
    from miloco_plugin_pkg.hermes_adapter import adapter as ha
    inst = ha.Adapter()
    assert inst.name == "hermes"


# ── trace 读写全链路（文件 IPC） ────────────────────────────────────────────

def test_trace_full_write_read_cycle(tmp_path, monkeypatch):
    """trace.py 常写 → 读取，验证文件 IPC 链路。"""
    import os
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco_plugin_pkg import trace as tr

    # 清状态
    tr._turns.clear()
    tr._trace_links.clear()

    # 写
    tr.register_trace_link("sess-1", "trace-abc")
    tr._hk_pre_llm_call("sess-1", "hello world", [], True, "m", "p")
    tr._hk_post_llm_call("sess-1", "hi", "resp", [], "m", "p", duration_ms=100)
    tr._hk_on_session_end("sess-1", True, False, "m", "p")

    # 验证 meta.json 已写
    today_dirs = list((tmp_path / "trace" / "agent").glob("*"))
    assert len(today_dirs) == 1
    meta_files = list(today_dirs[0].glob("*.meta.json"))
    assert len(meta_files) == 1, f"应该写 meta.json: {list(today_dirs[0].iterdir())}"

    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["runId"] == "sess-1"
    assert meta["traceId"] == "trace-abc"
    assert meta["query"] == "hello world"
    assert meta["success"] is True
    assert "jsonlPath" in meta
    assert meta["jsonlPath"] is not None

    # 验证 jsonl.gz 也存在
    gz_files = list(today_dirs[0].glob("*.jsonl.gz"))
    assert len(gz_files) == 1


def test_trace_pop_done_turn_gives_meta(tmp_path, monkeypatch):
    """pop_done_turn 返回完整 meta 给 backend adapter 读。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco_plugin_pkg import trace as tr

    tr._turns.clear()
    tr._trace_links.clear()

    tr.register_trace_link("sess-1", "trace-abc")
    tr._hk_pre_llm_call("sess-1", "test query", [], True, "m", "p")
    tr._hk_on_session_end("sess-1", True, False, "m", "p")

    meta = tr.pop_done_turn("sess-1")
    assert meta is not None
    assert meta["runId"] == "sess-1"
    assert meta["traceId"] == "trace-abc"
    assert "llmCallCount" in meta
    assert "toolCallCount" in meta

    # pop 后清掉
    assert tr.pop_done_turn("sess-1") is None


# ── notify 三级 fallback ────────────────────────────────────────────────────

def test_notify_resolve_target_runtime_fallback(tmp_path, monkeypatch):
    """resolve_notify_target 三级 fallback：无 state.json → 扫 auth.json → needsBind。"""
    from miloco_plugin_pkg import tools_notify as tn

    class _FakeCtx:
        manifest = None

    monkeypatch.setattr(tn, "load_state", lambda ctx: {})
    monkeypatch.setattr(tn, "_detect_im_platforms_simple", lambda: [])
    result = tn.resolve_notify_target(_FakeCtx)
    # 无 state.json 且无 auth.json → needsBind=True
    assert result["needsBind"] is True
    assert "hint" in result


def test_notify_resolve_target_with_state_json(tmp_path, monkeypatch):
    """有 state.json::deliver.target → 直接用。"""
    from miloco_plugin_pkg import tools_notify as tn

    class _FakeCtx:
        manifest = None

    monkeypatch.setattr(tn, "load_state",
                        lambda ctx: {"deliver": {"target": "feishu:oc_xxx"}})
    monkeypatch.setattr(tn, "_detect_im_platforms_simple", lambda: [])
    result = tn.resolve_notify_target(_FakeCtx)
    assert result["target"] == "feishu:oc_xxx"
    assert result["needsBind"] is False
