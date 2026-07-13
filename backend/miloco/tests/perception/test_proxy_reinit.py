# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for hot-reinit of the perception engine without a process restart.

出厂态(启动时缺 key / 模型没下完 / 临时创建失败)在用户补完前置条件后,应通过
每个推理 tick 的 ``PipelineProcessor.try_reinit_engine`` → ``PerceptionEngineProxy
.try_reinit`` 自愈,无需 service restart。这两条新公开方法承担状态机翻转 + 副作用
(重挂 tier_c frame provider)挂接,以下用例覆盖:

- 三种 dormant 可恢复态(no_omni_api_key / models_missing / engine_init_failed)
  reinit 成功后翻到 ready + lifecycle READY
- 已 ready 时是 no-op,不重跑 _init_engine、不碰已有引擎实例
- _status_message 在成功路径被清空
- 失败回退(key 仍空 / _create_engine 抛异常)状态正确
- reinit 成功必须重挂 tier_c frame provider(__init__ 时 engine 为 None,当时
  set_tierc_frame_provider 是静默 no-op,是这条契约唯一的回归门);no-op 时不重挂
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.node_monitor import Lifecycle, NodeName
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.engine.resource_validator import (
    EngineReadiness,
    ValidationResult,
)
from miloco.perception.processor import PipelineProcessor


def _make_proxy(status: str, *, engine=None, message: str = "stale") -> PerceptionEngineProxy:
    """Build a proxy bypassing __init__ (which would run _init_engine)."""
    p = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    p.perception_engine = engine
    p._status = status
    p._status_message = message
    p._last_captions = {}
    p._inference_worker = None
    p._engine_lock = asyncio.Lock()
    return p


def _ready() -> ValidationResult:
    return ValidationResult(status=EngineReadiness.READY)


def _not_configured() -> ValidationResult:
    return ValidationResult(
        status=EngineReadiness.NOT_CONFIGURED, message="Omni API Key 未配置"
    )


def _models_missing() -> ValidationResult:
    return ValidationResult(
        status=EngineReadiness.MODELS_MISSING, message="模型文件缺失"
    )


@contextmanager
def _patched(validation: ValidationResult):
    """Patch every _init_engine dependency except _create_engine (set per-test).

    validate_resources is imported inside _init_engine via `from ... import`,
    so patching the module attribute takes effect on each call.
    """
    settings = MagicMock()
    settings.perception.engine = {}
    mon = MagicMock()
    with patch("miloco.perception.client.get_settings", return_value=settings), patch(
        "miloco.perception.client.get_monitor", return_value=mon
    ), patch(
        "miloco.perception.client.resolve_omni_api_key",
        side_effect=lambda k="": k or "stub-key",
    ), patch(
        "miloco.perception.engine.resource_validator.validate_resources",
        return_value=validation,
    ):
        yield mon


def _lifecycle_calls(mon: MagicMock, life: Lifecycle) -> list:
    return [
        c
        for c in mon.set_lifecycle.call_args_list
        if c.args[:2] == (NodeName.ENGINE, life)
    ]


# ── proxy: 三种可恢复态 → ready ────────────────────────────────────────

def test_try_reinit_promotes_no_key_to_ready():
    proxy = _make_proxy("no_omni_api_key")
    fake_engine = MagicMock()
    proxy._create_engine = MagicMock(return_value=fake_engine)

    with _patched(_ready()) as mon:
        assert proxy.try_reinit() is True

    assert proxy.status == "ready"
    assert proxy.perception_engine is fake_engine
    # 成功路径清掉上一轮 "未配置" 残留消息
    assert proxy.status_message == ""
    # lifecycle 翻到 READY;且成功路径确经 STARTING(回归门:STARTING 行被误删则红)
    assert _lifecycle_calls(mon, Lifecycle.READY)
    assert _lifecycle_calls(mon, Lifecycle.STARTING)


def test_try_reinit_promotes_models_missing_to_ready():
    """守卫放宽后 models_missing 也可恢复(补完模型文件即 ready)。"""
    proxy = _make_proxy("models_missing")
    proxy._create_engine = MagicMock(return_value=MagicMock())

    with _patched(_ready()):
        assert proxy.try_reinit() is True

    assert proxy.status == "ready"


def test_try_reinit_skips_engine_init_failed_in_tick_path():
    """tick-driven 默认路径不恢复 engine_init_failed:重型 _create_engine 不该每 tick
    重试(会阻塞 event loop)。守卫直接拒绝,不进 _init_engine。"""
    proxy = _make_proxy("engine_init_failed")
    proxy._init_engine = MagicMock()  # spy:不应被调用

    assert proxy.try_reinit() is False
    proxy._init_engine.assert_not_called()
    assert proxy.status == "engine_init_failed"


def test_try_reinit_recovers_engine_init_failed_with_include_failed():
    """显式重启(include_failed=True,经 runner.start)恢复 engine_init_failed:
    临时故障(如磁盘满)补救后靠「重启感知」按钮重建一次。"""
    proxy = _make_proxy("engine_init_failed")
    proxy._create_engine = MagicMock(return_value=MagicMock())

    with _patched(_ready()):
        assert proxy.try_reinit(include_failed=True) is True

    assert proxy.status == "ready"


# ── proxy: ready 时 no-op ──────────────────────────────────────────────

def test_try_reinit_noop_when_already_ready():
    """已 ready:守卫直接拒绝,不重跑 _init_engine、不替换已有引擎实例。"""
    existing = MagicMock()
    proxy = _make_proxy("ready", engine=existing, message="")
    proxy._init_engine = MagicMock()  # spy:不应被调用

    assert proxy.try_reinit() is False
    proxy._init_engine.assert_not_called()
    assert proxy.perception_engine is existing
    assert proxy.status == "ready"


# ── proxy: 失败回退路径 ────────────────────────────────────────────────

def test_try_reinit_key_still_missing_stays_dormant_without_lifecycle_churn():
    """补 key 前每 tick reinit:validate 仍 NOT_CONFIGURED → 返 False,停在
    no_omni_api_key。P2 回归门:STARTING 已挪到 validate 通过后,等外部条件态不再产生
    STARTING lifecycle 翻转 → set_lifecycle 对同态(PREREQ_MISSING)不 emit → 每 tick
    零 event_log 噪声。"""
    proxy = _make_proxy("no_omni_api_key")
    proxy._create_engine = MagicMock()  # 不应被调用(validate 没过)

    with _patched(_not_configured()) as mon:
        assert proxy.try_reinit() is False

    assert proxy.status == "no_omni_api_key"
    proxy._create_engine.assert_not_called()
    # 等外部条件态零 lifecycle churn:未进 STARTING(挪 STARTING 的回归门)
    assert not _lifecycle_calls(mon, Lifecycle.STARTING)


def test_try_reinit_models_still_missing_stays_dormant_without_lifecycle_churn():
    """补模型前每 tick reinit:validate 仍 MODELS_MISSING → 返 False,停在
    models_missing(对称于 key-still-missing:守卫放行 models_missing 但 validate 没过
    时不构造、不进 STARTING,零 lifecycle churn)。"""
    proxy = _make_proxy("models_missing")
    proxy._create_engine = MagicMock()  # 不应被调用(validate 没过)

    with _patched(_models_missing()) as mon:
        assert proxy.try_reinit() is False

    assert proxy.status == "models_missing"
    proxy._create_engine.assert_not_called()
    assert not _lifecycle_calls(mon, Lifecycle.STARTING)


def test_try_reinit_create_raises_marks_failed():
    """validate 过但 _create_engine 抛异常(如临时磁盘满)→ engine_init_failed,返 False。"""
    proxy = _make_proxy("no_omni_api_key")
    proxy._create_engine = MagicMock(side_effect=RuntimeError("disk full"))

    with _patched(_ready()):
        assert proxy.try_reinit() is False

    assert proxy.status == "engine_init_failed"
    assert "disk full" in proxy.status_message


# ── processor: 重挂 tier_c frame provider 契约 ─────────────────────────

def test_pipeline_try_reinit_reattaches_tierc_provider():
    """reinit 成功必须重挂 tier_c provider:__init__ 时 engine 为 None、
    set_tierc_frame_provider 是静默 no-op,不重挂则 gate 关停时 live 检测取帧会丢。"""
    proxy = MagicMock()
    proxy.try_reinit.return_value = True
    collector = MagicMock()

    proc = PipelineProcessor.__new__(PipelineProcessor)
    proc._perception_engine_proxy = proxy
    proc._collector = collector

    proc.try_reinit_engine()

    proxy.set_tierc_frame_provider.assert_called_once_with(collector.peek_latest_frame)


def test_pipeline_try_reinit_noop_skips_reattach():
    """非可恢复态(reinit 返 False)→ 不重挂 provider,避免每 tick 冗余挂接。"""
    proxy = MagicMock()
    proxy.try_reinit.return_value = False
    collector = MagicMock()

    proc = PipelineProcessor.__new__(PipelineProcessor)
    proc._perception_engine_proxy = proxy
    proc._collector = collector

    proc.try_reinit_engine()

    proxy.set_tierc_frame_provider.assert_not_called()


def test_pipeline_try_reinit_forwards_include_failed():
    """runner.start 显式重启传 include_failed=True,透传给 proxy.try_reinit
    (使 engine_init_failed 仅在显式重启、而非每 tick 恢复)。"""
    proxy = MagicMock()
    proxy.try_reinit.return_value = True
    collector = MagicMock()

    proc = PipelineProcessor.__new__(PipelineProcessor)
    proc._perception_engine_proxy = proxy
    proc._collector = collector

    proc.try_reinit_engine(include_failed=True)

    proxy.try_reinit.assert_called_once_with(include_failed=True)


# ── proxy: rebuild 强制重建(改感知参数后生效) ─────────────────────────

@pytest.mark.asyncio
async def test_rebuild_forces_recreate_even_when_ready():
    """rebuild 与 try_reinit 相反:ready 态也强制 close 旧实例 + 重跑 _init_engine
    (读新 config 的 omni_fps)。否则「应用设置」改 omni_fps 不生效——ready 时
    try_reinit 是 no-op,旧引擎带旧配置原样恢复。"""
    old_engine = MagicMock()
    old_engine.close = AsyncMock()
    proxy = _make_proxy("ready", engine=old_engine, message="")

    def _fake_init():
        # 模拟 _init_engine 用新 config 重建出一个新实例
        proxy.perception_engine = MagicMock(name="new_engine")
        proxy._status = "ready"

    proxy._init_engine = MagicMock(side_effect=_fake_init)

    with patch("miloco.perception.client.get_monitor", return_value=MagicMock()):
        await proxy.rebuild()

    # 旧实例被关闭,_init_engine 被调用重建
    old_engine.close.assert_awaited_once()
    proxy._init_engine.assert_called_once()
    # 引擎实例已被替换(不再是旧的)
    assert proxy.perception_engine is not old_engine
    assert proxy.status == "ready"


@pytest.mark.asyncio
async def test_rebuild_no_prior_engine_skips_close():
    """无引擎实例(未配 key/模型的降级态)时 rebuild 只 _init_engine,不 close。"""
    proxy = _make_proxy("no_omni_api_key", engine=None)
    proxy._init_engine = MagicMock()

    with patch("miloco.perception.client.get_monitor", return_value=MagicMock()):
        await proxy.rebuild()

    proxy._init_engine.assert_called_once()
