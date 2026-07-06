# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""感知「休息」开关持久化 —— 覆盖三处：持久化往返 + 开机门控 + 重新授权门控。

背景：用户在 Web 上「让它休息」（暂停感知）后，后台一旦重启，感知又被无条件拉起、
继续烧云端多模态 token。修复把「用户是否要开启感知」这个意图落盘（缺省=开），所有
系统自动拉起处启动前先查它。本文件锁住三条关键行为：

1. **持久化往返**：写 false 后新建 KVRepo(重读同一库 = 模拟重启)仍为 false；缺省=开。
2. **开机门控**：init_perception_module 在 flag=false 时**不** await PerceptionRunner.start，
   缺省/true 时**已** await。
3. **重新授权门控**：_restart_perception_engine 在 flag=false 时 stop 后**不** start，true 时 start。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeKV:
    """最小 KV 桩：只实现 is_perception_enabled 需要的 get()，raw 直控门控取值。"""

    def __init__(self, raw: str | None):
        self._raw = raw

    def get(self, key: str, default_value: str | None = None) -> str | None:
        return self._raw


# ─── 1. 持久化往返（真 SQLite，模拟重启）────────────────────────────────────


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """Each test case gets a fresh SQLite DB.（复用 test_perception_repo_sqlite 的做法）"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()

    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    yield db_file

    reset_settings()


def test_persist_roundtrip_survives_restart(real_db):
    """写入的暂停意图跨 KVRepo 实例（= 跨进程重启）保持；key 缺省时默认开启。"""
    from miloco.database.kv_repo import KVRepo
    from miloco.perception.engine_state import (
        is_perception_enabled,
        set_perception_enabled,
    )

    # 从未设置 → 默认开启（老部署 / 新装维持既有行为）
    assert is_perception_enabled(KVRepo()) is True

    # 用户「让它休息」→ 落盘 false
    set_perception_enabled(KVRepo(), False)
    # 新建 KVRepo 重读同一临时库（缓存不复用）= 模拟后台重启后仍为 false
    assert is_perception_enabled(KVRepo()) is False

    # 用户「唤醒」→ 落盘 true，重启后仍为 true
    set_perception_enabled(KVRepo(), True)
    assert is_perception_enabled(KVRepo()) is True


# ─── 2. 开机门控（init_perception_module）───────────────────────────────────


@contextmanager
def _patched_init_deps():
    """patch init_perception_module 的重构造依赖，只留门控 + PerceptionRunner.start 可观测。

    模块级依赖在 miloco.perception 命名空间；PerceptionRunner / PerceptionService 是函数内
    import，patch 其原模块路径。start 设 AsyncMock 以便 assert_awaited / assert_not_awaited。
    """
    runner_cls = MagicMock()
    runner_cls.return_value.start = AsyncMock()
    with patch("miloco.perception.PerceptionLogRepo"), patch(
        "miloco.perception.PerceptionEngineProxy"
    ), patch("miloco.perception.CameraDeviceAdapter"), patch(
        "miloco.perception.MultimodalCollector"
    ), patch("miloco.perception.PipelineProcessor"), patch(
        "miloco.perception.runner.PerceptionRunner", runner_cls
    ), patch("miloco.perception.service.PerceptionService"):
        yield runner_cls


async def test_boot_gate_skips_start_when_paused():
    """flag=false：开机不 await PerceptionRunner.start（尊重上次休息意图）。"""
    from miloco.perception import init_perception_module

    with _patched_init_deps() as runner_cls:
        await init_perception_module(MagicMock(), _FakeKV(json.dumps(False)))

    runner_cls.return_value.start.assert_not_awaited()


async def test_boot_gate_starts_when_enabled():
    """flag=true：开机 await 一次 PerceptionRunner.start。"""
    from miloco.perception import init_perception_module

    with _patched_init_deps() as runner_cls:
        await init_perception_module(MagicMock(), _FakeKV(json.dumps(True)))

    runner_cls.return_value.start.assert_awaited_once()


async def test_boot_gate_starts_by_default_when_unset():
    """key 缺省（从未暂停）：开机照常 await start，老部署行为不变。"""
    from miloco.perception import init_perception_module

    with _patched_init_deps() as runner_cls:
        await init_perception_module(MagicMock(), _FakeKV(None))

    runner_cls.return_value.start.assert_awaited_once()


# ─── 3. 重新授权门控（MiotService._restart_perception_engine）────────────────


def _make_service(kv_raw: str | None):
    """绕过 __init__ 构造 MiotService，只挂 _restart_perception_engine 用到的 _kv_repo（经 _miot_proxy）。"""
    from miloco.miot.service import MiotService

    svc = MiotService.__new__(MiotService)
    svc._miot_proxy = MagicMock()
    svc._miot_proxy._kv_repo = _FakeKV(kv_raw)
    return svc


def _mock_perception_service():
    ps = MagicMock()
    ps.start_engine = AsyncMock()
    ps.stop_engine = AsyncMock()
    return ps


async def test_reauth_gate_skips_start_when_paused():
    """flag=false：重新授权 stop 后不 start（stop 必被 await，证明是门控挡下而非异常早退）。"""
    svc = _make_service(json.dumps(False))
    ps = _mock_perception_service()
    fake_manager = MagicMock()
    fake_manager.perception_service = ps

    with patch("miloco.manager.get_manager", return_value=fake_manager):
        await svc._restart_perception_engine()

    ps.stop_engine.assert_awaited_once()
    ps.start_engine.assert_not_awaited()


async def test_reauth_gate_starts_when_enabled():
    """flag=true：重新授权 stop 后照常 start。"""
    svc = _make_service(json.dumps(True))
    ps = _mock_perception_service()
    fake_manager = MagicMock()
    fake_manager.perception_service = ps

    with patch("miloco.manager.get_manager", return_value=fake_manager):
        await svc._restart_perception_engine()

    ps.stop_engine.assert_awaited_once()
    ps.start_engine.assert_awaited_once()


# ─── 4. 写路径（HTTP 端点确实落盘意图）──────────────────────────────────────
#
# 上面三组只测「读」侧（助手往返 + 两处门控喂入 _FakeKV）。写侧——即唯一真正落盘意图的
# 两个用户端点——若被重构写反布尔或漏掉落盘调用，本修复要治的 bug 就会静默复现，而上面
# 的测试照样全绿。这里直接驱动路由函数、用新建 KVRepo 重读磁盘，锁死「停=落 false / 起=落 true」。


def _patched_router_manager(kv):
    """patch router 模块级 manager：kv_repo 用真库，perception_service 启停用 AsyncMock。"""
    fake_manager = MagicMock()
    fake_manager.kv_repo = kv
    fake_manager.perception_service.start_engine = AsyncMock()
    fake_manager.perception_service.stop_engine = AsyncMock()
    return fake_manager


async def test_stop_endpoint_persists_paused_intent(real_db):
    """/engine/stop：落盘 false（新建 KVRepo 重读磁盘确认，防写反 / 漏写）。"""
    from miloco.database.kv_repo import KVRepo
    from miloco.perception import router as perception_router
    from miloco.perception.engine_state import is_perception_enabled

    fm = _patched_router_manager(KVRepo())
    with patch.object(perception_router, "manager", fm):
        await perception_router.stop_engine()

    fm.perception_service.stop_engine.assert_awaited_once()
    assert is_perception_enabled(KVRepo()) is False


async def test_start_endpoint_persists_enabled_intent(real_db):
    """/engine/start：把已暂停(false)翻回 true（证明确实由端点写入，而非默认值）。"""
    from miloco.database.kv_repo import KVRepo
    from miloco.perception import router as perception_router
    from miloco.perception.engine_state import (
        is_perception_enabled,
        set_perception_enabled,
    )

    # 先落 false 模拟「已休息」，再验唤醒端点确实翻回 true
    set_perception_enabled(KVRepo(), False)
    fm = _patched_router_manager(KVRepo())
    with patch.object(perception_router, "manager", fm):
        await perception_router.start_engine()

    fm.perception_service.start_engine.assert_awaited_once()
    assert is_perception_enabled(KVRepo()) is True
