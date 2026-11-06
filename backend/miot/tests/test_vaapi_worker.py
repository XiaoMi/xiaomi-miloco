"""Pure unit tests: no device, subprocess, network or camera required."""

import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from miot import vaapi

pytestmark = pytest.mark.unit


def test_missing_device_does_not_start_ffmpeg(monkeypatch):
    monkeypatch.setattr(vaapi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vaapi.Path, "is_char_device", lambda p: False)
    create = Mock()
    monkeypatch.setattr(vaapi.subprocess, "Popen", create)
    with pytest.raises(RuntimeError, match="render device"):
        vaapi.FFmpegVAAPIDecoder(Mock(), ffmpeg="/usr/bin/ffmpeg", on_frame=Mock())
    create.assert_not_called()


def test_missing_executable_does_not_parse_or_launch(monkeypatch):
    monkeypatch.setattr(vaapi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vaapi.Path, "is_char_device", lambda p: True)
    monkeypatch.setattr(vaapi.os, "access", lambda *a: False)
    create = Mock()
    monkeypatch.setattr(vaapi.subprocess, "Popen", create)
    with pytest.raises(RuntimeError, match="absolute FFmpeg"):
        vaapi.FFmpegVAAPIDecoder(Mock(), ffmpeg="/missing", on_frame=Mock())
    create.assert_not_called()


def test_b_frames_rejected_before_launch(monkeypatch):
    monkeypatch.setattr(vaapi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vaapi.Path, "is_char_device", lambda p: True)
    monkeypatch.setattr(vaapi.os, "access", lambda *a: True)
    template = Mock()
    template.streams.video = [
        SimpleNamespace(codec_context=SimpleNamespace(width=640, has_b_frames=True))
    ]
    monkeypatch.setattr(vaapi.av, "open", Mock(return_value=template))
    create = Mock()
    monkeypatch.setattr(vaapi.subprocess, "Popen", create)
    with pytest.raises(RuntimeError, match="without B frames"):
        vaapi.FFmpegVAAPIDecoder(
            SimpleNamespace(data=b"key"), ffmpeg="/usr/bin/ffmpeg", on_frame=Mock()
        )
    template.close.assert_called_once()
    create.assert_not_called()


def test_metadata_backlog_is_bounded():
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._error = None
    worker._stop = threading.Event()
    worker._origin = 0
    worker._last_pts = 0
    worker._lock = threading.Lock()
    worker._pending = OrderedDict((i, None) for i in range(128))
    with pytest.raises(RuntimeError, match="metadata backlog"):
        worker.push(SimpleNamespace(timestamp=200))
    assert len(worker._pending) == 128


def test_timestamp_regression_is_rejected():
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._error = None
    worker._stop = threading.Event()
    worker._origin = 100
    worker._last_pts = 67
    with pytest.raises(RuntimeError, match="non-monotonic"):
        worker.push(SimpleNamespace(timestamp=166))
