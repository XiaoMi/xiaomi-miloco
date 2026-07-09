# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""SSE 推送 + /api/events/stream endpoint 集成测试(D3-T19).

覆盖:
- _persist_meaningful_event 末尾调 pipeline._publish("meaningful_event", payload)
- SSE 订阅者过滤掉 metric / preview 类型
- 多订阅者广播
- publish 失败不阻塞 _persist 主路径(B11)
"""

import asyncio

import pytest
from miloco.perception.types import (
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
)


class _FakePipeline:
    """模拟 PipelineProcessor 的 subscribe_sse / unsubscribe_sse / _publish 三件套."""

    def __init__(self):
        self._subs: list[asyncio.Queue] = []
        self.publish_calls: list[tuple[str, dict]] = []
        self.raise_on_publish = False

    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subs.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        self._subs = [s for s in self._subs if s is not q]

    def _publish(self, event_type: str, data: dict) -> None:
        if self.raise_on_publish:
            raise RuntimeError("simulated publish failure")
        self.publish_calls.append((event_type, data))
        for q in self._subs:
            try:
                q.put_nowait((event_type, data))
            except asyncio.QueueFull:
                pass


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """独立 DB + Manager singleton + 注入 FakePipeline."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    # 给 manager 注入一个 fake perception_service(避免起完整引擎)
    fake_pipeline = _FakePipeline()

    class _FakeService:
        _pipeline = fake_pipeline

    mgr = manager_module.Manager()
    mgr._perception_service = _FakeService()
    # 拾音默认关(opt-in)后，speech 需相机在拾音白名单才落库/推 SSE：把测试相机加进白名单。
    from miloco.database.kv_repo import KVRepo
    from miloco.miot.filter import set_cameras_voice_in_use

    mgr._kv_repo = KVRepo()
    set_cameras_voice_in_use(mgr._kv_repo, ["cam_living_01"], True)

    yield tmp_path, fake_pipeline

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


def _make_clip(kind: str = "mp4") -> "tuple[bytes, str]":
    """造 (bytes, kind) 元组模拟 omni push_clip_bytes 出来的 artifacts.clips payload."""
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100, kind


def _artifacts(clips: dict | None = None):
    """造 OmniEventArtifacts 实例,只填 clips,trace 留 None."""
    from miloco.perception.snapshot_context import OmniEventArtifacts

    return OmniEventArtifacts(clips=clips or {})


@pytest.mark.asyncio
class TestSSEPublishFromPersist:
    async def test_publish_after_persist(self, isolated_env):
        """_persist 完成后 pipeline._publish("meaningful_event", ...) 被调用,
        payload 字段与 /api/events list 元素同形."""
        _, fake_pipeline = isolated_env

        from miloco.perception.client import _persist_meaningful_event

        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")],
            speeches=[
                Speech(
                    needs_response=True,
                    speaker="u",
                    content="开灯",
                    is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ],
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({"cam_living_01": _make_clip()}),
        )

        assert len(fake_pipeline.publish_calls) == 1
        event_type, payload = fake_pipeline.publish_calls[0]
        assert event_type == "meaningful_event"
        # 字段对齐 /api/events list 元素
        assert {"event_id", "timestamp", "text", "has_rule_hit",
                "has_suggestion", "has_asr", "snapshot_count",
                "device_ids"}.issubset(payload.keys())
        assert payload["has_rule_hit"] is True
        assert payload["has_asr"] is True
        assert payload["device_ids"] == ["cam_living_01"]
        # publish 在落盘完成后,snapshot_count 是真实值(语义:成功落 clip 的 device 数).
        # 1 device 落 1 个 clip.mp4 → count=1.
        assert payload["snapshot_count"] == 1

    async def test_no_publish_when_not_meaningful(self, isolated_env):
        """纯 caption(不入表)→ 也不 publish."""
        from miloco.perception.types import CaptionEntry

        _, fake_pipeline = isolated_env
        from miloco.perception.client import _persist_meaningful_event

        result = RealtimePerceptionResult(
            caption=[CaptionEntry(description="看电视")]
        )
        await _persist_meaningful_event(
            result=result, device_ids=["cam_a"], artifacts=_artifacts()
        )
        assert fake_pipeline.publish_calls == []

    async def test_publish_for_metadata_only_event(self, isolated_env):
        """B1 修复:meaningful event 但 frames_jpeg 空 → 仍入表 + 仍 publish(count=0).

        前端依赖 SSE 实时拿到新事件;若无 frames 时不 publish,user 看到延迟到下次
        chip 切换 / 5min reload 才出现新行(体验明显延迟).
        """
        _, fake_pipeline = isolated_env
        from miloco.perception.client import _persist_meaningful_event

        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")]
        )
        await _persist_meaningful_event(
            result=result, device_ids=["cam_a"], artifacts=_artifacts()
        )
        assert len(fake_pipeline.publish_calls) == 1
        event_type, payload = fake_pipeline.publish_calls[0]
        assert event_type == "meaningful_event"
        assert payload["snapshot_count"] == 0  # 无 frames → count 留 0
        assert payload["has_rule_hit"] is True

    async def test_publish_failure_does_not_block_persist(self, isolated_env):
        """B11:publish 抛异常 → _persist 主路径仍走完(入表 + 落盘),不抛."""
        _, fake_pipeline = isolated_env
        fake_pipeline.raise_on_publish = True

        from miloco.manager import get_manager
        from miloco.perception.client import _persist_meaningful_event

        result = RealtimePerceptionResult(
            suggestions=[
                __import__("miloco.perception.types", fromlist=["Suggestion"]).Suggestion(
                    event="e", action="a"
                )
            ]
        )
        # 不应抛
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_a"],
            artifacts=_artifacts({"cam_a": _make_clip()}),
        )
        # 入表仍发生
        rows = get_manager().meaningful_events_dao.query()
        assert len(rows) == 1


@pytest.mark.asyncio
class TestSSEEndpoint:
    async def test_endpoint_filters_non_meaningful(self, isolated_env):
        """stream endpoint generator 只 yield meaningful_event 类型."""
        _, fake_pipeline = isolated_env

        from miloco.perception.events_router import events_stream

        # 调用 generator(EventSourceResponse 是 starlette response,需要 await async)
        response = await events_stream()
        # 拿到 inner generator(注入测试事件后取 yield)
        gen = response.body_iterator

        # 模拟 _publish 推 metric 和 meaningful_event
        fake_pipeline._publish("metric", {"foo": "bar"})
        fake_pipeline._publish("meaningful_event", {"event_id": "e1"})
        fake_pipeline._publish("preview", {"frames": []})
        fake_pipeline._publish("meaningful_event", {"event_id": "e2"})

        # 取前两个 yield
        received = []
        for _ in range(2):
            evt = await asyncio.wait_for(anext(gen), timeout=1.0)
            received.append(evt)

        # ServerSentEvent 对象有 event 和 data 属性
        events = [r.event if hasattr(r, "event") else r.get("event") for r in received]
        datas = [r.data if hasattr(r, "data") else r.get("data") for r in received]
        assert events == ["new_event", "new_event"]
        # data 是 JSON 字符串
        import json

        ids = [json.loads(d)["event_id"] for d in datas]
        assert ids == ["e1", "e2"]

        # 关闭 generator,确保 unsubscribe 在 finally 触发
        await gen.aclose()
        assert len(fake_pipeline._subs) == 0

    async def test_multi_subscriber_broadcast(self, isolated_env):
        """多个订阅者各收到同一条 push."""
        _, fake_pipeline = isolated_env

        q1 = fake_pipeline.subscribe_sse()
        q2 = fake_pipeline.subscribe_sse()
        fake_pipeline._publish("meaningful_event", {"event_id": "x"})

        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert e1 == e2 == ("meaningful_event", {"event_id": "x"})


@pytest.mark.asyncio
class TestProcessorSSEQueueBounded:
    """真 PipelineProcessor subscribe_sse 队列上限保护(M2 修复回归)."""

    async def test_subscribe_sse_queue_has_maxsize(self):
        """subscribe_sse 返回的 Queue 必须有 maxsize,避免慢消费 OOM."""
        # 不需要真 collector / engine_proxy / log_repo,只测 SSE 方法
        from unittest.mock import MagicMock

        from miloco.perception.processor import PipelineProcessor

        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        q = proc.subscribe_sse()
        try:
            assert q.maxsize > 0, "queue must be bounded to prevent OOM on slow consumers"
            # 填满队列后下一次 put_nowait 应抛 QueueFull(由 _publish catch + log)
            for i in range(q.maxsize):
                q.put_nowait(("e", {"i": i}))
            # _publish 不传播 QueueFull,只 log + drop
            proc._publish("e", {"overflow": True})
            # 队列还在 maxsize,没溢出
            assert q.qsize() == q.maxsize
        finally:
            proc.unsubscribe_sse(q)
