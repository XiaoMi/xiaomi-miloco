# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for PerceptionService.on_demand_perceive artifact orchestration.

Covers:
- DB insert failure → orphan artifact rollback (shutil.rmtree)
- Empty answer + omni not responded → clips discarded, trace kept
- Empty answer + omni responded → clips kept
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


def _make_artifacts(*, clips=None, trace=None):
    a = OmniEventArtifacts()
    if clips is not None:
        a.clips = clips
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
