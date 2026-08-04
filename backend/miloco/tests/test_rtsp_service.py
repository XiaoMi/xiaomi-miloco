from __future__ import annotations

import asyncio
import json
import stat
from unittest.mock import MagicMock

import numpy as np
import pytest
from miloco.rtsp.schema import RtspCameraCreate, RtspCameraUpdate
from miloco.rtsp.service import RtspCameraService, record_rtsp_clip
from pydantic import ValidationError


def test_rtsp_url_validation():
    payload = RtspCameraCreate(name="  Front door  ", url="rtsp://camera/live")
    assert payload.name == "Front door"
    assert payload.url == "rtsp://camera/live"

    for url in ("http://camera/live", "rtsp:///live", "rtsp://camera:bad/live"):
        with pytest.raises(ValidationError):
            RtspCameraCreate(name="Camera", url=url)

    with pytest.raises(ValidationError):
        RtspCameraUpdate()


def test_registry_migrates_legacy_records_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    path = tmp_path / "rtsp_cameras.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "rtsp:legacy",
                    "name": "Legacy",
                    "url": "rtsp://camera/live",
                    "created_at": "2026-01-02T03:04:05Z",
                    "updated_at": "2026-01-02T03:04:06Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    records = RtspCameraService().list_records()

    assert [record.did for record in records] == ["rtsp:legacy"]
    assert isinstance(records[0].created_at, int)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0]["did"] == "rtsp:legacy"
    assert "id" not in saved[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_registry_crud_updates_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    service = RtspCameraService()
    monkeypatch.setattr(service, "ensure_reader", MagicMock())

    created = service.create(
        RtspCameraCreate(name="Entrance", url="rtsp://camera/entrance")
    )
    assert created.did.startswith("rtsp:")

    reader = MagicMock()
    service._readers[created.did] = reader
    updated = service.update(
        created.did,
        RtspCameraUpdate(name="Garage", url="rtsps://camera/garage"),
    )
    assert updated.name == "Garage"
    assert updated.url == "rtsps://camera/garage"
    assert updated.updated_at >= created.updated_at
    reader.update_url.assert_called_once_with("rtsps://camera/garage")

    service.delete(created.did)
    reader.stop.assert_called_once_with()
    assert service.list_records() == []


@pytest.mark.asyncio
async def test_record_rtsp_clip_feeds_existing_recorder():
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    class FakeService:
        def ensure_reader(self, did: str):
            assert did == "rtsp:test"

        def latest_frame(self, did: str):
            assert did == "rtsp:test"
            return frame

    class FakeRecorder:
        def __init__(self, duration_ms: int):
            assert duration_ms == 0
            self.frames = []
            self.cancelled = False

        async def feed_bgr(self, bgr, ts_ms: int):
            self.frames.append((bgr.copy(), ts_ms))

        async def wait(self, timeout: float) -> bytes:
            assert timeout > 0
            assert len(self.frames) == 1
            return b"mp4"

        def cancel(self):
            self.cancelled = True

    recorder = FakeRecorder(0)
    result = await record_rtsp_clip(
        "rtsp:test",
        duration_ms=0,
        service=FakeService(),  # type: ignore[arg-type]
        recorder_factory=lambda duration_ms: recorder,
        poll_interval_s=0,
        timeout_s=1,
    )

    assert result == b"mp4"
    assert recorder.cancelled is True


@pytest.mark.asyncio
async def test_record_rtsp_clip_cancels_recorder_on_timeout():
    class EmptyService:
        def ensure_reader(self, did: str):
            pass

        def latest_frame(self, did: str):
            return None

    recorder = MagicMock()
    recorder.feed_bgr = MagicMock()

    with pytest.raises(asyncio.TimeoutError):
        await record_rtsp_clip(
            "rtsp:test",
            duration_ms=1,
            service=EmptyService(),  # type: ignore[arg-type]
            recorder_factory=lambda duration_ms: recorder,
            poll_interval_s=0,
            timeout_s=0,
        )

    recorder.cancel.assert_called_once_with()
