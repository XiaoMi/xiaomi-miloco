"""Persistent RTSP camera registry and OpenCV frame readers."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.middleware.exceptions import ResourceNotFoundException, ValidationException
from miloco.rtsp.schema import RtspCameraCreate, RtspCameraRecord, RtspCameraUpdate
from miloco.utils.paths import miloco_home

logger = logging.getLogger(__name__)

FrameCallback = Callable[[str, NDArray[np.uint8], int, int, int], None]


class ClipRecorder(Protocol):
    async def feed_bgr(self, bgr: NDArray[np.uint8], ts_ms: int) -> None: ...

    async def wait(self, timeout: float) -> bytes: ...

    def cancel(self) -> None: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _coerce_epoch_ms(value: Any, *, fallback: int) -> int:
    """Accept current integer milliseconds plus legacy ISO timestamps."""
    if value is None or isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        if text.isdigit():
            return int(text)
        try:
            return int(
                datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            return fallback
    return fallback


def _normalize_record(item: Any) -> tuple[dict[str, Any] | None, bool]:
    """Normalize records written by the original w-miloco implementation."""
    if not isinstance(item, dict):
        return None, False
    data = dict(item)
    changed = False
    if "did" not in data and data.get("id"):
        data["did"] = data["id"]
        changed = True
    now = _now_ms()
    created_at = _coerce_epoch_ms(data.get("created_at"), fallback=now)
    updated_at = _coerce_epoch_ms(data.get("updated_at"), fallback=created_at)
    if data.get("created_at") != created_at:
        data["created_at"] = created_at
        changed = True
    if data.get("updated_at") != updated_at:
        data["updated_at"] = updated_at
        changed = True
    if "room_name" not in data:
        data["room_name"] = "RTSP"
        changed = True
    if "id" in data:
        data.pop("id", None)
        changed = True
    return data, changed


class _RtspReader:
    """One reconnecting OpenCV reader thread per RTSP camera."""

    def __init__(self, did: str, url: str):
        self.did = did
        self.url = url
        self._lock = threading.Lock()
        self._callbacks: dict[str, FrameCallback] = {}
        self._latest: NDArray[np.uint8] | None = None
        self._latest_unix_ms = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def online(self) -> bool:
        with self._lock:
            return bool(
                self._latest_unix_ms and _now_ms() - self._latest_unix_ms < 10_000
            )

    def latest_frame(self) -> NDArray[np.uint8] | None:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"rtsp-reader-{self.did}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=4.0)
            if thread.is_alive():
                logger.warning("RTSP reader %s did not stop within timeout", self.did)
        self._thread = None
        with self._lock:
            self._latest = None
            self._latest_unix_ms = 0

    def add_callback(self, key: str, callback: FrameCallback) -> None:
        with self._lock:
            self._callbacks[key] = callback
        self.start()

    def remove_callback(self, key: str) -> None:
        with self._lock:
            self._callbacks.pop(key, None)

    def update_url(self, url: str) -> None:
        if url == self.url:
            return
        self.stop()
        self.url = url
        self.start()

    def _open_capture(self) -> cv2.VideoCapture:
        params: list[int] = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            params.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000])
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            params.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
        if params:
            return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params)
        return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

    def _run(self) -> None:
        while not self._stop.is_set():
            capture: cv2.VideoCapture | None = None
            try:
                capture = self._open_capture()
                if not capture.isOpened():
                    self._stop.wait(2.0)
                    continue
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    if frame.ndim != 3 or frame.shape[2] != 3:
                        continue
                    unix_ms = _now_ms()
                    wall_ms = _monotonic_ms()
                    with self._lock:
                        self._latest = frame.copy()
                        self._latest_unix_ms = unix_ms
                        callbacks = list(self._callbacks.values())
                    for callback in callbacks:
                        try:
                            callback(self.did, frame, wall_ms, unix_ms, unix_ms)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "RTSP frame callback failed for %s: %s", self.did, e
                            )
            except Exception as e:  # noqa: BLE001
                logger.warning("RTSP reader failed for %s: %s", self.did, e)
            finally:
                if capture is not None:
                    capture.release()
            self._stop.wait(2.0)


class RtspCameraService:
    """Persistent camera registry plus lazily started frame readers."""

    def __init__(self):
        self._lock = threading.RLock()
        self._readers: dict[str, _RtspReader] = {}

    @property
    def _path(self) -> Path:
        return miloco_home() / "rtsp_cameras.json"

    def list_records(self) -> list[RtspCameraRecord]:
        return list(self._load().values())

    def create(self, payload: RtspCameraCreate) -> RtspCameraRecord:
        with self._lock:
            records = self._load()
            did = f"rtsp:{uuid.uuid4().hex[:12]}"
            now = _now_ms()
            record = RtspCameraRecord(
                did=did,
                name=payload.name,
                url=payload.url,
                created_at=now,
                updated_at=now,
            )
            records[did] = record
            self._save(records)
            self.ensure_reader(did)
            return record

    def update(self, did: str, payload: RtspCameraUpdate) -> RtspCameraRecord:
        with self._lock:
            records = self._load()
            record = records.get(did)
            if record is None:
                raise ResourceNotFoundException(f"RTSP camera {did!r} not found")
            data = record.model_dump()
            if payload.name is not None:
                data["name"] = payload.name
            if payload.url is not None:
                data["url"] = payload.url
            data["updated_at"] = _now_ms()
            updated = RtspCameraRecord.model_validate(data)
            records[did] = updated
            self._save(records)
            reader = self._readers.get(did)
            if reader is not None:
                reader.update_url(updated.url)
            return updated

    def delete(self, did: str) -> None:
        with self._lock:
            records = self._load()
            if did not in records:
                raise ResourceNotFoundException(f"RTSP camera {did!r} not found")
            records.pop(did)
            self._save(records)
            reader = self._readers.pop(did, None)
            if reader is not None:
                reader.stop()

    def get(self, did: str) -> RtspCameraRecord | None:
        return self._load().get(did)

    def ensure_reader(self, did: str) -> _RtspReader:
        with self._lock:
            record = self.get(did)
            if record is None:
                raise ResourceNotFoundException(f"RTSP camera {did!r} not found")
            reader = self._readers.get(did)
            if reader is None:
                reader = _RtspReader(did, record.url)
                self._readers[did] = reader
            elif reader.url != record.url:
                reader.update_url(record.url)
            reader.start()
            return reader

    def add_frame_callback(self, did: str, key: str, callback: FrameCallback) -> None:
        self.ensure_reader(did).add_callback(key, callback)

    def remove_frame_callback(self, did: str, key: str) -> None:
        reader = self._readers.get(did)
        if reader is not None:
            reader.remove_callback(key)

    def latest_frame(self, did: str) -> NDArray[np.uint8] | None:
        return self.ensure_reader(did).latest_frame()

    def is_online(self, did: str) -> bool:
        try:
            return self.ensure_reader(did).online
        except ResourceNotFoundException:
            return False

    def _load(self) -> dict[str, RtspCameraRecord]:
        path = self._path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read RTSP camera registry: %s", e)
            return {}
        if not isinstance(raw, list):
            raise ValidationException("rtsp_cameras.json must contain a list")
        records: dict[str, RtspCameraRecord] = {}
        changed = False
        for item in raw:
            normalized, item_changed = _normalize_record(item)
            if normalized is None:
                logger.warning("Skipping invalid RTSP camera registry item: %r", item)
                changed = True
                continue
            try:
                record = RtspCameraRecord.model_validate(normalized)
            except ValueError as e:
                logger.warning("Skipping invalid RTSP camera registry item: %s", e)
                changed = True
                continue
            records[record.did] = record
            changed = changed or item_changed
        if changed:
            self._save(records)
        return records

    def _save(self, records: dict[str, RtspCameraRecord]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            record.model_dump()
            for record in sorted(records.values(), key=lambda item: item.did)
        ]
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            tmp.replace(path)
            path.chmod(0o600)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


_service = RtspCameraService()


def get_rtsp_service() -> RtspCameraService:
    return _service


async def record_rtsp_clip(
    did: str,
    *,
    duration_ms: int,
    service: RtspCameraService | None = None,
    recorder_factory: Callable[[int], ClipRecorder] | None = None,
    poll_interval_s: float = 1 / 30,
    timeout_s: float | None = None,
) -> bytes:
    """Encode the reader's latest BGR frames with the existing clip recorder."""
    import asyncio

    if recorder_factory is None:
        from miloco.miot.ws import NalClipRecorder

        recorder_factory = NalClipRecorder

    rtsp_service = service or get_rtsp_service()
    rtsp_service.ensure_reader(did)
    recorder = recorder_factory(duration_ms)
    timeout_s = timeout_s if timeout_s is not None else duration_ms / 1000.0 + 8.0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    start_ts_ms: int | None = None

    try:
        while True:
            if loop.time() >= deadline:
                raise asyncio.TimeoutError
            frame = rtsp_service.latest_frame(did)
            if frame is None:
                await asyncio.sleep(poll_interval_s)
                continue

            now_ms = _monotonic_ms()
            if start_ts_ms is None:
                start_ts_ms = now_ms
            await recorder.feed_bgr(frame, now_ms)
            if now_ms - start_ts_ms >= duration_ms:
                break
            await asyncio.sleep(poll_interval_s)

        return await recorder.wait(timeout=max(0.1, deadline - loop.time()))
    finally:
        recorder.cancel()
