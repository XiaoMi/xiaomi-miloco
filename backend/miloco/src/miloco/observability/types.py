"""Trace dataclass:per-device 窄类 → DeviceTraceRecord(子表) → CycleTraceRecord(主表)。

每个 record 通过 ``to_row()`` 铺平成 SQLite 列名 → 值的 dict。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecodeTrace:
    video_avg_ms: float
    audio_avg_ms: float
    video_frame_count: int
    audio_frame_count: int


@dataclass(frozen=True)
class GateTrace:
    ms: float
    video_ms: float
    audio_ms: float
    video_pass: bool
    audio_pass: bool
    skipped: bool
    # gate 真实评估的打分(0-1)。None 表示该路径未跑真实 gate(on-demand bypass /
    # cycle 异常 fallback),写库时落 NULL,P50-P99 分布视图过滤掉。
    video_score: float | None = None
    audio_energy: float | None = None
    # 本窗 packet 由 hold 滞回拉起(visual 不通过、距上次 visual 通过 <= hold_duration_sec)。
    # 与 video_pass 互斥(hold 前置条件要求 visual 不通过);可与 audio_pass 共存。
    hold_pass: bool = False

    @property
    def passed(self) -> bool:
        return not self.skipped

    def __post_init__(self) -> None:
        # 任一通过 或 hold 拉起 → 不该 skipped;全不过且非 hold → 必 skipped。
        expected_skipped = not (self.video_pass or self.audio_pass or self.hold_pass)
        if self.skipped != expected_skipped:
            raise ValueError(
                f"GateTrace 字段不一致: skipped={self.skipped} 但 "
                f"video_pass={self.video_pass} / audio_pass={self.audio_pass} / "
                f"hold_pass={self.hold_pass}"
            )


@dataclass(frozen=True)
class IdentityTrace:
    ms: float


@dataclass(frozen=True)
class OmniTrace:
    ms: float
    error_code: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class AgentTrace:
    """openclaw get_trace webhook 返回的 agent turn 元数据(纯响应解析体)。"""
    run_id: str
    query: str
    duration_ms: float
    llm_call_count: int
    tool_call_count: int
    llm_total_ms: float
    tool_total_ms: float
    tool_max_ms: float
    slowest_tool_name: str | None
    success: bool
    error_count: int
    error_msg: str | None
    jsonl_path: str | None


@dataclass(frozen=True)
class AgentRunRecord:
    """一次 agent turn 调用的完整元数据,对应 agent_runs 表一行。

    trace_id / source / webhook_rtt_ms 由调用方测得并透传;其余从 AgentTrace 解包。
    """
    run_id: str
    trace_id: str
    timestamp: int               # 写入时刻 ms
    source: str                  # rule | interaction | suggestion
    webhook_rtt_ms: float | None
    query: str
    duration_ms: float
    llm_call_count: int
    tool_call_count: int
    llm_total_ms: float
    tool_total_ms: float
    tool_max_ms: float
    slowest_tool_name: str | None
    success: bool
    error_count: int
    error_msg: str | None
    jsonl_path: str | None

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "webhook_rtt_ms": self.webhook_rtt_ms,
            "query": self.query,
            "duration_ms": self.duration_ms,
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "llm_total_ms": self.llm_total_ms,
            "tool_total_ms": self.tool_total_ms,
            "tool_max_ms": self.tool_max_ms,
            "slowest_tool_name": self.slowest_tool_name,
            "success": int(self.success),
            "error_count": self.error_count,
            "error_msg": self.error_msg,
            "jsonl_path": self.jsonl_path,
        }


@dataclass(frozen=True)
class ActionLedgerRecord:
    """一次设备控制 / TTS / 场景触发的持久审计,对应 action_ledger 表一行。

    value_json 已是序列化好的字符串(set 值 / action in_params,含 TTS 全文);
    调用方负责 json.dumps,避免 record 层再持有原始对象。
    """
    id: str
    timestamp: int              # 写入时刻 ms
    action_type: str            # set_property | set_properties | call_action | scene_trigger
    did: str
    device_name: str | None
    room: str | None
    iid: str | None
    value_json: str | None
    result_code: int | None
    result_msg: str | None
    success: bool
    error: str | None
    trace_id: str | None = None  # v1 留 None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "action_type": self.action_type,
            "did": self.did,
            "device_name": self.device_name,
            "room": self.room,
            "iid": self.iid,
            "value_json": self.value_json,
            "result_code": self.result_code,
            "result_msg": self.result_msg,
            "success": int(self.success),
            "error": self.error,
            "trace_id": self.trace_id,
        }


@dataclass
class DeviceTraceRecord:
    device_trace_id: str
    cycle_id: str
    timestamp: int
    device_id: str
    room_name: str
    decode: DecodeTrace
    gate: GateTrace
    identity: IdentityTrace | None = None
    omni: OmniTrace | None = None
    dropped_windows_count: int = 0
    overflow_count: int = 0
    max_buffer_depth: int = 0
    last_overflow_action: str | None = None  # "clear" | "drop" | "skip" | None

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "device_trace_id": self.device_trace_id,
            "cycle_id": self.cycle_id,
            "timestamp": self.timestamp,
            "device_id": self.device_id,
            "room_name": self.room_name,
            "decode_video_avg_ms": self.decode.video_avg_ms,
            "decode_audio_avg_ms": self.decode.audio_avg_ms,
            "video_frame_count": self.decode.video_frame_count,
            "audio_frame_count": self.decode.audio_frame_count,
            "gate_ms": self.gate.ms,
            "gate_video_ms": self.gate.video_ms,
            "gate_audio_ms": self.gate.audio_ms,
            "gate_video_pass": int(self.gate.video_pass),
            "gate_audio_pass": int(self.gate.audio_pass),
            "gate_hold_pass": int(self.gate.hold_pass),
            "gate_skipped": int(self.gate.skipped),
            "gate_video_score": self.gate.video_score,
            "gate_audio_energy": self.gate.audio_energy,
            "dropped_windows_count": self.dropped_windows_count,
            "overflow_count": self.overflow_count,
            "max_buffer_depth": self.max_buffer_depth,
            "last_overflow_action": self.last_overflow_action,
        }
        if self.identity is not None:
            row["identity_ms"] = self.identity.ms
        if self.omni is not None:
            row["omni_ms"] = self.omni.ms
            row["omni_error_code"] = self.omni.error_code
            row["omni_retry_count"] = self.omni.retry_count
        return row


@dataclass
class CycleTraceRecord:
    trace_id: str
    timestamp: int
    device_count: int
    skipped: bool
    in_delay_ms: float
    out_delay_ms: float
    decode_ms: float
    collect_ms: float
    convert_ms: float
    log_ms: float
    cycle_total_ms: float
    pipeline_total_ms: float
    window_duration_ms: float
    window_first_frame_recv_ms: int | None
    stream_lag_ms: float | None
    gate_ms: float
    gate_video_ms: float
    gate_audio_ms: float
    gate_video_pass: bool
    gate_audio_pass: bool
    identity_ms: float
    omni_ms: float
    omni_call_count: int
    omni_error_count: int
    dropped_windows_total: int = 0
    overflow_count_total: int = 0
    # 任一 device 本窗 hold 拉起 → cycle 级 True。default False 向前兼容历史构造。
    gate_hold_pass: bool = False
    timing_detail: dict[str, float] | None = None
    # 非 OmniError 异常路径下 cycle 的错误摘要;omni 错误走 omni_error_count + traces_device.omni_error_code,不写这里。
    cycle_error_msg: str | None = None

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "device_count": self.device_count,
            "skipped": int(self.skipped),
            "in_delay_ms": self.in_delay_ms,
            "out_delay_ms": self.out_delay_ms,
            "decode_ms": self.decode_ms,
            "collect_ms": self.collect_ms,
            "convert_ms": self.convert_ms,
            "log_ms": self.log_ms,
            "cycle_total_ms": self.cycle_total_ms,
            "pipeline_total_ms": self.pipeline_total_ms,
            "window_duration_ms": self.window_duration_ms,
            "window_first_frame_recv_ms": self.window_first_frame_recv_ms,
            "stream_lag_ms": self.stream_lag_ms,
            "gate_ms": self.gate_ms,
            "gate_video_ms": self.gate_video_ms,
            "gate_audio_ms": self.gate_audio_ms,
            "gate_video_pass": int(self.gate_video_pass),
            "gate_audio_pass": int(self.gate_audio_pass),
            "gate_hold_pass": int(self.gate_hold_pass),
            "identity_ms": self.identity_ms,
            "omni_ms": self.omni_ms,
            "omni_call_count": self.omni_call_count,
            "omni_error_count": self.omni_error_count,
            "dropped_windows_total": self.dropped_windows_total,
            "overflow_count_total": self.overflow_count_total,
            "cycle_error_msg": self.cycle_error_msg,
        }
        if self.timing_detail is not None:
            row["timing_detail"] = json.dumps(self.timing_detail, ensure_ascii=False)
        return row
