# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
miot SDK 单元测试 — 不依赖真实设备或网络，可在 CI 中直接运行。
覆盖：error / const / types / storage / decoder(RingBuffer)
"""

# ruff: noqa: E402  — intentional block-style imports, each test section imports its own module

import pytest

# ─── error ───────────────────────────────────────────────────────────────────
from miot.error import MIoTError, MIoTErrorCode


def test_miot_error_default_code():
    err = MIoTError("something went wrong")
    assert err.code == MIoTErrorCode.CODE_UNKNOWN
    assert str(err) == "something went wrong"


def test_miot_error_custom_code():
    err = MIoTError("timeout", code=MIoTErrorCode.CODE_TIMEOUT)
    assert err.code == MIoTErrorCode.CODE_TIMEOUT


def test_miot_error_is_exception():
    with pytest.raises(MIoTError):
        raise MIoTError("test")


def test_miot_error_code_values_are_negative():
    for member in MIoTErrorCode:
        assert member.value < 0, f"{member.name} should be negative"


# ─── const ───────────────────────────────────────────────────────────────────


from miot.const import MIHOME_HTTP_API_TIMEOUT, OAUTH2_CLIENT_ID, PROJECT_CODE


def test_project_code_is_string():
    assert isinstance(PROJECT_CODE, str)
    assert len(PROJECT_CODE) > 0


def test_http_timeout_positive():
    assert MIHOME_HTTP_API_TIMEOUT > 0


def test_oauth2_client_id_is_numeric_string():
    assert OAUTH2_CLIENT_ID.isdigit()


# ─── types ───────────────────────────────────────────────────────────────────


from miot.types import MIoTOauthInfo, MIoTRoomInfo, MIoTUserInfo


def test_miot_user_info_construction():
    user = MIoTUserInfo(uid="123", nickname="张三", icon="https://example.com/icon.png", union_id="u-abc")
    assert user.uid == "123"
    assert user.nickname == "张三"


def test_miot_oauth_info_without_user():
    oauth = MIoTOauthInfo(
        access_token="at-xxx",
        refresh_token="rt-xxx",
        expires_ts=9999999999,
    )
    assert oauth.user_info is None
    assert oauth.access_token == "at-xxx"


def test_miot_oauth_info_with_user():
    user = MIoTUserInfo(uid="u1", nickname="李四", icon="https://i.mi.com/u1", union_id="union-1")
    oauth = MIoTOauthInfo(
        access_token="at-yyy",
        refresh_token="rt-yyy",
        expires_ts=9999999999,
        user_info=user,
    )
    assert oauth.user_info is not None
    assert oauth.user_info.uid == "u1"


def test_miot_room_info_construction():
    room = MIoTRoomInfo(room_id="r1", room_name="客厅", create_ts=0, dids=[])
    assert room.room_id == "r1"
    assert room.room_name == "客厅"


# ─── storage ─────────────────────────────────────────────────────────────────


from miot.storage import MIoTStorage


@pytest.mark.asyncio
async def test_storage_save_and_load_str(tmp_path):
    storage = MIoTStorage(root_path=str(tmp_path))
    await storage.save_async(domain="test", name="key1", data="hello")
    loaded = await storage.load_async(domain="test", name="key1", type_=str)
    assert loaded == "hello"


@pytest.mark.asyncio
async def test_storage_save_and_load_dict(tmp_path):
    storage = MIoTStorage(root_path=str(tmp_path))
    data = {"a": 1, "b": "two"}
    await storage.save_async(domain="test", name="cfg", data=data)
    loaded = await storage.load_async(domain="test", name="cfg", type_=dict)
    assert loaded == data


@pytest.mark.asyncio
async def test_storage_load_missing_returns_none(tmp_path):
    storage = MIoTStorage(root_path=str(tmp_path))
    result = await storage.load_async(domain="test", name="nonexistent", type_=str)
    assert result is None


@pytest.mark.asyncio
async def test_storage_remove(tmp_path):
    storage = MIoTStorage(root_path=str(tmp_path))
    await storage.save_async(domain="test", name="to_delete", data="bye")
    await storage.remove_async(domain="test", name="to_delete", type_=str)
    result = await storage.load_async(domain="test", name="to_delete", type_=str)
    assert result is None


@pytest.mark.asyncio
async def test_storage_creates_directory(tmp_path):
    sub = tmp_path / "subdir" / "deep"
    MIoTStorage(root_path=str(sub))
    assert sub.exists()


# ─── decoder RingBuffer ───────────────────────────────────────────────────────


from miot.types import MIoTCameraCodec, MIoTCameraFrameData, MIoTCameraFrameType

from miot.decoder import MIoTMediaRingBuffer


def _make_frame(frame_type: MIoTCameraFrameType, seq: int = 0) -> MIoTCameraFrameData:
    # MIoTMediaRingBuffer.put_video classifies key vs non-key by inspecting
    # the NAL byte stream (via _is_key_access_unit), not the wrapper's
    # frame_type metadata. Use real Annex-B NAL payloads so the helper
    # categorises each fixture frame correctly:
    #   - IDR slice (H.264 NAL type 5): NAL header byte 0x65
    #   - P slice   (H.264 NAL type 1): NAL header byte 0x41
    if frame_type == MIoTCameraFrameType.FRAME_I:
        nal = b"\x00\x00\x00\x01\x65" + b"\x00" * 5
    else:
        nal = b"\x00\x00\x00\x01\x41" + b"\x00" * 5
    return MIoTCameraFrameData(
        codec_id=MIoTCameraCodec.VIDEO_H264,
        length=len(nal),
        timestamp=seq,
        sequence=seq,
        frame_type=frame_type,
        channel=0,
        data=nal,
    )


def test_ring_buffer_put_and_step_video():
    """put_video 后 step() 能触发 on_video_frame 回调。"""
    rb = MIoTMediaRingBuffer(maxlen=5)
    rb.put_video(_make_frame(MIoTCameraFrameType.FRAME_I))
    received: list[MIoTCameraFrameData] = []
    rb.step(on_video_frame=received.append, on_audio_frame=lambda f: None, timeout=0.5)
    assert len(received) == 1
    assert received[0].frame_type == MIoTCameraFrameType.FRAME_I


def test_ring_buffer_step_empty_returns_without_callback():
    """空缓冲区 step() 应超时返回，不调用回调。"""
    rb = MIoTMediaRingBuffer(maxlen=5)
    called: list = []
    rb.step(on_video_frame=lambda f: called.append(f), on_audio_frame=lambda f: None, timeout=0.05)
    assert called == []


def test_ring_buffer_drops_non_keyframe_when_full():
    """P 帧填满后加 I 帧不应丢弃 I 帧。"""
    rb = MIoTMediaRingBuffer(maxlen=3)
    for i in range(3):
        rb.put_video(_make_frame(MIoTCameraFrameType.FRAME_P, seq=i))
    # Adding an I-frame should evict a P-frame and succeed
    rb.put_video(_make_frame(MIoTCameraFrameType.FRAME_I, seq=99))
    frames: list[MIoTCameraFrameData] = []
    for _ in range(10):
        rb.step(on_video_frame=frames.append, on_audio_frame=lambda f: None, timeout=0.02)
        if not frames or frames[-1].frame_type == MIoTCameraFrameType.FRAME_I:
            if frames and frames[-1].frame_type == MIoTCameraFrameType.FRAME_I:
                break
    i_frames = [f for f in frames if f.frame_type == MIoTCameraFrameType.FRAME_I]
    assert len(i_frames) >= 1


def test_ring_buffer_stop_clears_buffers():
    """stop() 应清空缓冲区并不抛出异常。"""
    rb = MIoTMediaRingBuffer(maxlen=5)
    rb.put_video(_make_frame(MIoTCameraFrameType.FRAME_I))
    rb.stop()
    # After stop(), _video_buffer and _audio_buffer should be empty
    assert len(rb._video_buffer) == 0
    assert len(rb._audio_buffer) == 0


# ─── _is_key_access_unit (NAL byte-stream key detection) ──────────────────────


from miot.decoder import _is_key_access_unit


def _nal_frame(codec_id: MIoTCameraCodec, data: bytes) -> MIoTCameraFrameData:
    """Build a minimal MIoTCameraFrameData with arbitrary NAL data.

    `frame_type` is intentionally always FRAME_P (the historically
    "broken" wrapper value) — _is_key_access_unit must not consult it,
    so passing FRAME_P keeps the test focused on byte-stream parsing.
    """
    return MIoTCameraFrameData(
        codec_id=codec_id,
        length=len(data),
        timestamp=0,
        sequence=0,
        frame_type=MIoTCameraFrameType.FRAME_P,
        channel=0,
        data=data,
        recv_unix_ms=0,
    )


def test_is_key_access_unit_h264_idr_4byte_sc():
    """H.264 IDR slice (NAL type 5) with 4-byte start code → True."""
    # SPS (0x67) + PPS (0x68) + IDR slice (0x65)
    data = bytes.fromhex(
        "00000001674200280000000168ce3c800000000165010203"
    )
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is True


def test_is_key_access_unit_h264_idr_3byte_sc():
    """H.264 IDR with 3-byte start code → True."""
    # 3-byte SC then NAL header 0x65 (IDR)
    data = bytes.fromhex("00000167420028" + "000001" + "65010203")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is True


def test_is_key_access_unit_h264_p_slice():
    """H.264 P slice (NAL type 1) → False."""
    data = bytes.fromhex("00000001419b22bcd80123456789abcdef")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is False


def test_is_key_access_unit_h264_only_sps_pps():
    """H.264 access unit with only SPS+PPS (no IDR slice) → False."""
    # SPS (type 7) + PPS (type 8), no slice
    data = bytes.fromhex("00000001674200280000000168ce3c80")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is False


def test_is_key_access_unit_h265_idr_w_radl():
    """H.265 IDR_W_RADL slice (NAL type 19, header byte 0x26) → True."""
    # NAL byte 0x26: (0x26 >> 1) & 0x3F = 19 = IDR_W_RADL
    data = bytes.fromhex("0000000126010203040506")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H265, data)) is True


def test_is_key_access_unit_h265_idr_n_lp():
    """H.265 IDR_N_LP slice (NAL type 20, header byte 0x28) → True."""
    # NAL byte 0x28: (0x28 >> 1) & 0x3F = 20 = IDR_N_LP
    data = bytes.fromhex("0000000128010203040506")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H265, data)) is True


def test_is_key_access_unit_h265_cra():
    """H.265 CRA_NUT (NAL type 21, header byte 0x2A) → True."""
    # NAL byte 0x2A: (0x2A >> 1) & 0x3F = 21 = CRA_NUT
    data = bytes.fromhex("000000012a010203040506")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H265, data)) is True


def test_is_key_access_unit_h265_trail_n():
    """H.265 TRAIL_N (NAL type 0, regular P slice) → False."""
    # NAL byte 0x02: (0x02 >> 1) & 0x3F = 1, but check 0x00 → 0 (TRAIL_N)
    data = bytes.fromhex("0000000100010203040506")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H265, data)) is False


def test_is_key_access_unit_h265_vps_only():
    """H.265 VPS NAL (NAL type 32) only — no IRAP slice → False."""
    # NAL byte 0x40: (0x40 >> 1) & 0x3F = 32 = VPS_NUT
    data = bytes.fromhex("000000014001020304")
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H265, data)) is False


def test_is_key_access_unit_truncated_4byte_sc():
    """Regression: 4-byte start code at very tail with no NAL header byte
    must NOT raise IndexError (was a real bug — see commit e3d9fde)."""
    data = b"\x00\x00\x00\x01"  # exactly the SC, nothing after
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is False


def test_is_key_access_unit_idr_followed_by_truncated_tail():
    """Valid IDR + trailing 4-byte SC tail still detects IDR (early return)."""
    data = bytes.fromhex(
        "00000001654200280000000168ce3c80" + "00000001"
    )
    assert _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, data)) is True


def test_is_key_access_unit_empty_or_too_short():
    """Empty / sub-start-code data → False, no crash."""
    for tiny in (b"", b"\x00", b"\x00\x00", b"\x00\x00\x00"):
        assert (
            _is_key_access_unit(_nal_frame(MIoTCameraCodec.VIDEO_H264, tiny))
            is False
        )


def test_is_key_access_unit_audio_codec_returns_false():
    """Audio codec_id always returns False, never tries NAL parse."""
    # Even with bytes that LOOK like an H.264 IDR, audio codec → False.
    h264_idr_bytes = bytes.fromhex("00000001650102")
    assert (
        _is_key_access_unit(_nal_frame(MIoTCameraCodec.AUDIO_OPUS, h264_idr_bytes))
        is False
    )


def test_is_key_access_unit_ignores_wrapper_frame_type():
    """Source-of-truth is NAL bytes, not the metadata field. A frame
    explicitly tagged FRAME_P at the wrapper level but carrying an
    IDR NAL must still be detected as key.

    This test pins down the design intent that fixed the original
    "全 P 帧" bug — see knowledge/03-features/live-camera-view.md §10.1.
    """
    idr_bytes = bytes.fromhex("0000000165420028")
    item = MIoTCameraFrameData(
        codec_id=MIoTCameraCodec.VIDEO_H264,
        length=len(idr_bytes),
        timestamp=0,
        sequence=0,
        frame_type=MIoTCameraFrameType.FRAME_P,  # ← lying metadata
        channel=0,
        data=idr_bytes,
        recv_unix_ms=0,
    )
    assert _is_key_access_unit(item) is True
