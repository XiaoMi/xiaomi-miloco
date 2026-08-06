# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""上传字节解码 / 落盘归一化 / ISO BMFF 判形 的单测（``identity/_image_utils`` + ``_avatar``）。

覆盖三件事：
1. ``decode_image``：cv2 快路径覆盖既有格式不回归 + HEIC 走 Pillow(pi-heif) 回退能解开。
2. ``normalize_for_storage``：白名单**逐字节直通**（零转码承诺）、其余重编、编码失败不落空字节。
3. ``is_still_image_container``：HEIF/AVIF 与 mp4/mov 共用 ftyp 容器，必须按 brand 分开——
   判错会让一张 HEIC 进视频抽帧路径，ffmpeg 只给 512x512 瓦片而不拼 tile grid，全链路静默跑错。
"""

from __future__ import annotations

import base64
import io

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity._avatar import normalize_for_storage
from miloco.perception.engine.identity._image_utils import (
    decode_image,
    is_still_image_container,
)
from PIL import Image

# 32x24 纯色 HEIC（466 字节）。用 base64 常量而非二进制 fixture 文件：生产依赖 pi-heif
# **只带解码器**（这正是它 LGPLv3 而非 GPLv2 的原因），测试期造不出 HEIC，只能内嵌现成样本。
_HEIC_B64 = (
    "AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAXxtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABwaWN0AAAAAAAA"
    "AAAAAAAAAAAAACJpbG9jAAAAAERAAAEAAQAAAAABoAABAAAAAAAAADIAAAAjaWluZgAAAAAAAQAAABVpbmZlAgAA"
    "AAABAABodmMxAAAAAA5waXRtAAAAAAABAAAA/GlwcnAAAADcaXBjbwAAAHVodmNDAQNwAAAAAAAAAAAAHvAA/P34"
    "+AAADwNgAAEAGEABDAH//wNwAAADAJAAAAMAAAMAHroCQGEAAQApQgEBA3AAAAMAkAAAAwAAAwAeoCCBBZbqrprm"
    "4CGgwIAAAAyAAAADAIRiAAEABkQBwXPBiQAAABNjb2xybmNseAABAA0ABoAAAAAUaXNwZQAAAAAAAABAAAAAQAAA"
    "AChjbGFwAAAAIAAAAAEAAAAYAAAAAf///+AAAAAC////2AAAAAIAAAAQcGl4aQAAAAADCAgIAAAAGGlwbWEAAAAA"
    "AAAAAQABBYECAwWEAAAAOm1kYXQAAAAuKAGvEyFiY0D1JyL//0Nqf+o8J/2F2WFncrrBW/L6wPZkm8DzqpGegIdp"
    "pzAVeA=="
)
HEIC_BYTES = base64.b64decode(_HEIC_B64)


def _img(fmt: str, size: tuple[int, int] = (64, 48)) -> bytes:
    im = Image.new("RGB", size, (20, 150, 90))
    buf = io.BytesIO()
    (im.convert("P") if fmt == "GIF" else im).save(buf, fmt)
    return buf.getvalue()


# ── decode_image ──────────────────────────────────────────────────────────


def test_decode_heic_via_fallback():
    """HEIC 是 cv2 解不了、必须走 Pillow 回退的那一类——这条断言就是本次改动的核心。"""
    assert cv2.imdecode(np.frombuffer(HEIC_BYTES, np.uint8), cv2.IMREAD_COLOR) is None
    img = decode_image(HEIC_BYTES)
    assert img is not None and img.shape == (24, 32, 3)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "AVIF", "TIFF", "BMP", "GIF"])
def test_decode_existing_formats_unchanged(fmt):
    """既有格式全走 cv2 快路径，加了回退不该改变它们的行为。"""
    img = decode_image(_img(fmt))
    assert img is not None and img.shape == (48, 64, 3)


@pytest.mark.parametrize("data", [b"", b"not-an-image-at-all", b"\xff\xd8\xfftruncated"])
def test_decode_rejects_garbage(data):
    assert decode_image(data) is None


def test_decode_rejects_pixel_bomb(monkeypatch):
    """字节闸挡不住解码后的像素量：小文件也能声明上亿像素。回退路径先看尺寸再决定解不解。"""
    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_MAX_DECODE_PIXELS", 100)  # 32*24=768 > 100 → 应被拒
    assert iu.decode_image(HEIC_BYTES) is None


# ── normalize_for_storage ─────────────────────────────────────────────────


@pytest.mark.parametrize("fmt,want", [("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")])
def test_normalize_passthrough_is_byte_identical(fmt, want):
    """白名单**原字节直通**：这是「存储层零转码」这条既有承诺的回归钉子，别让优化把它吃掉。"""
    raw = _img(fmt)
    got, ext = normalize_for_storage(raw)
    assert ext == want
    assert got == raw


def test_normalize_heic_to_lossless_webp():
    got, ext = normalize_for_storage(HEIC_BYTES, prefer="webp")
    assert ext == "webp"
    back = decode_image(got)
    assert back is not None and back.shape == (24, 32, 3)


def test_normalize_heic_to_jpeg_for_reference_crops():
    """参考图落盘走 JPEG：其唯一消费者 omni 恒收 JPEG，且 ref_crop_N.jpg 的后缀是硬编码。"""
    got, ext = normalize_for_storage(HEIC_BYTES, prefer="jpg")
    assert ext == "jpg"
    assert got[:3] == b"\xff\xd8\xff"
    back = decode_image(got)
    assert back is not None and back.shape == (24, 32, 3)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "GIF", "AVIF"])
def test_normalize_previously_rejected_formats(fmt):
    """行为扩面：这些格式原先能过 observe 却被头像/参考图端点 400，本轮消掉该不对称。"""
    got, ext = normalize_for_storage(_img(fmt))
    assert ext == "webp" and got


def test_normalize_returns_none_on_undecodable():
    assert normalize_for_storage(b"nope") is None


def test_normalize_falls_back_to_jpeg_when_webp_encode_fails():
    """WebP 边长上限 16383，超了 cv2.imencode 返回 ok=False 且只往 stderr 打——不判 ok 就会
    把空 buf 当图落盘。此处用超宽图触发真实失败路径（不 mock），确认退到 JPEG。"""
    wide = np.zeros((8, 20000, 3), np.uint8)
    ok, buf = cv2.imencode(".bmp", wide)  # BMP 非直通格式 → 必走重编
    assert ok
    got, ext = normalize_for_storage(buf.tobytes(), prefer="webp")
    assert ext == "jpg" and got[:3] == b"\xff\xd8\xff"


# ── is_still_image_container（判形）────────────────────────────────────────


def test_brand_table_separates_still_image_from_video():
    assert is_still_image_container(HEIC_BYTES[:16]) is True
    for brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif", b"miaf"):
        assert is_still_image_container(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 4)
    # 真视频容器不得被误判成图片（否则视频注册整条失效）
    for brand in (b"isom", b"mp42", b"qt  ", b"3gp4", b"M4V "):
        assert not is_still_image_container(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 4)
    # 非 ISO BMFF / 头部太短
    assert not is_still_image_container(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
    assert not is_still_image_container(b"ftyp")
