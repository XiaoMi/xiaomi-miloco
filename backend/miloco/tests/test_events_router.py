# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for events_router(D3-T10).

用 FastAPI TestClient 测两个 endpoint:
- GET /api/events
- GET /api/events/{event_id}/clip/{device_id}

verify_token 在 settings.server.token="" 时自动 bypass(默认值,测试无需鉴权).
"""

import time
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """每个 case 独立 DB + 独立 FastAPI app(只挂 events_router,不依赖其它服务)."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    # 构造一个仅含 events_router 的 minimal app(避免拉起完整 lifespan)
    # 同时挂上 catch_all middleware,把自定义 HTTPException 转成 HTTP 响应(对齐生产路径)
    from miloco.middleware.exception_handler import handle_exception
    from miloco.perception.events_router import router as events_router

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(events_router, prefix="/api")

    yield app, tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def client(isolated_app):
    app, _ = isolated_app
    return TestClient(app)


@pytest.fixture
def dao(isolated_app):
    from miloco.manager import get_manager

    return get_manager().meaningful_events_dao


def _insert(dao, **kwargs) -> str:
    eid = str(uuid.uuid4())
    defaults = dict(
        event_id=eid,
        timestamp=int(time.time() * 1000),
        text="t",
        payload_json="{}",
        has_rule_hit=False,
        has_suggestion=False,
        has_asr=False,
        device_ids=["cam_living_01"],
    )
    defaults.update(kwargs)
    assert dao.insert(**defaults) is True
    return eid


class TestListEventsEndpoint:
    def test_empty_returns_empty_list(self, client):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 0
        assert data["data"]["events"] == []

    def test_returns_event_with_correct_fields(self, client, dao):
        eid = _insert(
            dao,
            has_rule_hit=True,
            device_ids=["cam_a", "cam_b"],
        )
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()["data"]["events"]
        assert len(events) == 1
        e = events[0]
        assert e["event_id"] == eid
        assert e["has_rule_hit"] is True
        assert e["device_ids"] == ["cam_a", "cam_b"]
        # 内部字段不返
        assert "payload_json" not in e
        assert "schema_version" not in e
        assert "created_at" not in e

    def test_pagination(self, client, dao):
        for i in range(5):
            _insert(dao, timestamp=1000 + i)
        resp = client.get("/api/events?limit=2&offset=0")
        assert len(resp.json()["data"]["events"]) == 2
        resp = client.get("/api/events?limit=2&offset=4")
        assert len(resp.json()["data"]["events"]) == 1

    def test_time_window(self, client, dao):
        _insert(dao, timestamp=1000)
        eid_mid = _insert(dao, timestamp=2000)
        _insert(dao, timestamp=4000)
        resp = client.get("/api/events?since=1500&before=3000")
        events = resp.json()["data"]["events"]
        assert [e["event_id"] for e in events] == [eid_mid]

    def test_invalid_limit_422(self, client):
        """limit 超界 → FastAPI 自动 422."""
        resp = client.get("/api/events?limit=999")
        assert resp.status_code == 422

    def test_invalid_negative_offset_422(self, client):
        resp = client.get("/api/events?offset=-1")
        assert resp.status_code == 422

    def test_unknown_query_params_ignored(self, client, dao):
        """`?type=foo` 等未声明参数应被忽略(B 不接受 type 过滤)."""
        _insert(dao)
        resp = client.get("/api/events?type=rule&random=x")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["events"]) == 1


class TestGetClipEndpoint:
    def test_event_not_found_404(self, client):
        resp = client.get("/api/events/nonexistent/clip/cam_a")
        assert resp.status_code == 404

    def test_device_not_in_event_404(self, client, dao):
        eid = _insert(dao, device_ids=["cam_living_01"])
        resp = client.get(f"/api/events/{eid}/clip/cam_kitchen_01")
        assert resp.status_code == 404

    def test_event_exists_but_file_missing_410(self, client, dao):
        """event 存在 + device_id 合法,但未落盘 → 410 Gone."""
        eid = _insert(dao, device_ids=["cam_living_01"])
        resp = client.get(f"/api/events/{eid}/clip/cam_living_01")
        assert resp.status_code == 410

    def test_found_returns_mp4(self, client, dao):
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_living_01"])
        clip = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200  # 类 mp4 bytes
        save_event_artifacts(
            eid, OmniEventArtifacts(clips={"cam_living_01": (clip, "mp4")})
        )

        resp = client.get(f"/api/events/{eid}/clip/cam_living_01")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert resp.content == clip

    def test_found_supports_range_206(self, client, dao):
        """FileResponse 应支持 Range 请求,返 206 Partial Content
        (<video> scrubber seek 依赖这个)."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_living_01"])
        clip = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 500  # ≥ 区分前后段
        save_event_artifacts(
            eid, OmniEventArtifacts(clips={"cam_living_01": (clip, "mp4")})
        )

        # 拉取后半段
        resp = client.get(
            f"/api/events/{eid}/clip/cam_living_01",
            headers={"Range": "bytes=100-199"},
        )
        assert resp.status_code == 206
        assert resp.headers["content-type"] == "video/mp4"
        assert resp.content == clip[100:200]
        assert resp.headers.get("content-range", "").startswith("bytes 100-199/")

    def test_found_m4a_audio_mp4_media_type(self, client, dao):
        """audio-only 路径产物 clip.m4a → Content-Type=audio/mp4."""
        from miloco.perception.snapshot_writer import (
            get_snapshot_root,
            region_slug,
        )

        eid = _insert(dao, device_ids=["cam_audio_01"])
        # 手写 m4a 占位(snapshot_writer 当前不区分,这里测 service 探测顺序 + 路由 media_type)
        device_dir = get_snapshot_root() / eid / region_slug("cam_audio_01")
        device_dir.mkdir(parents=True, exist_ok=True)
        clip = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100
        (device_dir / "clip.m4a").write_bytes(clip)

        resp = client.get(f"/api/events/{eid}/clip/cam_audio_01")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mp4"
        assert resp.content == clip

    def test_content_disposition_filename_uses_event_timestamp(self, client, dao):
        """response 应带 Content-Disposition: inline; filename=clip-YYYY-MM-DD-HH-MM-SS.m4a
        — 文件名按 meaningful_events.timestamp 本地时间命名,用户保存后一眼能看出
        "哪天发生的什么事件"(比 event_id 前 8 位 UUID 字符串友好得多).
        inline 保证 <audio>/<video> 仍能页面内播放,filename 让用户右键"另存为"
        默认带 .m4a/.mp4 后缀.
        """
        from datetime import datetime

        from miloco.perception.snapshot_writer import (
            get_snapshot_root,
            region_slug,
        )

        # 用显式 timestamp 确认文件名按事件时间命名而非 event_id
        ts_ms = 1717741883000  # 2024-06-07 14:31:23 UTC = 2024-06-07 22:31:23 +08
        eid = _insert(dao, device_ids=["cam_audio_01"], timestamp=ts_ms)
        device_dir = get_snapshot_root() / eid / region_slug("cam_audio_01")
        device_dir.mkdir(parents=True, exist_ok=True)
        (device_dir / "clip.m4a").write_bytes(
            b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100
        )

        resp = client.get(f"/api/events/{eid}/clip/cam_audio_01")
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        # inline 关键:不触发浏览器下载弹窗,<audio>/<video> 仍 inline 播放
        assert cd.startswith("inline;"), f"expected inline disposition, got: {cd}"
        # 文件名按本地时区时间格式(YYYY-MM-DD-HH-MM-SS),全连字符避开 Windows 非法 ':'
        expected_time = datetime.fromtimestamp(ts_ms / 1000).strftime(
            "%Y-%m-%d-%H-%M-%S"
        )
        expected_filename = f"clip-{expected_time}.m4a"
        assert expected_filename in cd, f"expected {expected_filename!r} in {cd!r}"

    def test_content_disposition_mp4_extension(self, client, dao):
        """视频路径产物 clip.mp4 → Content-Disposition filename 后缀是 .mp4
        + 按 event timestamp 命名."""
        from datetime import datetime

        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        ts_ms = 1717741900000
        eid = _insert(dao, device_ids=["cam_living_01"], timestamp=ts_ms)
        save_event_artifacts(
            eid,
            OmniEventArtifacts(
                clips={"cam_living_01": (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200, "mp4")}
            ),
        )

        resp = client.get(f"/api/events/{eid}/clip/cam_living_01")
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert cd.startswith("inline;")
        expected_time = datetime.fromtimestamp(ts_ms / 1000).strftime(
            "%Y-%m-%d-%H-%M-%S"
        )
        assert f"clip-{expected_time}.mp4" in cd
