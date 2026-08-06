"""``H264LiveEncoder`` 的回归测试。

前三条固化两个曾在 PyAV 17 下崩的点(见 wiki transcoder-pyav17-bugs):

- Bug #1: PyAV 17 移除了 ``VideoCodecContext.close()``,旧代码在 rebuild/close 路径
  调它会抛 AttributeError,每次 toggle/断开泄漏一组 codec context + 编码线程。
- Bug #2: 摄像头 PTS 未知时发哨兵 ``0xFFFFFFFFFFFFFFFF`` (uint64),PyAV 17 的
  ``frame.pts`` 是 signed int64,直接赋值会 OverflowError 丢帧;PPCS 重连窗口内
  集中爆发刷几十条 error + 画面卡顿。

第四条与 PyAV 版本无关,守的是预览链 libx264 线程数这条硬约束(>1 → Chrome on
Linux 硬解 OperationError 拒流 + 破 tune=zerolatency)。**清理 PyAV 17 兼容性
用例时不要连它一起删。**

这些都不连真 SDK / 摄像头,只起真 libx264 编码器(本机 PyAV 自带),够轻。
"""

from __future__ import annotations

import asyncio

import numpy as np
from miloco.miot.transcoder import H264LiveEncoder


def _bgr(w: int = 320, h: int = 240) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_close_does_not_raise_on_pyav17():
    """实例化 + 推一帧 + close,不应抛 AttributeError(PyAV 17 无 codec.close())。"""
    async def go():
        enc = H264LiveEncoder()
        await enc.encode(_bgr(), pts_ms=0)
        await enc.close()  # 旧代码这里 codec.close() → AttributeError

    asyncio.run(go())


def test_encode_handles_uint64_sentinel_pts():
    """摄像头哨兵 PTS 0xFFFFFFFFFFFFFFFF 不应让 encode 抛 OverflowError,而是正常出帧。"""
    async def go():
        enc = H264LiveEncoder()
        # 哨兵值:旧代码 frame.pts = pts_ms 直接 OverflowError 丢帧
        pkts = await enc.encode(_bgr(), pts_ms=0xFFFFFFFFFFFFFFFF)
        assert isinstance(pkts, list)  # 不抛即过;首帧通常即出 1 个 IDR 包
        # 关键不变量:pts_ms 入参跟 frame.pts 解耦,走的是本地计数器路径——否则哨兵值
        # 会进 signed int64 setter 崩。计数器随帧数自增、与传入的哨兵值无关,才证明
        # "修回 frame.pts=pts_ms" 这类回归会被这条挡住(光断言不抛挡不住)。
        assert enc._pts_counter == 1
        # 连续哨兵帧也不崩(模拟 PPCS 重连窗口),计数器只跟帧数走
        for _ in range(5):
            await enc.encode(_bgr(), pts_ms=0xFFFFFFFFFFFFFFFF)
        assert enc._pts_counter == 6
        await enc.close()

    asyncio.run(go())


def test_pts_counter_resets_on_resolution_rebuild():
    """分辨率切换重建编码器后 PTS 计数器从 0 重起(防与新编码器时间基错位)。"""
    async def go():
        enc = H264LiveEncoder()
        await enc.encode(_bgr(320, 240), pts_ms=0)
        await enc.encode(_bgr(320, 240), pts_ms=0)
        assert enc._pts_counter == 2
        # 换分辨率 → _open_encoder 重建 → 计数器应被重置
        await enc.encode(_bgr(640, 480), pts_ms=0)
        # 重建那帧是新编码器的第 0 帧,encode 后计数器为 1
        assert enc._pts_counter == 1
        await enc.close()

    asyncio.run(go())


def test_open_encoder_pins_libx264_thread_count():
    """预览链的 thread_count 必须是 1——硬约束,不是调参值。

    >1 会退到 slice/帧级并行:Chrome on Linux 的硬解 OperationError 拒流、网页预览
    黑屏,且破 tune=zerolatency 的零攒帧。故这里断言字面量 1 而非 ``ENCODE_THREADS``
    ——共享常量被调大时本用例必须红,而不是跟着一起变绿。要给别的编码链调大,请按
    miot/tuning.py 的 ⚠ 先把预览链摘出独立常量,连带改这条断言。
    """
    from miot.tuning import ENCODE_THREADS

    preview_hard_limit = 1
    # 共享常量还没分家时两者必须一致,否则预览链已经悄悄拿到 >1 的值
    assert ENCODE_THREADS == preview_hard_limit, (
        f"ENCODE_THREADS={ENCODE_THREADS} 被调大了:预览链的 1 是硬约束,"
        "请先按 miot/tuning.py 的 ⚠ 把预览链摘出独立常量再调"
    )

    async def go():
        enc = H264LiveEncoder()
        await enc.encode(_bgr(), pts_ms=0)  # 触发 _open_encoder + codec.open()
        assert enc._codec is not None
        # 读 open 之后的值:libx264 若把线程数改回去,这里能抓到(同 omni 那条口径)。
        assert enc._codec.thread_count == preview_hard_limit
        await enc.close()

    asyncio.run(go())
