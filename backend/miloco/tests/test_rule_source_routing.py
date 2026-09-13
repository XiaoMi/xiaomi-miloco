# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""源分流收口：一条 rule 归哪个源，由条件项的 source_type 唯一决定。

收口之前分流是排除法 —— omni 靠「摄像头认领 did」、record 靠扫 DNF、milestone 靠
direction，而 record 规则躲开 omni 全靠一个不存在的哨兵 did。判据是「这个 did 不
存在」，不可穷举。
"""

from __future__ import annotations

import pytest
from miloco.rule.iot_source import iot_ref_of
from miloco.rule.record_source import RECORD_SOURCE_DID, record_ref_of
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    ConditionItem,
    Rule,
    RuleCondition,
    RuleConditionDNF,
    RuleMode,
)


def _rule(rule_id="r1", dnf=None, dids=None, **kw):
    return Rule(
        id=rule_id,
        name=rule_id,
        task_id="t1",
        mode=RuleMode.EVENT,
        condition=RuleCondition(
            perceive_device_ids=dids if dids is not None else ["cam1"], query="有人"
        ),
        condition_dnf=dnf,
        **kw,
    )


def _dnf(source_type: str, spec: dict | None = None) -> RuleConditionDNF:
    return RuleConditionDNF(
        any_of=[[ConditionItem(source_type=source_type, spec=spec or {})]]
    )


_IOT_SPEC = {"did": "1010455124", "iid": "5.1", "op": "eq", "value": 1}


# ── resolved_source_type ──────────────────────────────────────────────


def test_source_type_comes_from_the_condition_item():
    assert _rule(dnf=_dnf("iot", _IOT_SPEC)).resolved_source_type == "iot"


def test_missing_dnf_falls_back_to_omni():
    """存量 rule 与经 CLI 新建的 rule 这一列都是 NULL —— 回退是主路径。"""
    assert _rule(dnf=None).resolved_source_type == "omni"


def test_empty_dnf_falls_back_to_omni():
    assert _rule(dnf=RuleConditionDNF(any_of=[])).resolved_source_type == "omni"


def test_source_type_is_not_in_model_dump():
    """普通 @property，不进 dump —— 过滤点必须排在 model_dump 之前。

    进了 dump 的话这条会红，而那说明序列化契约变了（API 响应、前端类型都要跟着看）。
    """
    assert "resolved_source_type" not in _rule(dnf=_dnf("iot", _IOT_SPEC)).model_dump()


# ── 各源的认领方只认自己那一份判据 ────────────────────────────────────


def test_record_ref_is_none_for_an_iot_rule():
    assert record_ref_of(_rule(dnf=_dnf("iot", _IOT_SPEC))) is None


def test_iot_ref_is_none_for_a_record_rule():
    spec = {"task_id": "t1", "kind": "duration", "op": ">="}
    assert iot_ref_of(_rule(dnf=_dnf("record", spec))) is None


def test_iot_ref_reads_the_predicate():
    ref = iot_ref_of(_rule(dnf=_dnf("iot", _IOT_SPEC)))
    assert (ref.did, ref.iid, ref.op, ref.value) == ("1010455124", "5.1", "eq", 1)


# ── 手动触发的 source 键 ──────────────────────────────────────────────


def _runner(rules, monkeypatch):
    monkeypatch.setattr(
        "miloco.task_record.service.TaskRecordService.__init__", lambda self: None
    )
    return RuleRunner(
        rules=rules, miot_proxy=None, rule_log_repo=None, task_record_service=object()
    )


def test_manual_trigger_key_for_iot_rule_is_the_device(monkeypatch):
    """不按源取的话，OR 里会多出一个再也不会被喂的永真键，rule 永久卡在 on。"""
    rule = _rule(dnf=_dnf("iot", _IOT_SPEC), dids=[])
    runner = _runner([rule], monkeypatch)

    assert runner._manual_source_did(rule) == "1010455124"


def test_manual_trigger_key_for_record_rule_is_the_record_slot(monkeypatch):
    spec = {"task_id": "t1", "kind": "duration", "op": ">="}
    rule = _rule(dnf=_dnf("record", spec), dids=[])
    runner = _runner([rule], monkeypatch)

    assert runner._manual_source_did(rule) == RECORD_SOURCE_DID


def test_manual_trigger_key_for_omni_rule_is_the_camera(monkeypatch):
    rule = _rule(dids=["cam-001"])
    runner = _runner([rule], monkeypatch)

    assert runner._manual_source_did(rule) == "cam-001"


# ── 改条件项要清运行态 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_changing_the_iot_device_drops_the_old_source_state(monkeypatch):
    """改 iot 条件项的 did：旧 did 的残留不能留在 OR 里与新 did 的值并存。

    断旧 did 的 PerSourceState 不在了，不断「新 did 能触发」—— 后者在不清运行态时
    也是真的。
    """
    rule = _rule(dnf=_dnf("iot", _IOT_SPEC))
    runner = _runner([rule], monkeypatch)
    await runner.update_state("r1", "1010455124", True, "", skip_flicker=True)
    assert "1010455124" in runner._state["r1"].sources

    moved = _rule(dnf=_dnf("iot", {**_IOT_SPEC, "did": "999"}))
    runner.add_rule(moved)

    assert (
        "1010455124"
        not in runner._state.get("r1", type("S", (), {"sources": {}})).sources
    )


# ── 感知的下发筛选 ────────────────────────────────────────────────────


def test_omni_filter_drops_an_iot_rule():
    from miloco.perception.client import omni_rules_only

    iot = _rule("r-iot", dnf=_dnf("iot", _IOT_SPEC), dids=[])

    assert omni_rules_only([iot]) == []


def test_omni_filter_keeps_an_omni_rule():
    """与上一条方向相反。判据写反（或过滤挪到 dump 之后取到 None）时这条会红。"""
    from miloco.perception.client import omni_rules_only

    omni = _rule("r-omni")

    assert [r.id for r in omni_rules_only([omni])] == ["r-omni"]


def test_omni_filter_drops_a_record_rule():
    from miloco.perception.client import omni_rules_only

    spec = {"task_id": "t1", "kind": "duration", "op": ">="}
    record = _rule("r-rec", dnf=_dnf("record", spec), dids=[])

    assert omni_rules_only([record]) == []


def test_omni_filter_keeps_an_omni_rule_with_no_devices():
    """空设备列表 = 广播到全部摄像头，是 omni 的既有语义。收口不能顺手改掉它。"""
    from miloco.perception.client import omni_rules_only

    broadcast = _rule("r-all", dids=[])

    assert [r.id for r in omni_rules_only([broadcast])] == ["r-all"]


@pytest.mark.asyncio
async def test_effectively_enabled_rules_still_include_iot_rules(monkeypatch):
    """与上一组方向相反：GET /rules 与 admin 要看得到 iot rule。

    两组一起才把过滤点钉在唯一正确的位置 —— 下沉到这里会让接口和 admin 也看不到。
    """
    from miloco.rule.service import RuleService

    svc = RuleService.__new__(RuleService)
    iot = _rule("r-iot", dnf=_dnf("iot", _IOT_SPEC), dids=[])
    svc._repo = type("R", (), {"get_all": staticmethod(lambda enabled_only: [iot])})()
    svc._runner = type("N", (), {"is_task_paused": staticmethod(lambda _t: False)})()

    assert [r.id for r in await svc.get_effectively_enabled_rules()] == ["r-iot"]
