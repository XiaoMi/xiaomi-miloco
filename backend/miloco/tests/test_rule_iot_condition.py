# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 条件项的创建 / 更新校验与服务端渲染。

条件项的五步顺序有约束，写错会让其中几步空转 —— 而它们会打出绿灯，让人以为查过了。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.middleware.exceptions import ValidationException
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    ConditionItem,
    Rule,
    RuleCondition,
    RuleConditionDNF,
    RuleDirection,
    RuleUpdate,
)
from miloco.rule.service import RuleService

DID = "1010455124"

# 门锁的门状态：只 notify、枚举取值。真机上这一条读不回值，推送是它唯一的通道。
_DOOR_STATUS = {
    "description": "门 门状态",
    "format": "uint8",
    "readable": False,
    "writeable": False,
    "notify": True,
    # 真机 spec 的形状：name 是英文、description 是多语言转换后的文本。渲染给住户
    # 看的那句要取后者 —— 只写一个字段的话「取错了字段」这件事分不出来。
    "value_list": [
        {"name": "Open", "value": 1, "description": "开"},
        {"name": "Close", "value": 2, "description": "关"},
    ],
}
_BATTERY = {
    "description": "电池 电量",
    "format": "uint8",
    "readable": True,
    "writeable": False,
    "notify": False,
    "value_range": [0, 100, 1],
}
_TEMPERATURE = {
    "description": "温湿度传感器 温度",
    "format": "float",
    "readable": True,
    "writeable": False,
    "notify": True,
    "value_range": [-40, 125, 0.1],
    "unit": "℃",
}
_STEPPED = {
    "description": "风扇 档位",
    "format": "uint8",
    "readable": True,
    "writeable": True,
    "notify": True,
    "value_range": [0, 100, 5],
}
_SERIAL = {
    "description": "设备 序列号",
    "format": "string",
    "readable": True,
    "writeable": False,
    "notify": True,
}
_PRESET = {
    "description": "灯 预设",
    "format": "iids",
    "readable": True,
    "writeable": True,
    "notify": True,
}

_SPEC = {
    "prop.5.1": _DOOR_STATUS,
    "prop.4.1": _BATTERY,
    "prop.2.1": _TEMPERATURE,
    "prop.3.1": _STEPPED,
    "prop.6.1": _SERIAL,
    "prop.7.1": _PRESET,
}


@pytest.fixture
def service(monkeypatch):
    repo = MagicMock()
    repo.create = MagicMock(return_value="new-rule-id")
    repo.get_by_id = MagicMock(return_value=None)
    repo.update = MagicMock(return_value=True)
    repo.exists_by_name = MagicMock(return_value=False)
    repo.list_by_task = MagicMock(return_value=[])
    task_repo = MagicMock()
    task_repo.task_exists = MagicMock(return_value=True)
    task_repo.get_full_view = MagicMock(return_value=None)
    record_service = MagicMock()
    record_service.detect_record_kind = MagicMock(return_value=None)
    record_service.read_duration_target_state = MagicMock(return_value=None)
    proxy = AsyncMock()
    proxy.get_all_scenes = AsyncMock(return_value={})
    runner = RuleRunner(
        rules=[],
        miot_proxy=proxy,
        rule_log_repo=MagicMock(),
        task_record_service=record_service,
    )
    svc = RuleService(
        repo,
        MagicMock(),
        runner,
        proxy,
        task_repo=task_repo,
        task_record_service=record_service,
    )
    svc._get_valid_perceive_device_ids = AsyncMock(return_value=["cam-001"])
    svc.reconfigure_task = lambda *_a, **_kw: None
    svc.sync_rule_actions_to_task = lambda *_a, **_kw: None

    manager = MagicMock()
    manager.miot_service.get_device_spec = AsyncMock(
        return_value={"did": DID, "name": "玄关门锁", "spec": dict(_SPEC)}
    )
    monkeypatch.setattr("miloco.manager.get_manager", lambda: manager)
    svc._manager = manager
    return svc


def _iot_dnf(iid="5.1", op="eq", value=1, did=DID) -> RuleConditionDNF:
    return RuleConditionDNF(
        any_of=[
            [
                ConditionItem(
                    source_type="iot",
                    spec={"did": did, "iid": iid, "op": op, "value": value},
                )
            ]
        ]
    )


def _iot_rule(dnf=None, query="", dids=None, **kw) -> Rule:
    return Rule(
        id="",
        name="门开了",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(
            perceive_device_ids=[] if dids is None else dids, query=query
        ),
        condition_dnf=_iot_dnf() if dnf is None else dnf,
        action_descriptions=["播报门开了"],
        **kw,
    )


# ── 渲染 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_renders_the_query_from_the_predicate(service):
    """落库的就是渲染后的那句 —— 断具体文本，不断「非空」。

    断非空的话，CLI 传的占位空串换成任何一个字符都能让它变绿。
    """
    rule = _iot_rule()

    await service.create_rule(rule)

    stored = service._repo.create.call_args[0][0]
    assert stored.condition.query == "玄关门锁 门 门状态 = 开"


@pytest.mark.asyncio
async def test_placeholder_query_is_not_rejected_as_empty(service):
    """iot 传的空串占位不能被非空校验拦掉 —— 那道闸必须排在渲染之后。

    与「空 query 的 omni rule 被拒」方向相反，两条一起才把顺序钉住。
    """
    await service.create_rule(_iot_rule(query=""))

    assert service._repo.create.called


# ── iot 条件项的静态校验 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_property_without_notify_is_rejected(service):
    """只 read 不 notify 的属性拿不到推送：规则只会在启动那一刻算一次。"""
    with pytest.raises(ValidationException, match="notify"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="4.1", value=50)))


@pytest.mark.asyncio
async def test_iid_missing_from_spec_is_rejected(service):
    """容器里有值但解析出来的 spec 里没有的属性（厂商私有命名空间）建不出规则。

    断错误文案指向「不在 spec 里」，不断「建失败」—— notify 那条校验在同一个输入上
    也会拒，两条判据分不开时改坏任一条都不红。
    """
    with pytest.raises(ValidationException, match="不在设备"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="8.25")))


@pytest.mark.asyncio
async def test_did_with_slash_is_rejected(service):
    """桥接子设备的 did 带 '/'，订阅建不起来，规则落库成功但永远没有输入。"""
    with pytest.raises(ValidationException, match="'/'"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(did="blt.3.abc/2")))


@pytest.mark.asyncio
async def test_empty_spec_reports_the_spec_not_the_iid(service):
    """拿不到 spec 与「这台设备真没属性」在调用侧同形，报的必须是拿不到 spec。

    报「iid 不存在」会把人指去改一个抄对了的 iid。
    """
    service._manager.miot_service.get_device_spec = AsyncMock(
        return_value={"did": DID, "name": "玄关门锁", "spec": {}}
    )
    with pytest.raises(ValidationException, match="拿不到设备"):
        await service.create_rule(_iot_rule())


@pytest.mark.asyncio
async def test_non_scalar_format_is_rejected(service):
    with pytest.raises(ValidationException, match="不是标量"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="7.1", value=1)))


@pytest.mark.asyncio
async def test_string_property_rejects_ordering_op(service):
    with pytest.raises(ValidationException, match="大小比较"):
        await service.create_rule(
            _iot_rule(dnf=_iot_dnf(iid="6.1", op="gt", value="abc"))
        )


@pytest.mark.asyncio
async def test_value_outside_the_enum_is_rejected(service):
    with pytest.raises(ValidationException, match="取值列表"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="5.1", value=9)))


@pytest.mark.asyncio
async def test_bool_value_on_a_numeric_property_is_rejected(service):
    """bool 是 int 的子类，不单独分族的话「开关配了数值阈值」会被静默当成合法。"""
    with pytest.raises(ValidationException, match="类型不兼容"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="3.1", value=True)))


# ── value_range 的步长 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eq_on_an_unreachable_step_is_rejected(service):
    """range [0,100,5] 上 eq 3 恒假：设备永远不上报 3，而规则看起来完全正常。"""
    with pytest.raises(ValidationException, match="步长"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="3.1", value=3)))


@pytest.mark.asyncio
async def test_ne_on_an_unreachable_step_is_rejected(service):
    """同一个值配 ne 更糟：不是不触发，而是一直触发。

    eq 的漏检表现是不触发、ne 是持续误触发，两条都要有。
    """
    with pytest.raises(ValidationException, match="步长"):
        await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="3.1", op="ne", value=3)))


@pytest.mark.asyncio
async def test_ordering_op_does_not_require_the_value_to_be_reachable(service):
    """range [0,100,5] 上 gt 3 是有意义的（设备报 5 时成立）。

    对所有 op 一律查步长会把正常配置拒掉。
    """
    await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="3.1", op="gt", value=3)))

    assert service._repo.create.called


@pytest.mark.asyncio
async def test_ordering_op_with_no_satisfiable_value_is_rejected(service):
    with pytest.raises(ValidationException, match="没有任何取值"):
        await service.create_rule(
            _iot_rule(dnf=_iot_dnf(iid="3.1", op="gt", value=200))
        )


@pytest.mark.asyncio
async def test_float_step_uses_a_tolerance(service):
    """浮点取模的误差会把合法值判成越界：0.1 的步长上 25.3 是合法的。"""
    await service.create_rule(_iot_rule(dnf=_iot_dnf(iid="2.1", value=25.3)))

    assert service._repo.create.called


# ── 结构与源 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_source_type_is_rejected(service):
    dnf = RuleConditionDNF(
        any_of=[[ConditionItem(source_type="presence", spec={"who": "mom"})]]
    )
    with pytest.raises(ValidationException, match="presence"):
        await service.create_rule(_iot_rule(dnf=dnf))


@pytest.mark.asyncio
async def test_negate_is_rejected(service):
    dnf = _iot_dnf()
    dnf.any_of[0][0].negate = True
    with pytest.raises(ValidationException, match="negate"):
        await service.create_rule(_iot_rule(dnf=dnf))


@pytest.mark.asyncio
async def test_non_omni_rule_may_not_carry_perceive_device_ids(service):
    with pytest.raises(ValidationException, match="留空"):
        await service.create_rule(_iot_rule(dids=["cam-001"]))


@pytest.mark.asyncio
async def test_non_omni_rule_may_not_carry_a_free_text_query(service):
    """带了别的非空 query 会被渲染静默覆盖，用户输入无声丢失。"""
    with pytest.raises(ValidationException, match="服务端"):
        await service.create_rule(_iot_rule(query="门开了就播报"))


@pytest.mark.asyncio
async def test_the_original_query_is_checked_before_rendering(service):
    """上一条要在**渲染开着**的情况下仍然报出来。

    把「查原始值」挪到渲染之后时 query 已被覆盖成渲染值，上一条会变绿。
    """
    rule = _iot_rule(query="门开了就播报")
    with pytest.raises(ValidationException):
        await service.create_rule(rule)

    assert not service._repo.create.called


# ── condition_dnf 的补齐（§5.4）────────────────────────────────────────


@pytest.mark.asyncio
async def test_omni_rule_gets_a_backfilled_dnf(service):
    """经 API 建的 omni rule 落库后这一列非空、source_type 是 omni。

    **绕过读取路径直接查落库的那一列**：读侧 resolved_source_type 对 NULL 回退成
    omni，走读取路径断言时补齐做没做都是 "omni"，永绿。
    """
    rule = Rule(
        id="",
        name="有人经过",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人经过"),
        action_descriptions=["播报"],
    )

    await service.create_rule(rule)

    stored = service._repo.create.call_args[0][0]
    item = stored.condition_dnf.any_of[0][0]
    assert item.source_type == "omni"
    assert item.spec == {"perceive_device_ids": ["cam-001"], "query": "有人经过"}


@pytest.mark.asyncio
async def test_omni_dnf_inconsistent_with_condition_is_rejected(service):
    """两份都带且对不上时，无论以哪份为准都是静默覆盖另一份。"""
    dnf = RuleConditionDNF(
        any_of=[
            [
                ConditionItem(
                    source_type="omni",
                    spec={"perceive_device_ids": ["cam-001"], "query": "别的话"},
                )
            ]
        ]
    )
    rule = Rule(
        id="",
        name="有人经过",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人经过"),
        condition_dnf=dnf,
        action_descriptions=["播报"],
    )

    with pytest.raises(ValidationException, match="不一致"):
        await service.create_rule(rule)


@pytest.mark.asyncio
async def test_empty_query_on_an_omni_rule_is_rejected(service):
    """omni 拿到一句空 prompt 永远不触发，而规则看起来配得完全正常。"""
    rule = Rule(
        id="",
        name="空条件",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query=""),
        action_descriptions=["播报"],
    )

    with pytest.raises(ValidationException, match="不能为空"):
        await service.create_rule(rule)


# ── PUT / PATCH（§5.5）────────────────────────────────────────────────


def _stored_iot_rule(query="玄关门锁 门 门状态 = 开") -> Rule:
    rule = _iot_rule(query=query)
    rule.id = "r1"
    return rule


@pytest.mark.asyncio
async def test_put_can_send_back_what_get_returned(service):
    """GET 回来的是渲染后的非空 query，客户端原样 PUT 回来是最自然的用法。

    判据写成「只认空串」时这条会红。
    """
    stored = _stored_iot_rule()
    service._repo.get_by_id = MagicMock(return_value=stored.model_copy(deep=True))

    assert await service.update_rule(stored.model_copy(deep=True)) is True


@pytest.mark.asyncio
async def test_put_still_works_after_the_device_was_renamed(service):
    """设备改名之后 GET 的响应仍要能原样 PUT 回去。

    判据写成「与现在渲染出来的那句比」时这条会红，而上一条仍然绿 —— 两条一起才把
    判据钉在「库里已存的值」上。
    """
    stored = _stored_iot_rule()
    service._repo.get_by_id = MagicMock(return_value=stored.model_copy(deep=True))
    service._manager.miot_service.get_device_spec = AsyncMock(
        return_value={"did": DID, "name": "大门锁", "spec": dict(_SPEC)}
    )

    assert await service.update_rule(stored.model_copy(deep=True)) is True


@pytest.mark.asyncio
async def test_put_rejects_a_different_query(service):
    """与库里已存的不同的文本仍然拒 —— 那道防线一字没松。"""
    stored = _stored_iot_rule()
    service._repo.get_by_id = MagicMock(return_value=stored.model_copy(deep=True))
    incoming = stored.model_copy(deep=True)
    incoming.condition.query = "门开了就播报"

    with pytest.raises(ValidationException, match="服务端"):
        await service.update_rule(incoming)


@pytest.mark.asyncio
async def test_patch_rejects_condition_and_condition_dnf_together(service):
    """两份真相的闸。"""
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())

    with pytest.raises(ValidationException, match="不能同时给"):
        await service.patch_rule(
            "r1",
            RuleUpdate(
                condition={"query": "x"}, condition_dnf=_iot_dnf(iid="5.1", value=2)
            ),
        )


@pytest.mark.asyncio
async def test_patch_can_replace_the_iot_condition(service):
    """iot rule 的唯一编辑入口。改完之后落库的 query 是按新谓词渲染的。"""
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())

    assert await service.patch_rule(
        "r1", RuleUpdate(condition_dnf=_iot_dnf(iid="5.1", value=2))
    )

    stored = service._repo.update.call_args[0][0]
    assert stored.condition.query == "玄关门锁 门 门状态 = 关"


@pytest.mark.asyncio
async def test_patch_replacing_the_condition_clears_the_runtime_state(service):
    """改完条件项要清运行态：旧 did 的残留不能与新 did 的值在 OR 里并存。"""
    stored = _stored_iot_rule()
    service._repo.get_by_id = MagicMock(return_value=stored)
    # runner 里那份是独立对象 —— 生产里它来自 DB 的另一次构造。共用一个对象的话，
    # PATCH 就地改完两边恒等，add_rule 的比较永远看不出变化。
    service._runner.add_rule(stored.model_copy(deep=True))
    await service._runner.update_state("r1", DID, True, "", skip_flicker=True)
    assert DID in service._runner._state["r1"].sources

    await service.patch_rule(
        "r1", RuleUpdate(condition_dnf=_iot_dnf(iid="5.1", value=2))
    )

    assert "r1" not in service._runner._state


@pytest.mark.asyncio
async def test_patch_touching_neither_condition_field_is_allowed(service):
    """只改 --name 的请求两个条件字段都不给，那是正常的。

    措辞写成「恰好给一个」时这条会红 —— 那蕴含了「都不给也拒」。
    """
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())

    assert await service.patch_rule("r1", RuleUpdate(name="新名字")) is True


@pytest.mark.asyncio
async def test_patch_rejects_switching_the_source(service):
    """跨源改直接拒：旧源的字段会留成脏数据。"""
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())
    omni_dnf = RuleConditionDNF(
        any_of=[
            [
                ConditionItem(
                    source_type="omni",
                    spec={"perceive_device_ids": ["cam-001"], "query": "有人"},
                )
            ]
        ]
    )

    with pytest.raises(ValidationException, match="触发源"):
        await service.patch_rule("r1", RuleUpdate(condition_dnf=omni_dnf))


@pytest.mark.asyncio
async def test_put_rejects_switching_the_source(service):
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())
    incoming = _stored_iot_rule()
    incoming.condition_dnf = None
    incoming.condition = RuleCondition(
        perceive_device_ids=["cam-001"], query="有人经过"
    )

    with pytest.raises(ValidationException, match="触发源"):
        await service.update_rule(incoming)


@pytest.mark.asyncio
async def test_put_rejects_changing_the_task(service):
    stored = _stored_iot_rule()
    service._repo.get_by_id = MagicMock(return_value=stored.model_copy(deep=True))
    incoming = stored.model_copy(deep=True)
    incoming.task_id = "t2"

    with pytest.raises(ValidationException, match="移到"):
        await service.update_rule(incoming)


@pytest.mark.asyncio
async def test_patch_rejects_changing_the_task(service):
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())

    with pytest.raises(ValidationException, match="移到"):
        await service.patch_rule("r1", RuleUpdate(task_id="t2"))


# ── seed 的落点（§4.9）────────────────────────────────────────────────


class _RecordingIotSource:
    """只记 seed 调用与它们相对于 reconfigure 的先后。"""

    def __init__(self):
        self.calls: list[str] = []

    def seed_rule(self, rule_id):
        self.calls.append(f"seed_rule:{rule_id}")

    def seed_rules(self, rule_ids):
        self.calls.append(f"seed_rules:{sorted(rule_ids)}")

    def rebuild_index(self):
        pass


@pytest.fixture
def recording(service):
    source = _RecordingIotSource()
    service._runner._iot_source = source
    original = service.reconfigure_task
    service.reconfigure_task = lambda *a, **kw: source.calls.append("reconfigure")
    yield source
    service.reconfigure_task = original


@pytest.mark.asyncio
async def test_create_seeds_after_the_task_topology_is_ready(service, recording):
    """seed 挂在 add_rule 里的话，新建一条「条件已经为真」的 iot rule 会立刻产生
    ENTERED，而此刻 task 还没登记新拓扑、动作快照也还没同步 —— 动作被跳过，之后
    属性不再变化就不会补发。
    """
    await service.create_rule(_iot_rule())

    assert recording.calls == ["reconfigure", "seed_rule:new-rule-id"]


@pytest.mark.asyncio
async def test_re_enabling_a_task_seeds_its_iot_rules(service, recording):
    """停用清掉了条件层状态。属性持续为真的话没有任何变更到达，rule 永远等不到
    ENTERED。

    落点在 apply_task_status 的 active 分支，**不在 reconfigure_task 里
    record_source.arm 那一位** —— 那里被 `runtime_state is ON` 守着，而 suspend
    停用时已经把运行态置成 OFF，重新启用走到那儿时守卫恒假。
    """
    stored = _stored_iot_rule()
    service._runner.add_rule(stored)

    service.apply_task_status("t1", active=True)

    assert "seed_rules:['r1']" in recording.calls
    assert recording.calls.index("reconfigure") < recording.calls.index(
        "seed_rules:['r1']"
    )


@pytest.mark.asyncio
async def test_duration_seconds_on_an_iot_rule_is_rejected(service):
    """滑窗按墙上时钟分 round、断流用 0 补齐，事件驱动的喂法填不满窗口。

    拒绝比静默不触发好：后者用户看不出来，而且规则看起来配得完全正确。
    """
    with pytest.raises(ValidationException, match="duration_seconds"):
        await service.create_rule(_iot_rule(duration_seconds=120))


@pytest.mark.asyncio
async def test_duration_seconds_on_an_omni_rule_is_still_allowed(service):
    """与上一条方向相反：判据写成「一律拒」时这条会红。"""
    rule = Rule(
        id="",
        name="学习了两小时",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="孩子在书桌前"),
        action_descriptions=["播报"],
        duration_seconds=120,
    )

    assert await service.create_rule(rule)


@pytest.mark.asyncio
async def test_patch_cannot_edit_the_query_of_an_iot_rule(service):
    """非 omni rule 的 query 是服务端渲染的占位，放行的话用户输入会被下一次渲染
    静默覆盖。错误文案要指向「改条件走 condition_dnf」，不是「换了触发源」。"""
    service._repo.get_by_id = MagicMock(return_value=_stored_iot_rule())

    with pytest.raises(ValidationException, match="condition_dnf"):
        await service.patch_rule("r1", RuleUpdate(condition={"query": "门开了"}))


@pytest.mark.asyncio
async def test_patch_can_still_edit_the_query_of_an_omni_rule(service):
    """与上一条方向相反：这是住户在抽屉里改触发条件的正规路径。"""
    omni = Rule(
        id="r1",
        name="有人经过",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人经过"),
        action_descriptions=["播报"],
    )
    service._repo.get_by_id = MagicMock(return_value=omni)

    assert await service.patch_rule(
        "r1", RuleUpdate(condition={"query": "有人在门口停留"})
    )

    stored = service._repo.update.call_args[0][0]
    assert stored.condition.query == "有人在门口停留"
    assert stored.condition_dnf.any_of[0][0].spec["query"] == "有人在门口停留"


@pytest.mark.asyncio
async def test_patching_an_omni_query_to_empty_is_rejected(service):
    """空 query 的校验在 create 与 update 两条路上各要有一条 —— PATCH 走的是另一个
    分支（只校验这次真的动了的字段），create 那条绿不代表 update 也拦。"""
    omni = Rule(
        id="r1",
        name="有人经过",
        task_id="t1",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人经过"),
        action_descriptions=["播报"],
    )
    service._repo.get_by_id = MagicMock(return_value=omni)

    with pytest.raises(ValidationException, match="不能为空"):
        await service.patch_rule("r1", RuleUpdate(condition={"query": "  "}))


@pytest.mark.asyncio
async def test_the_rendered_value_uses_the_translated_label(service):
    """真机 spec 里 name 是英文（Open / Cool），description 才是住户看得懂的那个。

    取错字段的话渲染出来是「玄关门锁 门 门状态 = Open」。
    """
    await service.create_rule(_iot_rule())

    assert service._repo.create.call_args[0][0].condition.query.endswith("= 开")


@pytest.mark.asyncio
async def test_the_rendered_value_falls_back_to_the_english_name(service):
    """标准库没收录那条枚举时只有英文名，那是上游数据的事 —— 退回它而不是显示裸数字。"""
    spec = {k: dict(v) for k, v in _SPEC.items()}
    spec["prop.5.1"]["value_list"] = [{"name": "Open", "value": 1}]
    service._manager.miot_service.get_device_spec = AsyncMock(
        return_value={"did": DID, "name": "玄关门锁", "spec": spec}
    )

    await service.create_rule(_iot_rule())

    assert service._repo.create.call_args[0][0].condition.query.endswith("= Open")
