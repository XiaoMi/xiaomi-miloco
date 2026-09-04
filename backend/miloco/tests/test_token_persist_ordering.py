"""新令牌必须先落库，再做副作用。

小米的 refresh_token 是一次性的（官方文档：一个 Refresh Token 只能刷新一次
Access Token）。换取成功那一刻旧令牌就在云端作废了，新令牌只存在于内存——此时
若进程死掉，令牌永久丢失，住户只能重新授权。

2026-08-27 的故障正是这样：刷新发出后 1.019 秒进程 SIGSEGV，而落库排在三个副作用
之后，其中一个是跨 FFI 的原生相机调用——段错误连 except 都不会执行。

这里钉住的是**顺序**：落库必须发生在任何副作用之前，且副作用失败不得让已经到手
的令牌丢失。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from miot.client import MIoTClient
from miot.types import MIoTOauthInfo, MIoTUserInfo


def _client_with_spies():
    """造一个只装了本测试关心的部件的 MIoTClient，并记录调用顺序。"""
    c = MIoTClient.__new__(MIoTClient)
    order: list[str] = []

    c._oauth_info = MIoTOauthInfo(
        access_token="old_at",
        refresh_token="old_rt",
        expires_ts=1,
        user_info=MIoTUserInfo(uid="u1", nickname="n", icon="", union_id="x"),
    )

    new_token = MIoTOauthInfo(
        access_token="new_at", refresh_token="new_rt", expires_ts=9999999999
    )

    async def _exchange(_rt):
        order.append("exchange")
        return new_token.model_copy(deep=True)

    c._oauth_client = MagicMock()
    c._oauth_client.refresh_access_token_async = _exchange

    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: order.append("http_header")

    async def _cam(**kw):
        order.append("camera_native")

    c._camera_client = MagicMock()
    c._camera_client.update_access_token_async = _cam

    async def _mips(_t):
        order.append("mips")

    c._mips_cloud = MagicMock()
    c._mips_cloud.update_access_token = _mips

    return c, order


@pytest.mark.asyncio
async def test_persist_happens_before_every_side_effect():
    """落库必须紧跟换取，排在三个副作用之前。"""
    c, order = _client_with_spies()
    saved: list[str] = []

    def persist(info: MIoTOauthInfo) -> None:
        order.append("persist")
        saved.append(info.refresh_token)

    await c.refresh_access_token_async("old_rt", persist=persist)

    assert order[0] == "exchange"
    assert order[1] == "persist", f"落库必须紧跟换取，实际顺序: {order}"
    assert saved == ["new_rt"], "落库拿到的必须是新令牌"
    # 三个副作用都在落库之后
    for effect in ("http_header", "camera_native", "mips"):
        assert order.index(effect) > order.index("persist"), f"{effect} 排在了落库之前"


@pytest.mark.asyncio
async def test_native_camera_crash_does_not_lose_the_token():
    """相机原生调用抛错时，新令牌已经落库，且刷新整体仍算成功。

    这是 8-27 那条链上最可能的引信：它跨 FFI 进原生库，是刷新路径上唯一能
    产生段错误的一步。Python 层能抛的异常至少要先兜住。
    """
    c, order = _client_with_spies()
    saved: list[str] = []

    async def _boom(**kw):
        order.append("camera_native")
        raise RuntimeError("native camera blew up")

    c._camera_client.update_access_token_async = _boom

    result = await c.refresh_access_token_async(
        "old_rt", persist=lambda i: saved.append(i.refresh_token)
    )

    assert saved == ["new_rt"], "相机炸了也不能丢令牌"
    assert result.refresh_token == "new_rt"
    assert "mips" in order, "一项副作用失败不应阻断其余项"


@pytest.mark.asyncio
async def test_camera_client_none_does_not_break_refresh():
    """相机 client 为 None 时直接跳过。

    它确实会被置 None，而本文件另外两处早就判了空，唯独刷新路径没判——
    保护不对称本身就是遗漏的证据。
    """
    c, order = _client_with_spies()
    c._camera_client = None
    saved: list[str] = []

    result = await c.refresh_access_token_async(
        "old_rt", persist=lambda i: saved.append(i.refresh_token)
    )

    assert saved == ["new_rt"]
    assert result.refresh_token == "new_rt"
    assert "camera_native" not in order
    assert "mips" in order


@pytest.mark.asyncio
async def test_persist_failure_does_not_abort_refresh():
    """落库回调自己抛错时，刷新不整体失败。

    令牌此刻已在内存里；硬抛会让调用方把「已经拿到新令牌」也一起当成失败，
    反而扩大损失。落库失败由 SDK 记日志，调用方还有第二次落库机会。
    """
    c, order = _client_with_spies()

    def _bad_persist(_i):
        order.append("persist")
        raise RuntimeError("db locked")

    result = await c.refresh_access_token_async("old_rt", persist=_bad_persist)

    assert result.refresh_token == "new_rt"
    assert "mips" in order, "落库失败不应阻断后续副作用"


@pytest.mark.asyncio
async def test_refresh_is_serialized_by_lock():
    """并发刷新必须串行。

    一次性轮换语义下，后到的那条会拿着已被前一条消费掉的令牌去请求，必然被
    云端拒绝——把一次正常刷新变成「授权失效」。
    """
    from miloco.miot.client import MiotProxy

    proxy = MiotProxy.__new__(MiotProxy)
    proxy._token_refresh_lock = asyncio.Lock()
    proxy._oauth_info = MIoTOauthInfo(
        access_token="at", refresh_token="rt", expires_ts=9999999999
    )

    concurrent = 0
    peak = 0

    async def _slow_refresh():
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        return None

    proxy._do_refresh_token = _slow_refresh
    await asyncio.gather(*(proxy.refresh_xiaomi_home_token_info() for _ in range(5)))

    assert peak == 1, f"刷新没有串行，峰值并发 {peak}"
