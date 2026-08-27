"""Engine-scoped shared ONNX sessions for camera tracking.

Trackers contain mutable, camera-local state and must never be shared. The
underlying detector and ReID ONNX sessions are immutable inference resources,
however, and ONNX Runtime supports concurrent ``InferenceSession.run`` calls.
Keeping them here lets every camera owned by one ``PerceptionEngine`` reuse the
same native model weights and thread pools.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class TrackingModelResources:
    """Lazily create and retain ONNX sessions for one perception engine."""

    def __init__(
        self,
        *,
        use_gpu: bool = False,
        num_threads: int | None = None,
    ) -> None:
        self._use_gpu = use_gpu
        self._num_threads = num_threads
        self._sessions: dict[tuple[str, bool, int | None], Any] = {}
        self._lock = threading.Lock()

    def get_session(self, model_path: str):
        """Return the shared session for ``model_path``, creating it once."""
        resolved_path = str(Path(model_path).expanduser().resolve())
        key = (resolved_path, self._use_gpu, self._num_threads)

        # Camera pipelines can start concurrently. Cache access and the rare
        # first load are serialized; inference itself remains fully concurrent.
        # Keeping every cache access under the lock also makes release safe.
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                from miloco.perception.inference.ort_utils import make_session

                session = make_session(
                    resolved_path,
                    use_gpu=self._use_gpu,
                    num_threads=self._num_threads,
                )
                self._sessions[key] = session
                _LOGGER.info(
                    "created shared tracking ORT session #%d for %s",
                    len(self._sessions),
                    resolved_path,
                )
        return session

    def release(self) -> None:
        """Drop all engine-owned session references."""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
        if count:
            _LOGGER.info("released %d shared tracking ORT session(s)", count)

    @property
    def session_count(self) -> int:
        """Number of sessions retained (primarily for diagnostics)."""
        with self._lock:
            return len(self._sessions)
