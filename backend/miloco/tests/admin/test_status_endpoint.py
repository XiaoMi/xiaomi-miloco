"""GET /api/admin/status 的 ``enabled_rules`` 口径。

task 停用后 ``rule.enabled`` 仍是 1 (§19.9), 所以这个数字必须走「有效启用」,
否则运维看到的启用数比实际参与判定的规则数多。代建的达标规则要排掉 —— 用户在
``rule list`` 里看不到它, 计进来的差额无从解释。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router
from miloco.middleware import verify_token
from miloco.rule.schema import RuleDirection


def _rule(direction=RuleDirection.ENTER):
    """只需要 resolved_direction —— 统计口径不看别的字段。"""
    return SimpleNamespace(resolved_direction=direction)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    return TestClient(app)


def test_enabled_rules_uses_effective_enablement(client, monkeypatch):
    """两个口径给出不同的数, 断言取的是有效启用那个。"""
    rule_service = MagicMock()
    rule_service._repo.get_all = MagicMock(return_value=[_rule(), _rule(), _rule()])
    rule_service.get_effectively_enabled_rules = AsyncMock(return_value=[_rule()])

    manager = MagicMock()
    manager.miot_proxy.check_token_valid = AsyncMock(return_value=True)
    manager.rule_service = rule_service
    monkeypatch.setattr("miloco.admin.router.manager", manager)

    body = client.get("/api/admin/status").json()

    assert body["code"] == 0
    assert body["data"]["rule_engine"] == {"total_rules": 3, "enabled_rules": 1}


def test_enabled_rules_excludes_the_auto_built_milestone_rule(client, monkeypatch):
    """代建的达标规则不计入 —— 与 GET /rules 的默认口径一致。"""
    rule_service = MagicMock()
    rule_service._repo.get_all = MagicMock(
        return_value=[
            _rule(RuleDirection.ENTER),
            _rule(RuleDirection.EXIT),
            _rule(RuleDirection.MILESTONE),
        ]
    )
    rule_service.get_effectively_enabled_rules = AsyncMock(
        return_value=[
            _rule(RuleDirection.ENTER),
            _rule(RuleDirection.EXIT),
            _rule(RuleDirection.MILESTONE),
        ]
    )

    manager = MagicMock()
    manager.miot_proxy.check_token_valid = AsyncMock(return_value=True)
    manager.rule_service = rule_service
    monkeypatch.setattr("miloco.admin.router.manager", manager)

    body = client.get("/api/admin/status").json()

    assert body["data"]["rule_engine"]["enabled_rules"] == 2


def test_total_rules_excludes_it_too(client, monkeypatch):
    """两个数同口径。只排启用数的话差额落在总数上, 面板读出来是"有一条规则被停用
    了" —— 而那条规则用户在任何界面都找不到, 正是排它想消灭的那种无从解释的差额。
    """
    rule_service = MagicMock()
    rule_service._repo.get_all = MagicMock(
        return_value=[_rule(RuleDirection.ENTER), _rule(RuleDirection.MILESTONE)]
    )
    rule_service.get_effectively_enabled_rules = AsyncMock(
        return_value=[_rule(RuleDirection.ENTER)]
    )

    manager = MagicMock()
    manager.miot_proxy.check_token_valid = AsyncMock(return_value=True)
    manager.rule_service = rule_service
    monkeypatch.setattr("miloco.admin.router.manager", manager)

    body = client.get("/api/admin/status").json()

    assert body["data"]["rule_engine"] == {"total_rules": 1, "enabled_rules": 1}
