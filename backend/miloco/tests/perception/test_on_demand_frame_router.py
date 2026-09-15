# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for perception/router.py 的 on-demand 取帧端点(图像推理模式产物).

用 FastAPI TestClient 测:
- GET /api/perception/on-demand-logs/{log_id}/frame/{device_id}/{index}
- GET /api/perception/on-demand-logs(数帧 → 取帧 的串联)

与 events_router 的同名端点(events_router.py)是两条独立路由、两套 gating:
那边按 meaningful_events 表的 device_ids,这边按 on-demand 行的 clip_dids。
本文件只覆盖后者。

perception.router 在 import 期就把 manager 绑成了模块全局(get_manager() 的返回值),
故这里整体替换模块属性 —— 与 test_perception_config_dispatch 对 admin router 的做法一致。

verify_token 在 settings.server.token="" 时自动 bypass(默认值,测试无需鉴权).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """每个 case 独立 DB + 独立 FastAPI app(只挂 perception router).

    挂的是**真** PerceptionService + 真 OnDemandLogRepo(只 mock 引擎侧依赖)——
    本文件要验的正是"数帧"与"取帧"两个端点共用同一套落盘布局,换成假 service 就验不到了。
    """
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    connector_module.db_connector = None
    connector_module.init_database()

    from miloco.database.on_demand_log_repo import OnDemandLogRepo
    from miloco.perception.service import PerceptionService

    collector = MagicMock()
    service = PerceptionService(
        collector=collector,
        pipeline=AsyncMock(),
        perception_runner=MagicMock(),
        log_repo=MagicMock(),
        on_demand_log_repo=OnDemandLogRepo(),
    )

    # 同 test_events_router:catch_all middleware 把自定义 HTTPException 转成响应
    # (对齐生产路径),否则 404/410 会以异常形式冒到 TestClient 外。
    import miloco.perception.router as router_mod
    from miloco.middleware.exception_handler import handle_exception

    monkeypatch.setattr(
        router_mod, "manager", MagicMock(perception_service=service)
    )

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(router_mod.router, prefix="/api")

    yield app, service

    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def client(isolated_app):
    app, _ = isolated_app
    return TestClient(app)


@pytest.fixture
def repo(isolated_app):
    return isolated_app[1]._od_log_repo


def _save_frames(log_id: str, device_id: str, n: int) -> None:
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.snapshot_writer import save_event_artifacts

    save_event_artifacts(
        log_id,
        OmniEventArtifacts(
            frames={device_id: [b"\xff\xd8\xff\xe0" + bytes([i]) * 40 for i in range(n)]}
        ),
    )


def _insert_log(repo, *, clip_dids: list[str], clip_kinds: dict[str, str] | None = None) -> str:
    """落一行 on-demand 日志(帧由 _save_frames 写进同一 log_id 目录)."""
    from miloco.perception.schema import OnDemandLogEntry

    log_id = str(uuid.uuid4())
    ok = repo.append(
        OnDemandLogEntry(
            id=log_id,
            timestamp=1751000000000,
            query="谁在客厅?",
            answer="没人",
            sources=list(clip_dids),
            latency_ms=120,
            snapshot_count=len(clip_dids),
            clip_dids=list(clip_dids),
            clip_kinds=clip_kinds or {d: "frames" for d in clip_dids},
        )
    )
    assert ok is True
    return log_id


class TestOnDemandFrameEndpoint:
    """GET /perception/on-demand-logs/{log_id}/frame/{device_id}/{index}."""

    def test_log_not_found_404(self, client):
        resp = client.get("/api/perception/on-demand-logs/nonexistent/frame/cam1/0")
        assert resp.status_code == 404

    def test_device_not_in_clip_dids_404(self, client, repo):
        """不在 clip_dids 里的设备没有产物 → 404(与 clip 端点同款 gating)."""
        log_id = _insert_log(repo, clip_dids=["cam_living_01"])
        resp = client.get(
            f"/api/perception/on-demand-logs/{log_id}/frame/cam_kitchen_01/0"
        )
        assert resp.status_code == 404

    def test_video_log_no_frames_410(self, client, repo):
        """视频模式的日志(盘上是 clip.mp4)没有帧 → 410,前端据此不渲染帧组."""
        log_id = _insert_log(repo, clip_dids=["cam1"], clip_kinds={"cam1": "mp4"})
        resp = client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/0")
        assert resp.status_code == 410

    def test_index_out_of_range_410(self, client, repo):
        log_id = _insert_log(repo, clip_dids=["cam1"])
        _save_frames(log_id, "cam1", 2)
        assert (
            client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/1").status_code
            == 200
        )
        assert (
            client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/2").status_code
            == 410
        )

    def test_negative_index_422(self, client, repo):
        """index 声明为 ge=0 —— 负号不该落到 locate_frame_file 里当文件名拼."""
        log_id = _insert_log(repo, clip_dids=["cam1"])
        resp = client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/-1")
        assert resp.status_code == 422

    def test_non_integer_index_422(self, client, repo):
        log_id = _insert_log(repo, clip_dids=["cam1"])
        resp = client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/abc")
        assert resp.status_code == 422

    def test_found_returns_jpeg(self, client, repo):
        import re

        log_id = _insert_log(repo, clip_dids=["cam1"])
        _save_frames(log_id, "cam1", 2)
        resp = client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/1")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content.startswith(b"\xff\xd8")
        # inline 而非 attachment:前端直接 <img src> 展示,不该触发下载
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("inline")
        assert re.search(
            r'filename="frame001-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.jpg"', disposition
        )

    def test_index_zero_filename_prefix(self, client, repo):
        """序号进文件名且 0-based 补零 —— 下载下来的第 0 张不该叫 frame000 以外的名."""
        import re

        log_id = _insert_log(repo, clip_dids=["cam1"])
        _save_frames(log_id, "cam1", 1)
        resp = client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/0")
        assert resp.status_code == 200
        assert re.search(r'filename="frame000-', resp.headers["content-disposition"])

    def test_device_id_needing_slug_still_serves(self, client, repo):
        """device_id 需要 slug 化时:URL 段用**原值**、盘上目录用 slug,两端各归各的.

        gating 比的是 clip_dids 里的原值,落盘/取盘走 region_slug —— 若把 URL 段也
        预先 slug 化再比,这类设备会永远 404。
        (device_id 里含 '/' 的情形不在本测试范围:那条 URL 会被 Web 框架解码成多段路径,
        整个 /frame 端点族都路由不到,见 events_router 的同款端点。)
        """
        from miloco.perception.snapshot_writer import region_slug

        did = "cam living 01"
        assert region_slug(did) == "cam_living_01"  # slug 规则变了这里要跟着改
        log_id = _insert_log(repo, clip_dids=[did])
        _save_frames(log_id, did, 1)
        resp = client.get(
            f"/api/perception/on-demand-logs/{log_id}/frame/cam%20living%2001/0"
        )
        assert resp.status_code == 200


def test_frame_counts_survive_list_endpoint_roundtrip(client, repo):
    """列表端点就地数帧 → 前端拿到的 frame_counts 决定遍历 0..N-1 拉帧.

    这条把「数帧」与「取帧」两个端点串起来验:一处 off-by-one(比如数成 0)
    会让前端渲染出空帧组,而两端各自单测都是绿的。
    """
    log_id = _insert_log(repo, clip_dids=["cam1"])
    _save_frames(log_id, "cam1", 3)

    data = client.get("/api/perception/on-demand-logs").json()["data"]
    row = next(r for r in data["logs"] if r["id"] == log_id)
    assert row["frame_counts"] == {"cam1": 3}
    assert row["clip_kinds"] == {"cam1": "frames"}

    for i in range(row["frame_counts"]["cam1"]):
        assert (
            client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/{i}").status_code
            == 200
        )
    # 越界那一下:前端遍历只到 N-1,若上面数成了 4 这里会绿、而第 4 张实际拉不到
    assert (
        client.get(
            f"/api/perception/on-demand-logs/{log_id}/frame/cam1/"
            f"{row['frame_counts']['cam1']}"
        ).status_code
        == 410
    )


def test_expired_frames_count_zero_and_frame_410(client, repo):
    """cleanup 清掉帧后 → 列表报 0、取帧 410(前端据此显示"无帧"占位)."""
    log_id = _insert_log(repo, clip_dids=["cam1"])
    _save_frames(log_id, "cam1", 2)

    from miloco.perception.snapshot_writer import get_snapshot_root, region_slug

    for f in (get_snapshot_root() / log_id / region_slug("cam1")).glob("frame_*.jpg"):
        f.unlink()

    data = client.get("/api/perception/on-demand-logs").json()["data"]
    row = next(r for r in data["logs"] if r["id"] == log_id)
    assert row["frame_counts"] == {"cam1": 0}
    assert (
        client.get(f"/api/perception/on-demand-logs/{log_id}/frame/cam1/0").status_code
        == 410
    )


def test_list_row_is_json_serializable_with_frame_counts(client, repo):
    """frame_counts 是运行时注入的额外 key,不能被响应模型吃掉(前端读的就是它)."""
    log_id = _insert_log(repo, clip_dids=["cam1"])
    _save_frames(log_id, "cam1", 2)
    resp = client.get("/api/perception/on-demand-logs")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["data"]["logs"] if r["id"] == log_id)
    assert set(row) >= {"frame_counts", "clip_kinds", "clip_dids"}
    assert isinstance(row["frame_counts"]["cam1"], int)
