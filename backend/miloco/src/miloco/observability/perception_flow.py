"""Runtime-only perception flow diagnostics and Graph JSON adapter."""

from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ShortText = Annotated[str, StringConstraints(max_length=128)]
DisplayText = Annotated[str, StringConstraints(max_length=512)]
GraphValue = str | float | int | bool


class GraphSource(str, Enum):
    OBSERVED = "observed"
    CONFIGURED = "configured"
    MODEL_FIXED = "model_fixed"
    UNKNOWN = "unknown"


class GraphStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    SKIPPED = "skipped"
    BACKPRESSURE = "backpressure"
    ERROR = "error"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class GraphFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class GraphKind(str, Enum):
    SOURCE = "source"
    BUFFER = "buffer"
    TRANSFORM = "transform"
    FILTER = "filter"
    INFERENCE = "inference"
    ENCODER = "encoder"
    EXTERNAL = "external"
    SINK = "sink"
    GENERIC = "generic"


class GraphRole(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    CONFIG = "config"
    FLOW = "flow"
    STATE = "state"
    DETAIL = "detail"


class GraphSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class StrictGraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _validate_nullable_source(value: object | None, source: GraphSource) -> None:
    if value is None and source is not GraphSource.UNKNOWN:
        raise ValueError("null value requires unknown source")
    if value is not None and source is GraphSource.UNKNOWN:
        raise ValueError("non-null value cannot use unknown source")


class GraphIntegerDatum(StrictGraphModel):
    value: int | None
    source: GraphSource

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("integer datum must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_source(self) -> GraphIntegerDatum:
        _validate_nullable_source(self.value, self.source)
        return self


class GraphNumberDatum(StrictGraphModel):
    value: float | None
    source: GraphSource

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: float | None) -> float | None:
        if value is not None and (value < 0 or not math.isfinite(value)):
            raise ValueError("number datum must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_source(self) -> GraphNumberDatum:
        _validate_nullable_source(self.value, self.source)
        return self


class GraphMedia(StrictGraphModel):
    width: GraphIntegerDatum | None
    height: GraphIntegerDatum | None
    fps: GraphNumberDatum | None
    frame_count: GraphIntegerDatum | None
    duration_ms: GraphNumberDatum | None


class GraphMetric(StrictGraphModel):
    key: ShortText
    label: DisplayText
    value: GraphValue | None
    unit: ShortText | None
    source: GraphSource
    role: GraphRole
    severity: GraphSeverity | None

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: GraphValue | None) -> GraphValue | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metric value must be finite")
        if isinstance(value, str) and len(value) > 512:
            raise ValueError("metric string value is too long")
        return value

    @model_validator(mode="after")
    def validate_source(self) -> GraphMetric:
        _validate_nullable_source(self.value, self.source)
        return self


class GraphWarning(StrictGraphModel):
    code: ShortText
    message: DisplayText
    severity: GraphSeverity
    params: dict[ShortText, GraphValue]


class GraphEvidence(StrictGraphModel):
    trace_id: ShortText | None
    device_trace_id: ShortText | None
    observed_at: int | None
    age_ms: float | None

    @field_validator("age_ms")
    @classmethod
    def validate_age(cls, value: float | None) -> float | None:
        if value is not None and (value < 0 or not math.isfinite(value)):
            raise ValueError("age_ms must be finite and non-negative")
        return value


class GraphNode(StrictGraphModel):
    id: ShortText
    kind: GraphKind
    label: DisplayText
    group: ShortText | None
    rank: int | None
    order: int
    status: GraphStatus
    freshness: GraphFreshness
    last_observed_status: GraphStatus | None
    standalone: bool
    metrics: list[GraphMetric] = Field(max_length=32)
    input: GraphMedia | None
    output: GraphMedia | None
    warnings: list[GraphWarning] = Field(max_length=16)
    evidence: GraphEvidence | None


class GraphEdge(StrictGraphModel):
    id: ShortText
    from_: ShortText = Field(alias="from")
    to: ShortText
    label: DisplayText | None
    active: bool
    status: GraphStatus
    freshness: GraphFreshness
    last_observed_status: GraphStatus | None
    media: GraphMedia | None
    metrics: list[GraphMetric] = Field(max_length=32)
    warnings: list[GraphWarning] = Field(max_length=16)
    evidence: GraphEvidence | None


class GraphLayout(StrictGraphModel):
    rank_separation: float
    node_separation: float

    @field_validator("rank_separation", "node_separation")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("layout values must be finite")
        return value


class GraphGroup(StrictGraphModel):
    id: ShortText
    label: DisplayText
    order: int


class GraphDefinition(StrictGraphModel):
    id: ShortText
    label: DisplayText
    direction: Literal["LR", "TB"]
    layout: GraphLayout
    groups: list[GraphGroup]
    nodes: list[GraphNode] = Field(max_length=64)
    edges: list[GraphEdge] = Field(max_length=128)

    @model_validator(mode="after")
    def validate_topology(self) -> GraphDefinition:
        GraphResponse.validate_graph(self.nodes, self.edges)
        group_ids = [group.id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("duplicate group id")
        known_groups = set(group_ids)
        for node in self.nodes:
            if node.group is not None and node.group not in known_groups:
                raise ValueError(f"node {node.id} references unknown group {node.group}")
        return self


class GraphScope(StrictGraphModel):
    device_id: ShortText | None
    room_name: DisplayText | None


class GraphDeviceSummary(StrictGraphModel):
    device_id: ShortText
    room_name: DisplayText | None
    trace_id: ShortText | None
    device_trace_id: ShortText | None
    observed_at: int | None
    freshness: GraphFreshness
    status: GraphStatus


class GraphSummary(StrictGraphModel):
    latest_trace_id: ShortText | None
    latest_device_trace_id: ShortText | None
    observed_at: int | None
    freshness: GraphFreshness
    devices: list[GraphDeviceSummary]


class GraphResponse(StrictGraphModel):
    schema_version: Literal[1]
    generated_at: int
    process_started_at: int
    stale_after_sec: float
    scope: GraphScope
    graph: GraphDefinition
    summary: GraphSummary

    @field_validator("stale_after_sec")
    @classmethod
    def validate_stale_after(cls, value: float) -> float:
        if value < 0 or not math.isfinite(value):
            raise ValueError("stale_after_sec must be finite and non-negative")
        return value

    @staticmethod
    def validate_graph(nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        node_ids = [node.id for node in nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("duplicate node id")
        edge_ids = [edge.id for edge in edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge id")
        known = set(node_ids)
        adjacency = {node_id: [] for node_id in node_ids}
        for edge in edges:
            if edge.from_ not in known or edge.to not in known:
                raise ValueError(f"edge {edge.id} references unknown node")
            adjacency[edge.from_].append(edge.to)
        state: dict[str, int] = {}

        def visit(node_id: str) -> None:
            if state.get(node_id) == 1:
                raise ValueError("graph contains a cycle")
            if state.get(node_id) == 2:
                return
            state[node_id] = 1
            for target in adjacency[node_id]:
                visit(target)
            state[node_id] = 2

        for node_id in node_ids:
            visit(node_id)

        sources = {node.id for node in nodes if node.kind is GraphKind.SOURCE}
        sinks = {node.id for node in nodes if node.kind is GraphKind.SINK}
        if not sources or not sinks:
            raise ValueError("graph requires source and sink nodes")

        reachable_from_source: set[str] = set()
        pending = list(sources)
        while pending:
            node_id = pending.pop()
            if node_id in reachable_from_source:
                continue
            reachable_from_source.add(node_id)
            pending.extend(adjacency[node_id])

        reverse_adjacency = {node_id: [] for node_id in node_ids}
        for source, targets in adjacency.items():
            for target in targets:
                reverse_adjacency[target].append(source)
        can_reach_sink: set[str] = set()
        pending = list(sinks)
        while pending:
            node_id = pending.pop()
            if node_id in can_reach_sink:
                continue
            can_reach_sink.add(node_id)
            pending.extend(reverse_adjacency[node_id])

        disconnected = next(
            (
                node.id
                for node in nodes
                if not node.standalone
                and node.status is not GraphStatus.INACTIVE
                and (
                    node.id not in reachable_from_source
                    or node.id not in can_reach_sink
                )
            ),
            None,
        )
        if disconnected is not None:
            raise ValueError(
                f"active node is not on a source-to-sink path: {disconnected}"
            )


@dataclass
class PerDeviceFlowDiagnostics:
    device_id: str
    room_name: str | None
    trace_id: str
    device_trace_id: str
    observed_at: int
    status: GraphStatus = GraphStatus.UNKNOWN
    source_width: int | None = None
    source_height: int | None = None
    source_frame_count: int | None = None
    source_window_duration_ms: float | None = None
    last_drain_observed_at: int | None = None
    last_drain_ready_depth_before: int | None = None
    last_drain_ready_depth_after: int | None = None
    dropped_windows_count: int = 0
    overflow_count: int = 0
    max_buffer_depth: int = 0
    last_overflow_action: str | None = None
    pipeline_input_frame_count: int | None = None
    pipeline_output_frame_count: int | None = None
    pipeline_fps: float | None = None
    gate_checked_frame_count: int | None = None
    gate_output_width: int | None = None
    gate_output_height: int | None = None
    gate_status: GraphStatus = GraphStatus.UNKNOWN
    identity_frame_count: int | None = None
    detector_input_width: int | None = None
    detector_input_height: int | None = None
    reid_input_width: int | None = None
    reid_input_height: int | None = None
    identity_status: GraphStatus = GraphStatus.UNKNOWN
    omni_frame_count: int | None = None
    omni_fps: float | None = None
    omni_status: GraphStatus = GraphStatus.UNKNOWN
    omni_sample_status: GraphStatus = GraphStatus.UNKNOWN
    media_transform_status: GraphStatus = GraphStatus.UNKNOWN
    media_encode_status: GraphStatus = GraphStatus.UNKNOWN
    omni_request_status: GraphStatus = GraphStatus.UNKNOWN
    encoded_width: int | None = None
    encoded_height: int | None = None
    encoded_fps: float | None = None
    encoded_frame_count: int | None = None
    encoded_has_audio: bool | None = None
    audio_only: bool = False
    audio_sample_rate: int | None = None
    smart_crop_enabled: bool | None = None
    smart_crop_applied: bool | None = None
    crop_region: tuple[int, int, int, int] | None = None
    transformed_width: int | None = None
    transformed_height: int | None = None
    transformed_frame_count: int | None = None
    error_stage: str | None = None
    error_code: str | None = None
    warnings: list[GraphWarning] = field(default_factory=list)
    diagnostics_schema_version: int = 1


@dataclass
class PerceptionFlowCycleDiagnostics:
    cycle_id: str
    observed_at: int
    devices: dict[str, PerDeviceFlowDiagnostics] = field(default_factory=dict)


@dataclass(frozen=True)
class PerceptionFlowConfigSnapshot:
    window_size_sec: int | None = None
    max_windows: int | None = None
    full_action: str | None = None
    pipeline_fps: int | None = None
    gate_check_fps: int | None = None
    omni_fps: int | None = None
    video_short_edge: int | None = None
    stream_profile: str | None = None


@dataclass
class PerceptionFlowStoreSnapshot:
    latest_cycle_id: str | None = None
    observed_at: int | None = None
    per_device: dict[str, PerDeviceFlowDiagnostics] = field(default_factory=dict)
    active_device_ids: set[str] = field(default_factory=set)


class PerceptionFlowSnapshotStore:
    """Thread-safe latest-observation store; data never leaves process memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = PerceptionFlowStoreSnapshot()

    def merge_cycle(
        self,
        cycle_id: str,
        observed_at: int,
        devices: dict[str, PerDeviceFlowDiagnostics],
    ) -> None:
        replacements = copy.deepcopy(devices)
        with self._lock:
            merged = copy.deepcopy(self._snapshot.per_device)
            merged.update(replacements)
            self._snapshot = PerceptionFlowStoreSnapshot(
                cycle_id,
                observed_at,
                merged,
                set(self._snapshot.active_device_ids),
            )

    def retain_devices(self, active_device_ids: set[str]) -> None:
        with self._lock:
            retained = {
                device_id: copy.deepcopy(diagnostic)
                for device_id, diagnostic in self._snapshot.per_device.items()
                if device_id in active_device_ids
            }
            self._snapshot = PerceptionFlowStoreSnapshot(
                self._snapshot.latest_cycle_id,
                self._snapshot.observed_at,
                retained,
                set(active_device_ids),
            )

    def clear(self) -> None:
        with self._lock:
            self._snapshot = PerceptionFlowStoreSnapshot()

    def snapshot(self) -> PerceptionFlowStoreSnapshot:
        with self._lock:
            return copy.deepcopy(self._snapshot)


_bound_store: PerceptionFlowSnapshotStore | None = None
_process_started_at: int | None = None


def bind_perception_flow_store(
    store: PerceptionFlowSnapshotStore | None,
    *,
    process_started_at: int | None = None,
) -> None:
    global _bound_store, _process_started_at
    _bound_store = store
    _process_started_at = process_started_at if store is not None else None


def get_perception_flow_store() -> PerceptionFlowSnapshotStore | None:
    return _bound_store


def get_perception_flow_process_started_at() -> int | None:
    return _process_started_at


@dataclass(frozen=True)
class _NodeTemplate:
    id: str
    kind: GraphKind
    label: str
    group: str
    rank: int
    order: int


_NODE_TEMPLATES = (
    _NodeTemplate("media.decoded", GraphKind.SOURCE, "Decoded Camera Frames", "source", 0, 10),
    _NodeTemplate("buffer.sync_window", GraphKind.BUFFER, "Sync Window", "buffer", 1, 10),
    _NodeTemplate("buffer.ready_queue", GraphKind.BUFFER, "Ready Queue", "buffer", 2, 10),
    _NodeTemplate("pipeline.sample", GraphKind.TRANSFORM, "Pipeline Sampling", "processing", 3, 10),
    _NodeTemplate("gate.visual", GraphKind.FILTER, "Visual Gate", "processing", 4, 10),
    _NodeTemplate("identity.track", GraphKind.INFERENCE, "Detection, Tracking, ReID", "processing", 5, 10),
    _NodeTemplate("omni.sample", GraphKind.TRANSFORM, "Omni Sampling", "output", 6, 10),
    _NodeTemplate("media.transform", GraphKind.TRANSFORM, "Media Transform", "output", 7, 10),
    _NodeTemplate("media.encode", GraphKind.ENCODER, "Media Encoding", "output", 8, 10),
    _NodeTemplate("omni.request", GraphKind.EXTERNAL, "Omni Request", "output", 9, 10),
    _NodeTemplate("result.complete", GraphKind.SINK, "Result Complete", "output", 10, 10),
)

_EDGE_PAIRS = tuple(
    (f"{source}-to-{target}", source, target)
    for source, target in zip(
        (template.id for template in _NODE_TEMPLATES[:-1]),
        (template.id for template in _NODE_TEMPLATES[1:]),
    )
)


def build_perception_flow_graph(
    store: PerceptionFlowSnapshotStore,
    *,
    device_id: str | None,
    generated_at: int | None = None,
    stale_after_sec: float = 30,
    process_started_at: int | None = None,
    active_device_ids: set[str] | None = None,
    config: PerceptionFlowConfigSnapshot | None = None,
) -> GraphResponse:
    generated_at = generated_at if generated_at is not None else int(time.time() * 1000)
    process_started_at = process_started_at if process_started_at is not None else generated_at
    config = config or _current_config_snapshot()
    snapshot = store.snapshot()
    selected = snapshot.per_device.get(device_id) if device_id else None
    freshness_by_device = {
        key: _freshness(value, generated_at, stale_after_sec)
        for key, value in snapshot.per_device.items()
    }
    effective_active_device_ids = (
        active_device_ids
        if active_device_ids is not None
        else snapshot.active_device_ids
    )
    devices = [
        _device_summary(
            device_id=listed_device_id,
            diagnostic=snapshot.per_device.get(listed_device_id),
            freshness=freshness_by_device.get(
                listed_device_id, GraphFreshness.UNKNOWN
            ),
        )
        for listed_device_id in sorted(
            set(snapshot.per_device) | effective_active_device_ids
        )
    ]
    if selected is not None:
        selected_freshness = freshness_by_device[selected.device_id]
        summary = GraphSummary(
            latest_trace_id=selected.trace_id,
            latest_device_trace_id=selected.device_trace_id,
            observed_at=selected.observed_at,
            freshness=selected_freshness,
            devices=devices,
        )
        nodes = [
            _device_node(template, selected, selected_freshness, generated_at, config)
            for template in _NODE_TEMPLATES
        ]
        edges = [
            _device_edge(edge_id, source, target, selected, selected_freshness, generated_at)
            for edge_id, source, target in _device_edge_pairs(selected)
        ]
        room_name = selected.room_name
    elif device_id is not None:
        summary = GraphSummary(
            latest_trace_id=None,
            latest_device_trace_id=None,
            observed_at=None,
            freshness=GraphFreshness.UNKNOWN,
            devices=devices,
        )
        nodes = [_empty_node(template) for template in _NODE_TEMPLATES]
        edges = [_empty_edge(*edge) for edge in _EDGE_PAIRS]
        room_name = None
    else:
        fresh = [
            value
            for key, value in snapshot.per_device.items()
            if freshness_by_device[key] is GraphFreshness.FRESH
        ]
        freshness = (
            GraphFreshness.FRESH
            if fresh
            else GraphFreshness.STALE
            if snapshot.per_device
            else GraphFreshness.UNKNOWN
        )
        summary = GraphSummary(
            latest_trace_id=None,
            latest_device_trace_id=None,
            observed_at=max((item.observed_at for item in fresh), default=None),
            freshness=freshness,
            devices=devices,
        )
        nodes = [
            _global_node(
                template,
                fresh,
                active_count=len(effective_active_device_ids),
                snapshot_count=len(snapshot.per_device),
                fresh_count=len(fresh),
                stale_count=len(snapshot.per_device) - len(fresh),
                freshness=freshness,
            )
            for template in _NODE_TEMPLATES
        ]
        edges = [
            _global_edge(*edge, freshness=freshness, has_fresh=bool(fresh))
            for edge in _EDGE_PAIRS
        ]
        room_name = None

    return GraphResponse(
        schema_version=1,
        generated_at=generated_at,
        process_started_at=process_started_at,
        stale_after_sec=stale_after_sec,
        scope=GraphScope(device_id=device_id, room_name=room_name),
        graph=GraphDefinition(
            id="perception-flow",
            label="Perception Flow",
            direction="LR",
            layout=GraphLayout(rank_separation=48, node_separation=24),
            groups=[
                GraphGroup(id="source", label="Source", order=10),
                GraphGroup(id="buffer", label="Buffer", order=20),
                GraphGroup(id="processing", label="Processing", order=30),
                GraphGroup(id="output", label="Output", order=40),
            ],
            nodes=nodes,
            edges=edges,
        ),
        summary=summary,
    )


def _device_summary(
    *,
    device_id: str,
    diagnostic: PerDeviceFlowDiagnostics | None,
    freshness: GraphFreshness,
) -> GraphDeviceSummary:
    if diagnostic is None:
        return GraphDeviceSummary(
            device_id=device_id,
            room_name=None,
            trace_id=None,
            device_trace_id=None,
            observed_at=None,
            freshness=GraphFreshness.UNKNOWN,
            status=GraphStatus.UNKNOWN,
        )
    return GraphDeviceSummary(
        device_id=diagnostic.device_id,
        room_name=diagnostic.room_name,
        trace_id=diagnostic.trace_id,
        device_trace_id=diagnostic.device_trace_id,
        observed_at=diagnostic.observed_at,
        freshness=freshness,
        status=diagnostic.status,
    )


def _freshness(
    diagnostic: PerDeviceFlowDiagnostics,
    generated_at: int,
    stale_after_sec: float,
) -> GraphFreshness:
    age_ms = max(0, generated_at - diagnostic.observed_at)
    return (
        GraphFreshness.STALE
        if age_ms > stale_after_sec * 1000
        else GraphFreshness.FRESH
    )


def _datum_int(
    value: int | None,
    source: GraphSource = GraphSource.OBSERVED,
) -> GraphIntegerDatum:
    return GraphIntegerDatum(
        value=value,
        source=source if value is not None else GraphSource.UNKNOWN,
    )


def _datum_number(
    value: float | None,
    source: GraphSource = GraphSource.OBSERVED,
) -> GraphNumberDatum:
    return GraphNumberDatum(
        value=value,
        source=source if value is not None else GraphSource.UNKNOWN,
    )


def _media(
    *,
    width: int | None,
    height: int | None,
    fps: float | None,
    frame_count: int | None,
    duration_ms: float | None,
) -> GraphMedia:
    return GraphMedia(
        width=_datum_int(width),
        height=_datum_int(height),
        fps=_datum_number(fps),
        frame_count=_datum_int(frame_count),
        duration_ms=_datum_number(duration_ms),
    )


def _metric(
    key: str,
    label: str,
    value: GraphValue | None,
    *,
    unit: str | None = None,
    source: GraphSource = GraphSource.OBSERVED,
    role: GraphRole = GraphRole.DETAIL,
    severity: GraphSeverity | None = None,
) -> GraphMetric:
    return GraphMetric(
        key=key,
        label=label,
        value=value,
        unit=unit,
        source=source if value is not None else GraphSource.UNKNOWN,
        role=role,
        severity=severity,
    )


def _dimensions(width: int | None, height: int | None) -> str | None:
    if width is None or height is None:
        return None
    return f"{width}×{height}"


def _status_for_node(
    node_id: str,
    diagnostic: PerDeviceFlowDiagnostics,
) -> GraphStatus:
    if node_id in {
        "media.decoded",
        "buffer.sync_window",
        "pipeline.sample",
    }:
        return GraphStatus.OK
    if node_id == "buffer.ready_queue":
        return (
            GraphStatus.BACKPRESSURE
            if diagnostic.dropped_windows_count
            else GraphStatus.OK
        )
    if node_id == "gate.visual":
        return diagnostic.gate_status
    if node_id == "identity.track":
        return diagnostic.identity_status
    if node_id == "omni.sample":
        return diagnostic.omni_sample_status
    if node_id == "media.transform":
        if diagnostic.audio_only:
            return GraphStatus.INACTIVE
        return diagnostic.media_transform_status
    if node_id == "media.encode":
        return diagnostic.media_encode_status
    if node_id == "omni.request":
        if diagnostic.omni_request_status is not GraphStatus.UNKNOWN:
            return diagnostic.omni_request_status
        if diagnostic.omni_status is GraphStatus.ERROR:
            return GraphStatus.ERROR
        return GraphStatus.UNKNOWN
    if node_id == "result.complete":
        return diagnostic.status
    return diagnostic.status


def _evidence(
    diagnostic: PerDeviceFlowDiagnostics,
    generated_at: int,
) -> GraphEvidence:
    return GraphEvidence(
        trace_id=diagnostic.trace_id,
        device_trace_id=diagnostic.device_trace_id,
        observed_at=diagnostic.observed_at,
        age_ms=max(0, generated_at - diagnostic.observed_at),
    )


def _device_node(
    template: _NodeTemplate,
    diagnostic: PerDeviceFlowDiagnostics,
    freshness: GraphFreshness,
    generated_at: int,
    config: PerceptionFlowConfigSnapshot,
) -> GraphNode:
    status = _status_for_node(template.id, diagnostic)
    metrics: list[GraphMetric] = []
    input_media: GraphMedia | None = None
    output_media: GraphMedia | None = None
    if template.id in {
        "media.decoded",
        "buffer.sync_window",
        "buffer.ready_queue",
    }:
        output_media = _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=_rate(
                diagnostic.source_frame_count,
                diagnostic.source_window_duration_ms,
            ),
            frame_count=diagnostic.source_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
        if template.id == "media.decoded":
            input_media = output_media
    if template.id == "media.decoded":
        metrics.append(
            _metric(
                "stream_profile",
                "Stream Profile",
                config.stream_profile,
                source=GraphSource.CONFIGURED,
                role=GraphRole.CONFIG,
            )
        )
    if template.id == "buffer.sync_window":
        metrics.append(
            _metric(
                "configured_window_size",
                "Config Window Size",
                config.window_size_sec,
                unit="s",
                source=GraphSource.CONFIGURED,
                role=GraphRole.CONFIG,
            )
        )
    if template.id == "buffer.ready_queue":
        metrics.extend(
            [
                _metric(
                    "last_drain_ready_depth_before",
                    "Ready Depth",
                    diagnostic.last_drain_ready_depth_before,
                    unit="windows",
                ),
                _metric(
                    "dropped_windows",
                    "Dropped Windows",
                    diagnostic.dropped_windows_count,
                    unit="windows",
                ),
                _metric(
                    "last_drain_ready_depth_after",
                    "Depth After Drain",
                    diagnostic.last_drain_ready_depth_after,
                    unit="windows",
                ),
                _metric(
                    "configured_max_windows",
                    "Max Windows",
                    config.max_windows,
                    unit="windows",
                    source=GraphSource.CONFIGURED,
                    role=GraphRole.CONFIG,
                ),
                _metric(
                    "configured_full_action",
                    "Config Full Action",
                    config.full_action,
                    source=GraphSource.CONFIGURED,
                    role=GraphRole.CONFIG,
                ),
                _metric(
                    "overflow_count",
                    "Overflow Count",
                    diagnostic.overflow_count,
                    unit="events",
                ),
                _metric(
                    "max_buffer_depth",
                    "Max Buffer Depth",
                    diagnostic.max_buffer_depth,
                    unit="windows",
                ),
                _metric(
                    "last_overflow_action",
                    "Last Overflow Action",
                    diagnostic.last_overflow_action,
                ),
            ]
        )
    if template.id == "pipeline.sample":
        metrics.append(
            _metric(
                "configured_fps",
                "Config FPS",
                config.pipeline_fps,
                unit="fps",
                source=GraphSource.CONFIGURED,
                role=GraphRole.CONFIG,
            )
        )
        input_media = _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=_rate(
                diagnostic.pipeline_input_frame_count,
                diagnostic.source_window_duration_ms,
            ),
            frame_count=diagnostic.pipeline_input_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
        output_media = _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.pipeline_fps,
            frame_count=diagnostic.pipeline_output_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if template.id == "gate.visual":
        metrics.extend(
            [
                _metric(
                    "check_frame_size",
                    "Diff Image Size",
                    _dimensions(
                        diagnostic.gate_output_width,
                        diagnostic.gate_output_height,
                    ),
                    source=GraphSource.OBSERVED,
                ),
                _metric(
                    "configured_check_fps",
                    "Config Check FPS",
                    config.gate_check_fps,
                    unit="fps",
                    source=GraphSource.CONFIGURED,
                    role=GraphRole.CONFIG,
                ),
                _metric(
                    "checked_frames",
                    "Checked Frames",
                    diagnostic.gate_checked_frame_count,
                    unit="frames",
                ),
            ]
        )
        input_media = _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.pipeline_fps,
            frame_count=diagnostic.pipeline_output_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
        if diagnostic.gate_status is GraphStatus.OK:
            output_media = _media(
                width=diagnostic.source_width,
                height=diagnostic.source_height,
                fps=diagnostic.pipeline_fps,
                frame_count=diagnostic.pipeline_output_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
    if template.id == "identity.track":
        if diagnostic.gate_status is GraphStatus.OK:
            input_media = _media(
                width=diagnostic.source_width,
                height=diagnostic.source_height,
                fps=diagnostic.pipeline_fps,
                frame_count=diagnostic.pipeline_output_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
        if diagnostic.identity_status is GraphStatus.OK:
            output_media = input_media
        metrics.extend(
            [
                _metric(
                    "detector_input_size",
                    "Detector Input Size",
                    _dimensions(
                        diagnostic.detector_input_width,
                        diagnostic.detector_input_height,
                    ),
                    source=GraphSource.MODEL_FIXED,
                ),
                _metric(
                    "reid_input_size",
                    "ReID Input Size",
                    _dimensions(
                        diagnostic.reid_input_width,
                        diagnostic.reid_input_height,
                    ),
                    source=GraphSource.MODEL_FIXED,
                ),
            ]
        )
    if template.id == "omni.sample":
        metrics.append(
            _metric(
                "configured_fps",
                "Config FPS",
                config.omni_fps,
                unit="fps",
                source=GraphSource.CONFIGURED,
                role=GraphRole.CONFIG,
            )
        )
        if diagnostic.identity_status is GraphStatus.OK:
            input_media = _media(
                width=diagnostic.source_width,
                height=diagnostic.source_height,
                fps=diagnostic.pipeline_fps,
                frame_count=diagnostic.identity_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
        if diagnostic.omni_sample_status is GraphStatus.OK:
            output_media = _media(
                width=diagnostic.source_width,
                height=diagnostic.source_height,
                fps=diagnostic.omni_fps,
                frame_count=diagnostic.omni_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
    if template.id == "media.transform" and not diagnostic.audio_only:
        if diagnostic.omni_sample_status is GraphStatus.OK:
            input_media = _media(
                width=diagnostic.source_width,
                height=diagnostic.source_height,
                fps=diagnostic.omni_fps,
                frame_count=diagnostic.omni_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
        if diagnostic.media_transform_status is GraphStatus.OK:
            output_media = _media(
                width=diagnostic.transformed_width,
                height=diagnostic.transformed_height,
                fps=diagnostic.omni_fps,
                frame_count=diagnostic.transformed_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
        metrics.extend(
            [
                _metric(
                    "configured_short_edge",
                    "Config Short Edge",
                    config.video_short_edge,
                    unit="px",
                    source=GraphSource.CONFIGURED,
                    role=GraphRole.CONFIG,
                ),
                _metric(
                    "transform_mode",
                    "Transform Mode",
                    (
                        "smart_crop"
                        if diagnostic.smart_crop_applied is True
                        else "panorama"
                        if diagnostic.smart_crop_applied is False
                        else None
                    ),
                ),
                _metric(
                    "smart_crop_enabled",
                    "Smart Crop Enabled",
                    diagnostic.smart_crop_enabled,
                ),
                _metric(
                    "smart_crop_applied",
                    "Smart Crop Applied",
                    diagnostic.smart_crop_applied,
                ),
            ]
        )
    if template.id == "media.encode":
        if diagnostic.audio_only:
            output_media = GraphMedia(
                width=None,
                height=None,
                fps=None,
                frame_count=None,
                duration_ms=_datum_number(diagnostic.source_window_duration_ms),
            )
            metrics.extend(
                [
                    _metric("container", "Container", "m4a"),
                    _metric(
                        "has_audio",
                        "Has Audio",
                        diagnostic.encoded_has_audio,
                    ),
                    _metric(
                        "audio_sample_rate",
                        "Audio Sample Rate",
                        diagnostic.audio_sample_rate,
                        unit="hz",
                    ),
                ]
            )
        else:
            if diagnostic.media_transform_status is GraphStatus.OK:
                input_media = _media(
                    width=diagnostic.transformed_width,
                    height=diagnostic.transformed_height,
                    fps=diagnostic.omni_fps,
                    frame_count=diagnostic.transformed_frame_count,
                    duration_ms=diagnostic.source_window_duration_ms,
                )
            if diagnostic.media_encode_status is GraphStatus.OK:
                output_media = _media(
                    width=diagnostic.encoded_width,
                    height=diagnostic.encoded_height,
                    fps=diagnostic.encoded_fps,
                    frame_count=diagnostic.encoded_frame_count,
                    duration_ms=diagnostic.source_window_duration_ms,
                )
            metrics.extend(
                [
                    _metric("container", "Container", "mp4"),
                    _metric(
                        "has_audio",
                        "Has Audio",
                        diagnostic.encoded_has_audio,
                    ),
                ]
            )
    if template.id == "omni.request" and diagnostic.media_encode_status is GraphStatus.OK:
        input_media = (
            GraphMedia(
                width=None,
                height=None,
                fps=None,
                frame_count=None,
                duration_ms=_datum_number(diagnostic.source_window_duration_ms),
            )
            if diagnostic.audio_only
            else _media(
                width=diagnostic.encoded_width,
                height=diagnostic.encoded_height,
                fps=diagnostic.encoded_fps,
                frame_count=diagnostic.encoded_frame_count,
                duration_ms=diagnostic.source_window_duration_ms,
            )
        )
    if template.id == "result.complete":
        metrics.append(
            _metric(
                "encoded_media_generated",
                "Encoded Media Generated",
                diagnostic.media_encode_status is GraphStatus.OK,
            )
        )
    return GraphNode(
        id=template.id,
        kind=template.kind,
        label=template.label,
        group=template.group,
        rank=template.rank,
        order=template.order,
        status=status,
        freshness=freshness,
        last_observed_status=(
            status if freshness is GraphFreshness.STALE else None
        ),
        standalone=False,
        metrics=metrics,
        input=input_media,
        output=output_media,
        warnings=copy.deepcopy(diagnostic.warnings),
        evidence=_evidence(diagnostic, generated_at),
    )


def _device_edge(
    edge_id: str,
    source: str,
    target: str,
    diagnostic: PerDeviceFlowDiagnostics,
    freshness: GraphFreshness,
    generated_at: int,
) -> GraphEdge:
    source_status = _status_for_node(source, diagnostic)
    target_status = _status_for_node(target, diagnostic)
    active = not (
        (diagnostic.audio_only and target == "media.transform")
        or source_status in {
            GraphStatus.SKIPPED,
            GraphStatus.ERROR,
            GraphStatus.INACTIVE,
            GraphStatus.UNKNOWN,
        }
    )
    status = target_status if active else GraphStatus.INACTIVE
    if not active:
        label = (
            "Gate blocked"
            if source == "gate.visual" and target == "identity.track"
            else "Inactive video path"
        )
    elif source == "omni.sample" and target == "media.encode":
        label = "Audio bypass"
    else:
        label = "Frames"
    media = _edge_media(source, target, diagnostic) if active else None
    return GraphEdge(
        id=edge_id,
        **{"from": source, "to": target},
        label=label,
        active=active,
        status=status,
        freshness=freshness,
        last_observed_status=(
            status if freshness is GraphFreshness.STALE else None
        ),
        media=media,
        metrics=[],
        warnings=[],
        evidence=_evidence(diagnostic, generated_at),
    )


def _edge_media(
    source: str,
    target: str,
    diagnostic: PerDeviceFlowDiagnostics,
) -> GraphMedia | None:
    if target in {"buffer.sync_window", "buffer.ready_queue"}:
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=_rate(
                diagnostic.source_frame_count,
                diagnostic.source_window_duration_ms,
            ),
            frame_count=diagnostic.source_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if source == "buffer.ready_queue" and target == "pipeline.sample":
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=_rate(
                diagnostic.pipeline_input_frame_count,
                diagnostic.source_window_duration_ms,
            ),
            frame_count=diagnostic.pipeline_input_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if target == "gate.visual":
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.pipeline_fps,
            frame_count=diagnostic.pipeline_output_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if target == "identity.track":
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.pipeline_fps,
            frame_count=diagnostic.pipeline_output_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if target == "omni.sample":
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.pipeline_fps,
            frame_count=diagnostic.identity_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if target == "media.transform":
        return _media(
            width=diagnostic.source_width,
            height=diagnostic.source_height,
            fps=diagnostic.omni_fps,
            frame_count=diagnostic.omni_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if source == "media.transform" and target == "media.encode":
        return _media(
            width=diagnostic.transformed_width,
            height=diagnostic.transformed_height,
            fps=diagnostic.omni_fps,
            frame_count=diagnostic.transformed_frame_count,
            duration_ms=diagnostic.source_window_duration_ms,
        )
    if target == "result.complete":
        return None
    if source == "omni.sample" and target == "media.encode":
        return None
    return _media(
        width=diagnostic.encoded_width,
        height=diagnostic.encoded_height,
        fps=diagnostic.encoded_fps,
        frame_count=diagnostic.encoded_frame_count,
        duration_ms=diagnostic.source_window_duration_ms,
    )


def _device_edge_pairs(
    diagnostic: PerDeviceFlowDiagnostics,
) -> tuple[tuple[str, str, str], ...]:
    if not diagnostic.audio_only:
        return _EDGE_PAIRS
    return _EDGE_PAIRS + (
        ("omni.sample-to-media.encode", "omni.sample", "media.encode"),
    )




def _empty_node(template: _NodeTemplate) -> GraphNode:
    return GraphNode(
        id=template.id,
        kind=template.kind,
        label=template.label,
        group=template.group,
        rank=template.rank,
        order=template.order,
        status=GraphStatus.UNKNOWN,
        freshness=GraphFreshness.UNKNOWN,
        last_observed_status=None,
        standalone=False,
        metrics=[],
        input=None,
        output=None,
        warnings=[],
        evidence=None,
    )


def _empty_edge(edge_id: str, source: str, target: str) -> GraphEdge:
    return GraphEdge(
        id=edge_id,
        **{"from": source, "to": target},
        label=None,
        active=True,
        status=GraphStatus.UNKNOWN,
        freshness=GraphFreshness.UNKNOWN,
        last_observed_status=None,
        media=None,
        metrics=[],
        warnings=[],
        evidence=None,
    )


def _global_node(
    template: _NodeTemplate,
    fresh: list[PerDeviceFlowDiagnostics],
    *,
    active_count: int,
    snapshot_count: int,
    fresh_count: int,
    stale_count: int,
    freshness: GraphFreshness,
) -> GraphNode:
    statuses = [_status_for_node(template.id, item) for item in fresh]
    current_status = _aggregate_status(statuses) if fresh else GraphStatus.UNKNOWN
    counts = {status: statuses.count(status) for status in GraphStatus}
    metrics = [
        _metric(
            "active_device_count",
            "Active Devices",
            active_count,
            unit="devices",
        ),
        _metric(
            "snapshot_device_count",
            "Snapshot Devices",
            snapshot_count,
            unit="devices",
        ),
        _metric(
            "fresh_device_count",
            "Fresh Devices",
            fresh_count,
            unit="devices",
        ),
        _metric(
            "stale_device_count",
            "Stale Devices",
            stale_count,
            unit="devices",
        ),
    ]
    metrics.extend(
        _metric(
            f"{status.value}_device_count",
            f"{status.value.title()} Devices",
            counts[status],
            unit="devices",
        )
        for status in (
            GraphStatus.OK,
            GraphStatus.WARNING,
            GraphStatus.ERROR,
            GraphStatus.BACKPRESSURE,
            GraphStatus.SKIPPED,
        )
    )
    return GraphNode(
        id=template.id,
        kind=template.kind,
        label=template.label,
        group=template.group,
        rank=template.rank,
        order=template.order,
        status=current_status,
        freshness=freshness,
        last_observed_status=None,
        standalone=False,
        metrics=metrics,
        input=None,
        output=None,
        warnings=[],
        evidence=None,
    )


def _global_edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    freshness: GraphFreshness,
    has_fresh: bool,
) -> GraphEdge:
    return GraphEdge(
        id=edge_id,
        **{"from": source, "to": target},
        label=None,
        active=True,
        status=GraphStatus.OK if has_fresh else GraphStatus.UNKNOWN,
        freshness=freshness,
        last_observed_status=None,
        media=None,
        metrics=[],
        warnings=[],
        evidence=None,
    )


def _aggregate_status(statuses: list[GraphStatus]) -> GraphStatus:
    priority = (
        GraphStatus.ERROR,
        GraphStatus.BACKPRESSURE,
        GraphStatus.WARNING,
        GraphStatus.SKIPPED,
        GraphStatus.OK,
    )
    return next(
        (status for status in priority if status in statuses),
        GraphStatus.UNKNOWN,
    )


def _rate(frame_count: int | None, duration_ms: float | None) -> float | None:
    if frame_count is None or not duration_ms or duration_ms <= 0:
        return None
    return frame_count * 1000 / duration_ms


def _current_config_snapshot() -> PerceptionFlowConfigSnapshot:
    try:
        from miloco.config import get_settings

        settings = get_settings()
        engine = settings.perception.engine
        input_config = engine.get("input", {})
        gate_config = engine.get("gate", {})
        return PerceptionFlowConfigSnapshot(
            window_size_sec=settings.perception.collect.window_size,
            max_windows=settings.perception.collect.max_windows,
            full_action=settings.perception.collect.full_action,
            pipeline_fps=input_config.get("fps", 3),
            gate_check_fps=gate_config.get("check_fps", 1),
            omni_fps=input_config.get("omni_fps", 1),
            video_short_edge=input_config.get("video_short_edge"),
            stream_profile="LOW",
        )
    except Exception:
        return PerceptionFlowConfigSnapshot()
