"""Pure unit tests: no device, subprocess, network or camera required."""

import threading
from collections import Counter, OrderedDict
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


def test_timestamp_regression_is_rejected():
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._error = None
    worker._stop = threading.Event()
    worker._origin = 100
    worker._last_pts = 67
    with pytest.raises(vaapi.TimestampDiscontinuity, match="non-monotonic"):
        worker.push(SimpleNamespace(timestamp=166))


def test_stalled_worker_is_killed_to_unblock_packet_writer(monkeypatch):
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._stop = Mock()
    worker._stop.wait.return_value = False
    worker._lock = threading.Lock()
    worker._pending = {1: None}
    worker._progress = 0.0
    worker._timeout = 5.0
    worker._error = None
    worker._stderr_tail = b""
    worker._process = Mock()
    worker._process.poll.return_value = None
    worker._process.stderr.read.side_effect = OSError("diagnostic pipe unavailable")
    monkeypatch.setattr(vaapi.time, "monotonic", lambda: 6.0)
    worker._watch()
    assert isinstance(worker._error, TimeoutError)
    worker._process.kill.assert_called_once()


def test_idle_worker_is_not_killed_without_pending_packets(monkeypatch):
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._stop = Mock()
    worker._stop.wait.side_effect = [False, True]
    worker._lock = threading.Lock()
    worker._pending = {}
    worker._progress = 0.0
    worker._timeout = 5.0
    worker._error = None
    worker._stderr_tail = b""
    worker._process = Mock()
    worker._process.poll.return_value = None
    worker._process.stderr.read.return_value = b""
    monkeypatch.setattr(vaapi.time, "monotonic", lambda: 100.0)
    worker._watch()
    assert worker._error is None
    worker._process.kill.assert_not_called()


def test_metadata_capacity_evicts_index_without_dropping_compressed_packet(monkeypatch):
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._error = None
    worker._stop = threading.Event()
    worker._origin = 0
    worker._last_pts = 127
    worker._timeout = 5
    worker._last_input = 0
    worker._progress = 0
    worker._lock = threading.Lock()
    worker._pending = OrderedDict((i, (object(), 0)) for i in range(128))
    worker._stats = Counter()
    worker._parameter_sets = {}
    worker._hevc = False
    worker._stream = object()
    worker._mux = Mock()
    monkeypatch.setattr(vaapi.av, "Packet", lambda data: SimpleNamespace())
    item = SimpleNamespace(
        timestamp=200, data=b"payload", model_copy=lambda **kwargs: object()
    )
    worker.push(item)
    assert len(worker._pending) == 128
    assert 0 not in worker._pending
    assert 200 in worker._pending
    worker._mux.mux.assert_called_once()
    assert worker._stats["metadata_evicted"] == 1
    assert worker._error is None


def test_no_input_does_not_misclassify_idle_decoder_as_failed(monkeypatch):
    worker = object.__new__(vaapi.FFmpegVAAPIDecoder)
    worker._stop = Mock()
    worker._stop.wait.side_effect = [False, True]
    worker._lock = threading.Lock()
    worker._pending = {1: None}
    worker._progress = 0
    worker._last_input = 0
    worker._timeout = 5
    worker._error = None
    worker._stderr_tail = b""
    worker._process = Mock()
    worker._process.poll.return_value = None
    worker._process.stderr.read.return_value = b""
    monkeypatch.setattr(vaapi.time, "monotonic", lambda: 6)
    worker._watch()
    worker._process.kill.assert_not_called()
    assert worker._error is None
