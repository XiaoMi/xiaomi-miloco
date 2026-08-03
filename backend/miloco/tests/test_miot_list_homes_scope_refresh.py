"""list_homes 兜底自动选家后刷新中枢 scope。

P1 修复验证：authorize_with_code 首登/换号时，list_homes 兜底自动选中第一个
家庭，但 SDK 的 _setup_central_hub_async 用的是空 _owned_group_ids —— 若这里
不刷新中枢 scope，mDNS 发现的网关会被跳过，本地控制不生效直到手动切家/重启。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.service import MiotService


class _FakeKV:
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)


def _make_service(kv=None, homes=None):
    """list_homes 测试用最小 stub proxy。"""
    kv = kv or _FakeKV()
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=SimpleNamespace(
                execute_update=lambda *a, **kw: 0,
                execute_query=lambda *a, **kw: [],
            ),
            get=kv.get,
            set=kv.set,
        ),
        miot_client=SimpleNamespace(
            get_homes_async=AsyncMock(return_value=homes or {}),
        ),
        get_devices=AsyncMock(return_value={}),
        get_cameras=AsyncMock(return_value={}),
        refresh_central_hub_scope_async=AsyncMock(),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._schedule_agent_session_reset = lambda: None  # no-op,不建任务
    return svc, proxy, kv


@pytest.mark.asyncio
async def test_list_homes_auto_select_refreshes_central_hub_scope():
    """启用集为空 → list_homes 兜底自动选第一个家庭 → 刷新中枢 scope。"""
    kv = _FakeKV()  # 空启用集
    svc, proxy, kv = _make_service(
        kv=kv,
        homes={
            "H1": SimpleNamespace(home_id="H1", home_name="home-1"),
        },
    )
    await svc.list_homes()

    # 自动选中 H1
    assert json.loads(kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY)) == ["H1"]
    # 中枢 scope 被刷新
    proxy.refresh_central_hub_scope_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_homes_no_auto_select_no_scope_refresh():
    """启用集已有家庭且与可见集有交集 → 不兜底、不刷中枢 scope。"""
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
    })
    svc, proxy, kv = _make_service(
        kv=kv,
        homes={
            "H1": SimpleNamespace(home_id="H1", home_name="home-1"),
        },
    )
    await svc.list_homes()

    # 不自动切换
    assert json.loads(kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY)) == ["H1"]
    # 不刷新中枢 scope（家庭没变）
    proxy.refresh_central_hub_scope_async.assert_not_awaited()
