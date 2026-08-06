"""Identity 内部 image 工具:pHash / Hamming / Sharpness 单一来源。

历史:这三个 helper 曾在 ``engine`` / ``extractor`` / ``library`` / ``registration_filter`` /
``tier_u`` 多处独立实现,注释都标 "同口径",但 ``compute_sharpness`` 实际已分裂
(``extractor`` 版在 4-channel BGRA / 单通道 ``(H,W,1)`` 形态下 ``cvtColor`` 会失败,
``engine`` 版有完整 defensive 处理)。``_phash`` 也已有 ``tier_u`` 跨模块 import extractor
的"自觉是 duplication"hack。本模块统一作为单一权威来源,消除分裂风险。

模块名以 ``_`` 开头表示包内私有 helper。例外:``decode_image`` 是「用户上传字节 → BGR」的
单一入口,pet / person 两侧 router 与 observe 都要用,因此允许跨包 import(否则每个入口各写
一遍 cv2/Pillow 双路径,必然分裂)。
"""

from __future__ import annotations

import io
import logging

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# HEIC/HEIF 解码器注册:进程级全局,放 import 期做一次。pi_heif 只接管 .heic/.heif,
# 不动 Pillow 12 自带的 AVIF 插件(已验证注册后 .avif 仍映射到原生 AVIF)。
try:
    import pi_heif

    pi_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:  # noqa: BLE001 — 缺失/加载失败不该让整个后端起不来
    _HEIF_OK = False
    logger.warning("event=heif_decoder_unavailable 上传的 HEIC/HEIF 将无法解码", exc_info=True)

# 解码后像素数上限。字节闸挡不住这个:HEIF 的网格容器、PNG 的高压缩比都能让 1MB 文件
# 解出上亿像素(BGR 三通道 → 每像素 3 字节)。1.2 亿像素 ≈ 360MB,已远超任何真实相机
# (12MP iPhone = 0.12 亿),留足余量的同时**挡住炸弹进入下游**。
# 口径说明:回退路径靠 PIL 懒加载能在解码**前**拒;cv2 快路径无懒加载,只能解完再拒——
# 那一次分配躲不掉,拦的是它继续进 YOLO / omni / 编码链路被反复复制放大。
_MAX_DECODE_PIXELS = 120_000_000


# ISO BMFF（``ftyp`` 盒）里属于**静态图片**的品牌。HEIF / AVIF 与 mp4/mov 共用同一套容器
# 结构，只靠「字节 4..8 == ftyp」判不出图与视频——而这个误判有实际后果：ffmpeg 会把 HEIC 的
# tile grid 当成几十条独立的 512x512 视频流暴露、不做拼接，于是「按视频处理一张 HEIC」会
# 静默拿到一块瓦片当整帧，全链路零报错地在错素材上跑。
_HEIF_BRANDS = frozenset(
    {
        b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
        b"mif1", b"msf1", b"miaf", b"mia1",
        b"avif", b"avis",
    }
)


def is_still_image_container(head: bytes) -> bool:
    """文件头看起来是「ISO BMFF 静态图片」（HEIF / AVIF 家族）→ True。

    给「判图还是判视频」的分叉点用：命中即**不得**送进视频抽帧路径。取头部 16 字节即可
    （``ftyp`` 盒在最前，紧跟 4 字节 major brand）。不是 ISO BMFF、或 brand 是 mp4/mov/qt
    这类真视频 → False，由调用方按原逻辑继续判。
    """
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS


def decode_image(data: bytes) -> NDArray[np.uint8] | None:
    """用户上传字节 → BGR ndarray;解不出返回 ``None``(不抛)。

    两级:
      1. ``cv2.imdecode`` 快路径——覆盖 jpg/png/webp/bmp/tiff/gif/avif,零拷贝、无 PIL 往返。
      2. Pillow 回退——只有 cv2 认不出的容器才走到,实际上就是 HEIC/HEIF(iPhone 默认格式)。

    方向:cv2(``IMREAD_COLOR``)对 jpg/png/webp/tiff 会应用 EXIF Orientation;HEIF 侧由
    libheif 读取时按 ``irot`` 转好(实测 iPhone HEIC 出来即为竖图)。``exif_transpose`` 留着
    只为覆盖将来可能新增的非 HEIF 回退输入——pi_heif 会把 EXIF tag 274 无条件重置为 1
    (真值挪到 ``info["original_orientation"]``),所以它对 HEIF 恒是 no-op。

    **不要**改用 ``original_orientation`` 自己转:该键即便在 libheif 已应用 ``irot`` 时也仍带
    着原值,拿它转会把已经正过来的图再转一次(实测 iPhone 竖拍 HEIC 因此变成横图)。代价是
    「只用 EXIF 记方向、无 irot」的非苹果 HEIF 会歪——无法与「已转好」区分,取舍上宁可保住
    绝对多数的苹果 HEIC。

    已知限制:cv2 能解 AVIF 但**不**应用 AVIF 的 EXIF Orientation(实测 4.13),故 AVIF 竖拍
    仍可能歪——属既有行为,本函数不改快路径判据以免动到所有既有格式的解码结果。
    """
    if not data:
        return None
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is not None and img.size > 0:
        # cv2 无懒加载，这一次解码的内存分配躲不掉；此闸的作用是**别让它进下游**——
        # YOLO / omni / 编码链路每一步还要再复制放大。PNG 的高压缩比同样能造炸弹，
        # 所以快路径也得卡，不能只卡回退分支。
        if img.shape[0] * img.shape[1] > _MAX_DECODE_PIXELS:
            logger.warning(
                "event=decode_reject_pixels w=%s h=%s", img.shape[1], img.shape[0]
            )
            return None
        return img
    if not _HEIF_OK and is_still_image_container(data[:16]):
        # 传的是 HEIC/HEIF 而解码器在 import 期就没装上：在**失败现场**点明原因，别让排查的人
        # 只拿到一句笼统的「打不开」、再去翻可能早已滚走的启动日志。
        logger.warning("event=heif_upload_without_decoder 缺 pi-heif，无法解码 HEIC/HEIF")
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            n_px = (im.width or 0) * (im.height or 0)
            if n_px <= 0 or n_px > _MAX_DECODE_PIXELS:
                logger.warning("event=decode_reject_pixels w=%s h=%s", im.width, im.height)
                return None
            im = ImageOps.exif_transpose(im)  # 非 HEIF 容器(如将来放开截断 JPEG)才用得上
            arr = np.asarray(im.convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError, MemoryError):
        return None
    except Exception:  # noqa: BLE001 — 第三方解码器的任意异常都不该穿到端点变 500
        logger.warning("event=decode_fallback_failed", exc_info=True)
        return None
    if arr.size == 0:
        return None
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def compute_sharpness(crop: NDArray[np.uint8]) -> float:
    """Laplacian variance 估算 crop 清晰度。

    经典做法:对灰度图跑 3×3 Laplacian → 方差越大边缘越锐利 → 越清晰。

    输入兼容(穷举所有 ndarray 形态,极端 case 返 0.0 防御,不抛错):
      ===== 非图像 =====
      - ``None`` / ``size == 0``       → 0.0
      - ``ndim < 2`` (0-D scalar / 1-D) → 0.0(无法定义"清晰度")
      - ``ndim > 3`` (4-D batch 等)     → 0.0(同上)
      ===== 图像 =====
      - ``ndim == 2``                  → 直接当灰度(已经是 2-D 灰度图)
      - ``ndim == 3, shape[2] == 1``   → squeeze 末维到 2-D 灰度
      - ``ndim == 3, shape[2] == 3``   → BGR → 灰度(``cv2.imread`` 默认走这条)
      - ``ndim == 3, shape[2] == 4``   → BGRA/RGBA,取前 3 通道按 BGR 转灰度
      - ``ndim == 3, shape[2] == 2``   → 双通道(罕见),通道平均当灰度
      - ``ndim == 3, shape[2] >= 5``   → 极端多通道,通道平均当灰度

    业务路径(``cv2.imread`` 默认 BGR 3-channel)始终走 ``shape[2] == 3`` 主路径,
    其余分支都是防御兜底,行为不依赖意外形态的精度。
    """
    if crop is None or crop.size == 0:
        return 0.0
    # 非图像形态直接返 0.0,不进 Laplacian 路径
    if crop.ndim < 2 or crop.ndim > 3:
        return 0.0
    if crop.ndim == 2:
        gray = crop
    else:  # ndim == 3
        channels = crop.shape[2]
        if channels == 1:
            # squeeze 末维:(H, W, 1) → (H, W)
            gray = crop[..., 0]
        elif channels == 3:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        elif channels == 4:
            # BGRA / RGBA:取前 3 通道当 BGR 转灰度
            gray = cv2.cvtColor(crop[..., :3], cv2.COLOR_BGR2GRAY)
        else:
            # 2 通道 / 5+ 通道极端 case:通道平均当灰度(语义模糊但安全)
            gray = crop.mean(axis=-1).astype(crop.dtype)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def phash(image: NDArray[np.uint8], hash_size: int = 8) -> int:
    """计算图像感知哈希（pHash），返回 64-bit 整数。

    简化版 DCT-based pHash：
      1. 灰度化 + resize 到 32×32
      2. DCT
      3. 取左上 8×8 低频块
      4. 比较每位与块中位数（去掉 DC），> 中位数 = 1，否则 = 0

    自实现,不引入 imagehash 依赖。
    """
    if image is None or image.size == 0:
        return 0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # resize 到 32x32 增加 DCT 频率分辨率
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(resized)
    block = dct[:hash_size, :hash_size].copy()
    # 去掉 DC 系数（block[0,0]）只看高频中位
    dc = block[0, 0]
    block[0, 0] = 0.0
    median = float(np.median(block))
    # 还原 DC 用于哈希位计算
    block[0, 0] = dc
    bits = (block > median).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def hamming(h1: int, h2: int) -> int:
    """两个 64-bit 哈希的汉明距离。"""
    return bin(h1 ^ h2).count("1")
