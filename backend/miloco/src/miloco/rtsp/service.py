"""User-managed RTSP camera registry and OpenCV frame readers."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

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
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        if text.isdigit():
            return int(text)
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return fallback
    return fallback


def _normalize_rtsp_record_item(item: Any) -> tuple[dict[str, Any] | None, bool]:
    """Normalize legacy registry entries to the current RtspCameraRecord shape."""
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
    """One OpenCV reader thread per RTSP camera."""

    def __init__(self, did: str, url: str):
        self.did = did
        self.url = url
        self._lock = threading.Lock()
        self._callbacks: dict[str, FrameCallback] = {}
        self._latest: NDArray[np.uint8] | None = None
        self._latest_unix_ms = 0
        self._online = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def online(self) -> bool:
        with self._lock:
            if self._latest_unix_ms and _now_ms() - self._latest_unix_ms < 10_000:
                return True
            return self._online

    def latest_frame(self) -> NDArray[np.uint8] | None:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"rtsp-reader-{self.did}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

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
        self.url = url
        self.stop()
        self.start()

    def _run(self) -> None:
        cap: cv2.VideoCapture | None = None
        while not self._stop.is_set():
            try:
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
                if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
                if not cap.isOpened():
                    self._set_online(False)
                    self._sleep(2.0)
                    continue

                self._set_online(True)
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        self._set_online(False)
                        break
                    if frame.ndim != 3:
                        continue
                    unix_ms = _now_ms()
                    wall_ms = _monotonic_ms()
                    with self._lock:
                        self._latest = frame.copy()
                        self._latest_unix_ms = unix_ms
                        self._online = True
                        callbacks = list(self._callbacks.values())
                    for cb in callbacks:
                        try:
                            cb(self.did, frame, wall_ms, unix_ms, unix_ms)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("RTSP frame callback failed for %s: %s", self.did, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("RTSP reader failed for %s: %s", self.did, e)
                self._set_online(False)
                self._sleep(2.0)
            finally:
                if cap is not None:
                    cap.release()
                    cap = None

    def _set_online(self, value: bool) -> None:
        with self._lock:
            self._online = value

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)


class RtspCameraService:
    """Persistent RTSP camera registry plus in-process frame readers."""

    def __init__(self):
        self._lock = threading.RLock()
        self._readers: dict[str, _RtspReader] = {}
        self._probe_cache: dict[str, tuple[bool, int]] = {}

    @property
    def _path(self) -> Path:
        return miloco_home() / "rtsp_cameras.json"

    def list_records(self) -> list[RtspCameraRecord]:
        return list(self._load().values())

    def list_state(self, *, denied: set[str], connected: set[str]) -> list[dict]:
        out: list[dict] = []
        for cam in self.list_records():
            online = self.is_online(cam.did)
            out.append(
                {
                    **cam.model_dump(),
                    "source": "rtsp",
                    "is_online": online,
                    "in_use": cam.did not in denied,
                    "connected": cam.did in connected,
                }
            )
        return out

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
            self._probe_cache.pop(did, None)
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
            self._probe_cache.pop(did, None)

    def get(self, did: str) -> RtspCameraRecord | None:
        return self._load().get(did)

    def ensure_reader(self, did: str) -> _RtspReader:
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
        reader = self._readers.get(did)
        if reader is not None and reader.online:
            return True
        record = self.get(did)
        if record is None:
            return False
        cached = self._probe_cache.get(did)
        now = _now_ms()
        if cached and now - cached[1] < 15_000:
            return cached[0]
        online = _probe_rtsp_tcp(record.url)
        self._probe_cache[did] = (online, now)
        return online

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
        out: dict[str, RtspCameraRecord] = {}
        changed = False
        for item in raw:
            normalized, item_changed = _normalize_rtsp_record_item(item)
            if normalized is None:
                logger.warning("Skipping invalid RTSP camera registry item: %r", item)
                changed = True
                continue
            record = RtspCameraRecord.model_validate(normalized)
            out[record.did] = record
            changed = changed or item_changed
        if changed:
            self._save(out)
        return out

    def _save(self, records: dict[str, RtspCameraRecord]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        payload = [r.model_dump() for r in sorted(records.values(), key=lambda r: r.did)]
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def _probe_rtsp_tcp(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (322 if parsed.scheme == "rtsps" else 554)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


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
    """Record an RTSP camera into an in-memory mp4 clip.

    RTSP readers keep only the latest BGR frame. For enrollment clips that is
    enough: sample the freshest frame at roughly 30fps and feed the same mp4
    recorder used by MiOT recording, avoiding any MiOT SDK registration.
    """
    import asyncio

    if recorder_factory is None:
        from miloco.miot.ws import NalClipRecorder

        recorder_factory = NalClipRecorder

    svc = service or get_rtsp_service()
    svc.ensure_reader(did)
    recorder = recorder_factory(duration_ms)
    timeout_s = timeout_s if timeout_s is not None else duration_ms / 1000.0 + 8.0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    start_ts_ms: int | None = None

    try:
        while True:
            if loop.time() >= deadline:
                raise asyncio.TimeoutError
            frame = svc.latest_frame(did)
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

        remaining_s = max(0.1, deadline - loop.time())
        return await recorder.wait(timeout=remaining_s)
    finally:
        recorder.cancel()
