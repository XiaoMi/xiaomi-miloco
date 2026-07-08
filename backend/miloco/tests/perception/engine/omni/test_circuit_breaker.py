"""circuit_breaker 单元测试。用 monkeypatch 冻结时间便于断言 backoff。"""

from __future__ import annotations

import time

import pytest
from miloco.perception.engine.omni.circuit_breaker import (
    CircuitOpenError,
    CircuitState,
    OmniCircuitBreaker,
)
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
)


def _rec(
    code: str = "unreachable", retry_after: float | None = None
) -> ClassifiedError:
    return ClassifiedError(code, "m", ErrorCategory.RECOVERABLE, retry_after)


def _cfg(code: str = "bad_key") -> ClassifiedError:
    return ClassifiedError(code, "m", ErrorCategory.CONFIG)


@pytest.fixture
def cb():
    return OmniCircuitBreaker(
        consecutive_threshold=3,
        window_seconds=60.0,
        window_min_samples=5,
        window_error_rate=0.5,
        backoff_start=1.0,
        backoff_multiplier=2.0,
        backoff_caps={"rate_limited": 60.0, "_default": 600.0},
        jitter_ratio=0.0,  # 关掉抖动便于断言
    )


@pytest.fixture
def frozen_time(monkeypatch):
    """冻结 time.monotonic(),测试可通过 tick(s) 推进。"""

    class Clock:
        now = 1_000_000.0

        def tick(self, sec: float):
            self.now += sec

    c = Clock()
    monkeypatch.setattr(time, "monotonic", lambda: c.now)
    return c


# ─── 初始态与成功路径 ───────────────────────────────────────────────────────


async def test_starts_closed(cb):
    await cb.before_call()  # 不抛
    assert cb.snapshot().state == "ok"


async def test_success_resets_consecutive_counter(cb):
    """1 fail → success → 2 fail 只是 2 连续,未达 threshold=3;
    且总样本 4 中 3 fail = 75% 但样本数 < min_samples=5,window 判据也不触发。"""
    await cb.record_failure(_rec())
    await cb.record_success()
    await cb.record_failure(_rec())
    await cb.record_failure(_rec())
    await cb.before_call()  # 仍 CLOSED,不抛


# ─── OPEN_RECOVERABLE 触发 ──────────────────────────────────────────────────


async def test_three_consecutive_failures_open_recoverable(cb):
    for _ in range(3):
        await cb.record_failure(_rec())
    with pytest.raises(CircuitOpenError):
        await cb.before_call()
    snap = cb.snapshot()
    assert snap.state == "warn"
    assert snap.consecutive_failures == 3


async def test_window_error_rate_triggers_open(cb):
    """交错样本、consecutive 达不到 3 但 window 错误率超阈值也触发。

    先 3 成功 + 1 失败(4 samples,fails=1,rate=25%,未触发)
    加 1 成功 + 2 失败(7 samples,fails=3;consecutive=2)
    → 3/7 ≈ 43% 未 >50%,仍 CLOSED。
    再加 1 失败:consecutive=3 → 已达阈值触发。
    为了单独验证 window 判据,我用 3 成功交错 5 失败,让 consecutive 保持在 2 内。
    """
    # succ, fail, succ, fail, succ, fail, fail, fail
    # → 8 samples,fails=5,consecutive=3(最后 3 连续)
    # 期望:consecutive 阈值触发(等价路径已覆盖);
    # 为强测 window 判据,先构造 consecutive 达不到 3 但 rate 超的
    # 5 samples:succ, fail, succ, fail, fail → consecutive=2, rate=3/5=60%(>50%)
    await cb.record_success()
    await cb.record_failure(_rec())
    await cb.record_success()
    await cb.record_failure(_rec())
    await cb.record_failure(_rec())
    # consecutive=2 未达 3;但 3/5=60%>50% → 触发
    with pytest.raises(CircuitOpenError):
        await cb.before_call()


# ─── OPEN_CONFIG 触发 ───────────────────────────────────────────────────────


async def test_single_config_error_opens_config(cb):
    await cb.record_failure(_cfg("bad_key"))
    with pytest.raises(CircuitOpenError):
        await cb.before_call()
    snap = cb.snapshot()
    assert snap.state == "error"
    assert snap.code == "bad_key"


async def test_config_error_takes_precedence_over_recoverable(cb):
    """recoverable 失败 2 次后再来一次 config 错 → OPEN_CONFIG,不是 OPEN_RECOVERABLE。"""
    await cb.record_failure(_rec())
    await cb.record_failure(_rec())
    await cb.record_failure(_cfg("bad_key"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG


# ─── 指数退避 ───────────────────────────────────────────────────────────────


async def test_backoff_grows_exponentially(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    # 首次 backoff = 1s
    snap1 = cb.snapshot()
    delta1 = snap1.next_probe_at_ms - int(time.time() * 1000)
    assert 800 < delta1 < 1200

    frozen_time.tick(1.5)
    await cb.record_probe_result(False, _rec())
    # 第二次 backoff = 2s
    snap2 = cb.snapshot()
    delta2 = snap2.next_probe_at_ms - int(time.time() * 1000)
    assert 1800 < delta2 < 2200


async def test_backoff_cap_rate_limited_60s(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec("rate_limited"))
    for _ in range(15):
        frozen_time.tick(1000)
        await cb.record_probe_result(False, _rec("rate_limited"))
    snap = cb.snapshot()
    delta = snap.next_probe_at_ms - int(time.time() * 1000)
    assert delta <= 60_100


async def test_backoff_cap_unreachable_600s(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec("unreachable"))
    for _ in range(15):
        frozen_time.tick(1000)
        await cb.record_probe_result(False, _rec("unreachable"))
    snap = cb.snapshot()
    delta = snap.next_probe_at_ms - int(time.time() * 1000)
    assert 60_000 < delta <= 600_100


async def test_retry_after_header_bumps_backoff(cb):
    """收到 Retry-After: 45s,即使 backoff 起始 1s 也要至少等 45s。"""
    for _ in range(3):
        await cb.record_failure(_rec("rate_limited", retry_after=45.0))
    snap = cb.snapshot()
    delta_ms = snap.next_probe_at_ms - int(time.time() * 1000)
    assert 40_000 < delta_ms < 50_000


# ─── HALF_OPEN 恢复 ─────────────────────────────────────────────────────────


async def test_probe_success_returns_to_closed(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    await cb.record_probe_result(True, None)
    await cb.before_call()  # CLOSED
    assert cb.snapshot().state == "ok"


async def test_probe_due_returns_false_before_deadline(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    assert cb.probe_due() is False  # 刚 open,还没到


async def test_probe_due_returns_true_after_deadline(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    assert cb.probe_due() is True


async def test_mark_half_open_transitions(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    await cb.mark_half_open()
    assert cb.state_for_test() == CircuitState.HALF_OPEN


# ─── 手动重试 ───────────────────────────────────────────────────────────────


async def test_retry_now_from_open_recoverable(cb):
    for _ in range(3):
        await cb.record_failure(_rec())
    await cb.retry_now()
    assert cb.state_for_test() == CircuitState.HALF_OPEN


async def test_retry_now_from_open_config(cb):
    """OPEN_CONFIG 也允许手动 retry。"""
    await cb.record_failure(_cfg("bad_key"))
    await cb.retry_now()
    assert cb.state_for_test() == CircuitState.HALF_OPEN


async def test_retry_now_from_closed_is_noop(cb):
    await cb.retry_now()
    assert cb.state_for_test() == CircuitState.CLOSED


# ─── 配置变化 ───────────────────────────────────────────────────────────────


async def test_reset_on_config_change_from_config_error(cb):
    await cb.record_failure(_cfg("bad_key"))
    await cb.reset_on_config_change()
    await cb.before_call()
    assert cb.snapshot().state == "ok"


async def test_reset_on_config_change_from_recoverable(cb):
    for _ in range(3):
        await cb.record_failure(_rec())
    await cb.reset_on_config_change()
    await cb.before_call()
    assert cb.snapshot().state == "ok"


# ─── 监听器 ─────────────────────────────────────────────────────────────────


async def test_listener_fires_on_state_change(cb):
    seen: list = []
    cb.register_listener(lambda snap: seen.append(snap.state))
    for _ in range(3):
        await cb.record_failure(_rec())
    assert "warn" in seen
    await cb.record_probe_result(True, None)
    assert seen[-1] == "ok"


async def test_listener_exceptions_are_swallowed(cb):
    """listener 抛异常不能连累熔断器。"""

    def bad(snap):
        raise RuntimeError("listener crashed")

    cb.register_listener(bad)
    # 不抛
    await cb.record_failure(_cfg("bad_key"))
    assert cb.snapshot().state == "error"


# ─── snapshot 字段 ─────────────────────────────────────────────────────────


async def test_snapshot_since_ms_zero_when_closed(cb):
    assert cb.snapshot().since_ms == 0


async def test_snapshot_since_ms_nonzero_when_open(cb, frozen_time):
    await cb.record_failure(_cfg("bad_key"))
    frozen_time.tick(5)
    assert cb.snapshot().since_ms > 4000
