# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""事件参数从 SDK 的 spec 解析一路透到 miloco 的 spec entry。

事件推送帧里只有数字 piid，而规则条件项、容器叶、spec 键三处认的是**同一个串**
`event.<siid>.<eiid>.<argname>`，所以 entry 必须同时带上名字和那三个数字：写入侧
靠数字反查名字，校验与渲染侧靠名字那一份。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.miot.client import MiotProxy
from miloco.rule.condition import render_iot_condition
from miot.spec import (
    MIoTSpecDevice,
    MIoTSpecEvent,
    MIoTSpecProperty,
    MIoTSpecService,
    MIoTSpecValueListItem,
    MIoTSpecValueRange,
)


def _parser(proxy: MiotProxy) -> MagicMock:
    """stub 上的 spec 解析器 —— 断言走这里，不透过 ``proxy.miot_client`` 去摸。"""
    return proxy._miot_client.spec_parser


def _proxy_with_spec(device: MIoTSpecDevice | Exception | None) -> MiotProxy:
    kv_repo = SimpleNamespace(
        get=lambda *a, **k: None,
        set=lambda *a, **k: True,
        # MiotService 构造时要拿它建 LRUStore，这里不碰 LRU，给个空壳
        db_connector=SimpleNamespace(
            execute_query=lambda *a, **k: [], execute_update=lambda *a, **k: 0
        ),
    )
    proxy = MiotProxy(uuid="u", redirect_uri="http://x", kv_repo=kv_repo)
    client = MagicMock()
    if isinstance(device, Exception):
        client.spec_parser.parse_async = AsyncMock(side_effect=device)
    else:
        client.spec_parser.parse_async = AsyncMock(return_value=device)
    client.spec_parser.parse_lite_async = AsyncMock(return_value={})
    proxy._miot_client = client
    return proxy


def _arg(iid=1, name="unlock-operation-position", **overrides) -> MIoTSpecProperty:
    fields: dict[str, Any] = {
        "iid": iid,
        "name": name,
        "type": "urn:miot-spec-v2:property:x:00000000:test:1",
        "description": "Unlock Operation Position",
        "description_trans": "开锁操作",
        "format": "uint8",
        "access": ["notify"],
    }
    fields.update(overrides)
    return MIoTSpecProperty(**fields)


def _device(*events, siid=2) -> MIoTSpecDevice:
    return MIoTSpecDevice(
        urn="urn:miot-spec-v2:device:lock:0000A000:test:1",
        name="Door Lock",
        description="Door Lock",
        description_trans="门锁",
        services=[
            MIoTSpecService(
                iid=siid,
                name="lock",
                type="urn:miot-spec-v2:service:lock:00007801:test:1",
                description="Lock",
                description_trans="门锁",
                properties=[
                    MIoTSpecProperty(
                        iid=1,
                        name="on",
                        type="urn:miot-spec-v2:property:on:00000006:test:1",
                        description="On",
                        description_trans="开关",
                        format="bool",
                        access=["read", "write", "notify"],
                    )
                ],
                events=list(events),
            )
        ],
    )


def _event(
    eiid=1020, name="unlock-operation", arguments=(), description_trans="开锁操作"
) -> MIoTSpecEvent:
    return MIoTSpecEvent(
        iid=eiid,
        name=name,
        type="urn:miot-spec-v2:event:unlock-operation:00002808:test:1",
        description="Unlock Operation",
        description_trans=description_trans,
        arguments=list(arguments),
    )


@pytest.mark.asyncio
async def test_an_event_argument_lands_under_its_spec_name():
    """键是 `event.<siid>.<eiid>.<argname>` —— 规则 iid、容器叶、spec 键三处同源。"""
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))

    spec = await proxy._fetch_device_events("urn:test:lock")

    assert list(spec) == ["event.2.1020.unlock-operation-position"]


@pytest.mark.asyncio
async def test_the_entry_carries_the_three_ids_the_writer_reverse_looks_up():
    """推送帧里只有 (siid, eiid, piid)，落叶子要的是名字 —— 少一个就翻不回来。"""
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg(piid=7, iid=7)])))

    entry = (await proxy._fetch_device_events("urn:test:lock"))[
        "event.2.1020.unlock-operation-position"
    ]

    assert (entry["siid"], entry["eiid"], entry["piid"]) == (2, 1020, 7)


@pytest.mark.asyncio
async def test_the_entry_carries_what_the_validator_and_the_renderer_read():
    """与属性 entry 同形：`format` 给校验、`description` 给渲染。

    渲染那句是给住户看的，取的是多语言转换后的文本 —— 只带英文名的话读起来是
    「Door Lock Unlock Operation Position 2」。描述按属性车道的口径拼：服务名打头，
    与前者相同的省掉。
    """
    arg = _arg(
        format="uint8",
        value_list=[
            MIoTSpecValueListItem(name="Inside", value=2, description="室内开锁"),
        ],
        value_range=MIoTSpecValueRange(min=0, max=10, step=1),
        unit="step",
    )
    proxy = _proxy_with_spec(_device(_event(arguments=[arg])))

    entry = (await proxy._fetch_device_events("urn:test:lock"))[
        "event.2.1020.unlock-operation-position"
    ]

    assert entry["format"] == "uint8"
    # 这条 fixture 的参数与事件同名，相邻重复的那段省掉
    assert entry["description"] == "门锁 最近一次开锁操作"
    assert entry["value_list"] == [
        {"name": "Inside", "value": 2, "description": "室内开锁"}
    ]
    assert entry["value_range"] == [0, 10, 1]
    assert entry["unit"] == "step"


@pytest.mark.asyncio
async def test_two_events_sharing_an_argument_do_not_render_the_same_sentence():
    """事件参数是按 piid 引用服务属性的，同一设备的多个事件共用同一个参数是常态。

    无线开关的 click/double-click/long-press 三个事件共享 piid=2，真实门锁
    （lumi-wbmcn1）的 lock-opened/lock-locked 共享 piid=1。参数名只描述「按键类型」，
    不带事件名的话，盯「单击」和盯「长按」的两条规则会落成逐字相同的住户文案 —— 用户
    和 agent 都分不出哪条是哪条。
    """
    proxy = _proxy_with_spec(
        _device(
            _event(eiid=1012, name="click", description_trans="单击", arguments=[_arg()]),
            _event(
                eiid=1014,
                name="long-press",
                description_trans="长按",
                arguments=[_arg()],
            ),
        )
    )

    spec = await proxy._fetch_device_events("urn:test:lock")
    click = spec["event.2.1012.unlock-operation-position"]
    long_press = spec["event.2.1014.unlock-operation-position"]

    assert click["description"] == "门锁 最近一次单击 开锁操作"
    assert long_press["description"] == "门锁 最近一次长按 开锁操作"
    # 真正要钉的是落库那句：两份 entry 喂给同一个渲染函数
    assert render_iot_condition("无线开关", click, "==", 2) != render_iot_condition(
        "无线开关", long_press, "==", 2
    )


@pytest.mark.asyncio
async def test_a_service_named_like_its_event_keeps_the_event_marker():
    """服务名与事件名同文时宁可重复一遍，也不省掉「最近一次」那个标记。

    标记没了这句话读起来就是一个属性条件 —— 而它判的是「最近一次发生时如何」。
    """
    proxy = _proxy_with_spec(_device(_event(description_trans="门锁", arguments=[_arg()])))

    entry = (await proxy._fetch_device_events("urn:test:lock"))[
        "event.2.1020.unlock-operation-position"
    ]

    assert entry["description"] == "门锁 最近一次门锁 开锁操作"


@pytest.mark.asyncio
async def test_an_untranslated_argument_falls_back_to_the_spec_name():
    """标准库没收录这条参数时退回英文原文 —— 这一段是住户句里的谓语，空着就只剩
    「门锁 开锁 = 2」这种读不出盯的是什么的话。
    """
    proxy = _proxy_with_spec(
        _device(
            _event(
                description_trans="开锁",
                arguments=[_arg(description_trans="", description="Lock Action")],
            )
        )
    )

    entry = (await proxy._fetch_device_events("urn:test:lock"))[
        "event.2.1020.unlock-operation-position"
    ]

    assert entry["description"] == "门锁 最近一次开锁 Lock Action"


@pytest.mark.asyncio
async def test_an_argument_without_a_name_is_skipped():
    """空名字落不了叶：写入侧收表时按名判空（`_event_arg_names`），这条参数永远写不
    进去 —— 入了表只会放行一条校验过得去、运行期恒 path_missing 的规则。

    注意**不是**段校验挡下的：空名拼出来的段是 ``2.1020.``，非空、也不含非法字符，
    `validate_segment` 反而放行。判空得自己判。"""
    proxy = _proxy_with_spec(
        _device(_event(arguments=[_arg(iid=1, name=""), _arg(iid=2, name="kept")]))
    )

    spec = await proxy._fetch_device_events("urn:test:lock")

    assert list(spec) == ["event.2.1020.kept"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a/b", id="含斜杠"),
        pytest.param("a*b", id="含星号"),
        # 名字自己没超 128，超的是拼上 `2.1020.` 之后的**整段** —— 判据量的是段不是名字，
        # 这一条专钉那个差（与下面 120 字符那条配对，边界正好落在两段限制之间）
        pytest.param("x" * 125, id="整段超长"),
    ],
)
async def test_an_argument_whose_name_is_not_a_path_segment_is_skipped(name):
    """段名非法的参数同样落不了叶 —— 写入侧拿 ``{siid}.{eiid}.{名字}`` 去 validate_segment
    会被拒（含 '/' 会劈出多一层路径，含 '*' 会被 pattern 语法占用，整段超长被段长上限拒）。

    这几条与空名同类：**入表的判据是「真能落成叶」，不是「像不像个名字」**。放进去的
    后果不对称 —— 校验过、规则建得成，运行期却永远 path_missing，而含 '*' 的那种还会让
    规则侧按路径读叶时每次求值抛一次 ValueError，被 reconcile 每 4 秒刷一条日志。"""
    proxy = _proxy_with_spec(
        _device(_event(arguments=[_arg(iid=1, name=name), _arg(iid=2, name="kept")]))
    )

    spec = await proxy._fetch_device_events("urn:test:lock")

    assert list(spec) == ["event.2.1020.kept"]


@pytest.mark.asyncio
async def test_a_long_name_still_lands_when_the_whole_segment_fits():
    """判据量的是**整段**（siid + eiid + 名字）而不是名字本身：名字 120 字符、加上
    ``2.1020.`` 是 127，还在 128 的上限里，就该照常入表 —— 多拦一条等于凭空少一个
    可用的事件参数，而写入侧本来是收的（上一条钉的是反面：125 字符时整段 132，拦）。"""
    name = "x" * 120
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg(iid=1, name=name)])))

    spec = await proxy._fetch_device_events("urn:test:lock")

    assert list(spec) == [f"event.2.1020.{name}"]


@pytest.mark.asyncio
async def test_an_event_without_arguments_contributes_nothing_but_does_not_break():
    """spec 允许无参事件。它的到达由帧那一侧计数，这里没有可落的东西。"""
    proxy = _proxy_with_spec(_device(_event(), _event(eiid=1021, arguments=[_arg()])))

    spec = await proxy._fetch_device_events("urn:test:lock")

    assert list(spec) == ["event.2.1021.unlock-operation-position"]


@pytest.mark.asyncio
async def test_the_events_need_the_full_parser_not_the_lite_one():
    """lite 视图只遍历 properties/actions，事件一条都不在里面。

    借 lite 那条缓存的话这里会拿到空表 —— 而且是静默的：事件照样到达，只是落不了叶。
    """
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))

    await proxy._fetch_device_events("urn:test:lock")

    _parser(proxy).parse_async.assert_awaited_once()
    _parser(proxy).parse_lite_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_parsed_spec_is_reused_across_calls():
    """按 URN 不变 —— 每次事件都重新解析一遍会把热路径变成一次云端请求。"""
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))
    first = await proxy._fetch_device_events("urn:test:lock")

    second = await proxy._fetch_device_events("urn:test:lock")

    assert _parser(proxy).parse_async.await_count == 1
    assert second == first


@pytest.mark.asyncio
async def test_the_caller_cannot_mutate_the_cache():
    """调用方拿到的是一份拷贝：改了它，下一台同型号设备会拿到被改过的表。

    进和出两侧各拷一次，两处都要钉：缓存里存的是**第一次**取表那份原件，而命中
    那条路返回的是**缓存本体**。少了出口那次拷贝，第一次取表之后每一台同型号设备
    都在共用同一份字典；少了入口那次，第一次那个调用方手上的就是缓存本体。所以这里
    分两段改 —— 改首取结果、再改命中结果，各改一次再看下一次拿到什么。
    """
    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))
    first = await proxy._fetch_device_events("urn:test:lock")
    first["event.2.1020.unlock-operation-position"]["piid"] = 99

    second = await proxy._fetch_device_events("urn:test:lock")
    assert second["event.2.1020.unlock-operation-position"]["piid"] == 1

    # 第二段：这一次改的是「命中那条路」给出来的那份，它走的还是缓存本体
    second["event.2.1020.unlock-operation-position"]["piid"] = 98

    third = await proxy._fetch_device_events("urn:test:lock")
    assert third["event.2.1020.unlock-operation-position"]["piid"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("miot_client not initialized"),  # 未授权那条出口
        ValueError("no such urn"),  # 云端报错那条出口
        None,  # 云端返回空 —— 第三条出口
    ],
)
@pytest.mark.asyncio
async def test_a_failure_is_neither_cached_nor_fatal(failure):
    """一次云端抖动不该让这台设备在整个进程生命周期里都解不出参数名。

    三条失败出口都要走一遍：把它们合成一条断言的话，某一条出口偷偷写进缓存时，
    另一条的用例还能绿。
    """
    proxy = _proxy_with_spec(failure)

    assert await proxy._fetch_device_events("urn:test:lock") == {}

    _parser(proxy).parse_async = AsyncMock(return_value=_device(_event(arguments=[_arg()])))

    assert list(await proxy._fetch_device_events("urn:test:lock")) == [
        "event.2.1020.unlock-operation-position"
    ]


# ── MiotService 那一层：形状与属性那份逐键对齐 ────────────────────────


@pytest.mark.asyncio
async def test_the_service_returns_the_property_shape_keys_that_matter(monkeypatch):
    """调用侧靠「同一个键名取不同命名空间的表」分流：装表的 ``spec`` 与设备的
    ``did`` / ``name`` 必须同名同义。

    整份键集要钉死 —— 多出来的键会被下游当成有值，少一个 ``spec`` 就直接取空表。
    与属性那份的差集是刻意的（省掉 home / room / model / online / category）。
    """
    from miloco.miot.service import MiotService

    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))
    monkeypatch.setattr(
        proxy,
        "get_devices",
        AsyncMock(
            return_value={
                "d1": SimpleNamespace(
                    did="d1", name="玄关门锁", urn="urn:test:lock", home_name="家"
                )
            }
        ),
    )
    service = MiotService(proxy)

    data = await service.get_device_event_spec("d1")

    assert set(data) == {"did", "name", "spec"}
    assert data["did"] == "d1"
    assert data["name"] == "玄关门锁"
    assert list(data["spec"]) == ["event.2.1020.unlock-operation-position"]


@pytest.mark.asyncio
async def test_opting_in_appends_the_event_table_under_its_own_key(monkeypatch):
    """事件表挂在 ``event_spec`` 上，**不并进** ``spec``。

    并进去的话属性表那三个既有消费方（设备目录、控制前参数校验、规则校验）都要各
    加一段跳过逻辑；分开则是一处赋值。
    """
    from miloco.miot.service import MiotService

    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))
    monkeypatch.setattr(
        proxy,
        "get_devices",
        AsyncMock(
            return_value={
                "d1": SimpleNamespace(
                    did="d1",
                    name="玄关门锁",
                    urn="urn:miot-spec-v2:device:lock:0000A000:test:1",
                    home_name="家",
                    room_name="玄关",
                    model="lumi-wbmcn1",
                    online=True,
                    sub_devices=[],
                )
            }
        ),
    )
    monkeypatch.setattr(proxy, "_fetch_device_spec", AsyncMock(return_value={"2.1": {}}))
    service = MiotService(proxy)

    data = await service.get_device_spec("d1", include_events=True)

    assert list(data["spec"]) == ["2.1"]
    assert list(data["event_spec"]) == ["event.2.1020.unlock-operation-position"]
    assert "event.2.1020.unlock-operation-position" not in data["spec"]


@pytest.mark.asyncio
async def test_the_service_does_not_take_the_event_table_by_default(monkeypatch):
    """默认不取这条要落在 service 上，不能只在路由那一层钉。

    路由那条用例把 service 换成桩，验的是默认值有没有传对；service 自己的默认值
    反过来写成 True 的话它照样绿 —— 而属性表那三个既有消费方都走 service，多出来的
    那次 spec 解析（SDK 那侧是另一套 parse、可能三次 30s 超时）就没人挡了。
    """
    from miloco.miot.service import MiotService

    proxy = _proxy_with_spec(_device(_event(arguments=[_arg()])))
    monkeypatch.setattr(
        proxy,
        "get_devices",
        AsyncMock(
            return_value={
                "d1": SimpleNamespace(
                    did="d1",
                    name="玄关门锁",
                    urn="urn:miot-spec-v2:device:lock:0000A000:test:1",
                    home_name="家",
                    room_name="玄关",
                    model="lumi-wbmcn1",
                    online=True,
                    sub_devices=[],
                )
            }
        ),
    )
    monkeypatch.setattr(proxy, "_fetch_device_spec", AsyncMock(return_value={"2.1": {}}))
    fetch_events = AsyncMock(return_value={})
    monkeypatch.setattr(proxy, "_fetch_device_events", fetch_events)
    service = MiotService(proxy)

    data = await service.get_device_spec("d1")

    assert "event_spec" not in data
    fetch_events.assert_not_awaited()


# ── 设备 spec 路由：事件表要显式问才取 ────────────────────────────────


def _spec_route_client(monkeypatch, service):
    """挂上真的 router，只把 manager.miot_service 换成桩。

    走路由而不是直接调 service：默认值是在这一层定死的，直接调 service 的话
    把默认改成取事件表也能绿。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from miloco.middleware import verify_token
    from miloco.miot import router as miot_router

    monkeypatch.setattr(
        miot_router, "manager", SimpleNamespace(miot_service=service), raising=False
    )
    app = FastAPI()
    app.include_router(miot_router.router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    return TestClient(app)


def test_the_route_does_not_take_the_event_table_by_default(monkeypatch):
    """默认不取是这次设计的立足点：属性表有三个既有消费方，它们都不认识事件键，
    取回来等于每个消费方都要跳过一批条目 —— 外加一次额外的 spec 解析。
    """
    service = MagicMock()
    service.get_device_spec = AsyncMock(return_value={"did": "d1", "spec": {}})
    client = _spec_route_client(monkeypatch, service)

    resp = client.get("/api/miot/devices/d1/spec")

    assert resp.status_code == 200
    assert service.get_device_spec.await_args.kwargs["include_events"] is False


def test_the_route_passes_the_opt_in_through(monkeypatch):
    service = MagicMock()
    service.get_device_spec = AsyncMock(return_value={"did": "d1", "spec": {}})
    client = _spec_route_client(monkeypatch, service)

    assert client.get("/api/miot/devices/d1/spec?include_events=true").status_code == 200
    assert service.get_device_spec.await_args.kwargs["include_events"] is True
