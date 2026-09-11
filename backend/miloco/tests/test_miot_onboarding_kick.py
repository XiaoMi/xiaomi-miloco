# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""authorize_with_code → onboarding 主动邀请 kick 的接线测试。

守两件事：① 授权成功路径在 ``list_homes``（自动选家）**之后**调用
``_kick_onboarding_trigger``——kick 吞异常，删掉这行调用不会有任何测试变红，
故显式断言接线存在与顺序；② kick 本身把 ``onboarding_trigger.maybe_trigger``
调度成后台 task 真正执行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.service import MiotService


def _make_service() -> MiotService:
    """最小 stub proxy（LRUStore 构造只存引用，不打 DB）。"""
    proxy = SimpleNamespace(
        # 真实代理有这个判据，夹具也要有：缺了它，生产侧任何「先问一句能不能用」
        # 的检查都会在这里撞 AttributeError，而那不是生产缺陷、是夹具没建模。
        is_operational=True,
        # 真实代理另有一对**不触发刷新**的取数口（降级态下写台账走它们，避免为一行
        # 留痕去打一趟注定 401 的云端）。夹具让它们与上面那两个刷新口返回同一份，
        # 缺了它们，被拒那条路会在这里撞 AttributeError。
        cached_devices={},
        get_cached_camera=lambda did: None,
        _kv_repo=SimpleNamespace(
            db_connector=SimpleNamespace(),
            delete=lambda key: True,
            # authorize_with_code 会先读旧账号 uid 决定要不要清 scope；
            # 本测试只关心调用顺序，读不到 uid 走「按换账号处理」即可。
            get=lambda key, default=None: None,
        ),
        get_miot_auth_info=AsyncMock(),
        refresh_cameras=AsyncMock(),
        refresh_devices=AsyncMock(),
        refresh_scenes=AsyncMock(),
    )
    return MiotService(miot_proxy=proxy)


@pytest.mark.asyncio
async def test_authorize_with_code_kicks_onboarding_after_home_select(monkeypatch):
    svc = _make_service()
    order: list[str] = []

    # 实例属性遮蔽绑定方法，逐步记录调用顺序。
    svc._clear_account_scope_state = lambda: order.append("clear_scope")

    async def fake_ensure_home_selected():
        order.append("ensure_home_selected")
        return [], True

    svc._ensure_home_selected = fake_ensure_home_selected
    svc._sync_camera_adapter = AsyncMock()
    svc._restart_perception_engine = AsyncMock()
    svc._kick_onboarding_trigger = lambda: order.append("kick_onboarding")

    await svc.authorize_with_code("code123", "state456")

    assert "kick_onboarding" in order, "授权成功路径必须触发 onboarding kick"
    # 建立启用集先于 kick，maybe_trigger 才能看到非空启用集。
    assert order.index("ensure_home_selected") < order.index("kick_onboarding")


@pytest.mark.asyncio
async def test_kick_schedules_maybe_trigger(monkeypatch):
    """kick 把 maybe_trigger 调度成后台 task 并真正执行（fire-and-forget）。"""
    import miloco.manager as manager_mod

    maybe_trigger = AsyncMock(return_value=True)
    monkeypatch.setattr(
        manager_mod,
        "get_manager",
        lambda: SimpleNamespace(onboarding_trigger=SimpleNamespace(maybe_trigger=maybe_trigger)),
    )

    svc = _make_service()
    svc._kick_onboarding_trigger()
    # 让 fire-and-forget task 跑完
    for _ in range(5):
        await asyncio.sleep(0)

    maybe_trigger.assert_awaited_once()
