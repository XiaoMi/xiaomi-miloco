"""Identity 内部 image 工具:pHash / Hamming / Sharpness 单一来源。

历史:这三个 helper 曾在 ``engine`` / ``extractor`` / ``library`` / ``registration_filter`` /
``tier_u`` 多处独立实现,注释都标 "同口径",但 ``compute_sharpness`` 实际已分裂
(``extractor`` 版在 4-channel BGRA / 单通道 ``(H,W,1)`` 形态下 ``cvtColor`` 会失败,
``engine`` 版有完整 defensive 处理)。``_phash`` 也已有 ``tier_u`` 跨模块 import extractor
的"自觉是 duplication"hack。本模块统一作为单一权威来源,消除分裂风险。

模块名以 ``_`` 开头表示包内私有 helper;外部调用方应去其它公开入口,不要直接 import 本模块。
"""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

# ``compute_sharpness`` 结果归一化到 [0,1] 时的饱和参考值(Laplacian variance),
# 用于 v4 §2.2.3 TierA / TierU 的候选评分公式。
# 与本模块的三个 helper 同理放在这里作单一来源:它此前在 ``extractor`` 与 ``tier_u``
# 各定义一份、靠注释宣称"沿用对方"维系,正是本模块顶部记录的那种分裂前状态。
SHARPNESS_NORM_REF: float = 300.0


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
