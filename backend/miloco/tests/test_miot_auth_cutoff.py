"""授权被云端永久拒绝后，走云端的下发一律停止。

失效之后系统实际处于「拿着一枚已判定无效的凭据继续运行」的状态：请求发出去只会
撞 401，而界面看起来还有部分功能正常。与其如此，不如当场拒绝并告知住户重新授权。

闸门放在代理层而不是逐个调用方——下发有好几条入口，逐个加必然漏，漏掉的那条会在
授权已失效时照常把请求发出去，住户看到的是「有的能用有的不能用」。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from miloco.middleware.exceptions import MiotAuthUnavailableError
from miloco.miot.auth_state import MiotAuthHealth
from miloco.miot.client import MiotProxy
from miot.error import MIoTErrorCode


def _proxy(*, degraded: bool):
    """造一个代理，只装本测试关心的部件。"""
    p = MiotProxy.__new__(MiotProxy)
    p._oauth_info = MagicMock()          # 凭据存在
    p._auth_health = MiotAuthHealth()
    p._token_refresh_lock = asyncio.Lock()
    p._scene_info_dict = {"s1": MagicMock()}
    if degraded:
        health, _ = p._auth_health.mark_failure(
            permanent=True,
            code=MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value,
            message="invalid refresh token",
        )
        p._auth_health = health

    sent: list[str] = []
    http = MagicMock()

    async def _set_props(_params):
        sent.append("set_props")
        return [{"code": 0}]

    async def _action(_param):
        sent.append("action")
        return {"code": 0}

    async def _save_text(_content):
        sent.append("save_text")
        return "n1"

    http.set_props_async = _set_props
    http.action_async = _action
    http.create_app_notify_async = _save_text
    client = MagicMock()
    client.http_client = http

    async def _run_scene(scene_info=None):
        sent.append("scene")
        return True

    async def _notify(_id):
        sent.append("notify")
        return True

    client.run_manual_scene_async = _run_scene
    client.send_app_notify_async = _notify
    p._miot_client = client
    return p, sent


# ─────────────── 失效后四条路径全部停止 ───────────────


@pytest.mark.asyncio
async def test_set_properties_refused_when_degraded():
    p, sent = _proxy(degraded=True)
    with pytest.raises(MiotAuthUnavailableError) as e:
        await p.set_device_properties([MagicMock()])
    assert "no longer valid" in str(e.value)
    assert sent == [], "请求不该被发出去"


@pytest.mark.asyncio
async def test_device_action_refused_when_degraded():
    p, sent = _proxy(degraded=True)
    with pytest.raises(MiotAuthUnavailableError):
        await p.call_device_action(MagicMock())
    assert sent == []


@pytest.mark.asyncio
async def test_scene_refused_when_degraded():
    """场景执行的约定是返回 bool、不抛——拒绝即返回 False，别改调用方的错误处理。"""
    p, sent = _proxy(degraded=True)
    assert await p.execute_miot_scene("s1") is False
    assert sent == []


@pytest.mark.asyncio
async def test_app_notify_refused_when_degraded():
    """通知有**两步**都走云端，闸门要挡在第一步——必须从服务入口进才测得到。

    只测代理上的 :meth:`send_app_notify` 会漏：服务层是先 ``save_text`` 换 id、
    再拿 id 去推，第一步没闸门的话请求照常发出去，而住户看到的是「通知内容不
    合适，请重新输入」——他会去改文案，反复改都不通。
    """
    from miloco.miot import message_dedup
    from miloco.miot import service as svc

    p, sent = _proxy(degraded=True)
    s = svc.MiotService.__new__(svc.MiotService)
    s._miot_proxy = p
    s._notify_deduper = message_dedup.MessageDeduper(window_sec=60)

    with pytest.raises(MiotAuthUnavailableError):
        await s.send_notify("回家了")
    assert sent == [], "请求不该被发出去"


@pytest.mark.asyncio
async def test_app_notify_works_when_healthy_via_service():
    """反向：闸门补到第一步之后，正常态的两步推送要照常走通。

    与上一条成对——只加闸门不验这条，把闸门加错位置（比如判据取反）也是绿的。
    """
    from miloco.miot import message_dedup
    from miloco.miot import service as svc

    p, sent = _proxy(degraded=False)
    s = svc.MiotService.__new__(svc.MiotService)
    s._miot_proxy = p
    s._notify_deduper = message_dedup.MessageDeduper(window_sec=60)

    await s.send_notify("回家了")
    assert sent == ["save_text", "notify"], "两步都要走到，且顺序不变"


# ─────────────── 未失效时四条路径照常 ───────────────


@pytest.mark.asyncio
async def test_all_paths_work_when_healthy():
    """闸门不能误伤正常态——四条路径都要照常走通。"""
    p, sent = _proxy(degraded=False)
    await p.set_device_properties([MagicMock()])
    await p.call_device_action(MagicMock())
    assert await p.execute_miot_scene("s1") is True
    assert await p.get_miot_app_notify_id("回家了") == "n1"
    assert await p.send_app_notify("n1") is True
    assert sent == ["set_props", "action", "scene", "save_text", "notify"]


@pytest.mark.asyncio
async def test_transient_failure_does_not_refuse():
    """瞬时故障不拦——一次超时就拒绝下发，会把可恢复的抖动变成可见的功能中断。"""
    p, sent = _proxy(degraded=False)
    for _ in range(3):
        health, _ = p._auth_health.mark_failure(
            permanent=False, code=None, message="timeout"
        )
        p._auth_health = health
    await p.set_device_properties([MagicMock()])
    assert sent == ["set_props"], "瞬时故障期间下发应当照常"


# ─────────────── 被拒的动作要留痕，且原因不被吞掉 ───────────────


@pytest.mark.asyncio
async def test_refusal_reason_is_not_swallowed_into_generic_failure():
    """授权失效要原样抛给上层，不能包成通用的「控制失败」。

    包了之后住户只看到「设备没反应」，不知道该做什么；而这件事恰恰是**需要住户
    动手**才能解的——必须让原因一路传到界面上。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError
    from miloco.miot import service as svc

    p, _ = _proxy(degraded=True)
    written: list[dict] = []

    async def _fake_ledger(_proxy, **kw):
        written.append(kw)

    orig = svc._write_action_ledger
    svc._write_action_ledger = _fake_ledger
    try:
        s = svc.MiotService.__new__(svc.MiotService)
        s._miot_proxy = p

        async def _allow(_did):
            return None

        s._assert_did_in_allowed_home = _allow

        # iid 要给真实格式：参数校验排在下发之前，桩不合法会先被它拦下，
        # 那样测到的就不是闸门了。
        req = MagicMock()
        req.type = "set_property"
        req.iid = "prop.2.1"
        req.value = True
        with pytest.raises(MiotAuthUnavailableError):
            await s.control_device("did-1", req)
    finally:
        svc._write_action_ledger = orig

    assert written, "被拒的动作也要留痕"
    assert written[0]["success"] is False
    assert "refused" in (written[0]["error"] or ""), (
        "留痕要记明是被拒，而不是笼统的失败"
    )


# ─────────────── 感知这一端：失效后拿不到相机 ───────────────


def _adapter(*, degraded: bool):
    """造一个相机适配器，代理的健康度按需置位。"""
    from miloco.perception.collect.camera_adapter import CameraDeviceAdapter

    p, _ = _proxy(degraded=degraded)
    asked: list[str] = []

    async def _get_cameras():
        asked.append("get_cameras")
        return {"cam-1": MagicMock()}

    p.get_cameras = _get_cameras

    # 正常路径会往下走到过滤逻辑，那里要读接入范围配置——给个空集即可，
    # 本测试关心的是「有没有去问云端」，不是过滤结果。
    class _KV:
        def get(self, key, default=None):
            return default

        def set(self, key, value):
            return True

        def delete(self, key):
            return True

    p._kv_repo = _KV()
    return CameraDeviceAdapter(p), asked


@pytest.mark.asyncio
async def test_discover_returns_nothing_when_degraded():
    """授权失效后，感知拿不到任何相机——且不该再去问云端。

    拉取账号下的相机列表本身就要有效令牌，失效后那一步会 401。与其发出去撞
    401、把错误刷进日志，不如在这里就停下。
    """
    a, asked = _adapter(degraded=True)
    assert await a.discover_devices() == {}
    assert asked == [], "已经判定失效，不该再去问云端"


@pytest.mark.asyncio
async def test_discover_works_when_healthy():
    """闸门不能误伤正常态。"""
    a, asked = _adapter(degraded=False)
    got = await a.discover_devices()
    assert asked == ["get_cameras"]
    assert isinstance(got, dict)


@pytest.mark.asyncio
async def test_discover_survives_transient_failure():
    """瞬时故障期间感知照跑——这条守的是本次改动最容易被误改的那道分界。

    把瞬时故障也算作失效的话，一次网络抖动就会让整套感知停摆，而那时凭据其实
    好好的。改判据时别把这条一起改掉。
    """
    a, asked = _adapter(degraded=False)
    for _ in range(3):
        health, _ = a._miot_proxy._auth_health.mark_failure(
            permanent=False, code=None, message="timeout"
        )
        a._miot_proxy._auth_health = health
    await a.discover_devices()
    assert asked == ["get_cameras"], "瞬时故障不该让感知停下"


# ─────────────── 结构性守卫：下发面别再漏一条 ───────────────
#
# 闸门收在代理层是对的，但「逐个方法去加」本身就是会漏的做法——通知的第一步
# (``save_text``) 就漏过：它名字叫 ``get_``，实际是往云端写文本。所以这里不按
# 名字判、也不靠人记得扫，而是把「哪些方法会打到云端」用 AST 枚举出来，逼每一个
# 都被归类。
#
# 下面两份清单是**可执行的**，不是注释里的重复枚举：新增一个走云端的代理方法而
# 没有归类，测试就红。别把它当成待清理的冗余。

#: 会改住户家里的状态、或向住户推送 —— 授权失效后必须拒绝。值是云端端点，
#: 供失败信息里指明「这条到底动了什么」。
DISPATCH_MUST_BE_GATED = {
    "set_device_properties": "/app/v2/miotspec/prop/set",
    "call_device_action": "/app/v2/miotspec/action",
    "execute_miot_scene": "…/AppSceneService/NewRunScene",
    "get_miot_app_notify_id": "/app/v2/oauth/save_text（名字是 get_，其实是推送第一步）",
    "send_app_notify": "/app/v2/oauth/send_push",
}

#: 明确不设闸门，各有理由。给恢复路径加闸门会让降级态再也出不去，那是比漏加
#: 更严重的错，所以这份清单也要反向断言。
MUST_NOT_BE_GATED = {
    # 恢复路径：拦了就死锁——住户再也没法把授权修回来
    "get_miot_login_url": "生成重新授权的入口地址",
    "_do_authorize": "住户重新授权时换令牌",
    "_do_refresh_token": "续期本身，正是要靠它翻回正常态",
    # 只读与订阅：失效后自然拿到 401 或空集，拦了不多一分信息；相机列表更是
    # 要靠「拿到空集」把感知停下来，拦在前面反而少了那一步
    "get_device_properties": "读属性",
    "_fetch_device_spec": "读设备 spec",
    "check_token_valid": "查令牌是否还有效",
    "refresh_user_info": "读账号身份",
    "refresh_cameras": "拉相机列表——空集正是感知停下的机制",
    "refresh_camera_online_status": "拉相机在线状态",
    "refresh_devices": "拉设备列表",
    "refresh_scenes": "拉场景列表",
    "_get_camera_instance": "建相机实例",
    "_sync_meta_subscriptions": "同步设备元信息订阅",
    "_sync_camera_state_subscriptions": "同步设备状态订阅",
    "_sync_scene_subscriptions": "同步场景订阅",
    "init": "初始化与回调注册",
    "deinit": "反初始化",
}


def _reaches_cloud(fn: ast.AST) -> bool:
    """这个方法体里有没有对 ``self.(_)miot_client...`` 的调用。

    按结构判而不按名字判：名字判会漏掉 ``get_miot_app_notify_id`` 这种「叫
    get_、其实往云端写」的。
    """
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        probe = node.func.value
        while isinstance(probe, ast.Attribute):
            if (
                isinstance(probe.value, ast.Name)
                and probe.value.id == "self"
                and probe.attr in ("_miot_client", "miot_client")
            ):
                return True
            probe = probe.value
    return False


def _has_gate(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_refuse_if_not_operational"
        for n in ast.walk(fn)
    )


def _cloud_facing_methods() -> dict[str, bool]:
    """``{方法名: 有没有闸门}``，只收会打到云端的那些。"""
    src = Path(inspect.getsourcefile(MiotProxy)).read_text()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "MiotProxy"
    )
    return {
        m.name: _has_gate(m)
        for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and _reaches_cloud(m)
    }


def test_every_cloud_facing_method_is_classified():
    """新增一个走云端的代理方法，必须归类——这条就是防「又漏一个」的那道守卫。

    归到 ``DISPATCH_MUST_BE_GATED`` 就得加闸门；归到 ``MUST_NOT_BE_GATED`` 得写
    明为什么不加。两边都不填，这条红。
    """
    found = set(_cloud_facing_methods())
    classified = set(DISPATCH_MUST_BE_GATED) | set(MUST_NOT_BE_GATED)

    unclassified = found - classified
    assert not unclassified, (
        f"这些代理方法会打到云端，但没归类：{sorted(unclassified)}。"
        "会改住户家里状态或向住户推送的，加进 DISPATCH_MUST_BE_GATED 并补闸门；"
        "只读 / 订阅 / 恢复路径，加进 MUST_NOT_BE_GATED 并写明理由。"
    )
    vanished = classified - found
    assert not vanished, (
        f"这些名字已不再打到云端（改名或删了）：{sorted(vanished)}，清单要跟着改。"
    )


def test_dispatch_paths_are_gated():
    """下发面每一条都要有闸门。"""
    gates = _cloud_facing_methods()
    missing = {
        name: endpoint
        for name, endpoint in DISPATCH_MUST_BE_GATED.items()
        if not gates.get(name)
    }
    assert not missing, (
        f"这些下发面没有闸门，授权失效后请求照样发出去：{missing}"
    )


def test_recovery_and_read_paths_are_not_gated():
    """反向：不该加闸门的地方别加。

    尤其是恢复路径——给换票 / 续期 / 生成授权入口加上闸门，降级态就再也出不去，
    住户点「重新绑定」会被自己的降级态挡回来。
    """
    gates = _cloud_facing_methods()
    wrongly_gated = {
        name: why for name, why in MUST_NOT_BE_GATED.items() if gates.get(name)
    }
    assert not wrongly_gated, (
        f"这些地方不该有闸门：{wrongly_gated}。恢复路径被拦住会让降级态无法自愈。"
    )
