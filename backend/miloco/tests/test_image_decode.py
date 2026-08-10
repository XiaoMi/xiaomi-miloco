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


def test_heic_without_decoder_logs_at_failure_site(monkeypatch, caplog):
    """pi-heif 缺失时，HEIC 上传要在**失败现场**留一条可定位的日志，而不是只在启动期打一次
    （那条很可能早已滚走）。这也是 _HEIF_OK 这个标志位存在的意义。"""
    import logging

    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_HEIF_OK", False)
    with caplog.at_level(logging.WARNING, logger=iu.__name__):
        assert iu.decode_image(HEIC_BYTES) is None
    assert "heif_upload_without_decoder" in caplog.text


def test_avif_not_short_circuited_when_heif_decoder_missing(monkeypatch):
    """缺 pi-heif 的短路只能针对**真正需要 libheif** 的 brand。AVIF 同为 ftyp 容器，但它由
    Pillow 自带插件解、与 pi-heif 无关，一并短路会凭空砍掉一种本可用的格式。

    普通 AVIF 走 cv2 快路径、到不了那个分支，所以这里把 cv2 打成「解不出」来逼它走回退——
    否则这条修复没有任何用例能触发，等于没钉。"""
    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_HEIF_OK", False)
    monkeypatch.setattr(iu.cv2, "imdecode", lambda *a, **k: None)
    assert iu.decode_image(_img("AVIF")) is not None  # 走 Pillow 回退，仍该解得出
    assert iu.decode_image(HEIC_BYTES) is None        # HEIC 确实需要 libheif → 仍短路


def test_non_heif_unaffected_when_decoder_missing(monkeypatch):
    """解码器缺失只该影响 HEIF 家族；既有格式仍走 cv2 快路径，不受牵连。"""
    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_HEIF_OK", False)
    assert iu.decode_image(_img("PNG")) is not None


def test_decode_rejects_pixel_bomb_on_fallback_path(monkeypatch):
    """回退路径（HEIF）：PIL 懒加载，能在解码**前**按声明尺寸拒掉。"""
    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_MAX_DECODE_PIXELS", 100)  # 32*24=768 > 100 → 应被拒
    assert iu.decode_image(HEIC_BYTES) is None


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_decode_rejects_pixel_bomb_on_cv2_fast_path(monkeypatch, fmt):
    """快路径也必须卡：注释点名的 PNG 炸弹走的正是 cv2，只卡回退分支等于没卡。
    cv2 无懒加载，那一次分配躲不掉；此闸拦的是它继续进 YOLO / omni / 编码链路被反复复制。"""
    import miloco.perception.engine.identity._image_utils as iu

    monkeypatch.setattr(iu, "_MAX_DECODE_PIXELS", 1000)  # 64*48=3072 > 1000 → 应被拒
    assert iu.decode_image(_img(fmt)) is None


def test_heic_fallback_returns_bgr_not_rgb():
    """**通道序**回归钉子：Pillow 回退拿到的是 RGB，整条流水线（YOLO / crop / imencode）都按
    BGR 处理，漏掉 cvtColor 会让所有 HEIC 的红蓝对调——而毛色正是宠物识别的核心特征。
    此前删掉那行 cvtColor，132 条相关用例全绿，等于零覆盖。"""
    # 用现成的 HEIC fixture：它是纯 RGB(200,40,40) 的红色块（pi-heif 只能解码不能编码，
    # 测试期造不出新 HEIC，见文件顶部说明）。HEIF 有损，用宽松阈值判通道归属即可。
    img = decode_image(HEIC_BYTES)
    assert img is not None
    b, g, r = img[12, 16].tolist()  # OpenCV 通道序 = BGR
    assert r > 150 and b < 110, f"红色被解成 BGR=({b},{g},{r})，通道序错了"


def test_oversized_image_returns_none_without_raising():
    """cv2 对超出 OpenCV 自身尺寸上限的图是 **CV_Assert 抛异常**而非返 None。29 字节的 GIF 头
    就能声明 40000x40000 触发它；不接住的话异常穿透全部 9 个接入点，把本该 400 的请求变成
    500，且违反 decode_image docstring 承诺的「解不出返回 None(不抛)」。"""
    import struct

    gif = (
        b"GIF89a"
        + struct.pack("<HHBBB", 40000, 40000, 0, 0, 0)
        + b"\x2C"
        + struct.pack("<HHHHB", 0, 0, 40000, 40000, 0)
        + b"\x02\x02\x44\x01\x00\x3B"
    )
    assert len(gif) < 100  # 字节闸放不住它
    assert decode_image(gif) is None  # 关键是**不抛**


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


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8\xff" + b"garbage" * 40,                 # 合法 JPEG 魔数 + 坏 body
        b"\x89PNG\r\n\x1a\n" + b"garbage" * 40,          # 合法 PNG 魔数 + 坏 body
        b"RIFF\x00\x00\x00\x00WEBP" + b"garbage" * 40,    # 合法 WebP 魔数 + 坏 body
    ],
)
def test_normalize_rejects_valid_magic_with_broken_body(data):
    """**回归钉子**：只查魔数不足以验真。传输截断 / 拷了一半的照片同样命中前几字节，
    若直通落盘，识别侧解不出会静默跳过 → 界面显示「3 张参考图」而实际注入 0 张，
    正是参考图端点注释点名要防的失败模式。所以白名单也必须先完整解码。"""
    assert normalize_for_storage(data) is None


@pytest.mark.parametrize(
    "prefer,const", [("webp", "_AVATAR_MAX_SIDE"), ("jpg", "_REF_CROP_MAX_SIDE")]
)
def test_normalize_caps_long_side_per_purpose(prefer, const):
    """重编支按用途分档封顶。两档都锚在真实消费端上：头像 256 = web 裁剪器的 OUT（让
    CLI/API 与 web 产出同规格）；参考图 640 = omni 拼图高 320 的一倍余量。封顶只作用于
    重编支——直通支的逐字节相等由 test_normalize_passthrough_is_byte_identical 守着。"""
    import miloco.perception.engine.identity._avatar as av

    cap = getattr(av, const)
    big = np.zeros((3000, 2000, 3), np.uint8)
    ok, buf = cv2.imencode(".bmp", big)  # BMP 非直通 → 必走重编
    assert ok
    got, _ext = normalize_for_storage(buf.tobytes(), prefer=prefer)
    back = decode_image(got)
    assert max(back.shape[:2]) == cap
    assert back.shape[:2] == (cap, cap * 2000 // 3000)


def test_avatar_cap_matches_web_cropper_output():
    """头像档必须与 web 裁剪器的 OUT 对齐——这个数字的意义就是「两条入口同规格」，
    改动其中一侧而不改另一侧，就把这条口径悄悄拆了。"""
    import re
    from pathlib import Path

    import miloco.perception.engine.identity._avatar as av

    src = (
        Path(__file__).resolve().parents[3]
        / "web/src/components/AvatarCropEditor.tsx"
    ).read_text(encoding="utf-8")
    m = re.search(r"const OUT = (\d+)", src)
    assert m, "AvatarCropEditor 里找不到 OUT 常量"
    assert av._AVATAR_MAX_SIDE == int(m.group(1))


def test_normalize_falls_back_to_jpeg_when_webp_encode_returns_not_ok():
    """钉住 `ok=False` 分支：cv2.imencode 写 WebP 失败时只往 stderr 打一行、不抛异常，
    不判 ok 就会把空 buf 当图落盘。尺寸没超限时只能靠 mock 触发。"""
    import miloco.perception.engine.identity._avatar as av

    real = cv2.imencode

    def _fake(ext, img, *a, **k):
        if ext == ".webp":
            return False, np.zeros((0,), np.uint8)
        return real(ext, img, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(av.cv2, "imencode", _fake)
        got, ext = normalize_for_storage(_img("BMP"), prefer="webp")
    assert ext == "jpg" and got[:3] == b"\xff\xd8\xff"


# ── is_still_image_container（判形）────────────────────────────────────────


def _ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 4


def test_brand_table_covers_every_declared_still_image_brand():
    """**全覆盖**表里每一个 brand，别只抽查几个——这张表是手工维护的（CLI 侧还有一份平行
    副本），漏一个就意味着那种 HEIF 变体会被当视频送进抽帧路径、静默拿到瓦片。"""
    import miloco.perception.engine.identity._image_utils as iu

    assert is_still_image_container(HEIC_BYTES[:16]) is True
    for brand in sorted(iu._HEIF_BRANDS):
        assert is_still_image_container(_ftyp(brand)), brand
    assert iu._HEIF_BRANDS == iu._HEIF_ONLY_BRANDS | iu._AVIF_BRANDS
    assert not (iu._HEIF_ONLY_BRANDS & iu._AVIF_BRANDS)  # 两子集不得重叠


def test_still_image_brand_table_is_pinned():
    """整表**字面量**快照，与 CLI 侧 _STILL_IMAGE_BRANDS 同步。理由见 CLI 侧同名用例：
    「遍历表本身」+「两侧相等」都漏掉『两边同时删同一个 brand』这种协同漂移。"""
    import miloco.perception.engine.identity._image_utils as iu

    assert iu._HEIF_BRANDS == {
        b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
        b"mif1", b"msf1", b"miaf", b"mia1",
        b"avif", b"avis",
    }


def test_video_brands_never_treated_as_still_image():
    """真视频容器不得被误判成图片，否则视频注册整条失效。"""
    for brand in (b"isom", b"mp42", b"mp41", b"qt  ", b"3gp4", b"3gp5", b"M4V ", b"avc1", b"iso2"):
        assert not is_still_image_container(_ftyp(brand)), brand


def test_non_isobmff_and_short_headers():
    assert not is_still_image_container(b"\xff\xd8\xff\xe0" + b"\x00" * 12)  # JPEG
    assert not is_still_image_container(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)  # PNG
    assert not is_still_image_container(b"ftyp")  # 头部太短
    assert not is_still_image_container(b"")


def test_cli_and_backend_brand_tables_agree():
    """两侧各有一份手工表（后端 Python / CLI Python，进程不同无法共享常量）。它们必须一致，
    否则会出现「CLI 放行、后端当视频」或反之的错位。"""
    import sys
    from pathlib import Path

    cli_src = Path(__file__).resolve().parents[3] / "cli" / "src"
    sys.path.insert(0, str(cli_src))
    try:
        from miloco_cli.commands.identity import _STILL_IMAGE_BRANDS
    finally:
        sys.path.remove(str(cli_src))
    import miloco.perception.engine.identity._image_utils as iu

    assert set(_STILL_IMAGE_BRANDS) == set(iu._HEIF_BRANDS)


# ── 结构性护栏：解码不得留在事件循环上 ──────────────────────────────────────


def test_no_sync_decode_image_in_routers():
    """``decode_image`` 在 router 里必须一律经 ``asyncio.to_thread``。

    这条是**防复发**的：格式放开后解码从「几十毫秒解 jpg」变成「几百毫秒走 libheif」，而后端是
    单进程 asyncio、同一个循环上还跑着直播转码 / 录制切片 / MQTT 感知推理。本 PR 一度只在三个
    存储端点上做了这件事、人像侧 7 处漏掉，注释却已宣称「本仓一律如此」——靠人读注释守不住，
    用测试钉死：新增裸调会直接红。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/miloco"
    offenders = []
    for f in (root / "person/router.py", root / "pet/router.py", root / "pet/observe.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # 裸调 = 出现 decode_image( 但同一行没有 to_thread
            if re.search(r"[=\s(]decode_image\(", line) and "to_thread" not in line:
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    # pet/observe.py 的 _prepare_crops 整段已由 observe_pet 包在 to_thread 里，属白名单
    offenders = [o for o in offenders if not o.startswith("observe.py")]
    assert not offenders, "发现未走 to_thread 的解码调用：\n" + "\n".join(offenders)
