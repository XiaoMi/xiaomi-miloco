# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""PUT /api/admin/perception-config 的 dispatch 测试 + GET 投影语义测试。

各参数生效路径不同，端点据「新值 != 旧值」分派：
- video_short_edge：每帧实时读 settings，写盘即生效 —— 既不热更也不重启。
- omni_fps：运行时热更 → ``apply_omni_fps_live``（免重建 / 免模型重载 / 不丢 track）。
- window_size：runner 构造期 cache → ``apply_config_restart``（stop→start 重读）。
- smart_crop_enabled / min_suggestion_urgency：均为热读，写盘 + reset_settings 即生效，
  两者都不参与热更 / 重启分派。

本测试 mock service 层，只验证端点把哪个参数分派到哪个入口（含「都不变则都不调」）。
另有若干用例不涉分派，只验 GET 投影的取值语义（分辨率档 roundtrip、Smart Crop 双闸、
min_suggestion_urgency 的默认值与 Literal 校验）。
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
    assert data["smart_crop_enabled"] is True  # settings.yaml 默认两闸全开

    # 往与当前值相反的方向写,否则「没变」和「写进去了」不可区分。
    resp = c.put(
        "/api/admin/perception-config",
        json={"smart_crop_enabled": False, "video_short_edge": 768},
    )
    assert resp.status_code == 200
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()
    data = resp.json()["data"]
    assert data["smart_crop_enabled"] is False
    assert data["video_short_edge"] == 768

    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_enabled"] is False
    assert data["video_short_edge"] == 768


def test_smart_crop_available_reflects_release_gate(client):
    """smart_crop_available 暴露发版级开关 crop_enhance.enabled（API 不可写）。

    前端据此置灰开关——available=false 时用户开关即便为 true 后端也不裁，
    必须让前端能如实提示，否则就是「开关开着但没生效」的静默失效。
    """
    c, _svc = client
    data = c.get("/api/admin/perception-config").json()["data"]
    assert data["smart_crop_available"] is True  # settings.yaml 里发版级开关已放开

    # 发版级开关不接受 API 写入：多余字段被 pydantic 忽略，不会渗进配置。
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


@pytest.mark.parametrize("bad", ["false", True, 1, ["enabled"]])
def test_crop_enhance_non_mapping_keeps_endpoint_alive(client, tmp_path, bad):
    """``crop_enhance`` 整块被写成非 mapping 时,GET 仍须 200 + available=false。

    `or {}` 只吞 falsy:env `MILOCO_PERCEPTION__ENGINE__CROP_ENHANCE=false` 得到的是字符串
    "false"、手写 config.json 可能是 `true` —— 这类 truthy 非 dict 会原样进到 `.items()`
    并抛 AttributeError。而本端点是「配置改错了改回来」的自救入口(PUT 也走同一个投影函数),
    一起 500 就把自救路堵死了。故必须 fail-closed 退默认(=禁用)而不是抛。
    """
    from miloco.config.settings import reset_settings

    c, _svc = client
    (tmp_path / "config.json").write_text(
        _json.dumps(
            {
                "perception": {
                    "engine": {
                        "input": {"omni_fps": 1, "video_short_edge": 512},
                        "crop_enhance": bad,
                    },
                    "collect": {"window_size": 8},
                }
            }
        ),
        encoding="utf-8",
    )
    reset_settings()

    resp = c.get("/api/admin/perception-config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smart_crop_available"] is False
    assert data["smart_crop_enabled"] is False


def test_input_block_non_mapping_keeps_endpoint_alive(client, tmp_path):
    """``perception.engine.input`` 整块被写成非 mapping 时,GET 仍须 200 + 取默认档。

    与上面 crop_enhance 同款坏值、同款连坐:`inp.get(...)` 会抛 AttributeError,而 PUT 先
    投影响应、后 update_shared_config,一抛连写盘都到不了 —— 用户在 UI 里改不回来。
    推理侧不至于崩(_get_video_short_edge 自带 try 回退 512),所以这里是唯一的 500 来源。
    """
    from miloco.config.settings import reset_settings

    c, _svc = client
    (tmp_path / "config.json").write_text(
        _json.dumps(
            {"perception": {"engine": {"input": "512"}, "collect": {"window_size": 8}}}
        ),
        encoding="utf-8",
    )
    reset_settings()

    resp = c.get("/api/admin/perception-config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["video_short_edge"] == 512  # 退默认档,不是 500
    assert data["omni_fps"] == 1


def test_crop_enhance_read_error_degrades_to_unavailable(client, monkeypatch):
    """读取意外抛错(非上面那类已 fail-closed 的情形)→ 报 available=false,不让端点 500。

    投影函数里只有这一步会去解析用户手写结构,单独兜底;GET/PUT 都靠它,500 会连带
    把 PUT 的 deep_merge 自愈也堵掉。
    """
    import miloco.perception.engine.omni.crop_enhance as ce_mod

    def _boom():
        raise RuntimeError("settings exploded")

    # 端点内是函数级 import,故 patch 源模块属性(patch router 模块属性不会生效)
    monkeypatch.setattr(ce_mod, "crop_enhance_config_from_settings", _boom)
    c, _svc = client
    resp = c.get("/api/admin/perception-config")
    assert resp.status_code == 200
    assert resp.json()["data"]["smart_crop_available"] is False


def test_min_urgency_change_writes_config_no_restart(client):
    """min_suggestion_urgency 走热读路径:落盘即生效,不参与 restart 分派。"""
    c, svc = client
    resp = c.put(
        "/api/admin/perception-config",
        json={"min_suggestion_urgency": "medium"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["min_suggestion_urgency"] == "medium"
    svc.apply_omni_fps_live.assert_not_awaited()
    svc.apply_config_restart.assert_not_awaited()


def test_min_urgency_get_returns_current(client):
    c, _ = client
    resp = c.get("/api/admin/perception-config")
    assert resp.status_code == 200
    # 默认值 low(与 backend Literal default 对齐,fixture 未写此字段)
    assert resp.json()["data"]["min_suggestion_urgency"] == "low"


def test_min_urgency_rejects_bogus_value(client):
    """Literal 类型校验:非 low/medium/high 应被 pydantic 拒(422)。"""
    c, _ = client
    resp = c.put(
        "/api/admin/perception-config",
        json={"min_suggestion_urgency": "urgent"},
    )
    assert resp.status_code == 422


def test_min_urgency_mixed_with_omni_fps(client):
    """混合 PUT:同时改 omni_fps + min_urgency,前者走 hot-reload,后者走 hot-read,
    互不干扰——apply_omni_fps_live 被调 1 次(omni_fps),apply_config_restart 不调。"""
    c, svc = client
    resp = c.put(
        "/api/admin/perception-config",
        json={"omni_fps": 2, "min_suggestion_urgency": "high"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["omni_fps"] == 2
    assert data["min_suggestion_urgency"] == "high"
    svc.apply_omni_fps_live.assert_awaited_once_with(2)
    svc.apply_config_restart.assert_not_awaited()
