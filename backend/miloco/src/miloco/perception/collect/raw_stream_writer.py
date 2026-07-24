# -*- coding: utf-8 -*-
"""Raw H.265 stream writer — saves camera video to .hevc files.

When the perception engine is not ready (no API key, omni down, etc.),
the camera P2P connection stays alive but decoded frames are wasted.
This module captures the raw encoded stream (before decoding) and saves
it to disk, organized by camera DID and date.

File layout:
    {save_dir}/{did}/{YYYY-MM-DD}/{HH-MM-SS}.hevc

Each .hevc file contains Annex-B H.265 NAL units, playable with:
    ffplay -f hevc 11-00-00.hevc
    ffmpeg -i 11-00-00.hevc -c copy output.mp4
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class RawStreamWriter:
    """Writes raw H.265 stream to segmented .hevc files.

    Thread-safe: the miot raw_data callback runs on the native P2P thread,
    so all file I/O is serialized via a lock.
    """

    def __init__(
        self,
        did: str,
        device_name: str,
        save_dir: str,
        segment_minutes: int = 60,
    ):
        self._did = did
        self._device_name = device_name
        self._save_dir = Path(os.path.expanduser(save_dir))
        self._segment_minutes = segment_minutes
        self._lock = threading.Lock()
        self._current_file = None
        self._current_segment_start = 0  # monotonic time when segment started
        self._bytes_written = 0
        self._frames_written = 0
        self._segment_path: Path | None = None

    def write_frame(self, data: bytes, timestamp: int, sequence: int) -> None:
        """Write one raw video frame to the current segment file.

        Called from the miot native thread (via __on_raw_data → raw_video callback).
        """
        with self._lock:
            try:
                self._maybe_rotate_segment(timestamp)
                if self._current_file is not None:
                    self._current_file.write(data)
                    self._bytes_written += len(data)
                    self._frames_written += 1
            except Exception as e:
                logger.error(
                    "[raw-writer] Failed to write frame for %s: %s",
                    self._did, e,
                )

    def close(self) -> None:
        """Close the current segment file."""
        with self._lock:
            self._close_current_file()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._current_file is not None

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "did": self._did,
                "bytes_written": self._bytes_written,
                "frames_written": self._frames_written,
                "current_segment": str(self._segment_path) if self._segment_path else None,
            }

    def _maybe_rotate_segment(self, timestamp: int) -> None:
        """Rotate to a new segment file if needed."""
        now = time.monotonic()
        need_rotate = (
            self._current_file is None
            or (now - self._current_segment_start) >= self._segment_minutes * 60
        )

        if not need_rotate:
            return

        self._close_current_file()
        self._open_new_segment(timestamp)

    def _open_new_segment(self, timestamp: int) -> None:
        """Open a new segment file based on current time."""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M-%S")

        # {save_dir}/{did}-{device_name}/{date}/{time}.hevc
        dir_name = f"{self._did}-{self._sanitize_name(self._device_name)}"
        segment_dir = self._save_dir / dir_name / date_str
        segment_dir.mkdir(parents=True, exist_ok=True)

        segment_path = segment_dir / f"{time_str}.hevc"

        try:
            self._current_file = open(segment_path, "ab")  # append mode
            self._segment_path = segment_path
            self._current_segment_start = time.monotonic()
            logger.info(
                "[raw-writer] Opened segment: %s (camera=%s)",
                segment_path, self._did,
            )
        except Exception as e:
            logger.error(
                "[raw-writer] Failed to open segment %s: %s",
                segment_path, e,
            )
            self._current_file = None

    def _close_current_file(self) -> None:
        """Close the current file handle."""
        if self._current_file is not None:
            try:
                self._current_file.close()
                if self._segment_path:
                    size_mb = self._segment_path.stat().st_size / (1024 * 1024)
                    logger.info(
                        "[raw-writer] Closed segment: %s (%.1fMB, %d frames)",
                        self._segment_path, size_mb, self._frames_written,
                    )
            except Exception as e:
                logger.error("[raw-writer] Error closing file: %s", e)
            finally:
                self._current_file = None
                self._segment_path = None

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize device name for use in file paths."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:30]


class RawStreamWriterManager:
    """Manages RawStreamWriter instances for multiple cameras."""

    def __init__(self, save_dir: str | None, segment_minutes: int = 60):
        self._save_dir = save_dir
        self._segment_minutes = segment_minutes
        self._writers: dict[str, RawStreamWriter] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._save_dir is not None

    def get_or_create(
        self, did: str, device_name: str
    ) -> RawStreamWriter | None:
        """Get or create a writer for the given camera."""
        if not self.enabled:
            return None

        with self._lock:
            if did not in self._writers:
                self._writers[did] = RawStreamWriter(
                    did=did,
                    device_name=device_name,
                    save_dir=self._save_dir,
                    segment_minutes=self._segment_minutes,
                )
                logger.info(
                    "[raw-writer] Created writer for camera %s (%s)",
                    did, device_name,
                )
            return self._writers[did]

    def close_writer(self, did: str) -> None:
        """Close and remove a writer."""
        with self._lock:
            writer = self._writers.pop(did, None)
            if writer:
                writer.close()
                logger.info("[raw-writer] Closed writer for camera %s", did)

    def close_all(self) -> None:
        """Close all writers."""
        with self._lock:
            for did, writer in self._writers.items():
                writer.close()
            self._writers.clear()
            logger.info("[raw-writer] Closed all writers")

    def get_stats(self) -> list[dict]:
        """Get stats for all active writers."""
        with self._lock:
            return [w.stats for w in self._writers.values()]
