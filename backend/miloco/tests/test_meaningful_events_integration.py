# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""端到端集成测试(D3-T17 汇总).

验证完整链路:_persist_meaningful_event → DB → /api/events → /snapshot
对应 task.md T17 中 12+ case 的端到端 sanity check.
"""


import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from miloco.perception.types import (
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
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

    # 拾音默认关(opt-in)后，speech 需相机在拾音白名单才落库/推 SSE：把测试相机加进白名单。
    from miloco.database.kv_repo import KVRepo
    from miloco.manager import get_manager
    from miloco.miot.filter import set_cameras_voice_in_use

    _mgr = get_manager()
    _mgr._kv_repo = KVRepo()
    set_cameras_voice_in_use(_mgr._kv_repo, ["cam_living_01"], True)

    yield tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def client(isolated_env):
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
    return TestClient(app)


def _make_clip(kind: str = "mp4") -> "tuple[bytes, str]":
    """造 (bytes, kind) 元组模拟 omni push_clip_bytes 出来的 artifacts.clips payload."""
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100, kind


def _artifacts(clips: dict | None = None):
    """造 OmniEventArtifacts 实例,只填 clips."""
    from miloco.perception.snapshot_context import OmniEventArtifacts

    return OmniEventArtifacts(clips=clips or {})


@pytest.mark.asyncio
async def test_end_to_end_rule_hit(isolated_env, client):
    """rule_hit → _persist → /api/events 能查到 → /clip 能拉到."""
    from miloco.perception.client import _persist_meaningful_event

    clip_payload = _make_clip()
    result = RealtimePerceptionResult(
        matched_rules=[MatchedRule(rule_id="r1", reason="kitchen_no_one")]
    )
    await _persist_meaningful_event(
        result=result,
        device_ids=["cam_living_01"],
        artifacts=_artifacts({"cam_living_01": clip_payload}),
    )

    # 1. GET /api/events 能拿到
    resp = client.get("/api/events")
    assert resp.status_code == 200
    events = resp.json()["data"]["events"]
    assert len(events) == 1
    eid = events[0]["event_id"]
    assert events[0]["has_rule_hit"] is True
    assert events[0]["device_ids"] == ["cam_living_01"]
    # snapshot_count 语义:成功落 clip 的 device 数(1 个 device → 1)
    assert events[0]["snapshot_count"] == 1

    # 2. GET /clip/{event_id}/{device_id} 能拉到 mp4
    r = client.get(f"/api/events/{eid}/clip/cam_living_01")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == clip_payload[0]

    # 3. 非法 device_id → 404
    r = client.get(f"/api/events/{eid}/clip/cam_other")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_end_to_end_multi_camera(isolated_env, client):
    """多摄像头同窗口 → 1 行 event + N device 各自 clip."""
    from miloco.perception.client import _persist_meaningful_event

    result = RealtimePerceptionResult(
        matched_rules=[MatchedRule(rule_id="r1", reason="x")],
        speeches=[
            Speech(
                needs_response=True, speaker="u", content="开灯", is_complete=True,
                source_device_ids=["cam_living_01"],
            )
        ],
    )
    await _persist_meaningful_event(
        result=result,
        device_ids=["cam_living_01", "cam_kitchen_01"],
        artifacts=_artifacts({
            "cam_living_01": _make_clip(),
            "cam_kitchen_01": _make_clip(),
        }),
    )

    resp = client.get("/api/events")
    events = resp.json()["data"]["events"]
    # 一次推理 = 1 行 event(R10 核心不变量)
    assert len(events) == 1
    e = events[0]
    assert e["has_rule_hit"] is True
    assert e["has_asr"] is True
    assert set(e["device_ids"]) == {"cam_living_01", "cam_kitchen_01"}
    # 2 device 各 1 个 clip → snapshot_count = 2
    assert e["snapshot_count"] == 2

    eid = e["event_id"]
    # 两个摄像头各自路径都能拉到
    for did in ("cam_living_01", "cam_kitchen_01"):
        r = client.get(f"/api/events/{eid}/clip/{did}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"


@pytest.mark.asyncio
async def test_end_to_end_caption_only_no_event(isolated_env, client):
    """纯 caption → 不入表 → /api/events 空."""
    from miloco.perception.client import _persist_meaningful_event
    from miloco.perception.types import CaptionEntry

    result = RealtimePerceptionResult(
        caption=[CaptionEntry(description="平静")]
    )
    await _persist_meaningful_event(
        result=result,
        device_ids=["cam_a"],
        artifacts=_artifacts({"cam_a": _make_clip()}),
    )
    resp = client.get("/api/events")
    assert resp.json()["data"]["events"] == []
