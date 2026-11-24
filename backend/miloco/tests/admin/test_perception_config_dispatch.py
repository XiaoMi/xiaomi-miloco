# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""验证感知设置的保存、运行时应用以及 GET 的生效值。"""
import asyncio
import json as _json
from unittest.mock import ANY, AsyncMock, MagicMock

import httpx
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

    svc = MagicMock()
    svc.schedule_available = False
    svc.schedule_mode = "batch"
    svc.window_size_effective = None
    svc.omni_fps_effective = 1

    async def apply(mode, save, *, restart_window, omni_fps):
        save()
        svc.schedule_mode = mode
        if omni_fps is not None:
            svc.omni_fps_effective = omni_fps

    svc.apply_schedule_config = AsyncMock(side_effect=apply)
    fake_manager = MagicMock()
    fake_manager.perception_service = svc
    monkeypatch.setattr(router_mod, "manager", fake_manager)

    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api")
    app.dependency_overrides[verify_token] = lambda: "test-user"
    yield TestClient(app), svc
    reset_settings()


def test_schedule_can_be_enabled_and_disabled(client):
    c, svc = client
    svc.schedule_available = True

    initial = c.get("/api/admin/perception-config").json()["data"]
    assert initial["schedule_mode"] == "batch"

    enabled = c.put("/api/admin/perception-config", json={"schedule_mode": "per_camera"})
    assert enabled.status_code == 200
    data = enabled.json()["data"]
    assert (data["schedule_mode"], data["schedule_effective"]) == (
        "per_camera", "per_camera",
    )

    disabled = c.put("/api/admin/perception-config", json={"schedule_mode": "batch"})
    assert disabled.status_code == 200
    data = disabled.json()["data"]
    assert (data["schedule_mode"], data["schedule_effective"]) == (
        "batch", "batch",
    )
    assert [call.args[0] for call in svc.apply_schedule_config.await_args_list] == ["per_camera", "batch"]


@pytest.mark.parametrize(
    ("settings", "restart_window", "omni_fps"),
    [
        ({"omni_fps": 2}, False, 2),
        ({"window_size": 12}, True, None),
        ({"omni_fps": 2, "window_size": 12}, True, 2),
    ],
)
def test_apply_uses_only_requested_runtime_changes(client, settings, restart_window, omni_fps):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json=settings)
    assert resp.status_code == 200
    assert resp.json()["data"]["restart_ok"] is True
    svc.apply_schedule_config.assert_awaited_once_with(
        "batch", ANY, restart_window=restart_window, omni_fps=omni_fps,
    )


def test_video_short_edge_only_neither_path(client):
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"video_short_edge": 720})
    assert resp.status_code == 200
    svc.apply_schedule_config.assert_not_awaited()


def test_unchanged_omni_fps_is_noop(client):
    """omni_fps 传了但等于当前值（1）→ 不触发热更（按值比对而非字段是否传入）。"""
    c, svc = client
    resp = c.put("/api/admin/perception-config", json={"omni_fps": 1})
    assert resp.status_code == 200
    svc.apply_schedule_config.assert_not_awaited()


@pytest.fixture
def live_settings(client, monkeypatch):
    import miloco.admin.router as router_mod
    from miloco.perception.client import PerceptionEngineProxy
    from miloco.perception.engine.api import PerceptionEngine
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.inference_admission import InferenceAdmission
    from miloco.perception.processor import PipelineProcessor
    from miloco.perception.runner import PerceptionRunner
    from miloco.perception.service import PerceptionService

    c, _ = client
    engine = PerceptionEngine.__new__(PerceptionEngine)
    engine._config = PerceptionConfig()
    engine._base_fps = engine.get_input_config().fps
    engine._tracking_mode = "real"
    engine._tracking_service_kwargs = {}
    engine._tracking_services = {}
    engine._identity_engines = {}
    proxy = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    proxy._engine_lock = asyncio.Lock()
    proxy.perception_engine = engine
    processor = PipelineProcessor.__new__(PipelineProcessor)
    processor._perception_engine_proxy = proxy
    processor._inference_admission = InferenceAdmission()
    processor._result_generation = 0
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._pipeline = processor
    runner._collector = MagicMock()
    runner._is_running = False
    runner._finish_stop = AsyncMock()
    runner._finish_start = AsyncMock()
    runner._schedule_mode = "batch"
    runner._camera_scheduler = None
    service = PerceptionService(MagicMock(), processor, runner, MagicMock(), MagicMock())
    monkeypatch.setattr(router_mod.manager, "perception_service", service)
    return c.app, service, runner, processor, engine


@pytest.mark.parametrize("mode", ["batch", "per_camera"])
@pytest.mark.parametrize("auth_holds_lock", [False, True])
async def test_busy_fps_save_keeps_configuration_and_same_request_retries(
    live_settings, monkeypatch, mode, auth_holds_lock,
):
    from miloco.admin.router import update_shared_config

    app, service, runner, processor, engine = live_settings
    update_shared_config(perception={"schedule": {"mode": mode}})
    runner._schedule_mode = mode
    scheduler = MagicMock() if mode == "per_camera" else None
    runner._camera_scheduler = scheduler
    release = asyncio.Event()
    owner = processor._inference_admission.start(release.wait)
    monkeypatch.setattr("miloco.perception.service._LIFECYCLE_LOCK_WAIT_S", 0)
    monkeypatch.setattr("miloco.perception.processor._INFERENCE_DRAIN_TIMEOUT_S", 0)
    auth = None
    try:
        if auth_holds_lock:
            auth = asyncio.create_task(service.restart_after_auth(lambda: True, wait_seconds=10))
            async with asyncio.timeout(1):
                while processor.accepting_inference:
                    await asyncio.sleep(0)
        settings = "/api/admin/perception-config"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            response = await asyncio.wait_for(c.put(settings, json={"omni_fps": 2}), timeout=1)
            assert response.status_code == 409
            assert response.json()["detail"] == "schedule_stop_deferred"
            blocked = (await c.get(settings)).json()["data"]
            assert (blocked["omni_fps"], blocked["omni_fps_effective"]) == (1, 1)

            release.set()
            await owner
            if auth is not None:
                await asyncio.wait_for(auth, timeout=1)
            response = await c.put(settings, json={"omni_fps": 2})
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["restart_ok"] is True
            assert (data["omni_fps"], data["omni_fps_effective"]) == (2, 2)
            assert engine.get_input_config().omni_fps == 2
            assert runner._camera_scheduler is scheduler
    finally:
        release.set()
        await owner
        if auth is not None:
            await auth


async def test_saved_fps_can_retry_after_runtime_application_failure(live_settings):
    app, _, _, _, engine = live_settings
    tracker = MagicMock()
    tracker.set_fps.side_effect = RuntimeError("simulated tracker refresh error")
    engine._tracking_services["camera"] = tracker
    settings = "/api/admin/perception-config"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.put(settings, json={"omni_fps": 2})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["restart_ok"] is False
        assert (data["omni_fps"], data["omni_fps_effective"]) == (2, 1)
        reloaded = (await c.get(settings)).json()["data"]
        assert (reloaded["omni_fps"], reloaded["omni_fps_effective"]) == (2, 1)

        tracker.set_fps.side_effect = None
        response = await c.put(settings, json={"omni_fps": 2})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["restart_ok"] is True
        assert (data["omni_fps"], data["omni_fps_effective"]) == (2, 2)
        assert tracker.set_fps.call_count == 2


async def test_settings_during_shutdown_return_retryable_response_without_saving(live_settings):
    app, _, _, processor, _ = live_settings
    processor.seal_inference()
    settings = "/api/admin/perception-config"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.put(settings, json={"omni_fps": 2, "window_size": 12})
        assert response.status_code == 409
        assert response.json()["detail"] == "schedule_stop_deferred"
        data = (await c.get(settings)).json()["data"]
        assert (data["omni_fps"], data["omni_fps_effective"], data["window_size"]) == (1, 1, 8)


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
    svc.apply_schedule_config.assert_not_awaited()
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
    svc.apply_schedule_config.assert_not_awaited()
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
    svc.apply_schedule_config.assert_not_awaited()


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
    """混合 PUT 只热更帧率，同时保存按需热读的紧急程度。"""
    c, svc = client
    resp = c.put(
        "/api/admin/perception-config",
        json={"omni_fps": 2, "min_suggestion_urgency": "high"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["omni_fps"] == 2
    assert data["min_suggestion_urgency"] == "high"
    svc.apply_schedule_config.assert_awaited_once_with("batch", ANY, restart_window=False, omni_fps=2)
