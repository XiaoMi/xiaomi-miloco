"""asyncio-task-bound omni 事件 artifacts 旁路收集器,给 meaningful_events 复用.

设计动机:
omni 推理链路深(processor → client.realtime_perceive → engine.api.run_batch_pipeline →
omni.run_omni_batch → prompt_builder.build_* → _encode_batch_video → _encode_video →
_encode_video_mp4),透穿 8 层函数签名加 out 参数会让 omni 模块跟 snapshot 模块强耦合.

改用 ContextVar(跟 task 绑定,asyncio-safe)从 omni 内部"旁路"出三类产物:
- clip 字节(视频路径 H264+AAC mp4,或 audio-only 路径纯 AAC m4a)— 通过
  push_clip_bytes 在 _encode_video_mp4 / _encode_audio_only_mp4 出口推
- 逐帧 JPEG 列表(图像推理路径,input_mode="image")— 通过 push_frames 在
  _encode_frames_as_jpegs 出口推;与 clip 互斥(同一窗只走一种模态)
- omni HTTP 调用 trace(prompt + response + latency + usage + error)— 通过
  push_omni_trace 在 call_omni / call_omni_stream / _call_omni_messages 的
  finally 里推

snapshot 模块开 event_artifacts_scope,omni 内部 push,推完后整包随 clip 同
event_dir 落盘 — 字节级 = omni 看到的,零重编;trace 文件级 = 一次推理一份.

参考 miloco.observability.context:trace_id 也是同款套路,reviewer 熟悉.

## 使用

processor 调用前后包一层:

    from miloco.perception.snapshot_context import OmniEventArtifacts, event_artifacts_scope

    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        result = await proxy.realtime_perceive(batch)

    # artifacts.clips 已被 omni 填上 per-device 的 (bytes, kind) 元组:
    #   - 视频路径 ("...", "mp4"):H264 + AAC
    #   - audio-only 路径 ("...", "m4a"):仅 AAC (ipod muxer)
    # 图像推理路径则填 artifacts.frames(per-device 的 JPEG 字节列表),与 clips 互斥。
    # artifacts.trace 已被 omni HTTP 调用填上 prompt + response 结构

底层 omni 出口:

    from miloco.perception.snapshot_context import push_clip_bytes, push_frames, push_omni_trace

    push_clip_bytes(mp4_bytes, "mp4")   # 在 _encode_video_mp4 出口
    push_frames(jpeg_list)              # 在 _encode_frames_as_jpegs 出口(整组覆盖写)
    push_omni_trace(                    # 在 call_omni finally 里
        request_messages=messages,
        response_raw=raw,
        latency_ms=...,
        error=None,
        model=...,
    )

device_id 来源:miloco.observability.context.DeviceContext.device_id — pipeline.py
在 omni call 期间已 set 好.无 active scope / 无 device_ctx 时静默 no-op.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from miloco.observability.context import get_device_context

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# clip 字节的容器/codec 类型,持久化层据此选 filename 与 Content-Type.
# 主流 UI <video> 控件对两者都能渲染,但浏览器 / 一些播放器靠扩展名 sniff 容器,
# 所以扩展名要跟实际容器一致(M4A 不能伪装成 .mp4).
ClipKind = Literal["mp4", "m4a"]

# 事件级产物的类型(SSE / list API 的 clip_kind 字段,以及 client 侧取 kind 的标注):
# 在 clip 容器之外多一档 "frames" —— 图像推理模式没有 clip,落的是 per-device 逐帧 JPEG
# (见 snapshot_writer.FRAME_PREFIX)。**不并入 ClipKind**:ClipKind 只描述 clip 容器,
# 塞进 "frames" 会让 push_clip_bytes 的入参语义与 _save_clips 的容器校验一起失真。
MediaKind = Literal["mp4", "m4a", "frames"]


@dataclass
class OmniEventArtifacts:
    """一次 omni 触发事件的所有产物.

    每个字段是一种独立产物,互不污染:
    - clips: per-device 视频/音频字节(omni 上传给 LLM 的原始字节,零重编)
    - trace: prompt + response 文本结构(便于复盘 LLM 决策)
    - gallery: per-person 画廊合成图(body/face JPEG,omni 推理时实际参考的样本)
    - ref_frames: per-device 全景参考帧 JPEG(仅 Smart Crop 模式;crop 视频送模型时
      同附的整帧上下文,字节级 = omni 所见).非 crop 模式为空.
    - crop_meta: per-device crop 元数据(region 坐标/全景帧尺寸/编码短边),不独立落盘,
      在 push_omni_trace 时挂到对应 device 的 call 记录里进 trace,供 badcase 复现.

    扩展方式:在 dataclass 加新字段、snapshot_writer.save_event_artifacts 加分支即可.
    """

    clips: dict[str, tuple[bytes, ClipKind]] = field(default_factory=dict)
    trace: dict[str, Any] | None = None
    gallery: dict[str, dict[str, bytes]] = field(default_factory=dict)
    ref_frames: dict[str, bytes] = field(default_factory=dict)
    crop_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    # frames: per-device 逐帧 JPEG 列表(图像推理模式,input_mode="image"),按时间先后排列。
    # 与 clips 互斥 —— 同一窗口只走一种模态,故落盘时一个 device 要么有 clip 要么有 frames,
    # 不会两者都有。落盘为 frame_00.jpg / frame_01.jpg…,字节级 = 送模型的帧(零重编)。
    # 整组覆盖写(见 push_frames),不是逐帧追加 —— 回退路径重编时会整组替换。
    frames: dict[str, list[bytes]] = field(default_factory=dict)


_artifacts: ContextVar[OmniEventArtifacts | None] = ContextVar(
    "artifacts", default=None
)


@contextmanager
def event_artifacts_scope(artifacts: OmniEventArtifacts) -> Iterator[None]:
    """在 with 块内开启事件 artifacts 收集,块结束自动 reset.

    Args:
        artifacts: 调用方提供的 OmniEventArtifacts 实例;块内 push_clip_bytes /
                   push_omni_trace 写入,退出后调用方读取.

    asyncio-safe — ContextVar 跟当前 task 绑定,跨 await 不丢;子 task spawn 时复制
    父 task 当前值.嵌套 scope 会覆盖外层(realtime/on_demand 路径都是单层).
    """
    token = _artifacts.set(artifacts)
    try:
        yield
    finally:
        _artifacts.reset(token)


def push_clip_bytes(clip_bytes: bytes, kind: ClipKind) -> None:
    """omni 内部出口:把当前 device 的 clip 字节(及容器类型)存到当前 task 的 artifacts.clips.

    device_id 自 observability.DeviceContext 取(pipeline 在 omni call 期间已 set).
    任一缺失(无 active scope / 无 device_ctx)时静默 no-op.

    clip_bytes 是 omni 实际上传给 LLM 的字节级数据;kind 告诉持久化层用什么扩展名:
    - "mp4":视频路径,H264 + AAC,落盘 clip.mp4 / Content-Type=video/mp4
    - "m4a":audio-only 路径,仅 AAC (ipod muxer),落盘 clip.m4a / Content-Type=audio/mp4
    """
    artifacts = _artifacts.get()
    if artifacts is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    artifacts.clips[ctx.device_id] = (clip_bytes, kind)


def push_frames(jpegs: list[bytes]) -> None:
    """omni 出口(图像推理模式):把当前 device 本窗的整组帧 JPEG 存到 artifacts.frames.

    device_id 自 observability.DeviceContext 取(pipeline 在 omni call 期间已 set).
    任一缺失(无 active scope / 无 device_ctx)时静默 no-op.

    jpegs 是图像模式下实际送模型的帧(字节级 = omni 所见),按时间先后排列,落盘
    frame_00.jpg / frame_01.jpg… 供复盘。

    **整组一次性传入,且是覆盖语义(不是追加)** —— 与 push_clip_bytes 的 dict 赋值对齐:
    Smart Crop 先编 crop 帧、任一步失败则调用方回退全景再编一遍,若这里改成 append,
    同窗就会留下 2N 张(crop N 张 + 全景 N 张)、与模型实际收到的 N 张错位。
    覆盖赋值天然幂等,回退路径重复调用只会留下最后那次(即真正送模型的那组)。

    空列表静默跳过:整批编码失败时不能把上一窗的帧留在 artifacts 里冒充本窗。

    与 push_clip_bytes 互斥 —— 图像模式不产 clip,故不会同时写入 artifacts.clips。
    """
    if not jpegs:
        return
    artifacts = _artifacts.get()
    if artifacts is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    artifacts.frames[ctx.device_id] = list(jpegs)


def push_ref_frame(image_bytes: bytes) -> None:
    """omni prompt 构建阶段(Smart Crop):把当前 device 的全景参考帧 JPEG 存到 artifacts.ref_frames.

    device_id 自 observability.DeviceContext 取(pipeline 在 omni call 期间已 set,含 fused
    单 device 路径).任一缺失(无 active scope / 无 device_ctx)时静默 no-op.

    image_bytes 是 crop 模式下与 crop 视频一并上送 LLM 的整帧 JPEG(字节级 = omni 所见),
    落盘 ref.jpg 供 badcase 复盘对照「模型看到的全景上下文」.仅 crop 路径调用.
    """
    artifacts = _artifacts.get()
    if artifacts is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    artifacts.ref_frames[ctx.device_id] = image_bytes


def push_crop_meta(
    *, region: tuple[int, int, int, int], frame_size: tuple[int, int], short_edge: int
) -> None:
    """omni prompt 构建阶段(Smart Crop):暂存当前 device 的 crop 元数据,供 trace 挂载.

    Args:
        region: crop 区域像素坐标 (x1, y1, x2, y2),相对全景帧(未缩放的 all_frames 空间).
        frame_size: 全景帧尺寸 (w, h),复现时据此把 region 归一/映射回参考帧.
        short_edge: crop 视频编码短边(降采样目标),记录 omni 实际所见的分辨率上限.

    不独立落盘 —— 在 push_omni_trace 里按 device_id 取出挂到 call 记录("crop"),随
    omni_trace.json.gz 一起持久化.device_id / active scope 缺失时静默 no-op.
    """
    artifacts = _artifacts.get()
    if artifacts is None:
        return
    ctx = get_device_context()
    if ctx is None:
        return
    x1, y1, x2, y2 = region
    w, h = frame_size
    artifacts.crop_meta[ctx.device_id] = {
        "region_xyxy": [int(x1), int(y1), int(x2), int(y2)],
        "frame_size_wh": [int(w), int(h)],
        "crop_short_edge": int(short_edge),
    }


def push_gallery_image(person_id: str, kind: str, image_bytes: bytes) -> None:
    """omni prompt 构建阶段:缓存当前事件使用的画廊合成图.

    Args:
        person_id: 成员 ID.
        kind: "body" 或 "face".
        image_bytes: JPEG 或 PNG 字节(落盘时按 magic bytes 自动判别扩展名).

    无 active scope 时静默 no-op.
    """
    artifacts = _artifacts.get()
    if artifacts is None:
        return
    if person_id not in artifacts.gallery:
        artifacts.gallery[person_id] = {}
    artifacts.gallery[person_id][kind] = image_bytes


def push_omni_trace(
    *,
    request_messages: list[dict[str, Any]],
    response_raw: dict[str, Any] | None,
    latency_ms: float,
    error: dict[str, Any] | None,
    model: str,
    inference_params: dict[str, Any] | None = None,
) -> None:
    """omni HTTP 调用出口(含失败 finally 分支):累积一次调用到 artifacts.trace.

    Args:
        request_messages: omni 上送的 messages list(OpenAI 形态).非 text block
            会被 _strip_base64 剥到只剩 type,base64 内容不进 trace.
        response_raw: omni 返回 raw dict(含 choices/usage).HTTP 失败时传 None.
            stream 路径调用方需自行拼伪 raw(content 拼接 chunks, usage 兜底空 dict).
        latency_ms: 单次 omni HTTP 调用耗时.
        error: 失败时 {"code": ..., "msg": ...},成功时 None.
        model: omni 模型 ID.
        inference_params: 推理参数(temperature / top_p / max_tokens 等，键名与 wire payload 对齐),
            供复现时可直接铺进请求体还原完整 API call.

    device_id 从 ContextVar(DeviceContext)取并写入 call 记录,让多摄像头 batch
    的多条 call 能跟 artifacts.clips 的 device 维度对齐.生产 batch pipeline
    (_process_device)在 omni call 期间已 set device_context → 正常记 device_id
    (Smart Crop 的 crop_meta 亦据此挂载);未 set device_context 的路径记 null,
    reader 据此识别"整批共享一次推理".

    无 active scope 时静默 no-op.内部任何异常吞掉 + logger.error,不影响 omni 主流程.
    """
    try:
        artifacts = _artifacts.get()
        if artifacts is None:
            return
        if artifacts.trace is None:
            artifacts.trace = {"schema_version": 1, "calls": []}
        ctx = get_device_context()
        call_record: dict[str, Any] = {
            "device_id": ctx.device_id if ctx is not None else None,
            "model": model,
            "request": _strip_base64(request_messages),
            "response": _pick_response_fields(response_raw),
            "latency_ms": latency_ms,
            "error": error,
        }
        if inference_params:
            call_record["inference_params"] = inference_params
        # Smart Crop:该 device 本次走了裁切 → 把 crop 坐标/尺寸挂进 call 记录,
        # 让 trace 能复现「模型看到的是全景哪块」(非 crop 无此 key).
        if ctx is not None:
            crop = artifacts.crop_meta.get(ctx.device_id)
            if crop:
                call_record["crop"] = crop
        artifacts.trace["calls"].append(call_record)
    except Exception as e:  # noqa: BLE001
        logger.error("push_omni_trace failed: %s", e)


def _strip_base64(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """剥掉非 text block 的 base64,重组为 {system, user_blocks}.

    text block 保留原文;video_url / image_url block 只保留 type 占位 — 字节级数据
    已经在 artifacts.clips 里独立落盘,trace 文件没必要再冗余 ~MB 级 base64.

    输入是 OpenAI messages list 形态;输出展平掉 role 维度(只取 system + user),
    reader 不用再过滤 role.
    """
    system = ""
    user_blocks: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str):
                system = content
        elif role == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    user_blocks.append({"type": "text", "text": block.get("text", "")})
                elif t in ("video_url", "image_url"):
                    user_blocks.append({"type": t})
    return {"system": system, "user_blocks": user_blocks}


def _pick_response_fields(raw: dict[str, Any] | None) -> dict[str, Any]:
    """从 OpenAI raw response 抽 choices[0].message.content + usage.

    raw=None(HTTP 失败)或 choices 为空时返空字符串 + 空 usage,保证 schema 稳定.
    """
    if raw is None:
        return {"content": "", "usage": {}}
    choices = raw.get("choices") or []
    content = ""
    if choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message") or {}
            if isinstance(message, dict):
                content = message.get("content", "") or ""
    usage = raw.get("usage") or {}
    return {"content": content, "usage": usage}
