# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 触发源的整链：建规则 → 属性变化 → 动作真的执行。

单元用例各自钉住一段，这里钉的是接线本身 —— 每一段都对而中间少接一根线的话，
上面那些照样全绿。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import Rule, RuleCondition, RuleDirection
from miloco.rule.service import RuleService
from miloco.state import StateStore

DID = "1010455124"
_SPEC = {
    "prop.5.1": {
        "description": "门 门状态",
        "format": "uint8",
        "notify": True,
        "value_list": [{"name": "开", "value": 1}, {"name": "关", "value": 2}],
    }
}


@pytest.fixture
async def wired(monkeypatch):
    """一条真容器 + 真 runner + 真 service，只把 DB 和米家换成替身。"""
    store = StateStore()
    store.start()

    created: dict[str, Rule] = {}
    repo = MagicMock()
    repo.exists_by_name = MagicMock(return_value=False)
    repo.list_by_task = MagicMock(side_effect=lambda _t: list(created.values()))

    def _create(rule):
        rule.id = "r1"
        created["r1"] = rule
        return "r1"

    repo.create = MagicMock(side_effect=_create)
    repo.get_by_id = MagicMock(side_effect=lambda rid: created.get(rid))

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
    runner.attach_iot_source(store)
    service = RuleService(
        repo,
        MagicMock(),
        runner,
        proxy,
        task_repo=task_repo,
        task_record_service=record_service,
    )
    service.reconfigure_task = lambda *_a, **_kw: None
    service.sync_rule_actions_to_task = lambda *_a, **_kw: None
    runner.set_task_actions("door_alert", {"on_enter_desc": "播报门开了"})

    manager = MagicMock()
    manager.miot_service.get_device_spec = AsyncMock(
        return_value={"did": DID, "name": "玄关门锁", "spec": dict(_SPEC)}
    )
    monkeypatch.setattr("miloco.manager.get_manager", lambda: manager)

    fired: list[str] = []

    async def _record(rule, event, *_a, **_kw):
        fired.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    yield store, service, runner, fired
    await runner.iot_source.stop()
    store.stop()


def _iot_rule(value=1) -> Rule:
    from miloco.rule.schema import ConditionItem, RuleConditionDNF

    return Rule(
        id="",
        name="门开了",
        task_id="door_alert",
        direction=RuleDirection.ENTER,
        condition=RuleCondition(perceive_device_ids=[], query=""),
        condition_dnf=RuleConditionDNF(
            any_of=[
                [
                    ConditionItem(
                        source_type="iot",
                        spec={"did": DID, "iid": "5.1", "op": "eq", "value": value},
                    )
                ]
            ]
        ),
        action_descriptions=["播报门开了"],
    )


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_property_change_fires_the_rule(wired):
    """整链：MQTT 推送写容器 → 源订阅命中 → 求值 → 条件层边沿 → 动作。"""
    store, service, _runner, fired = wired
    store.set(f"iot/device/{DID}/status/online", True, source="iot_align")
    store.set(f"iot/device/{DID}/prop/5.1", 2, source="iot_align")
    await service.create_rule(_iot_rule())
    await _settle()
    assert fired == []

    store.set(f"iot/device/{DID}/prop/5.1", 1, source="iot_push")
    await _settle()

    assert fired == ["ENTERED"]


@pytest.mark.asyncio
async def test_a_rule_created_on_an_already_true_condition_fires_at_once(wired):
    """运行中建一条「当前属性已经为真」的规则：容器里不会有新变更。

    只重建索引不 seed 的话它永远停在未就绪；seed 挂在 add_rule 里（reconfigure 之前）
    的话边沿产生了但动作被跳过 —— 所以断的是动作真的执行了。
    """
    store, service, _runner, fired = wired
    store.set(f"iot/device/{DID}/status/online", True, source="iot_align")
    store.set(f"iot/device/{DID}/prop/5.1", 1, source="iot_align")

    await service.create_rule(_iot_rule())
    await _settle()

    assert fired == ["ENTERED"]


@pytest.mark.asyncio
async def test_the_query_stored_is_the_rendered_sentence(wired):
    """住户日志和抽屉里显示的就是这一句 —— 展示层不现算。"""
    store, service, _runner, _fired = wired
    store.set(f"iot/device/{DID}/status/online", True, source="iot_align")
    store.set(f"iot/device/{DID}/prop/5.1", 2, source="iot_align")

    await service.create_rule(_iot_rule())

    assert service._repo.create.call_args[0][0].condition.query == (
        "玄关门锁 门 门状态 = 开"
    )


@pytest.mark.asyncio
async def test_an_offline_device_does_not_fire(wired):
    """离线时属性值即使为真也不算数 —— 那是「不知道」，不驱动任何东西。"""
    store, service, _runner, fired = wired
    store.set(f"iot/device/{DID}/status/online", False, source="iot_align")
    store.set(f"iot/device/{DID}/prop/5.1", 1, source="iot_align")

    await service.create_rule(_iot_rule())
    await _settle()

    assert fired == []


@pytest.mark.asyncio
async def test_deleting_the_rule_stops_it_from_firing(wired):
    """删掉之后反查索引里不能留下它的条目 —— 增量残留会拿已删规则触发。"""
    store, service, runner, fired = wired
    store.set(f"iot/device/{DID}/status/online", True, source="iot_align")
    store.set(f"iot/device/{DID}/prop/5.1", 2, source="iot_align")
    await service.create_rule(_iot_rule())
    await _settle()
    runner.remove_rule("r1")
    service._repo.get_by_id = MagicMock(return_value=None)

    store.set(f"iot/device/{DID}/prop/5.1", 1, source="iot_push")
    await _settle()

    assert fired == []
    # 索引里也不能留条目。留着不会误触发（求值时取不到 rule 就跳过），但它是一条随
    # 删除次数单调增长的残留，而且会让「有几条规则挂在这条路径上」这个读数说假话。
    assert runner.iot_source.diagnostics()["indexed_rules"] == 0


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "t.db"))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield
    reset_settings()


@pytest.mark.asyncio
async def test_init_rule_service_starts_the_iot_source(real_db):
    """接线本身：给了容器就要把源建起来并启动。

    少了这一根线的话上面所有用例照样全绿（它们自己调 attach_iot_source），而生产里
    整条 iot 链根本不存在 —— 而且是静默的。
    """
    from miloco.rule.service import init_rule_service

    store = StateStore()
    store.start()
    try:
        service = await init_rule_service(AsyncMock(), store)

        assert service.iot_source is not None
        assert service.iot_source.diagnostics()["consumer_alive"] is True
    finally:
        if service.iot_source is not None:
            await service.iot_source.stop()
        store.stop()


@pytest.mark.asyncio
async def test_init_rule_service_without_a_store_leaves_the_source_absent(real_db):
    """与上一条方向相反：不给容器时不能凭空造一个源出来。"""
    from miloco.rule.service import init_rule_service

    service = await init_rule_service(AsyncMock())

    assert service.iot_source is None
