# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""PUT /api/admin/perception-config 的三档 dispatch 测试。

三个感知参数生效路径不同，端点据「新值 != 旧值」分派：
- video_short_edge：每帧实时读 settings，写盘即生效 —— 既不热更也不重启。
- omni_fps：运行时热更 → ``apply_omni_fps_live``（免重建 / 免模型重载 / 不丢 track）。
- window_size：runner 构造期 cache → ``apply_config_restart``（stop→start 重读）。

本测试 mock service 层，只验证端点把哪个参数分派到哪个入口（含「都不变则都不调」）。
"""
import json as _json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import miloco.admin.router as router_mod
    from miloco.config.settings import reset_settings
    from miloco.middleware import verify_token

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    (tmp_path / "config.json").write_text(
        _json.dumps(
            {
                "perception": {
                    "engine": {"input": {"omni_fps": 1, "video_short_edge": 512}},
                    "collect": {"window_size": 8},
                }
            }
        ),
        encoding="utf-8",
    )
    reset_settings()

    # mock service 层：两条入口都返 True（成功）。perception_service 是 Manager 上的
    # property（无 setter），故整体替换模块级 manager 全局，而非在实例上 setattr。
    svc = MagicMock()
    svc.apply_omni_fps_live = AsyncMock(return_value=True)
    svc.apply_config_restart = AsyncMock(return_value=True)
    fake_manager = MagicMock()
    fake_manager.perception_service = svc
    monkeypatch.setattr(router_mod, "manager", fake_manager)

    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    yield TestClient(app), svc
    reset_settings()


def test_omni_fps_change_hot_reloads_not_restart(client):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"omni_fps": 2})
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_awaited_once_with(2)
    svc.apply_config_restart.assert_not_awaited()


def test_window_size_change_restarts_not_hot_reload(client):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"window_size": 12})
    assert resp.status_code == 200
    svc.apply_config_restart.assert_awaited_once()
    svc.apply_omni_fps_live.assert_not_awaited()


def test_both_change_triggers_both_paths(client):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"omni_fps": 2, "window_size": 12})
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_awaited_once_with(2)
    svc.apply_config_restart.assert_awaited_once()


def test_video_short_edge_only_neither_path(client):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"video_short_edge": 720})
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()


def test_unchanged_omni_fps_is_noop(client):
    """omni_fps 传了但等于当前值（1）→ 不触发热更（按值比对而非字段是否传入）。"""
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"omni_fps": 1})
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()
