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
from unittest.mock import MagicMock

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


# ─────────────── 重新授权必须重建云端长连接 ───────────────


def _client_tracking_mips():
    """造一个 MIoTClient，记录 mips 是「重建」还是「只换密码重连」。"""
    c = MIoTClient.__new__(MIoTClient)
    calls: list[str] = []

    c._oauth_info = MIoTOauthInfo(
        access_token="at",
        refresh_token="rt",
        expires_ts=9999999999,
        user_info=MIoTUserInfo(uid="u1", nickname="n", icon="", union_id="x"),
    )
    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: None
    c._camera_client = None

    async def _upd(_t):
        calls.append("reconnect")

    c._mips_cloud = MagicMock()
    c._mips_cloud.update_access_token = _upd

    async def _setup():
        calls.append("rebuild")

    c._setup_mips_async = _setup
    return c, calls


@pytest.mark.asyncio
async def test_reauthorize_rebuilds_mips_even_when_client_exists():
    """重新授权必须重建，不能退化成换密码重连。

    账号级主题 ``user/{uid}/g_op/bind`` / ``unbind`` 只在重建时发出，而重连只把
    ``_subs`` 里按 topic 字符串存着的旧 uid 主题原样重放。换了账号之后，新账号的
    绑定/解绑推送就再也订阅不到——用户在米家 App 里加设备，这边收不到通知。
    """
    c, calls = _client_tracking_mips()
    apply = c._MIoTClient__apply_access_token_async

    await apply(rebuild_mips=True)

    assert calls == ["rebuild"], (
        f"重新授权应当重建 mips，实际是 {calls}"
    )


@pytest.mark.asyncio
async def test_scheduled_refresh_only_reconnects():
    """定时续期不换账号，走廉价的换密码重连即可，不必重建。"""
    c, calls = _client_tracking_mips()
    apply = c._MIoTClient__apply_access_token_async

    await apply()

    assert calls == ["reconnect"], f"定时续期应当只重连，实际是 {calls}"


@pytest.mark.asyncio
async def test_missing_mips_is_built_from_scratch():
    """长连接还没建起来时（进程刚起、从库里恢复凭据做首次续期）应当重建。"""
    c, calls = _client_tracking_mips()
    c._mips_cloud = None
    apply = c._MIoTClient__apply_access_token_async

    await apply()

    assert calls == ["rebuild"]


@pytest.mark.asyncio
async def test_authorize_end_to_end_rebuilds_mips():
    """走完整的 ``get_access_token_async``，确认它真的要求了重建。

    单独测 ``__apply_access_token_async(rebuild_mips=True)`` 不够：那只证明参数
    生效，不证明授权路径传了它。把调用点的 ``rebuild_mips=True`` 去掉，上面那条
    仍会绿——所以必须有这一条从入口进的。
    """
    c, calls = _client_tracking_mips()

    async def _check_state(**kw):
        return True

    async def _exchange(code):
        return MIoTOauthInfo(
            access_token="at2", refresh_token="rt2", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.check_state_async = _check_state
    c._oauth_client.get_access_token_async = _exchange

    async def _user_info():
        c._oauth_info.user_info = MIoTUserInfo(
            uid="u2", nickname="n2", icon="", union_id="y"
        )
        return c._oauth_info.user_info

    c.get_user_info_async = _user_info

    await c.get_access_token_async(code="the_code", state="the_state")

    assert calls == ["rebuild"], (
        "重新授权走到底必须重建 mips（账号级主题只在重建时发出），"
        f"实际是 {calls}"
    )


# ─────────────── 取账号身份之前，令牌必须已经推给 HTTP 客户端 ───────────────


@pytest.mark.asyncio
async def test_identity_failure_after_refresh_still_pushes_the_new_token():
    """续期路径：取身份失败不许拖垮续期，新令牌照样推给下游。

    落库之后那一步是一次真实的业务请求，超时与 5xx 都会抛。让它冒泡出去，下面
    「把新令牌推给原生相机库与云端长连接」那一步就整个跳过——它们于是一直持有
    旧访问令牌，直到下一次自然临期续期才被换掉，量级是天；而上层看到的是「续期
    失败」，会按退避再刷几次，每次都再换一对令牌、再在同一处抛。身份缺失本身
    无害，下一轮刷新会补上。
    """
    order: list[str] = []
    c = MIoTClient.__new__(MIoTClient)
    c._oauth_info = None
    c._camera_client = None
    c._mips_cloud = None

    async def _setup():
        order.append("mips")

    c._setup_mips_async = _setup

    async def _refresh(refresh_token):
        return MIoTOauthInfo(
            access_token="AT_new", refresh_token="RT_new", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.refresh_access_token_async = _refresh

    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: order.append("http_header")

    async def _user_info():
        order.append("identity")
        raise RuntimeError("get_user_info 5xx")

    c.get_user_info_async = _user_info

    persisted: list[str] = []

    # 不抛：续期在语义上已经成功
    info = await c.refresh_access_token_async(
        refresh_token="RT_old", persist=lambda i: persisted.append(i.access_token)
    )

    assert info.access_token == "AT_new"
    assert "identity" in order, "前置：取身份确实被调到并抛了"
    assert order.index("http_header") < order.index("identity")
    assert "mips" in order, (
        "取身份抛错把「推新令牌给下游」整步跳过了——下游会一直持有旧令牌"
    )
    assert persisted and persisted[0] == "AT_new", "新令牌仍要落库"


@pytest.mark.asyncio
async def test_http_header_is_updated_before_identity_lookup_on_authorize():
    """首次/重新授权：取身份是一次真实 HTTP 请求，必须先拿到新令牌。

    取身份读的是 HTTP 客户端里缓存的 access_token，而那个缓存只有一个写入点。
    排在它后面的话：首次绑定时缓存是空串，uid 换取撞上「access token is empty」
    的硬校验，把一次已经成功、授权码已被消费的授权报成失败；换账号重绑时更隐蔽
    ——会拿回旧账号的 uid，让「是否同一账号」恒真，该清的配置永远不清。
    """
    order: list[str] = []
    c = MIoTClient.__new__(MIoTClient)
    c._oauth_info = None
    c._camera_client = None
    c._mips_cloud = None

    async def _setup():
        order.append("mips")

    c._setup_mips_async = _setup

    async def _check_state(**kw):
        return True

    async def _exchange(code):
        return MIoTOauthInfo(
            access_token="AT_new", refresh_token="RT_new", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.check_state_async = _check_state
    c._oauth_client.get_access_token_async = _exchange

    seen_token: list[str | None] = []
    c._http_client = MagicMock()

    def _upd(**kw):
        order.append("http_header")

    c._http_client.update_http_header = _upd

    async def _user_info():
        # 取身份的时刻，令牌必须已经推进去了
        order.append("identity")
        seen_token.append("AT_new" if "http_header" in order else None)
        c._oauth_info.user_info = MIoTUserInfo(
            uid="u1", nickname="n", icon="", union_id="x"
        )
        return c._oauth_info.user_info

    c.get_user_info_async = _user_info

    await c.get_access_token_async(code="c", state="s")

    assert order.index("http_header") < order.index("identity"), (
        f"取身份排在了推令牌之前，实际顺序: {order}"
    )
    assert seen_token == ["AT_new"], "取身份时用的不是新令牌"


@pytest.mark.asyncio
async def test_persist_still_precedes_the_header_update():
    """推令牌排在取身份之前，但落库仍要排在最前——两个约束不冲突。

    推令牌是纯内存赋值，不会丢令牌也不会带走进程；真正需要被落库挡在后面的是
    那些会崩的副作用（原生相机、云端长连接）。
    """
    order: list[str] = []
    c = MIoTClient.__new__(MIoTClient)
    c._oauth_info = None
    c._camera_client = None
    c._mips_cloud = None

    async def _setup():
        order.append("mips")

    c._setup_mips_async = _setup

    async def _check_state(**kw):
        return True

    async def _exchange(code):
        return MIoTOauthInfo(
            access_token="AT_new", refresh_token="RT_new", expires_ts=9999999999
        )

    c._oauth_client = MagicMock()
    c._oauth_client.check_state_async = _check_state
    c._oauth_client.get_access_token_async = _exchange
    c._http_client = MagicMock()
    c._http_client.update_http_header = lambda **kw: order.append("http_header")

    async def _user_info():
        order.append("identity")
        c._oauth_info.user_info = MIoTUserInfo(
            uid="u1", nickname="n", icon="", union_id="x"
        )
        return c._oauth_info.user_info

    c.get_user_info_async = _user_info

    await c.get_access_token_async(
        code="c", state="s", persist=lambda _i: order.append("persist")
    )

    assert order[0] == "persist", f"落库必须最先，实际: {order}"
    assert order.index("persist") < order.index("http_header")
    assert order.index("http_header") < order.index("identity")
    assert order.index("identity") < order.index("mips")
