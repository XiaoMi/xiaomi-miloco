# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""推送写容器：两道闸、值形态、路径形状。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from miloco.miot import state_push
from miloco.miot.iid import leaf_path
from miloco.miot.state_push import SOURCE, IotPushWriter, write_online, write_prop
from miloco.state import MISSING, StateStore
from miloco.utils.time_utils import now_ms


@pytest.fixture
async def store():
    s = StateStore()
    s.start()
    yield s
    s.stop()


_DEFAULT_URN = object()  # 「没指定」与「指定成空」要分得开，见 _writer 的 urn 形参


def _writer(
    store, *, home=("d1",), online=True, aligned=True, events=None, urn=_DEFAULT_URN
):
    """proxy 只提供 devices_in_current_home；对齐判据由一个可调对象给。

    `events` 是事件推送那句 spec 查询的答案（`_fetch_device_events` 的返回）；传可调用
    对象时直接当那个方法用 —— 要数调用次数或者模拟取表失败时走这个口。
    `home` 与 `aligned` 同样收可调用对象：要在一次调用中途把闸关上（取表在飞期间设备
    搬出家庭、或切作用域）时，得让它们每次现读，而不是构造时就定死。
    `urn` 默认按 did 拼；传 `None` 是「这台设备没有 urn」那种形态。
    """
    take_home = home if callable(home) else lambda: home
    take_aligned = aligned if callable(aligned) else lambda: aligned

    async def devices_in_current_home():
        return {
            did: SimpleNamespace(
                home_id="H1",
                online=online,
                urn=f"urn:test:{did}" if urn is _DEFAULT_URN else urn,
            )
            for did in take_home()
        }

    if callable(events):
        _fetch_device_events = events
    else:

        async def _fetch_device_events(urn):
            return dict(events or {})

    return IotPushWriter(
        store,
        SimpleNamespace(
            devices_in_current_home=devices_in_current_home,
            _fetch_device_events=_fetch_device_events,
        ),
        scope_is_aligned=take_aligned,
    )


def _props(did="d1", changes=((2, 1, 26),)):
    return SimpleNamespace(
        did=did,
        changes=[SimpleNamespace(siid=s, piid=p, value=v) for s, p, v in changes],
    )


def _event(did="d1", siid=2, eiid=1020, args=((1, 2),)):
    """一条事件推送。`args` 是 `(piid, value)` 对 —— 帧里带的就是数字 piid。"""
    return SimpleNamespace(
        did=did,
        siid=siid,
        eiid=eiid,
        args=[SimpleNamespace(piid=p, value=v) for p, v in args],
    )


def _event_spec(siid=2, eiid=1020, args=(("unlock-operation-position", 1),)):
    """事件 spec 表：键是 `event.<siid>.<eiid>.<argname>`，条目带三个数字供反查。"""
    return {
        f"event.{siid}.{eiid}.{name}": {
            "name": name,
            "siid": siid,
            "eiid": eiid,
            "piid": piid,
        }
        for name, piid in args
    }


async def _settle(rounds: int = 200) -> None:
    """投递是异步的，等事件循环转够圈数。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _state(did="d1", event="online"):
    return SimpleNamespace(did=did, event=event)


async def test_a_property_push_lands_on_the_leaf(store):
    await _writer(store).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get_entry("iot/device/d1/prop/2.1").source == SOURCE


async def test_a_property_push_does_not_wipe_its_siblings(store):
    store.set("iot/device/d1/prop/3.1", True, source="iot_align")

    await _writer(store).on_device_props(_props())

    assert store.get("iot/device/d1/prop/3.1") is True


async def test_a_property_push_before_the_alignment_is_dropped(store):
    """对齐拿的是云端缓存值，推送先落地会被随后的对齐用旧值静默盖掉。"""
    await _writer(store, aligned=False).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_a_property_push_for_a_device_outside_the_home_is_dropped(store):
    await _writer(store, home=()).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_a_dict_value_is_refused_instead_of_becoming_a_subtree(store):
    """容器对 dict 不抛错，它会把叶子翻成子树，订阅旧形状的消费方从此收不到。"""
    await _writer(store).on_device_props(_props(changes=((2, 1, {"a": 1}),)))

    assert store.get("iot/device/d1/prop/2.1") is MISSING
    assert store.get("iot/device/d1/prop/2.1/a") is MISSING


async def test_a_scalar_list_is_written_as_is(store):
    await _writer(store).on_device_props(_props(changes=((2, 1, [1, 2]),)))

    assert store.get("iot/device/d1/prop/2.1") == (1, 2)


async def test_one_bad_entry_does_not_drop_the_rest_of_the_batch(store):
    await _writer(store).on_device_props(_props(changes=((2, 1, {"a": 1}), (2, 2, 7))))

    assert store.get("iot/device/d1/prop/2.2") == 7


async def test_an_online_push_lands_without_waiting_for_the_alignment(store):
    """online 的容器值与设备缓存同源，早写晚写都一样，不该被对齐门挡住。"""
    await _writer(store, aligned=False).on_device_state(_state())

    assert store.get("iot/device/d1/status/online") is True


async def test_an_offline_push_flips_the_flag(store):
    store.set("iot/device/d1/status/online", True, source="iot_align")

    await _writer(store).on_device_state(_state(event="offline"))

    assert store.get("iot/device/d1/status/online") is False


async def test_an_online_push_for_a_device_outside_the_home_is_dropped(store):
    await _writer(store, home=()).on_device_state(_state())

    assert store.get("iot/device/d1/status/online") is MISSING


async def test_a_bridged_did_never_reaches_the_container(store):
    """did 带 '/' 会把路径劈成两段，值落到别的设备下面。"""
    await _writer(store, home=("blt/1",)).on_device_props(_props(did="blt/1"))

    assert store.snapshot("iot/**") == {}


async def test_rewriting_the_same_online_value_delivers_no_event(store):
    """每次刷新设备都重写在线标志（见 refresh_devices），同值不产事件才敢这么写。

    观测点必须是投递而不是 `last_changed`：两次写落在同一毫秒里，时间戳分不开对错。
    """
    seen: list = []
    store.subscribe("iot/**", seen.append)
    write_online(store, "d1", True)
    await _settle()
    seen.clear()

    write_online(store, "d1", True)
    await _settle()

    assert seen == []


async def test_rewriting_a_changed_online_value_delivers_an_event(store):
    """上一条的反面 —— 少了它，「一律不投递」也能让上一条绿。"""
    seen: list = []
    store.subscribe("iot/**", seen.append)
    write_online(store, "d1", True)
    await _settle()
    seen.clear()

    write_online(store, "d1", False)
    await _settle()

    assert [change.new for change in seen] == [False]


async def test_the_proxy_hands_state_events_to_every_listener():
    """一个 listener 抛异常不该让另一个收不到 —— 缓存更新和写容器互不背锅。"""
    from unittest.mock import AsyncMock

    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._camera_info_dict = {}
    proxy._device_info_dict = {}
    proxy._cameras_loaded = True
    proxy._camera_state_listener = SimpleNamespace(on_event=AsyncMock())
    proxy._state_listeners = []

    boom = AsyncMock(side_effect=RuntimeError("boom"))
    ok = AsyncMock()
    proxy.add_device_state_listener(boom)
    proxy.add_device_state_listener(ok)

    await proxy._on_device_state_changed_event(_state())

    boom.assert_awaited_once()
    ok.assert_awaited_once()


async def test_the_manager_wires_every_push_lane_into_the_container(store):
    """漏挂一条 listener 的后果是推送静默不进容器，生产里看不出来。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []
    proxy._events_listeners = []
    proxy._mips_connect_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1")}

    proxy.devices_in_current_home = devices_in_current_home

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0

    manager._wire_iot_push()

    for callback in proxy._props_listeners:
        await callback(_props())
    for callback in proxy._state_listeners:
        await callback(_state())

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get("iot/device/d1/status/online") is True


async def test_a_push_travels_from_the_sdk_callback_into_the_container(store):
    """每段单测都过不代表接上了：这条从 proxy 注册给 SDK 的那个回调开始走。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy
    from miot.types import MIoTDevicePropsEvent, MIoTPropChange

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []
    proxy._events_listeners = []
    proxy._mips_connect_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1")}

    proxy.devices_in_current_home = devices_in_current_home

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0
    manager._wire_iot_push()

    await proxy._on_device_props_changed_event(
        MIoTDevicePropsEvent(
            did="d1",
            changes=[MIoTPropChange(siid=2, piid=1, value=26)],
            timestamp_ms=1,
        )
    )

    assert store.get("iot/device/d1/prop/2.1") == 26
    manager._prop_top_up.deinit()


# ── 重连拉属性：不用旧值盖掉更新的值 ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_pull_does_not_overwrite_a_value_that_arrived_during_it(store):
    """拉的往返中途推送到达的那条，不能被云端缓存里的旧值盖掉。

    容器没有时间戳可仲裁的话盖完 last_reported 是当下，事后看不出来。
    """
    started = now_ms()
    # 往返中途到达的推送：last_reported 不早于本次拉取开始
    write_prop(store, "d1", "2.1", 30)

    written = await _writer(store).write_pulled_props(
        "d1", {"2.1": 26}, keep_reported_since=started
    )

    assert written == 0
    assert store.get("iot/device/d1/prop/2.1") == 30


@pytest.mark.asyncio
async def test_a_pull_overwrites_a_value_older_than_the_pull(store):
    """与上一条方向相反：断连前写下的旧值就是要被覆盖的那些。

    判据写成「已经在树上就不写」时这条会红 —— 那正是上线补拉的判据，套到重连上会让
    整条恢复路径空转。
    """
    write_prop(store, "d1", "2.1", 26)

    written = await _writer(store).write_pulled_props(
        "d1", {"2.1": 30}, keep_reported_since=now_ms() + 1000
    )

    assert written == 1
    assert store.get("iot/device/d1/prop/2.1") == 30


async def test_a_pull_does_not_write_for_an_offline_device(store):
    written = await _writer(store, online=False).write_pulled_props(
        "d1", {"2.1": 30}
    )

    assert written == 0
    assert store.get("iot/device/d1/prop/2.1") is MISSING


@pytest.mark.asyncio
async def test_the_top_up_default_keeps_everything_already_in_the_tree(store):
    """上线补拉不传 keep_reported_since：已经在树上的一条都不动。"""
    write_prop(store, "d1", "2.1", 26)

    written = await _writer(store).write_pulled_props("d1", {"2.1": 30})

    assert written == 0
    assert store.get("iot/device/d1/prop/2.1") == 26


# ── 事件推送：第三条通道 ───────────────────────────────────────────────


async def test_an_event_arg_lands_under_the_event_subtree(store):
    """帧里只有数字 piid，叶子的键是参数名 —— 中间那次 spec 查询是这条的全部难点。"""
    await _writer(store, events=_event_spec()).on_device_event(_event())

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    assert (
        store.get_entry("iot/device/d1/event/2.1020.unlock-operation-position").source
        == SOURCE
    )


async def test_an_event_never_lands_in_the_prop_subtree(store):
    """两棵子树是分开的：落到 `/prop/` 会被属性侧的补拉和覆盖判据当成一条属性。"""
    await _writer(store, events=_event_spec()).on_device_event(_event())

    assert store.snapshot("iot/device/d1/prop/**") == {}


async def test_an_unknown_arg_is_skipped_without_taking_the_frame_down(store):
    """一个认不得的参数不该连坐同一帧里认得的那些。"""
    writer = _writer(store, events=_event_spec())

    await writer.on_device_event(_event(args=((1, 2), (9, 3))))

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    assert store.get("iot/device/d1/event/2.1020.9") is MISSING
    assert writer.stats()["event_arg_unknown"] == 1


async def test_an_event_of_a_siid_outside_the_spec_resolves_nothing(store):
    """spec 里查不到 (siid, eiid) 时给空表 —— 不能退化成「按 piid 猜一个名字」。"""
    writer = _writer(store, events=_event_spec())

    await writer.on_device_event(_event(eiid=9999, args=((1, 2),)))

    assert store.snapshot("iot/device/d1/event/**") == {}
    assert writer.stats()["event_arg_unknown"] == 1


async def test_an_argless_event_is_counted_instead_of_silently_swallowed(store):
    """spec 允许无参事件。它的到达是真的 —— 不落叶子，但「丢了多少帧」要答得出来。"""
    writer = _writer(store, events=_event_spec())

    await writer.on_device_event(_event(args=()))

    assert store.snapshot("iot/device/d1/event/**") == {}
    assert writer.stats()["event_no_args"] == 1


async def test_an_event_before_the_alignment_is_dropped(store):
    """与属性推送同一条理由：对齐整台替换会把它删掉。

    判据落在计数上，不停在「叶子是空的」：事件要比属性多过一次 spec 反查，闸门拆掉
    之后名字照样翻不出来、叶子照样是空的 —— 只看树的话这条用例会一直绿。
    """
    writer = _writer(store, aligned=False, events=_event_spec())

    await writer.on_device_event(_event())

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") is MISSING
    assert writer.stats()["event_not_aligned"] == 1
    assert "event_arg_unknown" not in writer.stats()


async def test_an_event_for_a_device_outside_the_home_is_dropped(store):
    """同上：这道闸拆掉后叶子仍是空的，只有计数能把两条出口分开。"""
    writer = _writer(store, home=(), events=_event_spec())

    await writer.on_device_event(_event())

    assert store.snapshot("iot/**") == {}
    assert writer.stats()["event_out_of_home"] == 1
    assert "event_arg_unknown" not in writer.stats()


async def test_a_bridged_event_did_never_reaches_the_container(store):
    """`did` 与 `iid` 都是路径段：不校验的话带 '/' 的 did 会把路径劈成两段，值落到
    别的设备下面 —— 桥接子设备（BLE 网关下挂的那些）的真实 did 就带 '/'。
    """
    await _writer(store, home=("blt/1",), events=_event_spec()).on_device_event(
        _event(did="blt/1")
    )

    assert store.snapshot("iot/**") == {}


async def test_an_event_did_carrying_a_dot_still_lands_on_one_segment(store):
    """带点的 did 不拦 —— 点是段内合法字符（叶子的末段本身就带点）。

    要钉的是**往返**：落下的路径按位置切回来还必须指回同一台设备，否则这台设备的
    事件叶能写进去、规则却永远查不到。
    """
    await _writer(store, home=("blt.1",), events=_event_spec()).on_device_event(
        _event(did="blt.1")
    )

    assert store.get("iot/device/blt.1/event/2.1020.unlock-operation-position") == 2


async def test_a_repeated_event_value_lands_but_raises_no_change(store):
    """事件叶 = 最后值：同一参数值再次到达只刷新时间戳，不产生变更。

    这条是**行为钉子**，不是缺陷：容器对同值走 `_same` 分支、不产 Change，于是引用这
    片叶子的规则不会被叫醒 —— 与属性车道一致（同值重写同样不产事件）。代价是「门锁
    连续两次室内开锁」这类同值重复对规则不可见，库和文档都按这个口径写。
    """
    seen: list = []
    store.subscribe("iot/device/d1/event/**", seen.append)
    writer = _writer(store, events=_event_spec())
    await writer.on_device_event(_event())
    await _settle()
    seen.clear()

    await writer.on_device_event(_event())
    await _settle()

    assert seen == []
    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    # 叶子落了、写没被拒 —— 计数说「进树了」，不说「规则看见了」
    assert writer.stats()["event_written"] == 2


async def test_a_changed_event_value_raises_a_change(store):
    """上一条的反面 —— 少了它，「一律不产变更」也能让上一条绿。"""
    seen: list = []
    store.subscribe("iot/device/d1/event/**", seen.append)
    writer = _writer(store, events=_event_spec())
    await writer.on_device_event(_event())
    await _settle()
    seen.clear()

    await writer.on_device_event(_event(args=((1, 3),)))
    await _settle()

    assert [change.new for change in seen] == [3]


async def test_a_dict_event_arg_is_refused_instead_of_becoming_a_subtree(store):
    writer = _writer(store, events=_event_spec())

    await writer.on_device_event(_event(args=((1, {"a": 1}),)))

    assert store.snapshot("iot/device/d1/event/**") == {}
    assert writer.stats()["event_rejected"] == 1


async def test_the_push_lane_writes_where_the_rule_lane_looks(store):
    """推送写的叶路径与规则读的叶路径必须是同一条。

    两边各拼一份 f-string（这里拼 `/prop/`、`/event/`，规则侧走 `iid.leaf_path`），
    只改一处就是「规则永远 path_missing」这种无声失效 —— 所以这条用例照规则侧的
    定义去读，读得到才算两边还对得上。
    """
    await _writer(store).on_device_props(_props())
    await _writer(store, events=_event_spec()).on_device_event(_event())

    assert store.get(leaf_path("d1", "2.1")) == 26
    assert store.get(leaf_path("d1", "event.2.1020.unlock-operation-position")) == 2


class _Clock:
    """替掉 state_push 里的 time 模块：静默期是按时间判的，测试得能推着它走。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


async def test_a_failed_event_spec_fetch_is_not_retried_on_every_frame(
    store, monkeypatch
):
    """取表没拿到可用结果后按 urn 静默一段，不按帧重试。

    这条路径上每一帧事件都要问一次 spec，而一次失败在 SDK 那侧是三次 30s 超时的云端
    请求 —— 不静默就等于按帧放大。静默期不改计数口径：那一帧的参数名照常算
    event_arg_unknown，「丢了多少」仍然看得见。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []

    async def _fetch_device_events(urn):
        calls.append(urn)
        return {}  # 取表失败，或这台设备真没有事件 —— 调用侧长得一样

    writer = _writer(store, events=_fetch_device_events)

    await writer.on_device_event(_event())
    await writer.on_device_event(_event())
    assert calls == ["urn:test:d1"]  # 第二帧没再出门
    assert writer.stats()["event_arg_unknown"] == 2  # 但两帧都照常计数

    clock.now += state_push._EVENT_SPEC_QUIET_SECONDS
    await writer.on_device_event(_event())
    assert len(calls) == 2  # 过期自动重试，不会把这台设备锁到进程结束


async def test_a_working_event_spec_fetch_is_never_held_back(store, monkeypatch):
    """静默只挂在失败上：取得到的时候一帧都不拦。

    记号要「没拿到可用表才留、拿到就撤」。写成「问之前先记时刻」且不撤，正常路径上
    同一台设备的第二帧就会白丢参数名 —— 落不了叶、还看不出跟谁有关，比放大更难查。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []

    async def _fetch_device_events(urn):
        calls.append(urn)
        return _event_spec()

    writer = _writer(store, events=_fetch_device_events)

    await writer.on_device_event(_event())
    await writer.on_device_event(_event())

    assert len(calls) == 2
    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    assert writer.stats()["event_written"] == 2


async def test_frames_arriving_during_an_in_flight_fetch_do_not_start_another(
    store, monkeypatch
):
    """一趟取表是分钟级的（三次重试，每次不止一个 30s 超时）。记号只盖「失败之后」是盖不住这一趟的。

    等第一趟回来了才记时刻的话，这期间每一帧看到的都是一张干净的字典，各自再起一趟
    —— 帧越密放大得越狠，而放大器正是那个本意是限流的记号。判据因此是**出门次数**：
    记号要在 await 之前落下。代价是这一小段里同一台设备的其他事件帧也落不了叶，
    它们本来就在等同一份表，所以那是等、不是丢。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []
    released = asyncio.Event()

    async def _fetch_device_events(urn):
        calls.append(urn)
        await released.wait()
        return _event_spec()

    writer = _writer(store, events=_fetch_device_events)

    first = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()  # 让第一帧走到 await 上
    second = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()

    assert calls == ["urn:test:d1"]  # 在飞的那一趟挡住了第二帧，它没有另起一趟

    released.set()
    await asyncio.gather(first, second)

    assert writer.stats()["event_written"] == 1
    assert writer.stats()["event_arg_unknown"] == 1  # 第二帧照常计数，静默期不改口径
    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2


async def test_an_in_flight_fetch_still_holds_frames_after_the_quiet_period_passes(
    store, monkeypatch
):
    """在飞这道闸按状态判，不按时间 —— 一趟取表是分钟级的，失败静默期只有 60s。

    把「正在飞」也记成一个 60s 的时间戳，飞过 60s 之后后来的帧就会各自再起一趟：并发放
    到两趟同一 urn 的冷取表，而放大器正是那个本意是限流的记号。上面那条在飞用例把时钟
    冻在 0，看不见这一截，所以这条专推时钟。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []
    released = asyncio.Event()

    async def _fetch_device_events(urn):
        calls.append(urn)
        await released.wait()
        return _event_spec()

    writer = _writer(store, events=_fetch_device_events)

    first = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()
    assert calls == ["urn:test:d1"]

    # 第一趟还没回来，时钟已经越过失败静默期
    clock.now += state_push._EVENT_SPEC_QUIET_SECONDS * 2
    second = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()
    assert calls == ["urn:test:d1"]  # 没有第二趟并发取表

    released.set()
    await asyncio.gather(first, second)

    assert writer.stats()["event_written"] == 1
    assert writer.stats()["event_arg_unknown"] == 1


async def test_a_cancelled_fetch_does_not_lock_the_urn_out_for_good(
    store, monkeypatch
):
    """取表中途被取消，在飞记号要撤掉 —— 否则这台设备在进程余下的寿命里再也取不到表。

    取消走的是 ``CancelledError``，它不继承 ``Exception``，所以 except 那条路碰不到它，
    撤销必须落在 finally 里。用时间戳兼表在飞的时候这个毛病是靠「到期自愈」兜住的，
    换成按状态挡之后就没有那层兜底了。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []
    hangs = asyncio.Event()

    async def _fetch_device_events(urn):
        calls.append(urn)
        if len(calls) == 1:
            await hangs.wait()  # 第一趟永远不返回，等被取消
        return _event_spec()

    writer = _writer(store, events=_fetch_device_events)

    first = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    # 第一趟被取消之后，这台设备还能再取表
    await writer.on_device_event(_event())

    assert calls == ["urn:test:d1", "urn:test:d1"]
    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2


async def test_a_device_without_a_urn_never_reaches_the_cloud(store, caplog):
    """没有 urn 就取不了表 —— 一帧都不该出门。

    空 urn 照样会被拼成一个缓存键、发一趟分钟级的云端请求，问回来的必然是「解析不了」；
    事件密的设备按帧来一遍就是这个文件的主题（放大）。落下来的参数与「表里没这个 piid」
    同一口径：计 event_arg_unknown，值不进树。
    """
    calls: list[str] = []

    async def _fetch_device_events(urn):
        calls.append(urn)
        return _event_spec()

    writer = _writer(store, events=_fetch_device_events, urn=None)

    await writer.on_device_event(_event())

    assert calls == []  # 一趟都没出门
    assert writer.stats()["event_arg_unknown"] == 1
    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") is MISSING


async def test_a_raising_fetch_is_timed_from_the_failure_not_from_the_wait(
    store, monkeypatch
):
    """取表**抛异常**那条路也要留静默记号，且按失败那一刻计时。

    客户端通常把取表失败吞成空表（走另一支），但抛异常这条路一旦漏掉记号，后果与空表
    那条完全一样：每一帧都再出门一趟。计时点也照旧取失败那一刻 —— 换成「出门那一刻」
    在长取表上会提前到期，等于把静默期缩短。
    """
    clock = _Clock()
    monkeypatch.setattr(state_push, "time", clock)
    calls: list[str] = []

    async def _fetch_device_events(urn):
        calls.append(urn)
        raise RuntimeError("spec 结构不对")

    writer = _writer(store, events=_fetch_device_events)

    await writer.on_device_event(_event())
    await writer.on_device_event(_event())

    assert calls == ["urn:test:d1"]  # 抛异常之后同样不再按帧出门
    assert writer.stats()["event_arg_unknown"] == 2

    clock.now += state_push._EVENT_SPEC_QUIET_SECONDS
    await writer.on_device_event(_event())
    assert len(calls) == 2  # 到期照常重试


async def test_an_event_never_lands_if_the_device_leaves_while_the_spec_is_fetched(
    store,
):
    """取表能飞几十秒，那期间设备可能搬出当前家庭 —— 判据要在写之前再读一次。

    不复查的话，一条在途的推送把删除调和刚删掉的叶子整个写回来，变成谁也删不掉的
    幽灵设备 —— 模块 docstring 里两道闸存在的理由就是这件事。判在取表之前只挡住
    「进来的时候就不在」，「进来时还在、取表回来时已经不在」得靠写入前那一次。
    """
    home: list[tuple[str, ...]] = [("d1",)]
    released = asyncio.Event()

    async def _fetch_device_events(urn):
        await released.wait()
        return _event_spec()

    writer = _writer(store, home=lambda: home[0], events=_fetch_device_events)

    frame = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()  # 让这一帧走到取表那个 await 上
    home[0] = ()  # 取表在飞期间，设备搬出了当前家庭
    released.set()
    await frame

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") is MISSING
    assert writer.stats().get("event_out_of_home") == 1
    assert "event_written" not in writer.stats()


async def test_an_event_never_lands_if_the_scope_is_switched_while_the_spec_is_fetched(
    store,
):
    """换作用域会把树清空。取表在飞期间切了，取表回来就不能再往树里写。

    这一笔要是落进去，写的就是**上一个账号**的事件值 —— 新账号的树里凭空多出一条
    自己设备没有的叶子。同一文件的拉取车道早有这个口径，事件车道此前只有取表前那一次。
    """
    aligned = [True]
    released = asyncio.Event()

    async def _fetch_device_events(urn):
        await released.wait()
        return _event_spec()

    writer = _writer(store, aligned=lambda: aligned[0], events=_fetch_device_events)

    frame = asyncio.ensure_future(writer.on_device_event(_event()))
    await _settle()
    aligned[0] = False  # 取表在飞期间切了作用域，树被清空
    released.set()
    await frame

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") is MISSING
    assert writer.stats().get("event_not_aligned") == 1
    assert "event_written" not in writer.stats()


async def test_the_manager_wires_the_event_lane_into_the_container(store):
    """漏挂这条 listener 的后果是事件推送静默不进容器，生产里看不出来。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []
    proxy._events_listeners = []
    proxy._mips_connect_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1", urn="urn:test:d1")}

    async def _fetch_device_events(urn):
        return _event_spec()

    proxy.devices_in_current_home = devices_in_current_home
    proxy._fetch_device_events = _fetch_device_events

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0

    manager._wire_iot_push()

    for callback in proxy._events_listeners:
        await callback(_event())

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    manager._prop_top_up.deinit()


async def test_an_event_travels_from_the_sdk_callback_into_the_container(store):
    """每段单测都过不代表接上了：这条从 proxy 注册给 SDK 的那个回调开始走。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy
    from miot.types import MIoTDeviceEvent, MIoTEventArg

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []
    proxy._events_listeners = []
    proxy._mips_connect_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1", urn="urn:test:d1")}

    async def _fetch_device_events(urn):
        return _event_spec()

    proxy.devices_in_current_home = devices_in_current_home
    proxy._fetch_device_events = _fetch_device_events

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0
    manager._wire_iot_push()

    await proxy._on_device_event(
        MIoTDeviceEvent(
            did="d1",
            siid=2,
            eiid=1020,
            args=[MIoTEventArg(piid=1, value=2)],
            timestamp_ms=1,
        )
    )

    assert store.get("iot/device/d1/event/2.1020.unlock-operation-position") == 2
    manager._prop_top_up.deinit()
