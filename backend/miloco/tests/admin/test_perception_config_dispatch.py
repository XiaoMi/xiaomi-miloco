# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""PUT /api/admin/perception-config 的三档 dispatch 测试 + GET 投影语义测试。

三个感知参数生效路径不同，端点据「新值 != 旧值」分派：
- video_short_edge：每帧实时读 settings，写盘即生效 —— 既不热更也不重启。
- omni_fps：运行时热更 → ``apply_omni_fps_live``（免重建 / 免模型重载 / 不丢 track）。
- window_size：runner 构造期 cache → ``apply_config_restart``（stop→start 重读）。

本测试 mock service 层，只验证端点把哪个参数分派到哪个入口（含「都不变则都不调」）。
另有若干用例不涉分派，只验 GET 投影的取值语义（分辨率档 roundtrip、Smart Crop 双闸）。
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


@pytest.mark.parametrize("bad", [0, 1, 32, 63])
def test_video_short_edge_below_64_rejected(client, bad):
    """短边下限 64。0 曾是「自适应」哨兵，Smart Crop 改走 smart_crop_enabled 独立开关后
    这个哨兵作废——0 会让 _encode_video_mp4 算出 scale=0，必须在 API 层就挡掉。"""
    c, _svc = client
    resp = c.put("/api/admin/perception-config", json={"video_short_edge": bad})
    assert resp.status_code == 422


def test_video_short_edge_fixed_value_roundtrip(client):
    """固定短边写盘 + 回读；热读免重启（既不热更也不重启）。"""
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"video_short_edge": 720})
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()
    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["video_short_edge"] == 720
    assert "resolution_mode" not in data  # 哨兵派生字段已随哨兵一并移除


def test_smart_crop_switch_roundtrip(client):
    """smart_crop_enabled 写进 crop_enhance.user_enabled，GET 回读；热读免重启。

    与 video_short_edge 正交：开裁切不动分辨率，二者各自独立回读。
    """
    c, svc = client
    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_enabled"] is False  # settings.yaml 默认

    resp = c.put(
        "/api/admin/perception-config",
        json={"smart_crop_enabled": True, "video_short_edge": 768},
    )
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()
    data = resp.json()["data"]
    assert data["smart_crop_enabled"] is True
    assert data["video_short_edge"] == 768

    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_enabled"] is True
    assert data["video_short_edge"] == 768


def test_smart_crop_available_reflects_ops_gate(client):
    """smart_crop_available 暴露 ops 灰度闸 crop_enhance.enabled（API 不可写）。

    前端据此置灰开关——available=false 时用户开关即便为 true 后端也不裁，
    必须让前端能如实提示，否则就是「开关开着但没生效」的静默失效。
    """
    c, _svc = client
    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_available"] is True  # settings.yaml 里 ops 闸已放开

    # ops 闸不接受 API 写入：多余字段被 pydantic 忽略，不会渗进配置。
    # 用 False 来验（写入方向与当前值相反，否则「没变」和「写进去了」不可区分）。
    resp = c.put("/api/admin/perception-config", json={"smart_crop_available": False})
    assert resp.status_code == 200
    assert c.get("/api/admin/perception-config").json()["data"]["smart_crop_available"] is True


def test_smart_crop_gates_non_bool_match_runtime(client, tmp_path):
    """闸位写成带引号的字符串时，GET 必须与运行时同判（退禁用），不能靠裸 bool() 判 truthy。

    `enabled: "false"` 是非空字符串：裸 bool() 得 True，而运行时
    ``crop_enhance_config_from_settings`` 的 isinstance(bool) 校验会整份退默认（=禁用）。
    两侧一分裂，前端就拿到 available=true 不置灰、用户开了开关而后端永不裁切，且 admin 侧
    看不到运行时那条 ``crop_enhance_config_bad`` 日志——正是 available 字段本身要防的静默失效。

    user_enabled 这里是真 bool 仍读作 False：闸位不合法时运行时是整份 config 退默认，
    GET 跟着一起退，才叫同源。
    """
    from miloco.config.settings import reset_settings

    c, _svc = client
    (tmp_path / "config.json").write_text(
        _json.dumps(
            {
                "perception": {
                    "engine": {
                        "input": {"omni_fps": 1, "video_short_edge": 512},
                        "crop_enhance": {"enabled": "false", "user_enabled": True},
                    },
                    "collect": {"window_size": 8},
                }
            }
        ),
        encoding="utf-8",
    )
    reset_settings()

    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_available"] is False
    assert data["smart_crop_enabled"] is False
