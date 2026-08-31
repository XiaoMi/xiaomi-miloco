"""GET /api/admin/status 的 ``enabled_rules`` 口径。

task 停用后 ``rule.enabled`` 仍是 1 (§19.9), 所以这个数字必须走「有效启用」,
否则运维看到的启用数比实际参与判定的规则数多。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router
from miloco.middleware import verify_token


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    return TestClient(app)


def test_enabled_rules_uses_effective_enablement(client, monkeypatch):
    """两个口径给出不同的数, 断言取的是有效启用那个。"""
    rule_service = MagicMock()
    rule_service._repo.count_all.return_value = 3
    rule_service._repo.count_enabled.return_value = 3
    rule_service.get_effectively_enabled_rules = AsyncMock(return_value=[object()])

    manager = MagicMock()
    manager.miot_proxy.check_token_valid = AsyncMock(return_value=True)
    manager.rule_service = rule_service
    monkeypatch.setattr("miloco.admin.router.manager", manager)

    body = client.get("/api/admin/status").json()

    assert body["code"] == 0
    assert body["data"]["rule_engine"] == {"total_rules": 3, "enabled_rules": 1}
