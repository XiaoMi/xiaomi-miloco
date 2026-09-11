# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""切换账号或家庭时重建状态容器：顺序、串行、以及解绑那条短序列。

不打真机：manager 用最小 stub，StateStore 是真的 —— 断言看容器里最终剩什么。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.service import MiotService
from miloco.state import StateStore


class _FakeKV:
    def __init__(self, data: dict[str, str] | None = None):
        self._data = dict(data or {})
        self.db_connector = SimpleNamespace(
            execute_query=lambda *a, **kw: [],
            execute_update=lambda *a, **kw: 0,
        )

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class _FakeManager:
    """只提供编排要用到的那几样，外加记下发生过的动作顺序。"""

    def __init__(self, store: StateStore, trace: list[str]):
        self.state_store = store
        self.state_align_task: asyncio.Task | None = None
        self._trace = trace
        self._scope = 0
        self._aligned_scope = -1
        self.aligns_started = 0

    def current_scope(self) -> int:
        return self._scope

    def begin_scope_switch(self) -> int:
        self._scope += 1
        self._trace.append("bump")
        return self._scope

    def mark_scope_aligned(self, scope: int) -> None:
        if scope == self._scope:
            self._aligned_scope = scope

    def scope_is_aligned(self) -> bool:
        return self._aligned_scope == self._scope

    def start_state_alignment(self) -> asyncio.Task:
        self.aligns_started += 1
        self._trace.append("align")
        scope = self._scope

        async def _run() -> None:
            await asyncio.sleep(0)
            self.mark_scope_aligned(scope)

        self.state_align_task = asyncio.create_task(_run())
        return self.state_align_task


@pytest.fixture
async def scene():
    """(service, manager, store, trace)。"""
    store = StateStore()
    store.start()
    trace: list[str] = []
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    proxy = SimpleNamespace(
        _kv_repo=kv,
        deinit=AsyncMock(),
        init=AsyncMock(),
        refresh_devices=AsyncMock(return_value=None),
        refresh_cameras=AsyncMock(return_value=None),
        refresh_scenes=AsyncMock(return_value=None),
        get_miot_auth_info=AsyncMock(),
        # 两个家庭：切到 H2 才是真切换，切到 H1（启用集里那个）是幂等点击
        get_devices=AsyncMock(
            return_value={
                "d1": SimpleNamespace(home_id="H1"),
                "d2": SimpleNamespace(home_id="H2"),
            }
        ),
        get_cameras=AsyncMock(return_value={}),
    )
    service = MiotService(miot_proxy=proxy)
    service._sync_camera_adapter = AsyncMock()
    service._restart_perception_engine = AsyncMock()
    service._kick_onboarding_trigger = lambda: None
    # 记下来而不是丢掉：换账号漏了它，旧账号的上下文会串进新账号的 agent 会话
    session_resets: list[int] = []
    service._schedule_agent_session_reset = lambda: session_resets.append(1)
    manager = _FakeManager(store, trace)

    import miloco.manager as manager_module

    original = manager_module.get_manager
    manager_module.get_manager = lambda: manager
    yield SimpleNamespace(
        service=service,
        manager=manager,
        store=store,
        trace=trace,
        proxy=proxy,
        session_resets=session_resets,
    )
    manager_module.get_manager = original
    store.stop()


async def test_a_switch_empties_the_container(scene):
    scene.store.set("iot/device/old/status/online", True, source="iot_align")

    await scene.service._reset_state_scope(AsyncMock())

    assert scene.store.snapshot("iot/device/old/**") == {}


async def test_the_running_alignment_is_cancelled_and_awaited_before_the_clear(scene):
    """cancel 只是投递取消请求；不等的话它可能在 clear 之后才写完最后一笔。"""
    store, trace = scene.store, scene.trace

    async def _slow_align() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            trace.append("old align stopped")
            store.set("iot/device/old/status/online", True, source="iot_align")
            raise

    scene.manager.state_align_task = asyncio.create_task(_slow_align())
    await asyncio.sleep(0)

    await scene.service._reset_state_scope(AsyncMock())

    assert trace.index("old align stopped") < trace.index("align")
    assert scene.store.snapshot("iot/device/old/**") == {}


async def test_two_switches_do_not_interleave(scene):
    """都在一个 loop 上不代表不会交错 —— 编排里每个 await 都是让出点。"""
    trace = scene.trace

    def _refresh(tag: str):
        async def _run() -> None:
            trace.append(f"{tag} start")
            await asyncio.sleep(0)
            trace.append(f"{tag} end")

        return _run

    await asyncio.gather(
        scene.service._reset_state_scope(_refresh("a")),
        scene.service._reset_state_scope(_refresh("b")),
    )

    order = [step for step in trace if step.endswith(("start", "end"))]
    assert order in (
        ["a start", "a end", "b start", "b end"],
        ["b start", "b end", "a start", "a end"],
    ), order


async def test_a_reset_without_realign_still_runs_its_middle_step(scene):
    """解绑之后没有账号：起新对齐只会拿不到设备、白打一轮日志，但重建连接照做。"""
    scene.store.set("iot/device/old/status/online", True, source="iot_align")

    async def _rebuild() -> None:
        scene.trace.append("rebuild")

    await scene.service._reset_state_scope(_rebuild, realign=False)

    assert scene.store.snapshot("iot/device/**") == {}
    assert scene.trace == ["bump", "rebuild"]
    assert scene.manager.aligns_started == 0


async def test_the_new_scope_is_refreshed_before_the_alignment_starts(scene):
    """对齐读的是设备缓存，缓存还没换就对齐等于把旧家庭的设备写进新作用域。"""

    async def _refresh() -> None:
        scene.trace.append("refresh")

    await scene.service._reset_state_scope(_refresh)

    assert scene.trace == ["bump", "refresh", "align"]


# ── 四个切换入口都要真的走编排 ─────────────────────────────────────────


async def _settle(store: StateStore, timeout: float = 1.0) -> None:
    """等后台那次编排跑到清空为止。切家和兜底选家都是 fire-and-forget。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if store.snapshot("iot/**") == {}:
            return
        await asyncio.sleep(0.01)


async def test_authorizing_a_new_account_rebuilds_the_container(scene):
    scene.store.set("iot/device/old_account/status/online", True, source="iot_align")

    await scene.service.authorize_with_code(code="c", state="s")

    assert scene.store.snapshot("iot/device/old_account/**") == {}
    assert scene.manager.aligns_started == 1


async def test_unbinding_empties_the_container_and_starts_no_alignment(scene):
    scene.store.set("iot/device/old_account/status/online", True, source="iot_align")

    await scene.service.unbind_miot()

    assert scene.store.snapshot("iot/**") == {}
    assert scene.manager.aligns_started == 0


async def test_switching_home_rebuilds_the_container(scene):
    scene.store.set("iot/device/old_home/status/online", True, source="iot_align")

    await scene.service.switch_home("H2")
    await _settle(scene.store)

    assert scene.store.snapshot("iot/device/old_home/**") == {}
    assert scene.manager.aligns_started == 1


async def test_switching_to_the_current_home_leaves_the_container_alone(scene):
    """点当前正启用的那个家是幂等操作，容器不该为此清空重建。

    那几秒里 `scope_is_aligned()` 为假 —— 属性推送全被对齐闸丢掉、补拉整个跳过、
    快照读到 MISSING，而语义上什么都没换。会话重置那边早就是「变了才重置」。
    """
    scene.store.set("iot/device/mine/status/online", True, source="iot_align")

    await scene.service.switch_home("H1")
    await _settle(scene.store)

    assert scene.store.get("iot/device/mine/status/online") is True
    assert scene.manager.aligns_started == 0
    assert scene.session_resets == []
    # 刷新不在此列：它幂等，且前端拿这一下当强制刷新（main 上就有用例钉住）
    assert scene.proxy.refresh_cameras.await_count == 1


async def test_the_fallback_home_selection_rebuilds_the_container(scene):
    """启用集失效时 list_homes 会自动选家 —— 那和用户显式切家一样换掉了作用域。"""
    scene.proxy._kv_repo._data[ScopeConfigKeys.HOME_WHITE_LIST_KEY] = json.dumps(
        ["gone"]
    )
    scene.store.set("iot/device/old_home/status/online", True, source="iot_align")

    await scene.service.list_homes()
    await _settle(scene.store)

    assert scene.store.snapshot("iot/device/old_home/**") == {}
    assert scene.manager.aligns_started == 1


async def test_listing_homes_without_a_change_leaves_the_container_alone(scene):
    """启用集没变就不是切换 —— 每次刷页面都清空重建，容器会长期是空的。"""
    scene.store.set("iot/device/mine/status/online", True, source="iot_align")

    await scene.service.list_homes()
    await asyncio.sleep(0.05)

    assert scene.store.get("iot/device/mine/status/online") is True
    assert scene.manager.aligns_started == 0


async def test_authorizing_a_new_account_resets_the_agent_session(scene):
    """换账号比换家庭更该清：旧账号的设备 / 房间 / 习惯不清会串进新账号。

    main 上这件事搭在 list_homes 的兜底选家分支里，授权路径改调 _ensure_home_selected
    之后那条路断了 —— 这条用例钉住补回来的那一行。
    """
    await scene.service.authorize_with_code(code="c", state="s")

    assert scene.session_resets == [1]
