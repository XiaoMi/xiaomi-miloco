# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for PerceptionService.on_demand_perceive artifact orchestration.

Covers:
- DB insert failure → orphan artifact rollback (shutil.rmtree)
- Empty answer + omni not responded → clips/frames discarded, trace kept
- Empty answer + omni responded → clips kept
- 图像推理路径:frames 落盘 → clip_kinds 记 "frames";列表 API 就地数帧
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.perception.schema import OnDemandPerceptionRequest
from miloco.perception.snapshot_context import OmniEventArtifacts


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco.config import reset_settings

    reset_settings()
    yield tmp_path
    reset_settings()


def _make_artifacts(*, clips=None, frames=None, trace=None):
    a = OmniEventArtifacts()
    if clips is not None:
        a.clips = clips
    if frames is not None:
        a.frames = frames
    if trace is not None:
        a.trace = trace
    return a


def _make_result(answer="有一个人在客厅"):
    r = MagicMock()
    r.answer = answer
    return r


def _build_service(*, od_log_repo=None):
    from miloco.perception.service import PerceptionService

    collector = MagicMock()
    collector.get_all_active_sources.return_value = {"cam1": object()}
    pipeline = AsyncMock()
    runner = MagicMock()
    runner.is_running = False
    log_repo = MagicMock()
    service = PerceptionService(
        collector=collector,
        pipeline=pipeline,
        perception_runner=runner,
        log_repo=log_repo,
        on_demand_log_repo=od_log_repo,
    )
    return service, pipeline


REQUEST = OnDemandPerceptionRequest(sources=["cam1"], query="谁在客厅？")


class TestOnDemandPerceiveArtifacts:
    """service.on_demand_perceive artifact orchestration."""

    @pytest.mark.asyncio
    async def test_db_insert_fail_rolls_back_artifacts(self, isolated_settings):
        """DB append returns False → snapshots/{log_id}/ cleaned up."""
        from miloco.database.on_demand_log_repo import OnDemandLogRepo
        from miloco.perception.snapshot_writer import get_snapshot_root

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = False

        service, pipeline = _build_service(od_log_repo=od_repo)

        result = _make_result()
        artifacts = _make_artifacts(
            clips={"cam1": (b"\x00" * 100, "mp4")},
            trace={"calls": [{"error": None}]},
        )
        pipeline.process_on_demand.return_value = (result, artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        od_repo.append.assert_called_once()
        log_id = od_repo.append.call_args[0][0].id
        snapshot_root = get_snapshot_root()
        assert not (snapshot_root / log_id).exists(), (
            "orphan artifact dir should be cleaned up after DB insert failure"
        )

    @pytest.mark.asyncio
    async def test_empty_answer_omni_not_responded_discards_clips(
        self, isolated_settings
    ):
        """omni all calls errored + empty answer → clips discarded."""
        from miloco.database.on_demand_log_repo import OnDemandLogRepo

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = True

        service, pipeline = _build_service(od_log_repo=od_repo)

        result = _make_result(answer="")
        artifacts = _make_artifacts(
            clips={"cam1": (b"\x00" * 100, "mp4")},
            trace={"calls": [{"error": "timeout"}]},
        )
        pipeline.process_on_demand.return_value = (result, artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        entry = od_repo.append.call_args[0][0]
        assert entry.clip_dids == [], "clips should be discarded when omni not responded"
        assert entry.snapshot_count == 0

    @pytest.mark.asyncio
    async def test_empty_answer_omni_responded_keeps_clips(self, isolated_settings):
        """omni responded (no error) but empty answer → clips kept."""
        from miloco.database.on_demand_log_repo import OnDemandLogRepo

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = True

        service, pipeline = _build_service(od_log_repo=od_repo)

        result = _make_result(answer="")
        artifacts = _make_artifacts(
            clips={"cam1": (b"\x00" * 100, "mp4")},
            trace={"calls": [{"error": None}]},
        )
        pipeline.process_on_demand.return_value = (result, artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        entry = od_repo.append.call_args[0][0]
        assert len(entry.clip_dids) > 0, "clips should be kept when omni responded"
        assert entry.snapshot_count > 0

    # ── 图像推理路径 ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_frames_recorded_as_frames_kind(self, isolated_settings):
        """frames → clip_dids 含该 device,clip_kinds[did]='frames'(前端据它铺平展开)."""
        from miloco.database.on_demand_log_repo import OnDemandLogRepo

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = True

        service, pipeline = _build_service(od_log_repo=od_repo)
        artifacts = _make_artifacts(
            frames={"cam1": [b"\xff\xd8\xff\xe0" + b"\x00" * 40] * 3},
            trace={"calls": [{"error": None}]},
        )
        pipeline.process_on_demand.return_value = (_make_result(), artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        entry = od_repo.append.call_args[0][0]
        assert entry.clip_dids == ["cam1"]
        assert entry.clip_kinds == {"cam1": "frames"}
        assert entry.snapshot_count == 1

    @pytest.mark.asyncio
    async def test_empty_answer_omni_not_responded_discards_frames(
        self, isolated_settings
    ):
        """模型没应答 → 帧也要清干净.

        只清 clips 的话,"没答上"的那次查询照样会把整组帧留在盘上 —— 而帧恰恰是那次
        推理唯一的产物,留着既占配额又会被列表当成一次有效查询的产物展示。
        """
        from miloco.database.on_demand_log_repo import OnDemandLogRepo
        from miloco.perception.snapshot_writer import get_snapshot_root

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = True

        service, pipeline = _build_service(od_log_repo=od_repo)
        artifacts = _make_artifacts(
            frames={"cam1": [b"\xff\xd8\xff\xe0" + b"\x00" * 40] * 3},
            trace={"calls": [{"error": "timeout"}]},
        )
        pipeline.process_on_demand.return_value = (_make_result(answer=""), artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        entry = od_repo.append.call_args[0][0]
        assert entry.clip_dids == []
        assert entry.clip_kinds == {}
        assert entry.snapshot_count == 0
        # 盘上也不能留:trace 仍落(trace 是排障依据,与 clip 同款保留),
        # 但 device 目录一张帧都不该有。
        log_dir = get_snapshot_root() / entry.id
        assert (log_dir / "omni_trace.json.gz").exists()
        assert not list(log_dir.glob("cam1/frame_*.jpg"))

    @pytest.mark.asyncio
    async def test_empty_answer_omni_responded_keeps_frames(self, isolated_settings):
        """模型答了(哪怕答案是空串)→ 帧保留,产物与推理输入一致."""
        from miloco.database.on_demand_log_repo import OnDemandLogRepo

        od_repo = MagicMock(spec=OnDemandLogRepo)
        od_repo.append.return_value = True

        service, pipeline = _build_service(od_log_repo=od_repo)
        artifacts = _make_artifacts(
            frames={"cam1": [b"\xff\xd8\xff\xe0" + b"\x00" * 40] * 2},
            trace={"calls": [{"error": None}]},
        )
        pipeline.process_on_demand.return_value = (_make_result(answer=""), artifacts)

        with patch(
            "miloco.perception.snapshot_writer.check_disk_space",
            return_value=True,
        ):
            await service.on_demand_perceive(REQUEST)

        entry = od_repo.append.call_args[0][0]
        assert entry.clip_dids == ["cam1"]
        assert entry.snapshot_count == 1


class TestQueryOnDemandLogsFrameCounts:
    """列表 API 的 frame_counts:就地数盘,不落 DB(帧数会随 cleanup 变)."""

    @staticmethod
    def _service_with_row(row: dict):
        od_repo = MagicMock()
        od_repo.query.return_value = [row]
        service, _ = _build_service(od_log_repo=od_repo)
        return service

    def test_frames_row_gets_counts_from_disk(self, isolated_settings):
        """clip_kinds 里标了 frames 的 device → 数出当下盘上还剩几张."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        save_event_artifacts(
            "log-1",
            OmniEventArtifacts(frames={"cam1": [b"\xff\xd8\xff\xe0" + b"\x00" * 40] * 3}),
        )
        service = self._service_with_row(
            {"id": "log-1", "clip_kinds": {"cam1": "frames"}, "clip_dids": ["cam1"]}
        )
        out = service.query_on_demand_logs(limit=10)
        assert out["logs"][0]["frame_counts"] == {"cam1": 3}

    def test_expired_frames_count_as_zero_from_disk(self, isolated_settings):
        """cleanup 清掉后 → 0,而不是回落到 DB 里的旧值(DB 根本没记帧数)."""
        from miloco.perception.snapshot_writer import get_snapshot_root

        (get_snapshot_root() / "log-2" / "cam1").mkdir(parents=True)
        service = self._service_with_row(
            {"id": "log-2", "clip_kinds": {"cam1": "frames"}, "clip_dids": ["cam1"]}
        )
        out = service.query_on_demand_logs(limit=10)
        assert out["logs"][0]["frame_counts"] == {"cam1": 0}

    def test_video_row_frame_counts_empty(self, isolated_settings):
        """视频/音频行的 frame_counts 是空 dict —— 前端据"这台在不在 map 里"决定走帧组还是播放器."""
        service = self._service_with_row(
            {"id": "log-3", "clip_kinds": {"cam1": "mp4"}, "clip_dids": ["cam1"]}
        )
        out = service.query_on_demand_logs(limit=10)
        assert out["logs"][0]["frame_counts"] == {}

    def test_mixed_kinds_only_frames_counted(self, isolated_settings):
        """同一行里 A 机出帧、B 机出音频 → 只数 A."""
        from miloco.perception.snapshot_context import OmniEventArtifacts
        from miloco.perception.snapshot_writer import save_event_artifacts

        save_event_artifacts(
            "log-4",
            OmniEventArtifacts(
                frames={"camA": [b"\xff\xd8\xff\xe0" + b"\x00" * 40] * 2},
                clips={"camB": (b"\x00\x00\x00\x20ftypM4A ", "m4a")},
            ),
        )
        service = self._service_with_row(
            {
                "id": "log-4",
                "clip_kinds": {"camA": "frames", "camB": "m4a"},
                "clip_dids": ["camA", "camB"],
            }
        )
        out = service.query_on_demand_logs(limit=10)
        assert out["logs"][0]["frame_counts"] == {"camA": 2}
