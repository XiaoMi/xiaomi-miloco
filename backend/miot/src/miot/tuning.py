# -*- coding: utf-8 -*-
# Copyright (C) 2026 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""FFmpeg / libx264 线程上限——单路实时场景的统一调参点。

想知道某个值用在哪,grep 常量名即可(不在此维护站点清单,清单会过期)。
"""

DECODE_THREADS = 4   # FFmpeg 解码器 slice 线程,含余量
SWSCALE_THREADS = 2  # 独立 swscale 像素转换:1 已够,留 1 压单帧尖峰。注意缩放上下文是
                     # 每帧新建的(reformatter 挂在 frame 上,见 av/video/frame.py:656),
                     # 这多出来的 1 是每帧建拆一次线程池、不是常驻余量——真要再省就压到 1,
                     # 但缺实测、本轮不动。
ENCODE_THREADS = 1   # libx264:每条 BGR→H.264 链各自已串行跑在专属线程里,内部并行
                     # 买不到吞吐。设它也顺带把 encode 内部那次 bgr24→yuv420p 的
                     # swscale 收敛到 1——PyAV 取的就是 codec.thread_count
                     # (av/video/codeccontext.py:95)。
                     #
                     # ⚠ 这个 1 不只是性能取值:预览链(miloco/miot/transcoder.py)靠它
                     # 维持 tune=zerolatency 的零攒帧——那条链的 x264-params 写了
                     # sliced-threads=0,一旦 >1 会退到帧级并行、先攒 N 帧再吐包,直播
                     # 预览起步延迟 +~33ms/线程。要调大请先把预览链单独摘出去用独立值。
