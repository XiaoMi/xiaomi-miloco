from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
from miloco.perception.engine.omni.provider import LocalMediaInfo
from miloco.perception.flow_context import (
    flow_diagnostics_scope,
    record_audio_encode_failure,
    record_audio_only_start,
    record_encoded_media,
    record_media_transform,
    record_smart_crop,
)


def _diagnostics() -> PerDeviceFlowDiagnostics:
    return PerDeviceFlowDiagnostics(
        device_id="camera-1",
        room_name="Living Room",
        trace_id="trace-1",
        device_trace_id="device-trace-1",
        observed_at=1,
    )


def test_flow_scope_records_encoded_media_and_smart_crop():
    diagnostics = _diagnostics()
    media = LocalMediaInfo(
        video_width=512,
        video_height=288,
        fps=1,
        frame_count=4,
        has_audio=True,
        audio_sample_rate=16000,
    )

    with flow_diagnostics_scope(diagnostics):
        record_media_transform(width=512, height=288, frame_count=4)
        record_encoded_media(media)
        record_smart_crop(region=(1, 2, 30, 40), applied=True)

    assert diagnostics.encoded_width == 512
    assert diagnostics.encoded_frame_count == 4
    assert diagnostics.encoded_has_audio is True
    assert diagnostics.media_transform_status.value == "ok"
    assert diagnostics.transformed_width == 512
    assert diagnostics.transformed_height == 288
    assert diagnostics.transformed_frame_count == 4
    assert diagnostics.media_encode_status.value == "ok"
    assert diagnostics.smart_crop_applied is True
    assert diagnostics.crop_region == (1, 2, 30, 40)


def test_flow_recorders_are_noop_without_scope():
    media = LocalMediaInfo(
        video_width=1,
        video_height=1,
        fps=1,
        frame_count=1,
        has_audio=False,
        audio_sample_rate=0,
    )
    record_encoded_media(media)
    record_smart_crop(region=None, applied=False)


def test_audio_only_start_marks_transform_skipped_before_encoding():
    diagnostics = _diagnostics()

    with flow_diagnostics_scope(diagnostics):
        record_audio_only_start()

    assert diagnostics.audio_only is True
    assert diagnostics.media_transform_status.value == "skipped"
    assert diagnostics.media_encode_status.value == "unknown"


def test_audio_encode_failure_marks_error_and_keeps_request_skipped():
    # 编码失败后 text-only 请求照发,request 不会被回标 OK,图中不出现
    # "encode 未知/失败 + request OK" 的矛盾组合。
    diagnostics = _diagnostics()

    with flow_diagnostics_scope(diagnostics):
        record_audio_only_start()
        record_audio_encode_failure()

    assert diagnostics.media_encode_status.value == "error"
    assert diagnostics.omni_request_status.value == "skipped"

    # 无 scope 时 recorder 是 noop,不崩
    record_audio_encode_failure()


def test_panorama_records_disabled_smart_crop_without_region():
    diagnostics = _diagnostics()

    with flow_diagnostics_scope(diagnostics):
        record_smart_crop(region=None, applied=False, enabled=False)

    assert diagnostics.smart_crop_enabled is False
    assert diagnostics.smart_crop_applied is False
    assert diagnostics.crop_region is None
