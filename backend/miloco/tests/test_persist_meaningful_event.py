# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""T6 集成测试:_persist_meaningful_event 后台任务 + B 系列强约束验证.

覆盖:
- 有意义事件 → INSERT + 落图 + update_snapshot_count
- 纯 caption / 仅闲聊 → 不入表
- INSERT 失败(模拟)→ 不抛 + 不阻断主路径(B4)
- 磁盘满预检(< snapshot_min_free_disk_mb)→ INSERT 仍走,但 snapshot_count=0(B6a)
- empty clips_by_device → INSERT 仍走,snapshot_count=0
- text == build_agent_text(result)(B2 单源真值,简化版)
- 多摄像头 device_ids 数组持久化正确
"""

from unittest.mock import patch

import pytest
from miloco.perception.client import _persist_meaningful_event
from miloco.perception.snapshot_context import OmniEventArtifacts
from miloco.perception.types import (
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
)


def _artifacts(clips: dict | None = None) -> OmniEventArtifacts:
    """造 OmniEventArtifacts 实例,只填 clips,trace 留 None."""
    return OmniEventArtifacts(clips=clips or {})


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个 case 独立 DB + 独立 snapshot_root + 独立 Manager singleton."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    # 直接赋值(而不是 monkeypatch.setattr,否则 fixture 退出时会恢复成上一个 case 的值)
    connector_module.db_connector = None
    connector_module.init_database()

    # 同时重置 Manager 类的 _instance 和模块级 manager_instance(get_manager 用它)
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    yield tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def dao(isolated_db):
    """共享 _persist 内部使用的同一个 DAO 实例(通过 manager singleton).

    这样测试断言读到的就是 _persist 写入的同一份 DB.
    """
    from miloco.manager import get_manager

    return get_manager().meaningful_events_dao


@pytest.fixture(autouse=True)
def _voice_allowlist(isolated_db):
    """拾音**默认关**(opt-in)后,speech 只有在相机拾音开启(在白名单)时才落库。
    本模块的持久化用例统一把测试相机 ``cam_living_01`` 加入拾音白名单,让 speech
    走到落库路径(否则第二道防线 _filter_voice_enabled 会按默认关剥掉)。
    voice 专项用例 test_asr_from_mic_off_cam_stripped_not_persisted 自建 KVRepo 覆盖此处。
    """
    from miloco.database.kv_repo import KVRepo
    from miloco.manager import get_manager
    from miloco.miot.filter import set_cameras_voice_in_use

    mgr = get_manager()
    mgr._kv_repo = KVRepo()
    set_cameras_voice_in_use(mgr._kv_repo, ["cam_living_01"], True)


def _clip_payload(
    seed: int = 0, kind: str = "mp4"
) -> "tuple[bytes, str]":
    """造一份 (bytes, kind) 元组模拟 omni push_clip_bytes 出来的 sink payload.

    对齐生产路径 — `processor.clips_by_device: dict[str, tuple[bytes, ClipKind]]`,
    避免测试用裸 bytes 拐弯绕开 client.py 标注收紧后的类型约束.
    """
    return b"\x00\x00\x00\x20ftypisom" + bytes([seed]) * 8 + b"\x00" * 100, kind


@pytest.mark.asyncio
class TestPersistMeaningfulEvent:
    async def test_meaningful_event_inserts_and_saves_clips(
        self, isolated_db, dao
    ):
        """rule_hit + 多 device clip → INSERT 一行 + 落 N 个 clip.mp4 + count 正确."""
        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="厨房在炒菜")]
        )
        clips_by_device = {
            "cam_living_01": _clip_payload(1),
            "cam_kitchen_01": _clip_payload(2),
        }

        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01", "cam_kitchen_01"],
            artifacts=_artifacts(clips_by_device),
        )

        # 验证 DB
        rows = dao.query()
        assert len(rows) == 1
        row = rows[0]
        assert row["has_rule_hit"] is True
        assert row["has_suggestion"] is False
        assert row["has_asr"] is False
        assert row["device_ids"] == ["cam_living_01", "cam_kitchen_01"]
        # snapshot_count 语义:成功落 clip 的 device 数(2 device → 2)
        assert row["snapshot_count"] == 2
        assert row["schema_version"] == 1
        # text 含 rule 命中信息(从 build_agent_text 出)
        assert "[感知引擎]规则提醒：" in row["text"]
        assert "r1" in row["text"]

        # 验证落盘:每 device 1 个 clip.mp4
        from miloco.perception.snapshot_writer import get_snapshot_root

        snapshot_root = get_snapshot_root()
        event_dir = snapshot_root / row["id"]
        assert event_dir.exists()
        assert (event_dir / "cam_living_01" / "clip.mp4").read_bytes() == _clip_payload(1)[0]
        assert (event_dir / "cam_kitchen_01" / "clip.mp4").read_bytes() == _clip_payload(2)[0]

    async def test_caption_only_does_not_insert(self, isolated_db, dao):
        """纯 caption(无 rule/suggestion/asr)→ 不入表(B5)."""
        from miloco.perception.types import CaptionEntry

        result = RealtimePerceptionResult(
            caption=[CaptionEntry(description="人在看电视")]
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({"cam_living_01": _clip_payload()}),
        )
        assert dao.query() == []

    async def test_asr_chat_does_not_insert(self, isolated_db, dao):
        """只有家人闲聊(needs_response=False)→ 不入表."""
        result = RealtimePerceptionResult(
            speeches=[
                Speech(
                    needs_response=False,
                    speaker="妈妈",
                    content="今天好热",
                    is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ]
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({"cam_living_01": _clip_payload()}),
        )
        assert dao.query() == []

    async def test_asr_complete_command_inserts(self, isolated_db, dao):
        """needs_response=True AND is_complete=True → has_asr=True,入表."""
        result = RealtimePerceptionResult(
            speeches=[
                Speech(
                    needs_response=True,
                    speaker="用户",
                    content="打开窗户",
                    is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ]
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({"cam_living_01": _clip_payload()}),
        )
        rows = dao.query()
        assert len(rows) == 1
        assert rows[0]["has_asr"] is True
        assert "[感知引擎]语音提醒：" in rows[0]["text"]
        assert "打开窗户" in rows[0]["text"]

    async def test_combined_rule_and_asr_single_row(self, isolated_db, dao):
        """同一推理同时含 rule + ASR → 1 行(同窗口合并)."""
        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")],
            speeches=[
                Speech(
                    needs_response=True,
                    speaker="u",
                    content="开灯",
                    is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ],
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({"cam_living_01": _clip_payload()}),
        )
        rows = dao.query()
        assert len(rows) == 1
        assert rows[0]["has_rule_hit"] is True
        assert rows[0]["has_asr"] is True

    async def test_insert_failure_does_not_raise(self, isolated_db, dao):
        """B4:INSERT 失败仅 error log,_persist 不抛,主路径不阻断."""
        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")]
        )

        from miloco.manager import get_manager

        # mock DAO 让 insert 永远返 False(模拟磁盘满 / 唯一键冲突等)
        get_manager().meaningful_events_dao  # 触发 lazy 创建
        with patch.object(
            get_manager()._meaningful_events_dao, "insert", return_value=False
        ):
            # 不应抛
            await _persist_meaningful_event(
                result=result,
                device_ids=["cam_living_01"],
                artifacts=_artifacts({"cam_living_01": _clip_payload()}),
            )

    async def test_insert_raises_does_not_propagate(self, isolated_db, dao):
        """B4 更强约束:DAO insert 内部 raise 仍被 _persist 兜底."""
        result = RealtimePerceptionResult(
            suggestions=[Suggestion(event="高温", action="开窗")]
        )
        from miloco.manager import get_manager

        get_manager().meaningful_events_dao
        with patch.object(
            get_manager()._meaningful_events_dao,
            "insert",
            side_effect=RuntimeError("simulated DB error"),
        ):
            # 不应抛
            await _persist_meaningful_event(
                result=result,
                device_ids=["cam_living_01"],
                artifacts=_artifacts({"cam_living_01": _clip_payload()}),
            )

    async def test_low_disk_skips_save_but_inserts(self, isolated_db, dao):
        """B6a 写前预检:磁盘剩余 < 500MB → 跳过落盘但 metadata 入表(snapshot_count=0)."""
        result = RealtimePerceptionResult(
            suggestions=[Suggestion(event="高温", action="开窗")]
        )
        with patch(
            "miloco.perception.snapshot_writer.check_disk_space", return_value=False
        ):
            await _persist_meaningful_event(
                result=result,
                device_ids=["cam_living_01"],
                artifacts=_artifacts({"cam_living_01": _clip_payload()}),
            )

        rows = dao.query()
        assert len(rows) == 1
        assert rows[0]["snapshot_count"] == 0  # 跳过落盘
        # metadata 仍正常
        assert rows[0]["has_suggestion"] is True
        assert "高温" in rows[0]["text"]

    async def test_empty_frames_inserts_with_zero_count(self, isolated_db, dao):
        """clips_by_device 为空(早 path 或 omni 跳过)→ 入表 + snapshot_count=0."""
        result = RealtimePerceptionResult(
            speeches=[
                Speech(
                    needs_response=True, speaker="u", content="c", is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ]
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({}),
        )
        rows = dao.query()
        assert len(rows) == 1
        assert rows[0]["snapshot_count"] == 0

    async def test_text_equals_build_agent_text(self, isolated_db, dao):
        """B2 单源真值:DB.text == build_agent_text(result)."""
        from miloco.perception.event_text_builder import build_agent_text

        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")],
            speeches=[
                Speech(
                    needs_response=True,
                    speaker="u",
                    content="开灯",
                    is_complete=True,
                    source_device_ids=["cam_living_01"],
                )
            ],
            suggestions=[Suggestion(event="e", action="a")],
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_living_01"],
            artifacts=_artifacts({}),
        )
        rows = dao.query()
        assert rows[0]["text"] == build_agent_text(result)

    async def test_rule_lookup_failure_skips_rule_name(self, isolated_db, dao):
        """D4 第 2 轮 F-Q7:rule_service.get_rule 抛异常 → rule_names 跳过该条,
        INSERT 仍成功(前端 fallback 用 reason 渲染).

        覆盖 client.py:545-550 内的 try/except 兜底逻辑.
        """
        from unittest.mock import AsyncMock

        from miloco.manager import get_manager

        result = RealtimePerceptionResult(
            matched_rules=[
                MatchedRule(rule_id="ghost-rule-id", reason="rule 已被删")
            ]
        )

        # 测试环境 manager 没 initialize,直接注入 fake rule_service 让 get_rule 抛
        mgr = get_manager()

        class _FakeRuleService:
            get_rule = AsyncMock(side_effect=RuntimeError("rule not found"))

        mgr._rule_service = _FakeRuleService()

        try:
            await _persist_meaningful_event(
                result=result,
                device_ids=["cam_a"],
                artifacts=_artifacts({}),
            )
        finally:
            # 清掉 fake,避免污染后续 case
            if hasattr(mgr, "_rule_service"):
                delattr(mgr, "_rule_service")

        rows = dao.query()
        assert len(rows) == 1
        # 反查失败 → rule_names dict 缺该 id(或整个为空)
        assert rows[0]["rule_names"] == {}
        # row 仍正常入,text 不受影响
        assert rows[0]["has_rule_hit"] is True
        assert "rule 已被删" in rows[0]["text"]

    async def test_asr_from_mic_off_cam_stripped_not_persisted(self, isolated_db, dao):
        """默认关(opt-in):相机不在拾音白名单 → speech 在 _persist 内被
        _filter_voice_enabled 剥掉,speech-only 结果不入表。

        显式把某相机设为拾音关闭(不加入白名单),验证「未开启拾音的相机转写不落库」
        这条第二道防线在真实 DB 路径上确实生效
        （与 test_perception_client 的 _filter_voice_enabled 单测同源）。
        """
        from miloco.database.kv_repo import KVRepo
        from miloco.manager import get_manager
        from miloco.miot.filter import set_cameras_voice_in_use

        # isolated Manager 未 initialize：现挂一个建在隔离 DB 上的真 KVRepo,
        # 让 _filter_voice_enabled 读到真实白名单(cam_muted 不在其中 → 剥离)。
        mgr = get_manager()
        mgr._kv_repo = KVRepo()
        set_cameras_voice_in_use(mgr._kv_repo, ["cam_muted"], False)  # 未开启拾音

        result = RealtimePerceptionResult(
            speeches=[
                Speech(
                    needs_response=True,
                    speaker="u",
                    content="开灯",
                    is_complete=True,
                    source_device_ids=["cam_muted"],
                )
            ]
        )
        await _persist_meaningful_event(
            result=result,
            device_ids=["cam_muted"],
            artifacts=_artifacts({"cam_muted": _clip_payload()}),
        )
        assert dao.query() == []

    async def test_device_ids_from_clips_keys(self, isolated_db, dao):
        """D4 第 2 轮 F-Q7 + B2:processor.py 改造后 device_ids 应来自
        clips_by_device.keys(),audio-only device 也走视频路径(clip 含音频).

        本测试直接验 _persist 接收的 device_ids 与 clips_by_device keys 对齐时,
        DB row.device_ids 与之一致.
        """
        result = RealtimePerceptionResult(
            matched_rules=[MatchedRule(rule_id="r1", reason="x")]
        )
        # 模拟 processor 改造后:device_ids === clips_by_device.keys()
        clips = {"cam_with_clip": _clip_payload()}
        await _persist_meaningful_event(
            result=result,
            device_ids=list(clips.keys()),  # 对齐落盘
            artifacts=_artifacts(clips),
        )

        rows = dao.query()
        assert len(rows) == 1
        assert rows[0]["device_ids"] == ["cam_with_clip"]
