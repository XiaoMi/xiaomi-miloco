# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""档案里的 Base URL 落盘前要去尾斜杠——它是模型身份的一半.

判「是不是同一个 endpoint」的地方有三处:凭证隔离(旧 key 还能不能沿用)、探活归一化、
以及用量记账与清除.前两处比的都是 rstrip("/") 之后的值,写入口若不归一化,住户重打一遍
地址顺手改掉尾斜杠时:凭证层认为 URL 没变、一次调用都不会失败、日志里也没有信号,而记账
从此把同一个 endpoint 拆成两条身份,合计对半分,明细上「只清这一项」也只清得掉其中一行.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "test.db"))
    from miloco.config import reset_settings

    reset_settings()

    # 验证记录读写要用到 manager.kv_repo;端到端测试只挂 router、不过 lifespan,
    # manager 单例的 _kv_repo 平时由 Manager.initialize() 灌注,这里补齐一份绑定到
    # 本用例 MILOCO_HOME 的 KVRepo,避免走到验证记录相关端点时 AttributeError。
    import miloco.database.connector as _connector_module
    from miloco.admin.router import manager as _manager
    from miloco.admin.router import router
    from miloco.database.kv_repo import KVRepo as _KVRepo
    from miloco.middleware import verify_token

    monkeypatch.setattr(_connector_module, "db_connector", None)
    _connector_module.init_database()
    monkeypatch.setattr(_manager, "_kv_repo", _KVRepo(), raising=False)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    yield TestClient(app)
    reset_settings()


@pytest.mark.parametrize("sent", ["https://api.example/v1/", "https://api.example/v1"])
def test_profile_base_url_is_stored_without_trailing_slash(client, sent):
    """带不带尾斜杠都存成同一个值,否则同一个 endpoint 会裂成两条记账身份。"""
    # 端点会先探活(防绕过 web「测试连接」的调用方)，这里只关心落盘的归一化
    with patch(
        "miloco.admin.router._probe.probe_omni",
        new=AsyncMock(return_value={"ok": True}),
    ):
        r = client.put(
            "/api/admin/omni-config",
            json={"label": "p1", "base_url": sent, "model": "m", "api_key": "sk-1"},
        )
    assert r.status_code == 200, r.text
    payload = r.json()["data"]
    stored = [p for p in payload["profiles"] if p["label"] == "p1"]
    assert stored, f"档案没写进去: {payload}"
    assert stored[0]["base_url"] == "https://api.example/v1", (
        f"落盘的地址没去尾斜杠: {stored[0]['base_url']!r}"
    )
