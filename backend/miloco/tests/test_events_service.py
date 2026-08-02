# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for EventsService(D3-T9).

覆盖:
- list_events:分页 / 时间窗 / DESC 排序 / 空 DB
- locate_clip 三种状态:found / gone(文件已删但 event 存在) / not_found(event 不存在 / device_id 不在 device_ids 内)
- Pydantic 序列化:不含 payload_json / schema_version / created_at
"""

import logging
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
class TestLocateRef:
    """locate_ref 三态 + has_ref 列表标记(Smart Crop 全景参考帧)."""

    async def test_event_not_found(self, svc):
        status, path, ts = await svc.locate_ref("does-not-exist", "cam_a")
        assert status == "not_found"
        assert path is None

    async def test_device_not_in_event(self, svc, dao):
        eid = _insert(dao, device_ids=["cam_living_01"])
        status, path, ts = await svc.locate_ref(eid, "cam_kitchen_01")
        assert status == "not_found"
        assert path is None

    async def test_no_ref_file_returns_gone(self, svc, dao):
        """event 合法但无 ref.jpg(非 crop 事件 / 已清)→ gone."""
        eid = _insert(dao, device_ids=["cam_living_01"])
        status, path, ts = await svc.locate_ref(eid, "cam_living_01")
        assert status == "gone"
        assert path is None

    async def test_found(self, svc, dao):
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        eid = _insert(dao, device_ids=["cam_living_01"], timestamp=1751000000000)
        save_event_artifacts(
            eid,
            OmniEventArtifacts(
                clips={"cam_living_01": (b"crop-video", "mp4")},
                ref_frames={"cam_living_01": jpg},
            ),
        )
        status, path, ts = await svc.locate_ref(eid, "cam_living_01")
        assert status == "found"
        assert path is not None
        assert path.name == "ref.jpg"
        assert path.read_bytes() == jpg
        # 事件时间随 found 一起返回:路由层据它拼「另存为」文件名(同 locate_clip)
        assert ts == 1751000000000

    async def test_has_ref_false_without_ref(self, svc, dao):
        _insert(dao, device_ids=["cam_a"])
        events = await svc.list_events()
        assert events[0].has_ref is False

    async def test_has_ref_true_with_ref(self, svc, dao):
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        eid = _insert(dao, device_ids=["cam_a"])
        save_event_artifacts(
            eid,
            OmniEventArtifacts(
                clips={"cam_a": (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200, "mp4")},
                ref_frames={"cam_a": b"\xff\xd8\xff\xe0" + b"\x00" * 100},
            ),
        )
        events = await svc.list_events()
        assert events[0].event_id == eid
        assert events[0].has_ref is True


@pytest.mark.asyncio
class TestReadCropMeta:
    """read_crop_meta 三态:从 omni_trace 里投影出 Smart Crop 的 crop 区域坐标.

    坐标不另落盘,已随 trace 持久化(push_crop_meta → calls[].crop),这里只做读侧投影.
    """

    @staticmethod
    def _save_trace(eid: str, calls: object) -> None:
        """落一份 omni_trace.用 object 而非 list[dict]:损坏形态用例要能塞进非 list 的 calls."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        save_event_artifacts(
            eid,
            OmniEventArtifacts(trace={"schema_version": 1, "calls": calls}),
        )

    async def test_event_not_found(self, svc):
        status, crop = await svc.read_crop_meta("does-not-exist", "cam_a")
        assert status == "not_found"
        assert crop is None

    async def test_device_not_in_event(self, svc, dao):
        eid = _insert(dao, device_ids=["cam_a"])
        status, crop = await svc.read_crop_meta(eid, "cam_b")
        assert status == "not_found"
        assert crop is None

    async def test_no_trace_file_returns_gone(self, svc, dao):
        """trace 未落盘 / 已被 cleanup 清 → gone(前端静默不画框)."""
        eid = _insert(dao, device_ids=["cam_a"])
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "gone"
        assert crop is None

    async def test_trace_without_crop_key_returns_gone(self, svc, dao):
        """非 Smart Crop 事件:trace 有 call 记录但没有 crop 键 → gone."""
        eid = _insert(dao, device_ids=["cam_a"])
        self._save_trace(eid, [{"device_id": "cam_a", "model": "m"}])
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "gone"
        assert crop is None

    async def test_found(self, svc, dao):
        eid = _insert(dao, device_ids=["cam_a"])
        meta = {
            "region_xyxy": [430, 122, 590, 318],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        self._save_trace(eid, [{"device_id": "cam_a", "crop": meta}])
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "found"
        assert crop is not None
        assert crop.model_dump() == meta

    async def test_picks_matching_device_not_first_call(self, svc, dao):
        """多摄像头同事件:各自的 crop 不能串台(按 device_id 匹配,不取第一条)."""
        eid = _insert(dao, device_ids=["cam_a", "cam_b"])
        meta_a = {
            "region_xyxy": [0, 0, 10, 10],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        meta_b = {
            "region_xyxy": [100, 100, 200, 200],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        self._save_trace(
            eid,
            [
                {"device_id": "cam_a", "crop": meta_a},
                {"device_id": "cam_b", "crop": meta_b},
            ],
        )
        _, crop = await svc.read_crop_meta(eid, "cam_b")
        assert crop is not None
        assert crop.model_dump() == meta_b

    async def test_picks_last_matching_call(self, svc, dao):
        """同 device 多条 call(stream / 重试追加)→ 以最后一次实际上送的为准."""
        eid = _insert(dao, device_ids=["cam_a"])
        old = {
            "region_xyxy": [0, 0, 10, 10],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        new = {
            "region_xyxy": [50, 60, 300, 200],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        self._save_trace(
            eid,
            [{"device_id": "cam_a", "crop": old}, {"device_id": "cam_a", "crop": new}],
        )
        _, crop = await svc.read_crop_meta(eid, "cam_a")
        assert crop is not None
        assert crop.model_dump() == new

    @pytest.mark.parametrize(
        "bad_crop",
        [
            {"region_xyxy": [10, 10], "frame_size_wh": [640, 360], "crop_short_edge": 360},
            {"region_xyxy": [0, 0, 10, 10], "frame_size_wh": [640], "crop_short_edge": 360},
            {"region_xyxy": [0, 0, 10, 10], "frame_size_wh": [640, 360]},
            {"region_xyxy": "0,0,10,10", "frame_size_wh": [640, 360], "crop_short_edge": 360},
        ],
        ids=["region-truncated", "frame-truncated", "missing-short-edge", "region-not-list"],
    )
    async def test_malformed_crop_returns_gone_not_raise(self, svc, dao, bad_crop):
        """crop 字段形状不合法(schema 演进 / 写入被截断)→ gone,不让 ValidationError 冒成 500.

        校验刻意放在 service 而非 router:router 里现构 EventCropMeta 会把半截 dict
        变成 500,与「拿不到就 410」的契约自相矛盾.
        """
        eid = _insert(dao, device_ids=["cam_a"])
        self._save_trace(eid, [{"device_id": "cam_a", "crop": bad_crop}])
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "gone"
        assert crop is None

    @pytest.mark.parametrize(
        "calls",
        [None, 123, "abc", [None], ["garbage"], [[1, 2]], [{"device_id": "cam_a"}, 7]],
        ids=[
            "calls-none",
            "calls-int",
            "calls-str",
            "element-none",
            "element-str",
            "element-list",
            "element-int-after-good",
        ],
    )
    async def test_malformed_calls_container_returns_gone(self, svc, dao, calls):
        """calls 本身或其元素形状不对也要折成 gone —— 不只有 crop 数组半截一种损坏形态.

        `reversed(123)` 抛 TypeError、`call.get` 在非 dict 上抛 AttributeError,
        这些若漏在 try 外就会变成 500,与「拿不到就 410」的契约冲突.
        """
        eid = _insert(dao, device_ids=["cam_a"])
        self._save_trace(eid, calls)
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "gone"
        assert crop is None

    async def test_log_lines_carry_no_injected_newline(self, svc, dao, caplog):
        """device_id 带 CR/LF 时,日志不得被撑成多行(CodeQL py/log-injection).

        断言的是「这条日志路径清洗过」,不是 _safe_log 的 replace 本身 —— 后者是同义
        反复。构造上借道既有的坏-crop 分支:crop 半截 → ValidationError → 正是那条
        带 event_id / device_id 的 warning。
        """
        evil = "cam_a\r\n2026-01-01 00:00:00 CRITICAL 伪造的整行"
        eid = _insert(dao, device_ids=[evil])
        self._save_trace(eid, [{"device_id": evil, "crop": {"region_xyxy": [1, 2]}}])
        with caplog.at_level(logging.WARNING, logger="miloco.perception.events_service"):
            status, crop = await svc.read_crop_meta(eid, evil)
        assert (status, crop) == ("gone", None)
        # 收成 list 再断言非空,不用 next():这里的 "bad crop meta" 是对生产日志文案的字面量
        # 耦合,文案一改就一条都匹配不到 —— 而 async 测试里 next() 抛的 StopIteration 会被
        # coroutine 机制包成 `RuntimeError: coroutine raised StopIteration` + 一坨 asyncio
        # 内栈,改文案的人第一反应会以为自己搞坏了事件循环,白付诊断成本。
        recs = [r for r in caplog.records if "bad crop meta" in r.getMessage()]
        assert recs, (
            "没抓到 read_crop_meta 的 bad-crop warning —— 日志文案可能改了,匹配串要跟着更新;"
            f"实际记录: {[r.getMessage()[:80] for r in caplog.records]}"
        )
        rec = recs[0]
        # 只看首行:末尾那个 %s 是 pydantic 的 ValidationError,它本来就是多行格式,
        # 断言整条 message 无换行会被它撑破 —— 那不是注入。event_id / device_id 都在
        # 首行,清洗失效时 "\r\n" 会把伪造内容推到第二行,首行断言正好红。
        head = rec.getMessage().split("\n", 1)[0]
        assert "\r" not in head
        assert "伪造的整行" in head  # 内容仍保留,只是折进同一行

    async def test_bad_element_does_not_hide_later_good_call(self, svc, dao):
        """坏元素只跳过,不该把同一 trace 里合法的那条 crop 一起废掉."""
        eid = _insert(dao, device_ids=["cam_a"])
        good = {
            "region_xyxy": [10, 20, 110, 140],
            "frame_size_wh": [640, 360],
            "crop_short_edge": 360,
        }
        # 坏元素放末尾:reversed 先撞上它,再取到合法的那条
        self._save_trace(eid, [{"device_id": "cam_a", "crop": good}, None])
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "found"
        assert crop is not None
        assert crop.region_xyxy == [10, 20, 110, 140]

    async def test_corrupt_trace_returns_gone_not_raise(self, svc, dao):
        """trace 不是合法 gzip/JSON → gone,不抛 500(画框是装饰,不该拖垮参考卡)."""
        from miloco.perception.snapshot_writer import get_snapshot_root

        eid = _insert(dao, device_ids=["cam_a"])
        event_dir = get_snapshot_root() / eid
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "omni_trace.json.gz").write_bytes(b"not-gzip-at-all")
        status, crop = await svc.read_crop_meta(eid, "cam_a")
        assert status == "gone"
        assert crop is None


@pytest.mark.asyncio
class TestBuildFeedbackIndex:
    def test_parses_uuid_with_uid_prefix(self, tmp_path, monkeypatch):
        eid = "12345678-1234-1234-1234-123456789abc"
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / f"feedback-user123-{eid}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
        assert eid in idx
        assert idx[eid][0].endswith(".tar.gz")

    def test_parses_uuid_without_uid_prefix(self, tmp_path, monkeypatch):
        eid = "12345678-1234-1234-1234-123456789abc"
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / f"feedback-{eid}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
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
        idx = EventsService.build_feedback_index()
        assert idx[eid][0] == new.as_posix()

    def test_empty_packs_dir(self, tmp_path, monkeypatch):
        packs = tmp_path / "packs"
        packs.mkdir()
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
        assert idx == {}

    def test_no_packs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
        assert idx == {}

    def test_finds_pack_in_timestamp_subdir(self, tmp_path, monkeypatch):
        eid = "12345678-1234-1234-1234-123456789abc"
        packs = tmp_path / "packs"
        subdir = packs / "20260701-143025"
        subdir.mkdir(parents=True)
        (subdir / f"feedback-user123-{eid}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
        assert eid in idx
        assert "20260701-143025" in idx[eid][0]

    def test_parses_on_demand_pack_name(self, tmp_path, monkeypatch):
        """主动查询包多一个字面量 od 段,findall 取最后一个匹配才不会误命中。"""
        log_id = "aaaaaaaa-1111-2222-3333-444444444444"
        packs = tmp_path / "packs"
        packs.mkdir()
        (packs / f"feedback-user123-od-{log_id}-20260701-143025.tar.gz").write_bytes(b"x")
        monkeypatch.setattr("miloco.perception.events_service.miloco_home", lambda: tmp_path)
        from miloco.perception.events_service import EventsService
        idx = EventsService.build_feedback_index()
        assert log_id in idx


class TestManagerSingleton:
    async def test_lazy_singleton(self, isolated_db):
        from miloco.manager import get_manager

        svc1 = get_manager().events_service
        svc2 = get_manager().events_service
        assert svc1 is svc2

        from miloco.perception.events_service import EventsService

        assert isinstance(svc1, EventsService)
