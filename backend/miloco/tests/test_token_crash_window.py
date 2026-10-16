"""在「云端已换票、本地还没落库」的窗口里注入崩溃，看令牌保不保得住。

这是对整条因果链的复现：刷新令牌是一次性的，换取成功那一刻旧令牌就在云端作废；
若此时进程死掉而新令牌还没落库，库里留下的就是一枚已经作废的令牌，之后每次刷新
都必然被拒绝，住户只能重新授权。

崩溃用「副作用里抛出 BaseException」来模拟——它跳过普通的 ``except Exception``，
足以代表「换取之后、落库之前出事」这一类。真正的段错误连 finally 都不会执行，
比这更狠；能扛住这个不代表能扛住段错误，但**落库位置**这个关键点是同一个。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from miot.types import MIoTOauthInfo, MIoTUserInfo


class _Store:
    """站在库的位置：只认最后一次真正落进来的那枚。"""

    def __init__(self, initial: str):
        self.refresh_token = initial
        self.writes: list[str] = []

    def persist(self, info: MIoTOauthInfo) -> None:
        self.refresh_token = info.refresh_token
        self.writes.append(info.refresh_token)


def _client(store: _Store, *, crash_in_side_effect: bool):
    """造一个 MIoTClient，可选地让副作用阶段崩溃。"""
    from miot.client import MIoTClient

    c = MIoTClient.__new__(MIoTClient)
    c._oauth_info = MIoTOauthInfo(
        access_token="A1",
        refresh_token=store.refresh_token,
        expires_ts=1,
        user_info=MIoTUserInfo(uid="u1", nickname="n", icon="", union_id="x"),
    )

    async def _exchange(rt):
        # 云端行为：认这一枚、消费掉、发一对全新的
        assert rt == store.refresh_token, "发出去的必须是库里那枚"
        return MIoTOauthInfo(
            access_token="A2", refresh_token="R2", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.refresh_access_token_async = _exchange

    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: None

    async def _cam(**kw):
        if crash_in_side_effect:
            # 用 BaseException 模拟「跳过常规异常处理的中断」
            raise KeyboardInterrupt("simulated hard interrupt inside side effect")

    c._camera_client = MagicMock()
    c._camera_client.update_access_token_async = _cam

    async def _mips(_t):
        return None

    c._mips_cloud = MagicMock()
    c._mips_cloud.update_access_token = _mips
    return c


@pytest.mark.asyncio
async def test_crash_inside_side_effect_still_keeps_the_new_token():
    """窗口里崩溃，新令牌必须已经在库里。

    这是本文件的主张：落库排在副作用之前，所以副作用阶段无论出什么事，
    库里拿到的都已经是换来的那一枚。
    """
    store = _Store("R1")
    c = _client(store, crash_in_side_effect=True)

    with pytest.raises(KeyboardInterrupt):
        await c.refresh_access_token_async("R1", persist=store.persist)

    assert store.refresh_token == "R2", (
        "崩溃发生在换取之后，库里必须已经是新令牌；"
        f"实际留下的是 {store.refresh_token!r}"
    )


@pytest.mark.asyncio
async def test_next_refresh_after_crash_uses_the_surviving_token():
    """崩溃后重启，下一轮刷新拿的是存活下来的新令牌，而不是已作废的旧令牌。

    这一条正是故障当晚失败的地方：那时库里留着已被消费的旧令牌，于是之后
    每一次刷新都被云端拒绝。
    """
    store = _Store("R1")
    c1 = _client(store, crash_in_side_effect=True)
    with pytest.raises(KeyboardInterrupt):
        await c1.refresh_access_token_async(store.refresh_token, persist=store.persist)

    # 模拟重启：新进程从库里读令牌
    c2 = _client(store, crash_in_side_effect=False)

    async def _exchange2(rt):
        assert rt == "R2", f"重启后发出去的应是新令牌，实际是 {rt!r}"
        return MIoTOauthInfo(
            access_token="A3", refresh_token="R3", expires_ts=9999999999
        )

    c2._oauth_client.refresh_access_token_async = _exchange2
    await c2.refresh_access_token_async(store.refresh_token, persist=store.persist)

    assert store.refresh_token == "R3"
    assert store.writes[0] == "R2", "第一次落的就该是 R2，而不是等到副作用做完"


@pytest.mark.asyncio
async def test_persist_lands_before_any_side_effect_runs():
    """更直接的一条：副作用第一次被调用时，库里就已经是新令牌了。"""
    store = _Store("R1")
    seen_at_side_effect: list[str] = []

    from miot.client import MIoTClient

    c = MIoTClient.__new__(MIoTClient)
    c._oauth_info = MIoTOauthInfo(
        access_token="A1", refresh_token="R1", expires_ts=1,
        user_info=MIoTUserInfo(uid="u", nickname="n", icon="", union_id="x"),
    )

    async def _exchange(_rt):
        return MIoTOauthInfo(
            access_token="A2", refresh_token="R2", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.refresh_access_token_async = _exchange
    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: seen_at_side_effect.append(
        store.refresh_token
    )

    async def _cam(**kw):
        seen_at_side_effect.append(store.refresh_token)

    c._camera_client = MagicMock()
    c._camera_client.update_access_token_async = _cam

    async def _mips(_t):
        seen_at_side_effect.append(store.refresh_token)

    c._mips_cloud = MagicMock()
    c._mips_cloud.update_access_token = _mips

    await c.refresh_access_token_async("R1", persist=store.persist)

    assert seen_at_side_effect, "副作用应当被调用"
    assert set(seen_at_side_effect) == {"R2"}, (
        "每一个副作用运行时，库里都应当已经是新令牌；"
        f"实际观察到 {seen_at_side_effect}"
    )


@pytest.mark.asyncio
async def test_interrupted_before_response_leaves_a_verification_intent():
    """响应回来之前就崩溃——留下的是「结果未知」，不是「令牌已死」。

    这是修复覆盖不到的那一格：库里那枚**可能**已被云端消费，也可能完好（硬杀
    发生在请求抵达云端之前）。两种现场留下的痕迹一模一样，所以这里只安排下一次
    检查立刻验一次，不预先定论——误判的代价是住户白白重绑一次，而不定论的代价
    只是真死时慢一次往返。
    """
    from miloco.miot.client import MiotProxy

    class _KV:
        def __init__(self):
            self.d = {}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def set(self, k, v):
            self.d[k] = v
            return True

        def delete(self, k):
            self.d.pop(k, None)
            return True

    kv = _KV()
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._kv_repo = kv
    proxy._oauth_info = MIoTOauthInfo(
        access_token="A1", refresh_token="R1", expires_ts=1
    )
    proxy._auth_health = proxy._load_auth_health()
    proxy._token_refresh_lock = asyncio.Lock()

    # 发请求前记下意向，然后「进程没了」——标记留在库里
    proxy._mark_refresh_inflight("R1")

    # 重启：新进程读到同一枚令牌 + 未清的标记
    reborn = MiotProxy.__new__(MiotProxy)
    reborn._kv_repo = kv
    reborn._oauth_info = MIoTOauthInfo(
        access_token="A1", refresh_token="R1", expires_ts=1
    )
    reborn._auth_health = reborn._load_auth_health()
    reborn._apply_interrupted_refresh_on_start()

    assert not reborn.auth_health.is_degraded, (
        "结果未知不等于令牌已死，不该在启动时就判永久失效"
    )
    assert reborn._verify_token_on_start, (
        "但也不能就这么放过——要安排下一次检查立刻验一次，别等自然临期"
    )
