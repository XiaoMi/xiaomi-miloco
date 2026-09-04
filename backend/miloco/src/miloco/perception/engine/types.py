"""Perception Engine MVP — Shared Types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from miloco.perception.types import DeviceSnapshot

# =============================================================================
# Data Input Layer
# =============================================================================

# InputSlice is now an alias for the canonical DeviceSnapshot from shared.
# DeviceSnapshot provides compatible properties: room_name, frames, audio_clip,
# sample_rate, start_timestamp, end_timestamp.
InputSlice = DeviceSnapshot


# =============================================================================
# Gate Layer
# =============================================================================


@dataclass
class GateTrigger:
    visual_changed: bool
    visual_change_score: float  # 0-1
    audio_active: bool
    audio_energy_level: float  # 0-1
    # 音频过能量 gate 后,silero VAD 判本窗有真人声。门控 speeches 字段:False 时
    # 下游只剥 speeches(env_sounds / 喂音频不受影响)。audio_active=False 时恒 False。
    speech_active: bool = False
    # 滞回拉起且本窗有帧。给 _is_audio_only 选路用:hold=True 不降级到 audio-only。
    # 三处 hold 取值的对照见 knowledge/03-features/perception-pipeline.md 的 Gate 小节。
    hold: bool = False


@dataclass
class GatePacket:
    packet_id: str
    room_name: str
    timestamp: float
    trigger: GateTrigger
    frames: list[NDArray[np.uint8]]
    audio_clip: NDArray[np.int16]
    sample_rate: int = 16000
    fps: int = 1  # actual fps of frames after pipeline downsampling


@dataclass(frozen=True)
class GateTiming:
    """gate 内部分模态计时与通过状态(返回给 pipeline 用)。

    video_score / audio_energy 是 gate 本轮评估的真实打分(0-1),用于配阈值的
    P50-P99 分布分析。无论 pass / skip 都填真实值;评估未实际执行的路径(如
    on-demand bypass)由调用方不读取这两个字段。
    """
    video_ms: float
    audio_ms: float
    video_pass: bool
    audio_pass: bool
    video_score: float = 0.0
    audio_energy: float = 0.0
    # silero VAD 推理耗时(ms);与 audio_ms(纯 RMS 能量)分开计,避免 gate_audio_ms 语义漂移。
    vad_ms: float = 0.0
    # silero VAD 本窗人声峰值概率(0-1);audio_active=False 时未评估、为 0。诊断 / 配阈值用。
    speech_prob: float = 0.0
    # 原始滞回判定,只看时间不看有没有帧。给 pipeline 的 HOLD_START / HOLD_EXPIRED /
    # HOLD_RECOVERED 状态机用 —— 置假会让零帧窗口凭空触发一次 HOLD_EXPIRED。
    hold_pass: bool = False
    # 滞回真把本窗开出来了(hold_pass 且本窗有帧)。落 traces_device.gate_hold_pass 那一列,
    # 该列须恒等于「本窗建了包」,见 knowledge/03-features/perception-pipeline.md 的 Gate 小节。
    hold_opened_window: bool = False
    # visual_score 拆分:窗内邻帧 max vs 跨窗(上窗末帧↔本窗首帧)的 max。
    # 诊断用:cross >> intra 持续高,基本是 ISP 长周期漂移(AGC/IR/AWB)误判 motion。
    video_intra_score: float = 0.0
    video_cross_score: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.video_ms + self.audio_ms + self.vad_ms


# =============================================================================
# Identity Layer — Tracking Service
# =============================================================================


class ObjectType(str, Enum):
    HUMAN_WITH_FACE = "human_with_face"
    HUMAN = "human"
    HUMAN_BODY = "human_body"
    HUMAN_FACE = "human_face"
    PET = "pet"


class BoxType(str, Enum):
    HUMAN_BODY = "human_body"
    HUMAN_FACE = "human_face"
    PET_BODY = "pet_body"


@dataclass
class TrackingBoxInfo:
    frame_index: int
    boxes: dict[str, tuple[int, int, int, int]]  # box_type -> (x, y, w, h)


@dataclass
class TrackedObject:
    type: ObjectType
    face_id: str
    track_id: int
    box_info: list[TrackingBoxInfo]
    # True ⟺ 本帧 Kalman update 真匹配到 detection；False = 本帧未匹配(coasting)，
    # 此时框是上一次真匹配时的检测框、原地冻结（不随 Kalman 外推移动，只是越来越旧）。
    # IdentityEngine 内 7 处闸靠它拒"人已离开或跟丢"产生的残留框：抗遮挡 IoA / omni
    # 候选 / 名册位置 / no_person 抑制区解除 / no_person 预标 / tier_u 陌生人池推图 /
    # tier_c 入队；离线视频抽样本路径(identity/extractor.py)另有一处同口径的闸。
    # 注：身份漂移自检也守同一条不变式，但它比的是整窗累积的质心、不是末帧快照，
    # 故不读本标记而是比对上一窗算出的相似度（见 state.py::drift_last_sim）。
    # 其中两处 no_person 闸在 ``no_person.reject_region_enabled`` 之下，该开关出厂关闭
    # (本包 ``config.py::NoPersonConfigDC`` 与 ``identity/default_config.yaml`` 均为
    # false)，所在函数开头即整体早退 —— 默认部署下引擎内实际跑到的是 5 处。
    # 另：``miloco/pet/observe.py``(注意是另一个顶层包、不在 perception/ 下)用
    # ``time_since_update == 0`` 表达同一条不变式(该路径直接消费 SortTracker 原始
    # dict、不经本类型)，改判据定义时需一并评估。
    # 必须由 tracker 一路透传到 engine。
    # 默认 True：mock 与旧 convert_response 路径不产生 coasting track，其输出的每个
    # 对象都对应一次真实检测。
    detected_this_frame: bool = True
    # 检测器在该 track **最近一次真匹配**时给出的置信度（coasting 帧上不刷新，同 bbox）。
    # tier_u 候选打分(det_norm，权重最大的一项)与质量门(detector_conf_min)消费；
    # 必须透传，否则打分里这一维退化成常数。默认 1.0 同上。
    detector_confidence: float = 1.0


@dataclass
class TrackingResponse:
    frame_info: FrameInfo
    object_info: list[TrackedObject]
    # 本窗口**每一抽帧**上的主体检测框(human / cat / dog),xyxy 像素,跨帧累积不去重。
    # 与 object_info 是两套需求,故并列而非塞进 box_info:
    #   - object_info/box_info 服务身份 —— 只关心 track 末帧那一个位置(贴名字、抽 ReID)
    #   - 本字段服务 Smart Crop —— 要的是空间覆盖:裁出的区域必须包住窗口内出现过的一切,
    #     所以需要逐帧、且需要不成 track 的类别(tracker 只跟 HUMAN,宠物永远进不了 box_info)
    # 无 tracker 的实现(Mock)留空 → crop 侧退化为只用运动块。
    main_det_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class FrameInfo:
    start_timestamp: float
    end_timestamp: float
    fps: int


# =============================================================================
# Identity Layer — Processing Results
# =============================================================================


class MotionState(str, Enum):
    STATIC = "static"
    DYNAMIC = "dynamic"


@dataclass
class IdentityTarget:
    """每个识别后的 track，供 omni prompt 渲染。

    V2 重命名：``face_id → person_id``，值域：
      - ``"none"``                   未识别（初始）
      - ``"pending"``                正在调 omni
      - ``"pending:<person_id>"``    pending 阶段已有 candidate（仅当 candidates 渲染开关开启时出现）
      - ``"<person_id>"``            confirmed
      - ``"unknown"`` / ``"unknown_<n>"``  确认为陌生人
    """

    type: ObjectType
    person_id: str
    track_id: int
    needs_omni_verify: bool
    box_info: list[TrackingBoxInfo]
    # 末帧归一化 [0,1000] bbox (x1,y1,x2,y2)，与 IdentityQueryItem 同源同坐标系；
    # 供 prompt 在"已识别人物/陌生人"名册里注入位置，多人时让 omni 把姓名挂到
    # 视频里的人。None = 本帧未被真实检测（coasting，框是上一次真匹配时的检测框），
    # 名册退化为纯名。
    bbox_xyxy_norm: tuple[int, int, int, int] | None = None
    # 翻身份黏旧名期(reverted_from_confirmed)的 track：显示仍黏旧成员名，但**不可作先验**进
    # 名册锚定 omni 重审（与 candidate_tids 同类去先验）。coasting（本窗未派发）时不在
    # candidate_tids 内，靠本标记兜住，防旧名先验把翻转翻不动。默认 False。
    suppress_as_prior: bool = False


@dataclass
class IdentityAssignment:
    """fused 模式 omni 主调用 response 中 ``identity_assignments`` 字段的解析结果。"""

    track_id: int
    person_id: str | None  # None = unknown
    confidence: float
    reason: str = ""


@dataclass
class SpeakerSegment:
    """Phase 4 声纹接入占位。窗口内一段确认了说话人的语音片段。"""

    speaker_person_id: str | None  # None = unknown
    start_ts: float
    end_ts: float
    confidence: float = 0.0


class FrameResolution(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"


@dataclass
class CropImage:
    track_id: int
    image: NDArray[np.uint8]
    resolution: FrameResolution


@dataclass
class SelectedFrame:
    frame_index: int
    image: NDArray[np.uint8]
    resolution: FrameResolution
    crops: list[CropImage]


class AudioType(str, Enum):
    SPEECH = "speech"
    NON_SPEECH = "non_speech"
    SILENCE = "silence"


@dataclass
class AudioAnalysis:
    type: AudioType
    is_urgent: bool
    energy_level: float  # 0-1


@dataclass
class IdentityPacket:
    packet_id: str
    room_name: str
    timestamp: float
    frame_info: FrameInfo
    targets: list[IdentityTarget]
    scene_motion: MotionState
    frames: list[SelectedFrame]
    all_frames: list[NDArray[np.uint8]]  # All downsampled frames (for video encoding)
    audio_clip: NDArray[np.int16]
    audio_analysis: AudioAnalysis
    sample_rate: int = 16000
    trigger: GateTrigger | None = None  # 透传自 GatePacket，下游分流 audio-only 路径用
    # 透传 TrackingResponse.main_det_boxes（窗口内每抽帧的 human/cat/dog 框，xyxy 像素），
    # 供 Smart Crop 算裁切区域并集。见该字段注释：与 targets/box_info 是两套需求。
    main_det_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)


# =============================================================================
# Omni Layer
# =============================================================================


@dataclass
class RuleCondition:
    rule_id: str
    rule_name: str
    query: str  # natural language condition for Omni to judge


@dataclass
class OmniContext:
    # 注：曾有 last_caption / last_suggestions（回灌模型上轮结论），因形成回声室、强化
    # 幻觉已停止注入；caption 变化去重与 suggestion 事件链去重均下沉到代码（见 api.py）。
    rule_conditions: list[RuleCondition] = field(default_factory=list)
    pending_speech: list[dict] | None = None  # [{"speaker": "xx", "content": "打开"}]
    current_time: str | None = None  # "HH:MM:SS" window start time (aligned with event text)
    room_name: str | None = None  # 设备所在房间名，作场景参考注入 U4（如"厨房""书房"）
    # 该设备自定义「感知须知」prompt（用户/agent 配置，KV CAMERA_PROMPT_MAP_KEY 逐窗实时读取）。
    # 注入 omni system prompt 尾部（video / audio 路由均注入），给模型补充机位环境 / 关注 / 忽略指导。
    camera_prompt: str | None = None
    # 该机位（合成 did，多通道逐路）的 Smart Crop per-camera 闸，KV CAMERA_CROP_DENY_LIST_KEY
    # 逐窗实时读取（不在关闭集 = True，故**默认 True**）。与全局双闸
    # perception.engine.crop_enhance.enabled / user_enabled **相与**才裁切，见
    # omni/prompt_builder._maybe_encode_adaptive。False = 该机位改走全景路径（不裁切）。
    # 名字带 per_camera_ 前缀是刻意的：admin API 的 PerceptionConfigBody 有个
    # smart_crop_enabled，指的是**全局**用户闸（= crop_enhance.user_enabled，见
    # admin/router.py），与本字段是相与关系的两个不同层级。两者都是 bool，同名的话跨层
    # 搬字段时接错哪一个不会有任何类型信号。
    per_camera_crop_enabled: bool = True



# Omni output types — reuse shared Pydantic models
from miloco.perception.types import (  # noqa: E402
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
)

OmniOutput = RealtimePerceptionResult
QueryOutput = OnDemandPerceptionResult


# =============================================================================
# Pipeline
# =============================================================================


@dataclass
class PipelineResult:
    input_slice: InputSlice
    gate_packet: GatePacket | None = None
    identity_packet: IdentityPacket | None = None
    omni_output: OmniOutput | None = None
    skipped: bool = False
    timing: dict[str, float] | None = None  # stage_name → ms


# =============================================================================
# Batch Pipeline
# =============================================================================


@dataclass
class DevicePipelineResult:
    """单设备 Gate+Identity+Omni 处理结果。

    omni 调用粒度为 per-device（per-camera）—— 每个 device 独立调一次 omni，输出
    存进自己的 ``omni_output`` 字段。原因：旧 per-room 合并版本里 ``_encode_batch_video``
    只发首个有 frames 的 device 视频，其它 device 的 candidate 没有对应视觉信息，
    omni 拿 bbox 文字 + gallery 文字硬猜导致识别准确率拉胯。per-device 化后每次调用
    都带自己的视频 + 自己的 candidate + 共享 gallery，调用次数翻倍但识别准确率显著上升。
    """

    device_id: str
    # input_slice 业务路径必填，但单测 / fixture 构造 ``_merge_results`` 等测试型数据
    # 时没必要造完整 DeviceSnapshot——改 Optional 让 fixture 简洁；业务调用方仍始终传
    input_slice: InputSlice | None = None
    gate_packet: GatePacket | None = None
    identity_packet: IdentityPacket | None = None
    omni_output: OmniOutput | None = None
    skipped: bool = False


@dataclass
class RoomPipelineResult:
    """单房间的完整处理结果（device-level omni 输出挂在 ``device_results[did].omni_output``）。"""

    room_name: str
    device_results: dict[str, DevicePipelineResult] = field(default_factory=dict)
    skipped: bool = False  # True if all devices skipped by gate
    # 主体是 float 耗时,"_" 前缀 key 装 per-device 元数据(device_trace_id 等)。
    timing: dict[str, Any] | None = None

    @property
    def identity_packets(self) -> list[IdentityPacket]:
        return [dr.identity_packet for dr in self.device_results.values() if dr.identity_packet]

    @property
    def omni_outputs(self) -> dict[str, OmniOutput]:
        """便捷访问：所有产出了 omni_output 的 device → 该 device 的 omni_output。

        per-camera 重构对原 ``RoomPipelineResult`` 做了两步改造:
          - 删除原 ``omni_output: OmniOutput | None`` **单数**字段(只能装一份 room
            级 omni 输出,跟 per-device 调用模型不兼容)
          - 新增本 ``omni_outputs``(**复数**) property,衍生自 ``device_results``,
            返回 ``dict[device_id, OmniOutput]``
        名字从 ``omni_output`` 改成 ``omni_outputs``,类型从单个 OmniOutput 改成
        dict —— 不是同名字段改 property。工程内消费方都已切到 ``device_results[did]
        .omni_output``(device 级访问);外部如有调用方 ``room.omni_output`` 老语法
        会 AttributeError。

        下游若做 ``len(room.omni_outputs)`` 等仍可用;新代码推荐直接读
        ``device_results[did].omni_output``。
        """
        return {
            did: dr.omni_output
            for did, dr in self.device_results.items()
            if dr.omni_output is not None
        }


@dataclass
class BatchPipelineResult:
    """批处理总结果。"""

    rooms: dict[str, RoomPipelineResult] = field(default_factory=dict)
    timing: dict[str, Any] | None = None
