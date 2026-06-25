from __future__ import annotations

import json

from miloco.rtsp.schema import RtspCameraCreate, RtspCameraUpdate
from miloco.rtsp.service import RtspCameraService


class _FakeReader:
    def __init__(self, did: str, url: str):
        self.did = did
        self.url = url
        self.started = 0
        self.stopped = 0

    @property
    def online(self) -> bool:
        return True

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def update_url(self, url: str) -> None:
        if url == self.url:
            return
        self.url = url
        self.stop()
        self.start()


def test_rtsp_registry_loads_legacy_id_and_iso_timestamps(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    registry = tmp_path / "rtsp_cameras.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "rtsp:21a976d72eca",
                    "name": "旧摄像头",
                    "url": "rtsp://127.0.0.1:8554/live",
                    "created_at": "2026-06-21T05:18:19.565061+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    service = RtspCameraService()
    [record] = service.list_records()

    assert record.did == "rtsp:21a976d72eca"
    assert record.name == "旧摄像头"
    assert record.url == "rtsp://127.0.0.1:8554/live"
    assert record.created_at == 1782019099565
    assert record.updated_at == 1782019099565

    migrated = json.loads(registry.read_text(encoding="utf-8"))
    assert migrated == [
        {
            "did": "rtsp:21a976d72eca",
            "name": "旧摄像头",
            "url": "rtsp://127.0.0.1:8554/live",
            "room_name": "RTSP",
            "created_at": 1782019099565,
            "updated_at": 1782019099565,
        }
    ]


def test_update_rtsp_camera_renames_and_reloads_reader_on_url_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr("miloco.rtsp.service._RtspReader", _FakeReader)

    service = RtspCameraService()
    record = service.create(
        RtspCameraCreate(name="门口", url="rtsp://127.0.0.1:8554/old")
    )
    reader = service.ensure_reader(record.did)

    updated = service.update(
        record.did,
        RtspCameraUpdate(name="后门", url="rtsp://127.0.0.1:8554/new"),
    )

    assert updated.did == record.did
    assert updated.name == "后门"
    assert updated.url == "rtsp://127.0.0.1:8554/new"
    assert service.get(record.did).name == "后门"
    assert reader.url == "rtsp://127.0.0.1:8554/new"
    assert reader.stopped == 1
    assert reader.started == 2

    saved = json.loads((tmp_path / "rtsp_cameras.json").read_text())
    assert saved[0]["name"] == "后门"
    assert saved[0]["url"] == "rtsp://127.0.0.1:8554/new"
