"""`identity` 命令的「视频误用」判据测试。

回归的是一条会连锁成灾的判错：HEIF/AVIF 与 mp4/mov 共用 ISO BMFF 容器（都以 ``ftyp`` 盒开头），
若只看「字节 4..8 == ftyp」就把 iPhone 的 HEIC 判成视频，则 ``--image`` 会被拒、报错文案又把
agent 引到 ``--video``；服务端不嗅内容直接按视频抽帧，而 ffmpeg 不拼 HEIC 的 tile grid、只暴露
512x512 瓦片，最终静默拿一块瓦片当整帧、返回「没识别到人」。所以 brand 必须分开。
"""

from miloco_cli.commands.identity import _looks_like_video_bytes


def _ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 8


def test_heif_avif_brands_are_not_video():
    for brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis", b"miaf"):
        assert _looks_like_video_bytes(_ftyp(brand)) is None, brand


def test_real_video_brands_still_detected():
    for brand in (b"isom", b"mp42", b"qt  ", b"3gp4", b"M4V "):
        assert _looks_like_video_bytes(_ftyp(brand)) == "mp4/mov", brand


def test_non_isobmff_containers_unchanged():
    assert _looks_like_video_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 12) == "matroska/webm"
    assert _looks_like_video_bytes(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 4) == "avi"
    assert _looks_like_video_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12) is None
    assert _looks_like_video_bytes(b"") is None
    assert _looks_like_video_bytes(b"ftyp") is None  # 头部太短


def test_ftyp_without_brand_bytes_falls_back_to_video():
    """只有 8 字节、读不到 brand 时保持原判（宁可拦下让人改 --video，也不放视频进图片路径）。"""
    assert _looks_like_video_bytes(b"\x00\x00\x00\x18ftyp") == "mp4/mov"
