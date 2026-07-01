# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for EventsService(D3-T9).

覆盖:
- list_events:分页 / 时间窗 / DESC 排序 / 空 DB
- locate_clip 三种状态:found / gone(文件已删但 event 存在) / not_found(event 不存在 / device_id 不在 device_ids 内)
- Pydantic 序列化:不含 payload_json / schema_version / created_at
"""

import time
import uuid

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
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

    yield tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def svc(isolated_db):
    """通过 manager singleton 拿 service(对齐生产路径)."""
    from miloco.manager import get_manager

    return get_manager().events_service


@pytest.fixture
def dao(isolated_db):
    from miloco.manager import get_manager

    return get_manager().meaningful_events_dao


def _insert(dao, *, has_rule_hit=False, device_ids=None, timestamp=None) -> str:
    eid = str(uuid.uuid4())
    # 默认 timestamp 比"现在"早 1 秒,避免与 list_events 默认 before=now() 的同毫秒边界(<不含)冲突
    ts = timestamp if timestamp is not None else int((time.time() - 1) * 1000)
    ok = dao.insert(
        event_id=eid,
        timestamp=ts,
        text=f"text for {eid}",
        payload_json='{"caption": []}',
        has_rule_hit=has_rule_hit,
        has_suggestion=False,
        has_asr=False,
        device_ids=device_ids if device_ids is not None else ["cam_living_01"],
    )
    assert ok
    return eid


@pytest.mark.asyncio
class TestListEvents:
    async def test_empty(self, svc):
        assert await svc.list_events() == []

    async def test_returns_pydantic_models(self, svc, dao):
        eid = _insert(dao, has_rule_hit=True, device_ids=["cam_a", "cam_b"])
        events = await svc.list_events()
        assert len(events) == 1
        e = events[0]
        # Pydantic 模型字段
        assert e.event_id == eid
        assert e.has_rule_hit is True
        assert e.device_ids == ["cam_a", "cam_b"]

    async def test_excludes_internal_fields(self, svc, dao):
        """响应不应含 payload_json / schema_version / created_at."""
        _insert(dao)
        events = await svc.list_events()
        dumped = events[0].model_dump()
        assert "payload_json" not in dumped
        assert "schema_version" not in dumped
        assert "created_at" not in dumped
        # 但应含业务字段
        assert "event_id" in dumped
        assert "device_ids" in dumped

    async def test_timestamp_desc_order(self, svc, dao):
        eid1 = _insert(dao, timestamp=1000)
        eid2 = _insert(dao, timestamp=3000)
        eid3 = _insert(dao, timestamp=2000)
        events = await svc.list_events()
        assert [e.event_id for e in events] == [eid2, eid3, eid1]

    async def test_time_window(self, svc, dao):
        _insert(dao, timestamp=1000)
        eid_mid = _insert(dao, timestamp=2000)
        _insert(dao, timestamp=4000)
        events = await svc.list_events(since=1500, before=3000)
        assert [e.event_id for e in events] == [eid_mid]

    async def test_pagination(self, svc, dao):
        for i in range(5):
            _insert(dao, timestamp=1000 + i)
        page1 = await svc.list_events(limit=2, offset=0)
        page2 = await svc.list_events(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # 不重叠
        assert {e.event_id for e in page1}.isdisjoint(
            {e.event_id for e in page2}
        )

    async def test_clip_kind_none_when_no_file(self, svc, dao):
        """未落盘 → clip_kind=None(metadata-only 事件 / 已被 cleanup 清掉)."""
        _insert(dao, device_ids=["cam_a"])
        events = await svc.list_events()
        assert events[0].clip_kind is None

    async def test_clip_kind_mp4_when_video_clip(self, svc, dao):
        """落 clip.mp4 → clip_kind='mp4'(UI 显 🎬)."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_a"])
        save_event_artifacts(
            eid,
            OmniEventArtifacts(
                clips={"cam_a": (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200, "mp4")}
            ),
        )
        events = await svc.list_events()
        assert events[0].event_id == eid
        assert events[0].clip_kind == "mp4"

    async def test_clip_kind_m4a_when_audio_only(self, svc, dao):
        """落 clip.m4a → clip_kind='m4a'(UI 显 🎤 音频).回归 18:42:05 误判 bug."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_a"])
        save_event_artifacts(
            eid,
            OmniEventArtifacts(
                clips={"cam_a": (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100, "m4a")}
            ),
        )
        events = await svc.list_events()
        assert events[0].event_id == eid
        assert events[0].clip_kind == "m4a"

    async def test_clip_kind_pydantic_literal_rejects_unknown(self):
        """S3 防御:Pydantic clip_kind 必须是 Literal['mp4','m4a']|None,拒绝其它字符串.

        防止未来有人把字段类型回滚到 str|None,绕过前端 isAudioOnly=='m4a' 的严格比较
        → 出现 'M4A' / 'mov' / 'webm' 等非法 kind 导致 UI 静默走错分支(回归 18:42:05).
        """
        import pydantic
        from miloco.perception.schema import MeaningfulEvent

        # 合法值不抛
        MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind="mp4")
        MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind="m4a")
        MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind=None)
        # 非法值抛 ValidationError
        with pytest.raises(pydantic.ValidationError):
            MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind="MP4")
        with pytest.raises(pydantic.ValidationError):
            MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind="webm")
        with pytest.raises(pydantic.ValidationError):
            MeaningfulEvent(event_id="x", timestamp=0, text="t", clip_kind="")


@pytest.mark.asyncio
class TestLocateClip:
    async def test_event_not_found_returns_not_found(self, svc):
        status, path, media_type, ts = await svc.locate_clip(
            "does-not-exist", "cam_a"
        )
        assert status == "not_found"
        assert path is None
        assert media_type is None
        assert ts is None

    async def test_device_id_not_in_event_returns_not_found(self, svc, dao):
        """event 存在,但 device_id 不在 device_ids 列表 → 404 not_found."""
        eid = _insert(dao, device_ids=["cam_living_01"])
        status, path, media_type, ts = await svc.locate_clip(
            eid, "cam_kitchen_01"
        )
        assert status == "not_found"
        assert path is None
        assert media_type is None
        assert ts is None

    async def test_file_missing_returns_gone(self, svc, dao):
        """event 存在且 device_id 合法,但文件还没落(snapshot_count=0)→ 410 gone."""
        eid = _insert(dao, device_ids=["cam_living_01"])
        # 未调 save_event_artifacts,文件不存在
        status, path, media_type, ts = await svc.locate_clip(eid, "cam_living_01")
        assert status == "gone"
        assert path is None
        assert media_type is None
        assert ts is None

    async def test_found_mp4(self, svc, dao, isolated_db):
        """落了 clip.mp4 后能正常返回 path + media_type=video/mp4 + timestamp."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        # 用显式 timestamp 验证 found 分支返出
        eid = _insert(dao, device_ids=["cam_living_01"], timestamp=1717741883000)
        clip = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200
        save_event_artifacts(
            eid, OmniEventArtifacts(clips={"cam_living_01": (clip, "mp4")})
        )

        status, path, media_type, ts = await svc.locate_clip(eid, "cam_living_01")
        assert status == "found"
        assert path is not None
        assert path.read_bytes() == clip
        assert media_type == "video/mp4"
        assert ts == 1717741883000  # 透传 DB.timestamp,给路由层拼按时间命名的下载文件名

    async def test_found_m4a(self, svc, dao, isolated_db, tmp_path, monkeypatch):
        """audio-only 路径落 clip.m4a → media_type=audio/mp4 + timestamp 透传."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_audio_01"], timestamp=1717741900000)
        clip = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100
        save_event_artifacts(
            eid, OmniEventArtifacts(clips={"cam_audio_01": (clip, "m4a")})
        )

        status, path, media_type, ts = await svc.locate_clip(eid, "cam_audio_01")
        assert status == "found"
        assert path is not None
        assert path.name == "clip.m4a"
        assert media_type == "audio/mp4"
        assert ts == 1717741900000

    async def test_device_id_with_unsafe_chars(self, svc, dao):
        """device_id 含 '/' → save 时被 slug 化,读时也用 slug,能找到."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam/living/01"])
        clip = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
        save_event_artifacts(
            eid, OmniEventArtifacts(clips={"cam/living/01": (clip, "mp4")})
        )

        status, path, _, _ = await svc.locate_clip(eid, "cam/living/01")
        assert status == "found"
        assert path is not None


@pytest.mark.asyncio
class TestBuildFeedbackIndex:
    def test_parses_uuid_with_uid_prefix(self, tmp_path, monkeypatch):
        eid = "12345678-1234-1234-1234-123456789abc"
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / f"feedback-user123-{eid}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService._build_feedback_index()
        assert eid in idx
        assert idx[eid][0].endswith(".tar.gz")

    def test_parses_uuid_without_uid_prefix(self, tmp_path, monkeypatch):
        eid = "12345678-1234-1234-1234-123456789abc"
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / f"feedback-{eid}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService._build_feedback_index()
        assert eid in idx

    def test_picks_latest_by_mtime(self, tmp_path, monkeypatch):
        import os
        eid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        packs = tmp_path / "packs"
        packs.mkdir()
        old = packs / f"feedback-uid1-{eid}-20260701-100000.tar.gz"
        new = packs / f"feedback-uid1-{eid}-20260701-120000.tar.gz"
        old.write_bytes(b"old")
        new.write_bytes(b"newer")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService._build_feedback_index()
        assert idx[eid][0] == new.as_posix()

    def test_empty_packs_dir(self, tmp_path, monkeypatch):
        packs = tmp_path / "packs"
        packs.mkdir()
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService._build_feedback_index()
        assert idx == {}

    def test_no_packs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService._build_feedback_index()
        assert idx == {}


class TestManagerSingleton:
    async def test_lazy_singleton(self, isolated_db):
        from miloco.manager import get_manager

        svc1 = get_manager().events_service
        svc2 = get_manager().events_service
        assert svc1 is svc2

        from miloco.perception.events_service import EventsService

        assert isinstance(svc1, EventsService)
