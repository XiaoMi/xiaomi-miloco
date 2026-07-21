# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""fps → 帧数换算的共享纯函数。

max_age / dead-track grace / tier_c cooldown / frames_per_window 等阈值对用户暴露的
都是墙钟秒数，内部按 fps 烘成帧数缓存（构造期算一次）。这些换算同时出现在构造期
``__init__`` 与运行时热更（``SortTracker.set_fps`` / ``DeepSortTracker.set_fps`` /
``IdentityEngine.set_engine_fps``，见 ``PerceptionEngine.apply_omni_fps``）两处。

把换算收敛到本模块的纯函数、两处都调它，从结构上杜绝「改一处公式漏改另一处 →
热更引擎与新建引擎的帧数阈值悄悄偏离」的漂移（原实现是两份硬编码拷贝，单测复刻
同一公式也抓不到）。
"""

from __future__ import annotations


def sec_to_frames(sec: float, fps: float) -> int:
    """把墙钟秒数按 fps 换算成帧数，至少 1 帧（防极端配置换算得 0）。"""
    return max(1, round(sec * fps))


def frames_per_window(fps: float, period_sec: float) -> float:
    """一个感知窗口内的帧数（fps × 窗口秒长），至少 1.0 帧。"""
    return max(1.0, fps * period_sec)
