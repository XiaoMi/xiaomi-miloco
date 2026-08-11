"""守住三处像素转换共享的 ``threads=`` 关键字参数在当前 PyAV 上真实存在。

调用点:``miot/decoder.py`` 的 ``to_ndarray`` / ``to_rgb``、
``miloco/miot/transcoder.py`` 的 ``reformat``(grep ``SWSCALE_THREADS``)。这三处是
本轮唯一「往第三方 API 传关键字参数」的一类——其余调参点都是给对象属性赋值,名字写错
当场 AttributeError。而 ``decoder.py`` 那处的 ``except Exception: continue`` 会把
TypeError 吞成「逐帧 warning + 帧全丢」:升降级 PyAV 后这条链会坏得毫无声响,故单独
锁住这个形参。

文件放在 ``miloco/tests`` 而不是 ``miot/tests``(三处里两处在 miot 侧):
``backend/pyproject.toml`` 的 ``norecursedirs`` 排除了 ``miot``,放那边 CI 收集不到。
"""

from __future__ import annotations

import av
from miot.tuning import SWSCALE_THREADS


def test_pyav_accepts_swscale_threads_kwarg():
    # 形参不存在 / 被改名 → TypeError,三条断言任一都会直接红
    yuv = av.VideoFrame(64, 64, format="yuv420p")
    assert yuv.to_ndarray(format="bgr24", threads=SWSCALE_THREADS).shape == (64, 64, 3)
    assert yuv.to_rgb(threads=SWSCALE_THREADS).format.name == "rgb24"

    bgr = av.VideoFrame(64, 64, format="bgr24")
    reformatted = bgr.reformat(format="yuv420p", threads=SWSCALE_THREADS)
    assert reformatted.format.name == "yuv420p"
