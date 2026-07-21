"""tick 驱动 omni 熔断器自动探测集成测试。

- try_arm_probe 三条件齐时 spawn probe task
- probe 成功 → 回 CLOSED
- probe 失败 → OPEN_RECOVERABLE + grow_backoff
- finally 兜底清 in-flight 位
"""

from __future__ import annotations

import asyncio
import time

import pytest
from miloco.perception.engine.omni.circuit_breaker import (
    CircuitState,
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
)


@pytest.fixture(autouse=True)
def _reset_cb():
    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


@pytest.fixture
def _mock_omni_config(monkeypatch):
    class _FakeOmni:
        model = "m"
        base_url = "https://x/v1"
        api_key = "sk-x"

    class _FakeModel:
        omni = _FakeOmni()

    class _FakeSettings:
        model = _FakeModel()

    monkeypatch.setattr(
        "miloco.config.get_settings", lambda: _FakeSettings()
    )


async def _wait_no_in_flight(cb, deadline_s: float = 2.0) -> None:
    """轮询等 probe task 跑完 (in_flight 清位)。probe 是 fire-and-forget spawn 的,
    调用 drive_omni_probe 后立即返回,需等 task 实际完成。"""
    t0 = time.monotonic()
    while cb._probe_in_flight:
        if time.monotonic() - t0 > deadline_s:
            raise AssertionError("probe task 超时未完成")
        await asyncio.sleep(0.01)


async def test_tick_drive_probe_success_recovers_to_closed(monkeypatch, _mock_omni_config):
    """probe 成功 → 熔断从 OPEN_RECOVERABLE 回 CLOSED,感知恢复。"""
    from miloco.perception import processor as _processor

    async def _fake_probe(model, base_url, api_key):
        return {"ok": True, "code": "ok", "status": 200, "latency_ms": 10}

    monkeypatch.setattr(
        "miloco.perception.engine.omni.probe.probe_omni", _fake_probe
    )

    cb = get_omni_circuit_breaker()
    # 3 次可恢复失败 → OPEN_RECOVERABLE
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE

    # 手动把 backoff 过期时间调到过去(避免真的 sleep 1s)
    cb._next_probe_at_monotonic = time.monotonic() - 1.0

    # 构造一个最小 PipelineProcessor:只用 drive_omni_probe,不依赖其他成员
    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    pipe.drive_omni_probe()
    await _wait_no_in_flight(cb)

    assert cb.state_for_test() == CircuitState.CLOSED


async def test_tick_drive_probe_failure_grows_backoff(monkeypatch, _mock_omni_config):
    """probe 继续失败 → 状态回 OPEN_RECOVERABLE,backoff 涨,in-flight 清位。"""
    from miloco.perception import processor as _processor

    async def _fake_probe(model, base_url, api_key):
        return {"ok": False, "code": "unreachable", "message": "still down"}

    monkeypatch.setattr(
        "miloco.perception.engine.omni.probe.probe_omni", _fake_probe
    )

    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    cb._next_probe_at_monotonic = time.monotonic() - 1.0
    backoff_before = cb._current_backoff

    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    pipe.drive_omni_probe()
    await _wait_no_in_flight(cb)

    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    assert cb._current_backoff > backoff_before  # 指数增长
    assert cb._probe_in_flight is False           # 位清了


async def test_tick_drive_noop_when_closed(_mock_omni_config):
    """CLOSED 稳态下 drive_omni_probe 直接 sync 返回,不 spawn。"""
    from miloco.perception import omni_probe_registry as _registry
    from miloco.perception import processor as _processor

    cb = get_omni_circuit_breaker()
    assert cb.state_for_test() == CircuitState.CLOSED

    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    task_set_before = set(_registry._OMNI_PROBE_TASKS)
    pipe.drive_omni_probe()
    task_set_after = set(_registry._OMNI_PROBE_TASKS)

    assert task_set_after == task_set_before  # 没 spawn


async def test_tick_drive_noop_when_backoff_not_due(_mock_omni_config):
    """OPEN_RECOVERABLE 但 backoff 未到期 → 不 spawn。"""
    from miloco.perception import omni_probe_registry as _registry
    from miloco.perception import processor as _processor

    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    # 不动 _next_probe_at_monotonic,刚 open 就检查(未到期)

    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    task_set_before = set(_registry._OMNI_PROBE_TASKS)
    pipe.drive_omni_probe()
    task_set_after = set(_registry._OMNI_PROBE_TASKS)

    assert task_set_after == task_set_before


async def test_probe_no_key_records_config_error(monkeypatch):
    """settings 里 api_key 为空 → probe task 直接记 bad_key 失败(不发 http)。"""
    from miloco.perception import processor as _processor

    class _EmptyOmni:
        model = "m"
        base_url = "https://x/v1"
        api_key = ""

    class _M:
        omni = _EmptyOmni()

    class _S:
        model = _M()

    monkeypatch.setattr("miloco.config.get_settings", lambda: _S())

    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    cb._next_probe_at_monotonic = time.monotonic() - 1.0

    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    pipe.drive_omni_probe()
    await _wait_no_in_flight(cb)

    # bad_key 是 CONFIG 类 → 转 OPEN_CONFIG
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG


async def test_probe_exception_falls_back_to_failure_record(monkeypatch, _mock_omni_config):
    """probe_omni 抛异常 → except 兜底记一次可恢复失败,位清。"""
    from miloco.perception import processor as _processor

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "miloco.perception.engine.omni.probe.probe_omni", _boom
    )

    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    cb._next_probe_at_monotonic = time.monotonic() - 1.0

    class _Stub:
        pass

    pipe = _Stub()
    pipe.drive_omni_probe = _processor.PipelineProcessor.drive_omni_probe.__get__(
        pipe, _Stub
    )

    pipe.drive_omni_probe()
    await _wait_no_in_flight(cb)

    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    assert cb._probe_in_flight is False


async def test_probe_cancelled_falls_back_to_open_recoverable(monkeypatch, _mock_omni_config):
    """review #1 回归:_run_omni_probe 在 mark_half_open 后 probe_omni 被 cancel
    (runner.stop / loop 关闭 / 显式 cancel task),之前只在 finally 清 _probe_in_flight
    不改 state,state 卡在 HALF_OPEN → tick 不 arm、before_call 短路、retry no-op,
    永久卡死。修复后 CancelledError 分支走 record_probe_result(cancelled, RECOVERABLE)
    把 state 回落到 OPEN_RECOVERABLE。"""
    import asyncio as _a

    from miloco.perception.processor import _run_omni_probe

    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
        )
    cb._probe_in_flight = True  # 模拟 try_arm_probe 已置位

    # probe_omni 挂起足够久,让我们能在中间 cancel
    async def _hang(*a, **k):
        await _a.sleep(30)

    monkeypatch.setattr("miloco.perception.engine.omni.probe.probe_omni", _hang)

    task = _a.create_task(_run_omni_probe())
    # 让 task 跑到 await probe_omni 那一步 (先 mark_half_open,再进 sleep)
    for _ in range(10):
        await _a.sleep(0)
        if cb.state_for_test() == CircuitState.HALF_OPEN:
            break
    assert cb.state_for_test() == CircuitState.HALF_OPEN

    task.cancel()
    with pytest.raises(_a.CancelledError):
        await task

    # 关键断言:state 已回落到 OPEN_RECOVERABLE,不再卡在 HALF_OPEN
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    assert cb._probe_in_flight is False
    assert cb.snapshot().code == "cancelled"
