"""Task-local runtime perception diagnostics carrier."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from miloco.observability.perception_flow import GraphStatus, PerDeviceFlowDiagnostics

if TYPE_CHECKING:
    from collections.abc import Iterator

    from miloco.perception.engine.omni.provider import LocalMediaInfo


_current_diagnostics: ContextVar[PerDeviceFlowDiagnostics | None] = ContextVar(
    "perception_flow_diagnostics",
    default=None,
)


@contextmanager
def flow_diagnostics_scope(
    diagnostics: PerDeviceFlowDiagnostics,
) -> Iterator[None]:
    token = _current_diagnostics.set(diagnostics)
    try:
        yield
    finally:
        _current_diagnostics.reset(token)


def set_flow_diagnostics(diagnostics: PerDeviceFlowDiagnostics):
    return _current_diagnostics.set(diagnostics)


def reset_flow_diagnostics(token) -> None:
    _current_diagnostics.reset(token)


def record_audio_only_start() -> None:
    diagnostics = _current_diagnostics.get()
    if diagnostics is None:
        return
    diagnostics.audio_only = True
    diagnostics.media_transform_status = GraphStatus.SKIPPED


def record_audio_encode_failure() -> None:
    """音频编码没产出可用媒体块(过短/失败),text-only 请求仍照发;encode 标 ERROR、
    request 标 SKIPPED,避免图中出现 encode 未知 + request OK 的矛盾组合。"""
    diagnostics = _current_diagnostics.get()
    if diagnostics is None:
        return
    diagnostics.media_encode_status = GraphStatus.ERROR
    diagnostics.omni_request_status = GraphStatus.SKIPPED


def record_encoded_media(media: LocalMediaInfo, *, audio_only: bool = False) -> None:
    diagnostics = _current_diagnostics.get()
    if diagnostics is None:
        return
    diagnostics.audio_only = audio_only
    if audio_only:
        diagnostics.media_transform_status = GraphStatus.SKIPPED
    diagnostics.media_encode_status = GraphStatus.OK
    diagnostics.encoded_has_audio = media.has_audio
    diagnostics.audio_sample_rate = media.audio_sample_rate or None
    if audio_only:
        diagnostics.encoded_width = None
        diagnostics.encoded_height = None
        diagnostics.encoded_fps = None
        diagnostics.encoded_frame_count = None
        return
    diagnostics.encoded_width = media.video_width
    diagnostics.encoded_height = media.video_height
    diagnostics.encoded_fps = float(media.fps)
    diagnostics.encoded_frame_count = media.frame_count


def record_media_transform(*, width: int, height: int, frame_count: int) -> None:
    diagnostics = _current_diagnostics.get()
    if diagnostics is not None:
        diagnostics.media_transform_status = GraphStatus.OK
        diagnostics.transformed_width = width
        diagnostics.transformed_height = height
        diagnostics.transformed_frame_count = frame_count


def record_smart_crop(
    *,
    region: tuple[int, int, int, int] | None,
    applied: bool,
    enabled: bool = True,
) -> None:
    diagnostics = _current_diagnostics.get()
    if diagnostics is None:
        return
    diagnostics.smart_crop_enabled = enabled
    diagnostics.smart_crop_applied = applied
    diagnostics.crop_region = region
