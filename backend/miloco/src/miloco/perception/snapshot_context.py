"""asyncio-task-bound omni 多模态 clip 旁路收集器,给 meaningful_events 复用.

设计动机:
omni 推理链路深(processor → client.realtime_perceive → engine.api.run_batch_pipeline →
omni.run_omni_batch → prompt_builder.build_* → _encode_batch_video → _encode_video →
_encode_video_mp4),透穿 8 层函数签名加 out 参数会让 omni 模块跟 snapshot 模块强耦合.

改用 ContextVar(跟 task 绑定,asyncio-safe)从 omni 内部"旁路"出 omni 实际拿到的
字节(视频路径 H264+AAC mp4,或 audio-only 路径纯 AAC m4a).snapshot 模块负责
set/clear,omni 内部只在底层 _encode_video_mp4 / _encode_audio_only_mp4 加一个 push
出口 — 字节级 = omni 看到的,零重编.

参考 miloco.observability.context:trace_id 也是同款套路,reviewer 熟悉.

## 使用

processor 调用前后包一层:

    from miloco.perception.snapshot_context import ClipKind, snapshot_collector_scope

    sink: dict[str, tuple[bytes, ClipKind]] = {}
    with snapshot_collector_scope(sink):
        result = await proxy.realtime_perceive(batch)

    # sink 现已被 omni 填上 per-device 的 (bytes, kind) 元组:
    #   - 视频路径 ("...", "mp4"):H264 + AAC
    #   - audio-only 路径 ("...", "m4a"):仅 AAC (ipod muxer)

底层 omni 出口:

    from miloco.perception.snapshot_context import push_clip_bytes

    with open(tmp_path, "rb") as f:
        mp4_bytes = f.read()
    push_clip_bytes(mp4_bytes, "mp4")   # video path
    # 或
    push_clip_bytes(m4a_bytes, "m4a")   # audio-only path

device_id 来源:miloco.observability.context.DeviceContext.device_id — pipeline.py
在 omni call 期间已 set 好.无 active scope / 无 device_ctx 时静默 no-op.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal, TypedDict

from miloco.observability.context import get_device_context

if TYPE_CHECKING:
    from collections.abc import Iterator

# clip 字节的容器/codec 类型,持久化层据此选 filename 与 Content-Type.
# 主流 UI <video> 控件对两者都能渲染,但浏览器 / 一些播放器靠扩展名 sniff 容器,
# 所以扩展名要跟实际容器一致(M4A 不能伪装成 .mp4).
ClipKind = Literal["mp4", "m4a", "frames"]


class FrameClip(TypedDict):
    data: bytes
    media_type: str
    frame_index: int

# device_id → (bytes, kind);None 表示当前 task 没启动收集.
# 同 task 同 device 内多次 push 后入覆盖前者(omni 一次推理一个 device 只产一份 clip).
_clip_sink: ContextVar[dict[str, tuple[bytes | list[FrameClip], ClipKind]] | None] = ContextVar(
    "clip_sink", default=None
)


@contextmanager
def snapshot_collector_scope(
    sink: dict[str, tuple[bytes | list[FrameClip], ClipKind]],
) -> Iterator[None]:
    """在 with 块内开启 clip 字节收集,块结束自动 reset.

    Args:
        sink: 调用方提供的 dict,块内 push_clip_bytes 写入;退出后调用方读取.
              典型用法 sink={};退出后 sink = {device_id: (bytes, kind)}.

    asyncio-safe — ContextVar 跟当前 task 绑定,跨 await 不丢;子 task spawn 时复制
    父 task 当前值.同一 task 内嵌套 scope 会覆盖外层(realtime/on_demand 路径都是单层).
    """
    token = _clip_sink.set(sink)
    try:
        yield
    finally:
        _clip_sink.reset(token)


def push_clip_bytes(clip_bytes: bytes, kind: ClipKind) -> None:
    """omni 内部出口:把当前 device 的 clip 字节(及容器类型)存到当前 task 的 sink 里.

    device_id 自 observability.DeviceContext 取(pipeline 在 omni call 期间已 set).
    任一缺失(无 active scope / 无 device_ctx)时静默 no-op.

    clip_bytes 是 omni 实际上传给 LLM 的字节级数据;kind 告诉持久化层用什么扩展名:
    - "mp4":视频路径,H264 + AAC,落盘 clip.mp4 / Content-Type=video/mp4
    - "m4a":audio-only 路径,仅 AAC (ipod muxer),落盘 clip.m4a / Content-Type=audio/mp4
    """
    sink = _clip_sink.get()
    if sink is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    sink[ctx.device_id] = (clip_bytes, kind)


def push_frame_sequence(frames: list[FrameClip]) -> None:
    """把抽帧图片序列旁路给 meaningful_events 复用."""
    if not frames:
        return
    sink = _clip_sink.get()
    if sink is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    sink[ctx.device_id] = (frames, "frames")
