# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""rule 路由的两处口径。

列表的「启用」是派生量: 用户意图 AND task 未停用。直接读 enabled 列会把停用 task
名下的规则报成启用, 和 admin 状态页的 enabled_rules 对不上 —— 用户排查「规则为什么
不触发」时, 正好被这个答案带偏。

建规则的响应要回 direction: mode 只是它在阶段 A 的存储投影, exit / milestone 在
mode 里都存成 event。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.middleware import verify_token
from miloco.rule.router import router
from miloco.rule.schema import Rule, RuleAction, RuleCondition


def _rule(name: str) -> Rule:
    return Rule(
        id=name,
        name=f"[t1] {name}",
        task_id="t1",
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        actions=[RuleAction(did="d1", iid="prop.2.1", value=True)],
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    return TestClient(app)


@pytest.fixture
def rule_service(monkeypatch):
    """两个口径故意返不同的集合, 断言才能分出取的是哪一个。"""
    svc = MagicMock()
    svc.get_all_rules = AsyncMock(return_value=[_rule("r_live"), _rule("r_paused")])
    svc.get_effectively_enabled_rules = AsyncMock(return_value=[_rule("r_live")])
    manager = MagicMock()
    manager.rule_service = svc
    monkeypatch.setattr("miloco.rule.router.manager", manager)
    return svc


def test_enabled_only_excludes_rules_under_a_paused_task(client, rule_service):
    body = client.get("/api/rules?enabled_only=true").json()

    assert [r["id"] for r in body["data"]] == ["r_live"]
    rule_service.get_all_rules.assert_not_called()


def test_the_default_listing_returns_everything(client, rule_service):
    """不带 enabled_only 时不能顺手也过滤掉停用 task 名下的规则。"""
    body = client.get("/api/rules").json()

    assert [r["id"] for r in body["data"]] == ["r_live", "r_paused"]
    rule_service.get_all_rules.assert_awaited_once_with(False)


def test_create_response_reports_the_direction(client, rule_service):
    """只回 mode 的话, --direction exit 建出来的规则回显成 event, 看着像没生效。"""
    rule_service.create_rule = AsyncMock(return_value="new-id")
    payload = _rule("r_new").model_dump(mode="json")
    payload["direction"] = "exit"

    data = client.post("/api/rules", json=payload).json()["data"]

    assert data["direction"] == "exit"
    assert data["mode"] == "event"
