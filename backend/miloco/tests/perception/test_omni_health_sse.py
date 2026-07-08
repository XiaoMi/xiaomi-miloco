"""circuit_breaker 状态变化 → PipelineProcessor SSE 广播 omni_health 事件。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from miloco.perception.engine.omni.circuit_breaker import (
    OmniCircuitBreaker, get_omni_circuit_breaker, reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError, ErrorCategory,
)


@pytest.fixture(autouse=True)
def _reset_cb():
    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


async def test_listener_emits_asdict_snapshot():
    """listener 收到 HealthSnapshot,可以 asdict 序列化(SSE 会 JSON 编码)。"""
    seen: list[dict] = []
    cb = OmniCircuitBreaker(consecutive_threshold=1, jitter_ratio=0.0)
    cb.register_listener(lambda snap: seen.append(asdict(snap)))
    await cb.record_failure(ClassifiedError("bad_key", "无效", ErrorCategory.CONFIG))
    assert len(seen) == 1
    payload = seen[0]
    assert payload["state"] == "error"
    assert payload["code"] == "bad_key"
    # 必需字段全在
    assert set(payload.keys()) >= {
        "state", "code", "message", "since_ms",
        "consecutive_failures", "next_probe_at_ms",
        "last_probe_at_ms", "last_probe_result",
    }


async def test_multiple_transitions_emit_multiple_events():
    seen: list[str] = []
    cb = OmniCircuitBreaker(consecutive_threshold=1, jitter_ratio=0.0)
    cb.register_listener(lambda snap: seen.append(snap.state))
    await cb.record_failure(ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE))
    await cb.mark_half_open()
    await cb.record_probe_result(True, None)
    # warn → warn(half_open 也是 warn) → ok
    assert seen == ["warn", "warn", "ok"]


async def test_bridge_to_pipeline_publish(monkeypatch):
    """perception 模块 init 时把 listener 桥接到 pipeline._publish。

    这里手动模拟 init 里的 bridge 逻辑,断言 pipeline._publish 收到 omni_health 事件。
    """
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker

    published: list[tuple[str, dict]] = []

    class _FakePipeline:
        def _publish(self, event_type: str, data: dict):
            published.append((event_type, data))

    fake = _FakePipeline()

    def _emit(snap):
        fake._publish("omni_health", asdict(snap))

    cb = get_omni_circuit_breaker()
    cb.register_listener(_emit)

    await cb.record_failure(ClassifiedError("bad_key", "无效", ErrorCategory.CONFIG))
    assert len(published) == 1
    ev, data = published[0]
    assert ev == "omni_health"
    assert data["state"] == "error"


async def test_short_circuit_records_zero_latency_trace(monkeypatch):
    """熔断短路时 push_omni_trace latency=0(spec §8)。"""
    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni import omni_client

    traces: list[dict] = []
    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni_client.push_omni_trace",
        lambda **kw: traces.append(kw),
    )

    # 让熔断进 OPEN_CONFIG
    cb = get_omni_circuit_breaker()
    await cb.record_failure(ClassifiedError("bad_key", "无效", ErrorCategory.CONFIG))

    cfg = OmniConfig(model="m", base_url="https://x/v1", api_key="sk-x",
                     temperature=0, top_p=1, max_completion_tokens=1, timeout=1.0, stream=False)
    with pytest.raises(omni_client.OmniError):
        await omni_client.call_omni({"system_prompt": "s", "user_content": "u"}, cfg)

    assert len(traces) == 1
    assert traces[0]["latency_ms"] == 0.0
    assert traces[0]["error"]["code"].startswith("skipped:cooling")
