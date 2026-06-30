# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""snapshot_context — ContextVar 旁路 event artifacts 收集器单测.

覆盖:
- scope 内 push_clip_bytes 进 artifacts.clips(kind 区分 mp4/m4a)
- scope 内 push_omni_trace 累积 artifacts.trace.calls
- scope 外 push 静默 no-op
- 无 device_ctx push_clip_bytes 静默 no-op
- 多 device 分组
- asyncio task 隔离(子 task 复制父 ContextVar 当前值,但不影响父)
- _strip_base64 剥多模态 base64
- _pick_response_fields 抽 OpenAI raw 字段
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.observability.context import (
    DeviceContext,
    reset_device_context,
    set_device_context,
)
from miloco.perception.snapshot_context import (
    OmniEventArtifacts,
    _pick_response_fields,
    _strip_base64,
    event_artifacts_scope,
    push_clip_bytes,
    push_omni_trace,
)


def test_artifacts_defaults():
    """OmniEventArtifacts 默认 clips 是空 dict、trace 是 None."""
    a = OmniEventArtifacts()
    assert a.clips == {}
    assert a.trace is None


def test_no_scope_is_noop():
    """无 active scope → push 静默 no-op,不抛."""
    token = set_device_context(DeviceContext(device_trace_id="t", device_id="cam_a", room_name="r"))
    try:
        push_clip_bytes(b"some-bytes", "mp4")  # 不应抛
        push_omni_trace(
            request_messages=[],
            response_raw=None,
            latency_ms=0,
            error=None,
            model="m",
        )
    finally:
        reset_device_context(token)


def test_no_device_context_is_noop():
    """有 scope 但无 device_ctx(未 set)→ push_clip_bytes no-op,artifacts.clips 保持空."""
    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        push_clip_bytes(b"some-bytes", "mp4")  # device_ctx 未 set
    assert artifacts.clips == {}


def test_scope_collects_per_device_with_kind():
    """scope 内 push_clip_bytes 按 device_id 分组写入 artifacts.clips,带 kind 标."""
    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        t1 = set_device_context(DeviceContext(device_trace_id="t1", device_id="cam_a", room_name="r"))
        try:
            push_clip_bytes(b"clip-A", "mp4")  # 视频路径
        finally:
            reset_device_context(t1)

        t2 = set_device_context(DeviceContext(device_trace_id="t2", device_id="cam_b", room_name="r"))
        try:
            push_clip_bytes(b"clip-B", "m4a")  # audio-only 路径
        finally:
            reset_device_context(t2)

    assert set(artifacts.clips.keys()) == {"cam_a", "cam_b"}
    assert artifacts.clips["cam_a"] == (b"clip-A", "mp4")
    assert artifacts.clips["cam_b"] == (b"clip-B", "m4a")


def test_scope_exit_resets():
    """scope 退出后,push 再次 no-op."""
    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        pass
    # scope 已退出
    t = set_device_context(DeviceContext(device_trace_id="t", device_id="cam_a", room_name="r"))
    try:
        push_clip_bytes(b"after-scope", "mp4")  # no-op
    finally:
        reset_device_context(t)
    # artifacts.clips 没被填(scope 内本来就没 push)
    assert artifacts.clips == {}


def test_push_omni_trace_accumulates():
    """scope 内 push_omni_trace 累积调用到 artifacts.trace.calls,首次调用建 schema."""
    artifacts = OmniEventArtifacts()
    raw = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    token = set_device_context(DeviceContext(device_trace_id="t", device_id="cam_a", room_name="客厅"))
    try:
        with event_artifacts_scope(artifacts):
            push_omni_trace(
                request_messages=[{"role": "system", "content": "sys"}],
                response_raw=raw,
                latency_ms=12.3,
                error=None,
                model="mimo-vl",
            )
    finally:
        reset_device_context(token)
    assert artifacts.trace is not None
    assert artifacts.trace["schema_version"] == 1
    assert len(artifacts.trace["calls"]) == 1
    call = artifacts.trace["calls"][0]
    assert call["device_id"] == "cam_a"
    assert call["model"] == "mimo-vl"
    assert call["request"]["system"] == "sys"
    assert call["response"]["content"] == "hello"
    assert call["response"]["usage"]["total_tokens"] == 15
    assert call["latency_ms"] == 12.3
    assert call["error"] is None


def test_push_omni_trace_error_path():
    """HTTP 失败时 response_raw=None,trace 仍记录 error 行."""
    artifacts = OmniEventArtifacts()
    token = set_device_context(DeviceContext(device_trace_id="t", device_id="cam_b", room_name="r"))
    try:
        with event_artifacts_scope(artifacts):
            push_omni_trace(
                request_messages=[],
                response_raw=None,
                latency_ms=1.0,
                error={"code": "TimeoutError", "msg": "deadline exceeded"},
                model="mimo-vl",
            )
    finally:
        reset_device_context(token)
    call = artifacts.trace["calls"][0]
    assert call["device_id"] == "cam_b"
    assert call["response"] == {"content": "", "usage": {}}
    assert call["error"] == {"code": "TimeoutError", "msg": "deadline exceeded"}


def test_push_omni_trace_fused_no_device_context():
    """fused 路径(batch 级单次调用)未 set device_context → device_id 记 null."""
    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        push_omni_trace(
            request_messages=[],
            response_raw={"choices": [{"message": {"content": "fused"}}], "usage": {}},
            latency_ms=2.0,
            error=None,
            model="mimo-vl",
        )
    call = artifacts.trace["calls"][0]
    assert call["device_id"] is None
    assert call["response"]["content"] == "fused"


def test_push_omni_trace_multi_device_keeps_per_call_attribution():
    """多摄像头 batch:per-device scope 切换后 push,每条 call 记录各自的 device_id,
    reader 能跟 artifacts.clips 的 device 维度对齐。"""
    artifacts = OmniEventArtifacts()
    raw = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    with event_artifacts_scope(artifacts):
        for did in ("cam_a", "cam_b", "cam_c"):
            t = set_device_context(DeviceContext(device_trace_id="t", device_id=did, room_name="r"))
            try:
                push_omni_trace(
                    request_messages=[], response_raw=raw,
                    latency_ms=1.0, error=None, model="mimo-vl",
                )
            finally:
                reset_device_context(t)
    calls = artifacts.trace["calls"]
    assert [c["device_id"] for c in calls] == ["cam_a", "cam_b", "cam_c"]


def test_strip_base64_keeps_text_drops_payload():
    """_strip_base64 保留 text 原文,video_url / image_url 只保留 type."""
    messages = [
        {"role": "system", "content": "sys text"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi there"},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,XXXX"}},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YYYY"}},
            ],
        },
    ]
    out = _strip_base64(messages)
    assert out["system"] == "sys text"
    assert out["user_blocks"] == [
        {"type": "text", "text": "hi there"},
        {"type": "video_url"},
        {"type": "image_url"},
    ]


def test_pick_response_fields_branches():
    """raw=None / 空 choices / 完整 raw 三分支."""
    assert _pick_response_fields(None) == {"content": "", "usage": {}}
    assert _pick_response_fields({}) == {"content": "", "usage": {}}
    assert _pick_response_fields({"choices": []}) == {"content": "", "usage": {}}
    full = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 7},
    }
    out = _pick_response_fields(full)
    assert out == {"content": "ok", "usage": {"total_tokens": 7}}


@pytest.mark.asyncio
async def test_async_task_isolation():
    """子 task 复制父 ContextVar 当前值,但子 task 修改不影响父 (PEP 567).

    这里测的是:外层无 scope,子 task 开 scope 后 push 进入子 task 自己的 artifacts,
    外层仍是 no-op.
    """
    parent_artifacts = OmniEventArtifacts()

    async def child():
        child_artifacts = OmniEventArtifacts()
        with event_artifacts_scope(child_artifacts):
            t = set_device_context(DeviceContext(device_trace_id="t", device_id="cam_c", room_name="r"))
            try:
                push_clip_bytes(b"clip-child", "mp4")
            finally:
                reset_device_context(t)
        return child_artifacts

    result = await asyncio.create_task(child())
    assert result.clips == {"cam_c": (b"clip-child", "mp4")}
    # 父 artifacts 不变
    assert parent_artifacts.clips == {}
