"""Unit gates for opt-in hardware decoding and sticky software fallback."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from miot import decoder as module
from miot.types import MIoTCameraCodec, MIoTCameraFrameData, MIoTCameraFrameType

pytestmark = pytest.mark.unit


def packet(key=True, ts=100):
    data = b"\x00\x00\x00\x01" + (b"\x65" if key else b"\x41") + b"payload"
    return MIoTCameraFrameData(
        codec_id=MIoTCameraCodec.VIDEO_H264,
        length=len(data),
        timestamp=ts,
        sequence=ts,
        frame_type=MIoTCameraFrameType.FRAME_I if key else MIoTCameraFrameType.FRAME_P,
        channel=0,
        data=data,
        recv_unix_ms=1000 + ts,
    )


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("MIOT_VAAPI_FFMPEG", raising=False)
    soft = Mock()
    soft.decode.return_value = []
    create = Mock(return_value=soft)
    monkeypatch.setattr(module, "VideoCodecContext", SimpleNamespace(create=create))
    factory = Mock()
    monkeypatch.setattr(module, "FFmpegVAAPIDecoder", factory)
    loop = Mock()
    callback = Mock()
    dec = module.MIoTMediaDecoder(
        10**12,
        Mock(),
        video_frame_callback=callback,
        enable_hw_accel=True,
        main_loop=loop,
    )
    return dec, soft, create, factory, callback


def test_default_software_without_opt_in(setup):
    dec, soft, create, factory, _ = setup
    dec._on_video_callback(packet())
    create.assert_called_once_with("h264", "r")
    factory.assert_not_called()
    soft.decode.assert_called_once()


def test_explicit_disable_wins(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec._enable_hw_accel = False
    dec._on_video_callback(packet())
    factory.assert_not_called()
    soft.decode.assert_called_once()


def test_start_failure_tries_once_then_software(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/missing/ffmpeg")
    factory.side_effect = RuntimeError("no device")
    dec._on_video_callback(packet())
    dec._on_video_callback(packet(False, 167))
    assert factory.call_count == 1
    assert soft.decode.call_count == 2


def test_runtime_failure_waits_for_key_and_never_retries(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    hw = factory.return_value
    hw.push.side_effect = [None, RuntimeError("device lost")]
    dec._on_video_callback(packet())
    dec._on_video_callback(packet(False, 167))
    soft.decode.assert_not_called()
    dec._on_video_callback(packet(False, 234))
    soft.decode.assert_not_called()
    dec._on_video_callback(packet(True, 301))
    soft.decode.assert_called_once()
    assert factory.call_count == 1
    hw.close.assert_called_once()


def test_callback_keeps_original_metadata(setup, monkeypatch):
    dec, _, _, factory, callback = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec._on_video_callback(packet())
    frame = Mock()
    frame.to_ndarray.return_value.astype.return_value = "bgr"
    emit = factory.call_args.kwargs["on_frame"]
    emit(frame, packet(ts=512))
    assert callback.call_args.args[:4] == ("bgr", 512, 0, 1512)


def test_non_key_waits_without_launching_worker(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec._on_video_callback(packet(False))
    factory.assert_not_called()
    soft.decode.assert_called_once()


def test_stop_closes_hardware_before_thread_join(setup, monkeypatch):
    dec, _, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec._on_video_callback(packet())
    hw = factory.return_value
    order = []
    hw.abort.side_effect = lambda: order.append("abort")
    hw.close.side_effect = lambda: order.append("close")
    dec.join = lambda **kwargs: order.append("join")
    dec.is_alive = lambda: False
    assert dec.stop()
    assert order == ["abort", "join", "close"]


def test_interval_change_rebuilds_hardware_at_key(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    dec._on_video_callback(packet())
    dec.set_decoded_frame_interval(333)
    dec._on_video_callback(packet(False, 167))
    old.close.assert_not_called()
    assert old.push.call_count == 2
    dec._on_video_callback(packet(True, 234))
    old.close.assert_called_once()
    new.push.assert_called_once()
    soft.decode.assert_not_called()
    assert not dec._hw_failed
    assert factory.call_count == 2
    assert factory.call_args.kwargs["interval_ms"] == 333


def test_pending_rate_changes_coalesce(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    dec._on_video_callback(packet())
    dec.set_decoded_frame_interval(333)
    dec.set_decoded_frame_interval(200)
    dec._on_video_callback(packet(True, 234))
    assert factory.call_count == 2
    assert factory.call_args.kwargs["interval_ms"] == 200
    assert not dec._hw_failed
    soft.decode.assert_not_called()


def test_unchanged_rate_keeps_worker(setup, monkeypatch):
    dec, _, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec._on_video_callback(packet())
    dec.set_decoded_frame_interval(0)
    dec._on_video_callback(packet(True, 234))
    assert factory.call_count == 1
    factory.return_value.close.assert_not_called()


def test_failed_reconfiguration_stays_on_software(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old = Mock()
    factory.side_effect = [old, RuntimeError("device unavailable")]
    dec._on_video_callback(packet())
    dec.set_decoded_frame_interval(333)
    dec._on_video_callback(packet(True, 234))
    dec._on_video_callback(packet(False, 301))
    assert factory.call_count == 2
    assert dec._hw_failed
    assert soft.decode.call_count == 2


def test_hevc_non_key_uses_software_until_keyframe(setup, monkeypatch):
    dec, soft, create, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    item = packet().model_copy(update={"codec_id": MIoTCameraCodec.VIDEO_H265})
    dec._on_video_callback(item)
    factory.assert_not_called()
    create.assert_called_once_with("hevc", "r")
    soft.decode.assert_called_once()


def test_stopped_decoder_does_not_emit(setup):
    dec, _, _, _, callback = setup
    dec._stop_requested = True
    dec._emit_video_frames([Mock()], packet())
    callback.assert_not_called()


def test_rate_change_during_worker_creation_is_not_lost(setup, monkeypatch):
    dec, _, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    dec.set_decoded_frame_interval(333)
    first, second = Mock(), Mock()

    def create(*args, **kwargs):
        dec.set_decoded_frame_interval(200)
        return first

    factory.side_effect = create
    dec._on_video_callback(packet())
    assert dec._hw_reconfigure
    factory.side_effect = None
    factory.return_value = second
    dec._on_video_callback(packet(True, 234))
    assert dec._hw_decoder is second
    assert factory.call_args.kwargs["interval_ms"] == 200


def test_runtime_device_failure_retries_hardware_once_at_next_key(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    old.push.side_effect = [None, module.HardwareWorkerError("device lost")]
    dec._on_video_callback(packet())
    dec._on_video_callback(packet(False, 167))
    assert not dec._hw_failed
    assert dec._hw_wait_key
    old.close.assert_called_once()
    dec._on_video_callback(packet(False, 234))
    assert factory.call_count == 1
    dec._on_video_callback(packet(True, 301))
    assert factory.call_count == 2
    assert dec._hw_decoder is new
    soft.decode.assert_not_called()


def test_second_runtime_device_failure_falls_back_without_retry_loop(
    setup, monkeypatch
):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    old.push.side_effect = [None, module.HardwareWorkerError("device lost")]
    new.push.side_effect = [None, module.HardwareWorkerError("still unavailable")]
    dec._on_video_callback(packet())
    dec._on_video_callback(packet(False, 167))
    dec._on_video_callback(packet(True, 234))
    dec._on_video_callback(packet(False, 301))
    dec._on_video_callback(packet(True, 368))
    assert dec._hw_failed
    assert factory.call_count == 2
    soft.decode.assert_called_once()


def test_timestamp_jump_restarts_at_key_without_device_retry_budget(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    old.push.side_effect = [None, module.TimestampDiscontinuity("timestamp reset")]
    dec._on_video_callback(packet())
    dec._on_video_callback(packet(False, 50))
    assert dec._hw_wait_key
    assert not dec._hw_failed
    assert dec._hw_recoveries == 0
    dec._on_video_callback(packet(True, 117))
    assert dec._hw_decoder is new
    soft.decode.assert_not_called()


def test_timestamp_jump_on_key_replays_current_key(setup, monkeypatch):
    dec, soft, _, factory, _ = setup
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    old, new = Mock(), Mock()
    factory.side_effect = [old, new]
    old.push.side_effect = [None, module.TimestampDiscontinuity("timestamp reset")]
    dec._on_video_callback(packet())
    item = packet(True, 50)
    dec._on_video_callback(item)
    new.push.assert_called_once_with(item)
    assert not dec._hw_failed
    soft.decode.assert_not_called()
