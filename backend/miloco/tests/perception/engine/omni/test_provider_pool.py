"""ProviderPool 故障转移 + 恢复探测 单元测试。"""

from __future__ import annotations

import asyncio

import pytest
from miloco.config.settings import OmniModelSettings
from miloco.perception.engine.omni.circuit_breaker import (
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.provider_pool import (
    OmniProviderPool,
    _provider_key,
    get_pool,
    init_pool,
    reset_pool_for_tests,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _omni(
    *,
    label: str = "default",
    model: str = "test-model",
    base_url: str = "https://test.local/v1",
    api_key: str = "sk-test",
) -> OmniModelSettings:
    return OmniModelSettings(label=label, model=model, base_url=base_url, api_key=api_key)


def _mock_settings(primary: OmniModelSettings, fallback_labels: list[str], profiles: list[OmniModelSettings], monkeypatch):
    """注入 mock get_settings，返回指定的 primary + fallback labels + profiles。"""

    class _M:
        omni = primary
        omni_fallbacks = fallback_labels
        omni_profiles = profiles

    class _S:
        model = _M()

    # patch 到 miloco.config（provider_pool 内部延迟 import 从此路径获取）
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _S(),
        raising=True,
    )


def _build_pool(loop: asyncio.AbstractEventLoop) -> OmniProviderPool:
    """创建一个测试用 Pool，时间常量设为 0 以便同步验证。"""
    return OmniProviderPool(
        loop,
        min_switch_interval_sec=0.0,
        recovery_probe_interval_sec=0.0,
    )


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset():
    """每个测试前后重置 CB 与 Pool 单例。"""
    reset_omni_circuit_breaker_for_tests()
    reset_pool_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()
    reset_pool_for_tests()


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


# ── test: 空 fallback 始终返回 primary ───────────────────────────────────────


def test_no_fallback_stays_on_primary(loop, monkeypatch):
    """omni_fallbacks 为空时，get_active() 始终返回 primary。"""
    primary = _omni(label="p", model="primary-model")
    _mock_settings(primary, [], [], monkeypatch)

    pool = _build_pool(loop)
    active = pool.get_active()
    assert active.model == "primary-model"
    assert active.label == "p"


# ── test: failover 到第一个健康备选 ───────────────────────────────────────────


async def test_failover_to_first_healthy(loop, monkeypatch):
    """主 failed 后，_try_failover 切到第一个有 key 且未 failed 的备选。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = _build_pool(loop)

    # 先让 CB 进入 error 状态，模拟主 provider 熔断
    cb = get_omni_circuit_breaker()
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("bad_key", "m", ErrorCategory.CONFIG)
        )
    assert cb.snapshot().state == "error"

    # 触发 failover
    ok = await pool._try_failover()
    assert ok
    active = pool.get_active()
    assert active.model == "fb-a-model"
    assert active.label == "a"


# ── test: 跳过已 failed 和无 key 的备选 ──────────────────────────────────────


async def test_failover_skips_failed_and_keyless(loop, monkeypatch):
    """跳过已 failed 的备选和 api_key 为空的备选。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")  # 有 key
    fb_b = _omni(label="b", model="fb-b-model", api_key="")  # 无 key
    fb_c = _omni(label="c", model="fb-c-model")  # 有 key
    _mock_settings(primary, ["a", "b", "c"], [fb_a, fb_b, fb_c], monkeypatch)

    pool = _build_pool(loop)

    # 手动标记 fb_a 为 failed（模拟它已被用过且 failed）
    pool._failed_keys.add(_provider_key(fb_a))

    # 让 CB 进入 error 状态
    cb = get_omni_circuit_breaker()
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("bad_key", "m", ErrorCategory.CONFIG)
        )

    ok = await pool._try_failover()
    assert ok
    active = pool.get_active()
    # 应跳过 a（已 failed）和 b（无 key），选中 c
    assert active.model == "fb-c-model"
    assert active.label == "c"


# ── test: 池耗尽维持暂停 ──────────────────────────────────────────────────


async def test_pool_exhausted_stays_paused(loop, monkeypatch):
    """所有备选都 failed → _try_failover 返回 False。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = _build_pool(loop)

    # 所有备选都已 failed
    pool._failed_keys.add(_provider_key(fb_a))
    pool._failed_keys.add(_provider_key(fb_b))

    cb = get_omni_circuit_breaker()
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )
    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("bad_key", "m", ErrorCategory.CONFIG)
        )

    ok = await pool._try_failover()
    assert not ok
    # active 应停留在 primary（_get_active_unlocked 在主 _active_label is None 时返回 primary）
    active = pool.get_active()
    assert active.model == "primary-model"


# ── test: 去抖跳过快速连续切换 ───────────────────────────────────────────────


async def test_debounce_skips_rapid_switch(loop, monkeypatch):
    """距上次切换不足 min_switch_interval 时跳过 failover；超过间隔后允许再次切换。

    关键点：必须配置 >=2 个健康备选。若只有单个备选，第二次 failover 会因池耗尽
    返回 False，与去抖返回的 False 无法区分——原测试是假阳性。本测试用 a/b 两个
    备选，分别验证「立即调用被去抖拦截」与「模拟间隔足够后再调用成功切到 b」。
    """
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = OmniProviderPool(loop, min_switch_interval_sec=30.0, recovery_probe_interval_sec=0.0)

    cb = get_omni_circuit_breaker()
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    async def _trip_cb():
        for _ in range(3):
            await cb.record_failure(
                ClassifiedError("bad_key", "m", ErrorCategory.CONFIG)
            )

    # 第一次切换：primary failed → 切到 a，CB 被 reset 回 ok
    await _trip_cb()
    assert await pool._try_failover()
    assert pool.get_active().label == "a"
    assert cb.snapshot().state == "ok"

    # 重新让 CB 进入 error（模拟 a 也挂了）
    await _trip_cb()
    assert cb.snapshot().state == "error"

    # 立即再触发：应被去抖拦截（CB 非 ok + 距上次不足 30s），停留在 a
    assert not await pool._try_failover()
    assert pool.get_active().label == "a"  # 没有被切走
    assert cb.snapshot().state == "error"  # 确认不是 CB-ok 分支

    # 模拟「30s 已过去」：重置 last_switch 时间戳，去抖不应再拦截
    pool._last_switch_monotonic = 0.0
    await _trip_cb()  # 维持 CB 非 ok
    assert await pool._try_failover()
    assert pool.get_active().label == "b"  # 成功切到下一个健康备选


# ── test: primary 恢复自动切回 ────────────────────────────────────────────────


async def test_probe_recovers_primary_switches_back(loop, monkeypatch):
    """探测 primary 恢复通过 → _switch_back_to_primary 将 _active_label 置为 None。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    _mock_settings(primary, ["a"], [fb_a], monkeypatch)

    pool = _build_pool(loop)

    # 手动设置：当前在备选 fb_a
    pool._active_label = "a"
    pool._failed_keys.add(_provider_key(primary))
    assert pool._active_label == "a"

    # 模拟 primary 恢复：手动调用 _switch_back_to_primary
    await pool._switch_back_to_primary()

    assert pool._active_label is None
    active = pool.get_active()
    assert active.model == "primary-model"


# ── test: fallbacks 被删除后 get_active 回退 primary ──────────────────────────


async def test_active_label_removed_from_fallbacks_falls_back(loop, monkeypatch):
    """当前 active label 已不在 fallback 列表中 → get_active 回退 primary。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = _build_pool(loop)
    pool._active_label = "a"

    assert pool.get_active().model == "fb-a-model"

    # 模拟管理员删除 fb_a：omni_fallbacks 变为 ["b"]
    _mock_settings(primary, ["b"], [fb_b], monkeypatch)

    # get_active 发现 label "a" 不存在 → 回退 primary
    active = pool.get_active()
    assert active.model == "primary-model"
    assert pool._active_label is None  # 已自动纠正


# ── test: 运行时拖拽重排不导致静默跳 provider ─────────────────────────────────


async def test_reorder_keeps_same_active_by_label(loop, monkeypatch):
    """管理员拖拽重排 fallback 顺序后，label-based 追踪保持指向同一 provider。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = _build_pool(loop)
    pool._active_label = "a"
    assert pool.get_active().model == "fb-a-model"

    # 管理员把 B 拖到 A 前面
    _mock_settings(primary, ["b", "a"], [fb_b, fb_a], monkeypatch)

    # label-based 追踪：仍然指向 A，不会静默跳到 B
    active = pool.get_active()
    assert active.model == "fb-a-model"
    assert active.label == "a"


# ── test: snapshot 正确反映运行时状态 ─────────────────────────────────────────


def test_snapshot_reflects_runtime_state(loop, monkeypatch):
    """snapshot() 返回完整运行时状态（所有 PoolSnapshot 字段）。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    fb_b = _omni(label="b", model="fb-b-model")
    _mock_settings(primary, ["a", "b"], [fb_a, fb_b], monkeypatch)

    pool = _build_pool(loop)

    # 初始状态：在 primary
    snap1 = pool.snapshot()
    assert snap1.active_is_primary
    assert snap1.active_index == 0
    assert snap1.active_model == "primary-model"
    assert snap1.active_label == "p"
    assert snap1.fallback_count == 2
    assert snap1.failed_keys == []
    assert snap1.last_switch_at_ms is None
    assert not snap1.recovery_loop_running

    # Failover 到 a 之后，并标记 primary failed
    pool._active_label = "a"
    pool._failed_keys.add(_provider_key(primary))
    pool._last_switch_monotonic = 1234.567
    snap2 = pool.snapshot()
    assert not snap2.active_is_primary
    assert snap2.active_index == 1
    assert snap2.active_model == "fb-a-model"
    assert snap2.failed_keys == [_provider_key(primary)]
    assert snap2.last_switch_at_ms == 1234567  # monotonic * 1000
    assert snap2.fallback_count == 2


# ── test: _resolve_providers 跳过不存在的 label ───────────────────────────────


def test_resolve_skips_missing_label(loop, monkeypatch):
    """omni_fallbacks 中的 label 不在 profiles 中时自动跳过。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    _mock_settings(primary, ["a", "missing-label"], [fb_a], monkeypatch)

    pool = _build_pool(loop)
    _, fallbacks = pool._resolve_providers_unlocked()
    assert len(fallbacks) == 1
    assert fallbacks[0].label == "a"


# ── test: start/stop 生命周期 ──────────────────────────────────────────────────


async def test_start_idempotent(loop, monkeypatch):
    """重复调用 start() 不创建多余后台 task。"""
    primary = _omni(label="p", model="primary-model")
    _mock_settings(primary, [], [], monkeypatch)

    pool = _build_pool(loop)
    await pool.start()
    assert pool._recovery_task is not None
    task1 = pool._recovery_task

    # 重复调用：应 no-op，task 不变
    await pool.start()
    assert pool._recovery_task is task1

    await pool.stop()


async def test_stop_idempotent_and_cleans_up(loop, monkeypatch):
    """stop() 幂等：已停止时 no-op；正常停止后 _recovery_task 置 None。"""
    primary = _omni(label="p", model="primary-model")
    _mock_settings(primary, [], [], monkeypatch)

    pool = _build_pool(loop)
    await pool.start()
    await pool.stop()
    assert pool._recovery_task is None

    # 重复 stop 不应抛异常
    await pool.stop()


# ── test: _probe_failed_providers 完整流程 ────────────────────────────────────


async def test_probe_failed_providers_recovery_flow(loop, monkeypatch):
    """完整探测流程：primary 恢复 → 从 failed 移除 + 自动切回。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    _mock_settings(primary, ["a"], [fb_a], monkeypatch)

    pool = _build_pool(loop)
    # 设置状态：primary failed，当前在备选 fb_a
    pool._active_label = "a"
    pool._failed_keys.add(_provider_key(primary))
    pool._failed_keys.add(_provider_key(fb_a))

    # Mock probe_omni：primary 恢复，fb_a 仍为失败
    async def _mock_probe(model, base_url, api_key):
        key = f"{model}@{base_url}"
        return {"ok": key == _provider_key(primary)}

    monkeypatch.setattr(
        "miloco.perception.engine.omni.probe.probe_omni",
        _mock_probe,
    )

    await pool._probe_failed_providers()

    # primary 从 failed 移除 + 自动切回
    assert _provider_key(primary) not in pool._failed_keys
    # fb_a 探测失败，仍在 failed 中
    assert _provider_key(fb_a) in pool._failed_keys
    assert pool._active_label is None
    assert pool.get_active().model == "primary-model"


async def test_probe_skips_keyless_provider(loop, monkeypatch):
    """无 api_key 的 provider 不探测，直接跳过。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model", api_key="")
    _mock_settings(primary, ["a"], [fb_a], monkeypatch)

    pool = _build_pool(loop)
    pool._failed_keys.add(_provider_key(fb_a))

    probe_calls = []

    async def _track_probe(model, base_url, api_key):
        probe_calls.append((model, base_url, api_key))
        return {"ok": True}

    monkeypatch.setattr(
        "miloco.perception.engine.omni.probe.probe_omni",
        _track_probe,
    )

    await pool._probe_failed_providers()
    # 无 key 的 provider 不会被探测
    assert len(probe_calls) == 0


# ── test: CB-ok 短路 / switch_back 幂等 / init_pool 幂等 ───────────────────────


async def test_try_failover_skips_when_cb_ok(loop, monkeypatch):
    """CB 状态为 ok 时 _try_failover 不执行 failover。"""
    primary = _omni(label="p", model="primary-model")
    fb_a = _omni(label="a", model="fb-a-model")
    _mock_settings(primary, ["a"], [fb_a], monkeypatch)

    pool = _build_pool(loop)

    # CB 默认状态为 ok
    assert get_omni_circuit_breaker().snapshot().state == "ok"

    result = await pool._try_failover()
    assert not result
    # active 保持不变
    active = pool.get_active()
    assert active.model == "primary-model"


async def test_switch_back_to_primary_when_already_primary(loop, monkeypatch):
    """已在 primary 时调用 _switch_back_to_primary 为 no-op。"""
    primary = _omni(label="p", model="primary-model")
    _mock_settings(primary, [], [], monkeypatch)

    pool = _build_pool(loop)
    assert pool._active_label is None

    # 不应抛异常，直接返回
    await pool._switch_back_to_primary()
    assert pool._active_label is None
    assert pool.get_active().model == "primary-model"


def test_init_pool_creates_and_is_idempotent(loop, monkeypatch):
    """init_pool 创建实例；重复调用返回同一实例。"""
    primary = _omni(label="p", model="primary-model")
    _mock_settings(primary, [], [], monkeypatch)

    pool1 = init_pool(
        loop, min_switch_interval_sec=0.0, recovery_probe_interval_sec=0.0
    )
    pool2 = init_pool(loop)

    assert pool1 is pool2  # 幂等：返回同一实例


def test_get_pool_returns_none_when_not_initialized():
    """未初始化时 get_pool 返回 None。"""
    reset_pool_for_tests()
    assert get_pool() is None
