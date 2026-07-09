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


async def test_single_config_error_does_not_open(cb):
    """瞬时 400/422(corrupted image/video)不应一击停感知——CONFIG 也走窗口阈值。"""
    await cb.record_failure(_cfg("bad_key"))
    await cb.before_call()  # 仍 CLOSED,不抛
    assert cb.state_for_test() == CircuitState.CLOSED


async def test_two_consecutive_config_errors_do_not_open(cb):
    """threshold=3,2 次 config 未达阈值,仍 CLOSED。"""
    await cb.record_failure(_cfg("bad_key"))
    await cb.record_failure(_cfg("bad_key"))
    assert cb.state_for_test() == CircuitState.CLOSED


async def test_three_consecutive_config_errors_open_config(cb):
    """连续 3 次 CONFIG(consecutive_threshold=3)→ OPEN_CONFIG。"""
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    with pytest.raises(CircuitOpenError):
        await cb.before_call()
    snap = cb.snapshot()
    assert snap.state == "error"
    assert snap.code == "bad_key"


async def test_config_error_takes_precedence_over_recoverable(cb):
    """recoverable 失败 2 次后再来一次 config 错,3 连续达阈值,最后一击是 CONFIG
    → OPEN_CONFIG,不是 OPEN_RECOVERABLE。"""
    await cb.record_failure(_rec())
    await cb.record_failure(_rec())
    await cb.record_failure(_cfg("bad_key"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG


async def test_open_config_refreshes_code_on_subsequent_config_error(cb):
    """已在 OPEN_CONFIG 后再来 config 错,不改状态但刷新 code/message 给前端横条。"""
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    await cb.record_failure(_cfg("not_found"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG
    assert cb.snapshot().code == "not_found"


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
    # jitter 只加不减(review Finding 6):下限约等于 45_000ms,不可显著低于 Retry-After
    # (允许 int(delta_s*1000) 与 time.time() 的 1-2ms rounding 抖动)
    assert delta_ms >= 44_990
    # jitter 上限 +20%: 45 * 1.2 = 54_000
    assert delta_ms < 55_000


async def test_jitter_never_shorter_than_base(monkeypatch):
    """review Finding 6 回归:jitter 双向抖动会把 next_probe 拉到 base*0.8,
    server 明示 Retry-After: 45s 时可能被抖成 36s,违反 server 意图。
    改成 [0, +ratio] 单向后,即使 random 返回 0 也保证 >= base。"""
    import random as _random

    from miloco.perception.engine.omni.circuit_breaker import OmniCircuitBreaker

    # random.uniform 恒返 0 (最容易触发早于 base 的边界)
    monkeypatch.setattr(_random, "uniform", lambda a, b: 0.0)
    cb = OmniCircuitBreaker(
        consecutive_threshold=1,
        backoff_start=10.0,
        backoff_multiplier=2.0,
        jitter_ratio=0.5,
    )
    await cb.record_failure(_rec("rate_limited", retry_after=45.0))
    snap = cb.snapshot()
    delta_ms = snap.next_probe_at_ms - int(time.time() * 1000)
    # random 恒 0 时 next = base * 1.0 = 45s,不再是 base*0.5=22.5s
    # (允许 1-2ms rounding)
    assert delta_ms >= 44_990


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
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    await cb.retry_now()
    assert cb.state_for_test() == CircuitState.HALF_OPEN


async def test_retry_now_from_closed_is_noop(cb):
    await cb.retry_now()
    assert cb.state_for_test() == CircuitState.CLOSED


# ─── 配置变化 ───────────────────────────────────────────────────────────────


async def test_reset_on_config_change_from_config_error(cb):
    for _ in range(3):
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


async def test_listener_exceptions_are_swallowed(cb, caplog):
    """listener 抛异常不能连累熔断器,但要走 warning + exc_info 让运维能看到。"""

    def bad(snap):
        raise RuntimeError("listener crashed")

    cb.register_listener(bad)
    # 不抛
    with caplog.at_level("WARNING"):
        for _ in range(3):
            await cb.record_failure(_cfg("bad_key"))
    assert cb.snapshot().state == "error"
    # _emit 里改成 logger.warning(exc_info=True) 后应能看到堆栈
    assert any("listener" in r.message for r in caplog.records)


# ─── snapshot 字段 ─────────────────────────────────────────────────────────


async def test_snapshot_since_ms_zero_when_closed(cb):
    assert cb.snapshot().since_ms == 0


async def test_snapshot_since_ms_nonzero_when_open(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    frozen_time.tick(5)
    assert cb.snapshot().since_ms > 4000


# ─── try_arm_probe / probe_in_flight ────────────────────────────────────────


async def test_try_arm_probe_false_when_closed(cb):
    assert cb.try_arm_probe() is False


async def test_try_arm_probe_false_when_open_config(cb):
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    assert cb.try_arm_probe() is False


async def test_try_arm_probe_false_when_backoff_not_due(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    # 刚 open,backoff 未到期
    assert cb.try_arm_probe() is False


async def test_try_arm_probe_true_when_all_three_conditions_met(cb, frozen_time):
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    assert cb.try_arm_probe() is True


async def test_try_arm_probe_singleflight(cb, frozen_time):
    """并发 arm 只有一个能拿到 True。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    assert cb.try_arm_probe() is True
    assert cb.try_arm_probe() is False  # in-flight 位已占


async def test_record_probe_result_clears_in_flight(cb, frozen_time):
    """record_probe_result 无论成功/失败都要清位,下一 tick 才能再 arm。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    assert cb.try_arm_probe() is True
    await cb.record_probe_result(False, _rec())  # 失败,回 OPEN_RECOVERABLE,清位
    # 新一轮 backoff 到期后应可再 arm
    frozen_time.tick(10)
    assert cb.try_arm_probe() is True


async def test_clear_probe_in_flight_bypasses_state_change(cb, frozen_time):
    """clear_probe_in_flight 只清位、不动状态,给 finally 兜底用。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    assert cb.try_arm_probe() is True
    prev_state = cb.state_for_test()
    cb.clear_probe_in_flight()
    assert cb.state_for_test() == prev_state  # 状态不动
    # 再次 arm 依然能成功(位已清)
    assert cb.try_arm_probe() is True


# ─── before_call 短路 HALF_OPEN ─────────────────────────────────────────────


async def test_before_call_short_circuits_half_open(cb, frozen_time):
    """HALF_OPEN 期间感知 omni 调用也要被短路,防"探测中真发带视频请求"漏发。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    await cb.mark_half_open()
    assert cb.state_for_test() == CircuitState.HALF_OPEN
    with pytest.raises(CircuitOpenError):
        await cb.before_call()


# ─── record_success 稳态不 emit ─────────────────────────────────────────────


async def test_record_success_no_emit_when_already_closed(cb):
    """CLOSED → CLOSED 稳态不 emit,避免感知每 4s 成功窗口全刷 SSE。"""
    seen: list = []
    cb.register_listener(lambda snap: seen.append(snap.state))
    await cb.record_success()
    await cb.record_success()
    await cb.record_success()
    assert seen == []


async def test_record_success_emits_on_transition(cb, frozen_time):
    """非 CLOSED → CLOSED 仍要 emit,让 UI 感知恢复。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    seen: list = []
    cb.register_listener(lambda snap: seen.append(snap.state))
    frozen_time.tick(1.5)
    await cb.record_probe_result(True, None)  # OPEN_RECOVERABLE → CLOSED
    assert "ok" in seen


# ─── record_success 严格门控:防多相机并发 200 抹掉断路 ────────────────────────


async def test_record_success_from_open_recoverable_is_noop(cb):
    """多相机 gather 并发:cam1 fail 触发 OPEN_RECOVERABLE 后,cam2 之前的 in-flight
    请求 200 到达调用 record_success —— 必须保持 OPEN_RECOVERABLE 不动,
    否则运行时 200 会抹掉刚打开的断路,`before_call` 又放行,失败风暴复发。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    await cb.record_success()  # cam2 的迟到 200
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE


async def test_record_success_from_open_config_is_noop(cb):
    """OPEN_CONFIG 同样不受运行时 200 影响。"""
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG
    await cb.record_success()
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG


async def test_record_success_from_half_open_closes(cb, frozen_time):
    """HALF_OPEN → CLOSED 才是 record_success 关闭断路的唯一路径。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    await cb.mark_half_open()
    assert cb.state_for_test() == CircuitState.HALF_OPEN
    await cb.record_success()
    assert cb.state_for_test() == CircuitState.CLOSED


# ─── 多相机 gather 并发场景(review Finding 1 回归防护) ─────────────────────


async def test_probe_in_flight_reflects_arm_state(cb, frozen_time):
    """review Finding 2 回归:tick.try_arm_probe 后 probe_in_flight() 返 True,
    router.retry_omni_probe 靠这个短路,防 tick arm 与用户 retry 撞车双 probe。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    assert cb.probe_in_flight() is False
    frozen_time.tick(1.5)
    assert cb.try_arm_probe() is True
    # arm 之后 mark_half_open 之前的窗口,state 仍是 OPEN_RECOVERABLE 但 in_flight 已 True
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    assert cb.probe_in_flight() is True


async def test_snapshot_retry_available_in_seconds(cb, frozen_time):
    """review Finding 7d 回归:snapshot 携带 retry_available_in_seconds (monotonic
    差算),前端用它同步本地按钮冷却截止点,避免锚早于后端 last_probe_at 记录点。"""
    from miloco.perception.engine.omni.circuit_breaker import RETRY_COOLDOWN_SEC

    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    # 触发一次 probe 完成后 last_probe_at 落点
    await cb.record_probe_result(False, _rec())
    snap = cb.snapshot()
    # 刚 record 完,retry_available ≈ COOLDOWN
    assert snap.retry_available_in_seconds is not None
    assert snap.retry_available_in_seconds > RETRY_COOLDOWN_SEC - 0.5
    # 过冷却期后归零
    frozen_time.tick(RETRY_COOLDOWN_SEC + 1)
    snap2 = cb.snapshot()
    assert snap2.retry_available_in_seconds == 0.0


async def test_multi_camera_concurrent_success_after_open(cb):
    """模拟 pipeline._run_device 多相机 gather:cam1 触发 OPEN_RECOVERABLE 后,
    cam2/cam3 的 in-flight 200 asyncio.gather 并发调 record_success —— 不应抹掉
    cam1 打开的断路。修复前:任意一个 success 都走 _transition_to_closed_locked。"""
    import asyncio as _asyncio

    for _ in range(3):
        await cb.record_failure(_rec())
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE

    await _asyncio.gather(
        cb.record_success(),
        cb.record_success(),
        cb.record_success(),
        cb.record_success(),
    )
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE


# ─── record_probe_result 非 CONFIG 失败不应覆盖 OPEN_CONFIG (review #3) ─────


async def test_probe_result_recoverable_fail_does_not_overwrite_open_config(
    cb, frozen_time
):
    """review #3 回归:try_arm_probe 通过后到 record_probe_result 之间的 await 窗口
    里,并发 record_failure(CONFIG) 可能把 state 推到 OPEN_CONFIG(真 auth failure)。
    此时 probe 失败(recoverable)不该无条件覆盖成 OPEN_RECOVERABLE —— 那会把"等改配置"
    降级到"自动退避重试",tick 继续探测注定失败的 key,浪费 backoff 周期。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    frozen_time.tick(1.5)
    cb.try_arm_probe()  # 占 in-flight 位

    # 并发窗口:probe await 期间外部有 CONFIG 错累积到阈值,推到 OPEN_CONFIG
    for _ in range(3):
        await cb.record_failure(_cfg("bad_key"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG

    # probe 完成,回来的是 recoverable 失败(比如 timeout)—— 不该覆盖 OPEN_CONFIG
    await cb.record_probe_result(False, _rec("timeout"))
    assert cb.state_for_test() == CircuitState.OPEN_CONFIG
    # code 也保持 CONFIG 侧的,不被 recoverable 的覆盖
    assert cb.snapshot().code == "bad_key"


async def test_probe_result_recoverable_fail_still_reopens_from_half_open(
    cb, frozen_time
):
    """守卫仅针对 OPEN_CONFIG。HALF_OPEN(mark_half_open 后 record_probe_result 前)
    下 probe 失败仍要正常回 OPEN_RECOVERABLE,不受守卫影响。"""
    for _ in range(3):
        await cb.record_failure(_rec())
    frozen_time.tick(1.5)
    await cb.mark_half_open()
    assert cb.state_for_test() == CircuitState.HALF_OPEN

    await cb.record_probe_result(False, _rec("unreachable"))
    assert cb.state_for_test() == CircuitState.OPEN_RECOVERABLE
    assert cb.snapshot().code == "unreachable"
