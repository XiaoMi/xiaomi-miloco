"""adapter 侧 trace_reader + gateway_watch 测试。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

# 用 importlib 装载 adapter（conftest 已加载 adapter_pkg）
from adapter_pkg import gateway_watch, trace_reader


# ─── trace_reader ─────────────────────────────────────────────────────────


def _write_meta(home: Path, run_id: str, **fields: Any) -> Path:
    today = home / "trace" / "agent" / time.strftime("%Y%m%d")
    today.mkdir(parents=True, exist_ok=True)
    meta = {"runId": run_id, "success": True, "llmCallCount": 3, "toolCallCount": 5,
            "durationMs": 1234, "query": "test query", **fields}
    p = today / f"{run_id}__test.jsonl.gz.meta.json"
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return p


def test_get_done_meta_specific_run_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _write_meta(tmp_path, "sess-abc")
    meta = trace_reader.get_done_meta("sess-abc")
    assert meta is not None
    assert meta["runId"] == "sess-abc"


def test_get_done_meta_missing_run_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _write_meta(tmp_path, "sess-abc")
    assert trace_reader.get_done_meta("nonexistent") is None


def test_get_done_meta_latest_when_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    p1 = _write_meta(tmp_path, "sess-1")
    time.sleep(0.05)  # 确保 mtime 不同
    p2 = _write_meta(tmp_path, "sess-2")
    meta = trace_reader.get_done_meta(None)
    assert meta is not None
    assert meta["runId"] == "sess-2"  # mtime 更新


def test_get_done_meta_empty_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    assert trace_reader.get_done_meta("x") is None
    assert trace_reader.get_done_meta(None) is None


def test_get_done_meta_handles_corrupt_json(tmp_path: Path, monkeypatch):
    """meta.json 损坏时静默跳过，不抛。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    today = tmp_path / "trace" / "agent" / time.strftime("%Y%m%d")
    today.mkdir(parents=True, exist_ok=True)
    (today / "sess-bad.meta.json").write_text("not json {", encoding="utf-8")
    _write_meta(tmp_path, "sess-good")
    meta = trace_reader.get_done_meta("sess-good")
    assert meta is not None
    assert meta["runId"] == "sess-good"


def test_get_trace_response_includes_full_meta(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _write_meta(tmp_path, "sess-1", slowestToolName="miloco_im_push", errorMsg="oops")
    resp = trace_reader.get_trace_response("sess-1")
    assert resp["status"] == "done"
    assert resp["llmCallCount"] == 3
    assert resp["toolCallCount"] == 5
    assert resp["durationMs"] == 1234
    assert resp["slowestToolName"] == "miloco_im_push"
    assert resp["errorMsg"] == "oops"


def test_get_trace_response_fallback_when_missing(tmp_path: Path, monkeypatch):
    """找不到 meta → 返 ``{status:'done'}``（向后兼容老 adapter 行为）。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    resp = trace_reader.get_trace_response("missing")
    assert resp == {"status": "done"}


def test_get_trace_response_error_when_success_false(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    _write_meta(tmp_path, "sess-err", success=False)
    resp = trace_reader.get_trace_response("sess-err")
    assert resp["status"] == "error"


# ─── gateway_watch ────────────────────────────────────────────────────────


def test_extract_api_url_variants():
    """兼容多种字段命名。"""
    assert gateway_watch._extract_api_url({"api_server": {"url": "http://x:8642"}}) == "http://x:8642"
    assert gateway_watch._extract_api_url({"api": {"url": "http://y:9000"}}) == "http://y:9000"
    assert gateway_watch._extract_api_url({"api_url": "http://z:7000"}) == "http://z:7000"
    assert gateway_watch._extract_api_url({"endpoints": {"api": "http://a:1234"}}) == "http://a:1234"
    # 尾部斜杠会被 strip
    assert gateway_watch._extract_api_url({"api_url": "http://x:8642/"}) == "http://x:8642"
    # 找不到 → None
    assert gateway_watch._extract_api_url({}) is None
    assert gateway_watch._extract_api_url(None) is None


def test_read_current_api_url_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert gateway_watch.read_current_api_url() is None


def test_read_current_api_url_valid(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text(
        json.dumps({"api_server": {"url": "http://127.0.0.1:8642"}}), encoding="utf-8"
    )
    assert gateway_watch.read_current_api_url() == "http://127.0.0.1:8642"


def test_read_current_api_url_corrupt_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text("not json {", encoding="utf-8")
    assert gateway_watch.read_current_api_url() is None


def test_watcher_triggers_on_change(tmp_path: Path, monkeypatch):
    """watcher 启动后修改文件 → on_change 被调。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_path = tmp_path / "gateway_state.json"
    state_path.write_text(json.dumps({"api_url": "http://old:8642"}), encoding="utf-8")

    changes = []

    class W(gateway_watch.GatewayUrlWatcher):
        def __init__(self):
            super().__init__(on_change=lambda n, o: changes.append((n, o)), interval_s=0.1)

    w = W()
    w.start()
    time.sleep(0.15)
    # 修改文件 → 触发 on_change
    state_path.write_text(json.dumps({"api_url": "http://new:8642"}), encoding="utf-8")
    time.sleep(0.3)  # 至少 poll 一次
    w.stop()
    assert len(changes) >= 1
    new, old = changes[0]
    assert new == "http://new:8642"
    assert old == "http://old:8642"


def test_watcher_no_change_when_unchanged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text(json.dumps({"api_url": "http://x:8642"}), encoding="utf-8")
    changes = []
    gateway_watch.GatewayUrlWatcher(
        on_change=lambda n, o: changes.append((n, o)), interval_s=0.1
    ).start()
    time.sleep(0.4)
    assert changes == []


def test_watcher_handles_missing_file(tmp_path: Path, monkeypatch):
    """文件不存在时 watcher 不抛、不触发 on_change。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    changes = []
    gateway_watch.GatewayUrlWatcher(
        on_change=lambda n, o: changes.append((n, o)), interval_s=0.1
    ).start()
    time.sleep(0.3)
    assert changes == []