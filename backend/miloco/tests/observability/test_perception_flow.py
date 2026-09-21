from __future__ import annotations

import pytest
from miloco.observability.perception_flow import (
    GraphEdge,
    GraphFreshness,
    GraphKind,
    GraphNode,
    GraphResponse,
    GraphSource,
    GraphStatus,
    PerceptionFlowConfigSnapshot,
    PerceptionFlowSnapshotStore,
    PerDeviceFlowDiagnostics,
    build_perception_flow_graph,
)
from pydantic import ValidationError


def _diagnostic(
    device_id: str = "camera-1",
    *,
    observed_at: int = 1_000,
    status: GraphStatus = GraphStatus.OK,
    room_name: str = "Living Room",
    trace_id: str = "trace-1",
    device_trace_id: str = "device-trace-1",
) -> PerDeviceFlowDiagnostics:
    return PerDeviceFlowDiagnostics(
        device_id=device_id,
        room_name=room_name,
        trace_id=trace_id,
        device_trace_id=device_trace_id,
        observed_at=observed_at,
        status=status,
        source_width=1920,
        source_height=1080,
        source_frame_count=60,
        source_window_duration_ms=4_000,
        pipeline_input_frame_count=60,
        pipeline_output_frame_count=12,
        pipeline_fps=3.0,
        gate_checked_frame_count=4,
        gate_status=status,
        identity_frame_count=12,
        identity_status=status,
        omni_frame_count=4,
        omni_fps=1.0,
        omni_status=status,
        omni_sample_status=status,
        media_transform_status=status,
        media_encode_status=status,
        omni_request_status=status,
        transformed_width=512,
        transformed_height=288,
        transformed_frame_count=4,
        encoded_width=512,
        encoded_height=288,
        encoded_fps=1.0,
        encoded_frame_count=4,
        encoded_has_audio=True,
        audio_only=False,
    )


def test_store_merges_cycle_atomically_and_returns_copies():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})

    snapshot = store.snapshot()
    assert snapshot.latest_cycle_id == "trace-1"
    assert snapshot.per_device["camera-1"].source_frame_count == 60

    snapshot.per_device["camera-1"].source_frame_count = 999
    assert store.snapshot().per_device["camera-1"].source_frame_count == 60


def test_store_retains_active_devices_and_removes_unregistered_devices():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})
    store.merge_cycle("trace-2", 2_000, {"camera-2": _diagnostic("camera-2")})

    store.retain_devices({"camera-2"})

    assert set(store.snapshot().per_device) == {"camera-2"}


def test_graph_lists_active_device_before_its_first_runtime_snapshot():
    store = PerceptionFlowSnapshotStore()
    store.retain_devices({"camera-1"})

    response = build_perception_flow_graph(
        store,
        device_id=None,
        generated_at=2_000,
        stale_after_sec=30,
    )

    assert [device.device_id for device in response.summary.devices] == ["camera-1"]
    device = response.summary.devices[0]
    assert device.room_name is None
    assert device.observed_at is None
    assert device.freshness == GraphFreshness.UNKNOWN
    assert device.status == GraphStatus.UNKNOWN


def test_graph_marks_old_snapshot_stale_and_preserves_last_status():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic(status=GraphStatus.ERROR)})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=40_000,
        stale_after_sec=30,
    )
    node = next(node for node in response.graph.nodes if node.id == "result.complete")
    assert node.freshness == "stale"
    assert node.status == GraphStatus.ERROR
    assert node.last_observed_status == GraphStatus.ERROR


def test_device_graph_keeps_successful_upstream_status_when_gate_skips():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic(status=GraphStatus.SKIPPED)
    diagnostic.gate_status = GraphStatus.SKIPPED
    diagnostic.identity_status = GraphStatus.SKIPPED
    diagnostic.omni_status = GraphStatus.SKIPPED
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    statuses = {node.id: node.status for node in response.graph.nodes}

    assert statuses["media.decoded"] == GraphStatus.OK
    assert statuses["buffer.ready_queue"] == GraphStatus.OK
    assert statuses["pipeline.sample"] == GraphStatus.OK
    assert statuses["gate.visual"] == GraphStatus.SKIPPED
    assert statuses["identity.track"] == GraphStatus.SKIPPED
    assert statuses["result.complete"] == GraphStatus.SKIPPED
    edges = {(edge.from_, edge.to): edge for edge in response.graph.edges}
    assert edges[("pipeline.sample", "gate.visual")].active is True
    assert edges[("gate.visual", "identity.track")].active is False
    assert edges[("gate.visual", "identity.track")].label == "Gate blocked"
    assert edges[("gate.visual", "identity.track")].media is None


def test_device_graph_shows_decoded_media_as_first_node_input_and_hides_routine_labels():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    nodes = {node.id: node for node in response.graph.nodes}
    edges = {(edge.from_, edge.to): edge for edge in response.graph.edges}

    decoded = nodes["media.decoded"]
    assert decoded.input.width.value == 1920
    assert decoded.input.height.value == 1080
    assert decoded.input.fps.value == 15
    assert decoded.output.width.value == 1920
    assert edges[("gate.visual", "identity.track")].label == "Frames"
    assert edges[("omni.request", "result.complete")].label == "Frames"


def test_device_graph_does_not_propagate_omni_error_to_upstream_nodes():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic(status=GraphStatus.ERROR)
    diagnostic.gate_status = GraphStatus.OK
    diagnostic.identity_status = GraphStatus.OK
    diagnostic.omni_status = GraphStatus.ERROR
    diagnostic.error_stage = "omni"
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    statuses = {node.id: node.status for node in response.graph.nodes}

    assert statuses["media.decoded"] == GraphStatus.OK
    assert statuses["pipeline.sample"] == GraphStatus.OK
    assert statuses["gate.visual"] == GraphStatus.OK
    assert statuses["identity.track"] == GraphStatus.OK
    assert statuses["omni.request"] == GraphStatus.ERROR
    assert statuses["result.complete"] == GraphStatus.ERROR


def test_global_graph_only_exposes_discrete_counts_and_no_media():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle(
        "trace-1",
        1_000,
        {
            "camera-1": _diagnostic(status=GraphStatus.OK),
            "camera-2": _diagnostic(
                "camera-2", observed_at=1_000, status=GraphStatus.ERROR
            ),
        },
    )

    response = build_perception_flow_graph(
        store,
        device_id=None,
        generated_at=2_000,
        stale_after_sec=30,
    )
    assert response.scope.device_id is None
    assert response.summary.latest_trace_id is None
    assert all(node.input is None and node.output is None for node in response.graph.nodes)
    assert any(metric.key == "active_device_count" for metric in response.graph.nodes[0].metrics)
    assert not any(metric.key in {"observed_fps", "frame_count"} for node in response.graph.nodes for metric in node.metrics)


def test_global_graph_does_not_count_snapshots_as_active_devices():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})
    store.retain_devices(set())

    response = build_perception_flow_graph(
        store,
        device_id=None,
        generated_at=2_000,
        stale_after_sec=30,
    )

    source = next(node for node in response.graph.nodes if node.id == "media.decoded")
    active = next(metric for metric in source.metrics if metric.key == "active_device_count")
    assert active.value == 0


def test_global_graph_aggregates_inactive_devices_honestly():
    # 全体设备 audio-only 时 media.transform 人人 INACTIVE:全局节点必须显示
    # INACTIVE 而不是 UNKNOWN,计数 metric 之和必须等于 fresh 设备数——
    # 否则操作员会误读成"transform 数据缺失"。
    store = PerceptionFlowSnapshotStore()

    def audio_only_diagnostic(device_id: str) -> PerDeviceFlowDiagnostics:
        diagnostic = _diagnostic(device_id, observed_at=1_000)
        diagnostic.audio_only = True
        return diagnostic

    store.merge_cycle(
        "trace-1",
        1_000,
        {
            "camera-1": audio_only_diagnostic("camera-1"),
            "camera-2": audio_only_diagnostic("camera-2"),
        },
    )

    response = build_perception_flow_graph(
        store,
        device_id=None,
        generated_at=2_000,
        stale_after_sec=30,
    )
    nodes = {node.id: node for node in response.graph.nodes}

    transform = nodes["media.transform"]
    assert transform.status == GraphStatus.INACTIVE

    transform_metrics = {m.key: m.value for m in transform.metrics}
    assert transform_metrics["inactive_device_count"] == 2

    summed = {
        key: value
        for key, value in transform_metrics.items()
        if key.endswith("_device_count") and key not in {
            "active_device_count",
            "snapshot_device_count",
            "fresh_device_count",
            "stale_device_count",
        }
    }
    assert sum(summed.values()) == transform_metrics["fresh_device_count"]


def test_audio_only_graph_does_not_emit_zero_video_dimensions():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    diagnostic.audio_only = True
    diagnostic.source_width = None
    diagnostic.source_height = None
    diagnostic.source_frame_count = None
    diagnostic.encoded_width = None
    diagnostic.encoded_height = None
    diagnostic.encoded_fps = None
    diagnostic.encoded_frame_count = None
    diagnostic.encoded_has_audio = True
    diagnostic.audio_sample_rate = 16_000
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    encode = next(node for node in response.graph.nodes if node.id == "media.encode")
    assert encode.output.width is None
    assert encode.output.height is None
    assert any(metric.key == "container" and metric.value == "m4a" for metric in encode.metrics)
    assert any(metric.key == "audio_sample_rate" for metric in encode.metrics)


def test_audio_only_keeps_completed_video_analysis_and_bypasses_transform():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    diagnostic.audio_only = True
    diagnostic.media_transform_status = GraphStatus.SKIPPED
    diagnostic.media_encode_status = GraphStatus.OK
    diagnostic.encoded_width = None
    diagnostic.encoded_height = None
    diagnostic.encoded_fps = None
    diagnostic.encoded_frame_count = None
    diagnostic.encoded_has_audio = True
    diagnostic.audio_sample_rate = 16_000
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    edges = {(edge.from_, edge.to): edge for edge in response.graph.edges}

    assert edges[("pipeline.sample", "gate.visual")].active is True
    assert edges[("gate.visual", "identity.track")].active is True
    assert edges[("identity.track", "omni.sample")].active is True
    assert edges[("identity.track", "omni.sample")].label == "Frames"
    transform = next(node for node in response.graph.nodes if node.id == "media.transform")
    assert transform.status == GraphStatus.INACTIVE
    assert edges[("omni.sample", "media.transform")].active is False
    assert edges[("omni.sample", "media.encode")].active is True
    assert edges[("omni.sample", "media.encode")].label == "Audio bypass"


def test_audio_only_encode_failure_does_not_claim_media_was_generated():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic(status=GraphStatus.ERROR)
    diagnostic.audio_only = True
    diagnostic.media_transform_status = GraphStatus.SKIPPED
    diagnostic.media_encode_status = GraphStatus.ERROR
    diagnostic.omni_request_status = GraphStatus.SKIPPED
    diagnostic.encoded_width = None
    diagnostic.encoded_height = None
    diagnostic.encoded_fps = None
    diagnostic.encoded_frame_count = None
    diagnostic.encoded_has_audio = None
    diagnostic.audio_sample_rate = None
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    result = next(node for node in response.graph.nodes if node.id == "result.complete")
    generated = next(
        metric for metric in result.metrics if metric.key == "encoded_media_generated"
    )

    assert generated.value is False


def test_device_graph_includes_configured_pipeline_targets():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})
    config = PerceptionFlowConfigSnapshot(
        window_size_sec=4,
        max_windows=3,
        full_action="clear",
        pipeline_fps=3,
        gate_check_fps=1,
        omni_fps=1,
        video_short_edge=512,
    )

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
        config=config,
    )

    metrics = {
        node.id: {metric.key: metric for metric in node.metrics}
        for node in response.graph.nodes
    }
    assert metrics["buffer.sync_window"]["configured_window_size"].value == 4
    assert metrics["buffer.sync_window"]["configured_window_size"].label == "Config Window Size"
    assert metrics["buffer.ready_queue"]["configured_max_windows"].value == 3
    assert metrics["buffer.ready_queue"]["configured_max_windows"].label == "Max Windows"
    assert metrics["buffer.ready_queue"]["configured_full_action"].value == "clear"
    assert metrics["buffer.ready_queue"]["configured_full_action"].label == "Config Full Action"
    assert metrics["pipeline.sample"]["configured_fps"].value == 3
    assert metrics["pipeline.sample"]["configured_fps"].label == "Config FPS"
    assert metrics["gate.visual"]["configured_check_fps"].value == 1
    assert metrics["gate.visual"]["configured_check_fps"].label == "Config Check FPS"
    assert metrics["omni.sample"]["configured_fps"].value == 1
    assert metrics["omni.sample"]["configured_fps"].label == "Config FPS"
    assert metrics["media.transform"]["configured_short_edge"].value == 512
    assert metrics["media.transform"]["configured_short_edge"].label == "Config Short Edge"

    ready_queue = next(
        node for node in response.graph.nodes if node.id == "buffer.ready_queue"
    )
    assert [metric.key for metric in ready_queue.metrics[:2]] == [
        "last_drain_ready_depth_before",
        "dropped_windows",
    ]


def test_tracking_uses_sampled_source_frames_and_exposes_internal_model_sizes():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    diagnostic.source_width = 1920
    diagnostic.source_height = 1080
    diagnostic.gate_output_width = 320
    diagnostic.gate_output_height = 240
    diagnostic.detector_input_width = 640
    diagnostic.detector_input_height = 640
    diagnostic.reid_input_width = 128
    diagnostic.reid_input_height = 256
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )

    tracking = next(node for node in response.graph.nodes if node.id == "identity.track")
    gate = next(node for node in response.graph.nodes if node.id == "gate.visual")
    passed_frames = next(
        edge
        for edge in response.graph.edges
        if edge.from_ == "gate.visual" and edge.to == "identity.track"
    )

    assert gate.output.width.value == 1920
    assert gate.output.height.value == 1080
    assert any(
        metric.key == "check_frame_size"
        and metric.value == "320×240"
        and metric.source == GraphSource.OBSERVED
        for metric in gate.metrics
    )
    assert tracking.input.width.value == 1920
    assert tracking.input.height.value == 1080
    assert tracking.output.width.value == 1920
    assert tracking.output.height.value == 1080
    assert tracking.label == "Detection, Tracking, ReID"
    assert any(
        metric.key == "detector_input_size" and metric.value == "640×640"
        for metric in tracking.metrics
    )
    assert any(
        metric.key == "reid_input_size" and metric.value == "128×256"
        for metric in tracking.metrics
    )
    assert passed_frames.label == "Frames"
    assert passed_frames.media.width.value == 1920
    assert passed_frames.media.height.value == 1080
    assert passed_frames.media.fps.value == 3.0
    assert passed_frames.media.frame_count.value == diagnostic.identity_frame_count


def test_gate_diff_size_is_first_visible_metric():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    diagnostic.gate_output_width = 320
    diagnostic.gate_output_height = 240
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    gate = next(node for node in response.graph.nodes if node.id == "gate.visual")

    assert gate.metrics[0].key == "check_frame_size"
    assert gate.metrics[0].label == "Diff Image Size"
    assert gate.metrics[0].value == "320×240"


def test_edges_describe_media_at_the_actual_stage_boundary():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    edges = {(edge.from_, edge.to): edge for edge in response.graph.edges}

    ready_to_sampling = edges[("buffer.ready_queue", "pipeline.sample")]
    sampling_to_gate = edges[("pipeline.sample", "gate.visual")]
    encode_to_request = edges[("media.encode", "omni.request")]
    request_to_result = edges[("omni.request", "result.complete")]

    assert ready_to_sampling.media.frame_count.value == 60
    assert ready_to_sampling.media.fps.value == 15.0
    assert sampling_to_gate.media.frame_count.value == 12
    assert sampling_to_gate.media.fps.value == 3.0
    assert encode_to_request.media.frame_count.value == 4
    assert encode_to_request.media.width.value == 512
    assert request_to_result.media is None
    assert request_to_result.label == "Frames"


def test_media_transform_shows_where_runtime_resolution_changes():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic()
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    nodes = {node.id: node for node in response.graph.nodes}

    transform = nodes["media.transform"]
    encode = nodes["media.encode"]
    assert transform.input.width.value == 1920
    assert transform.input.height.value == 1080
    assert transform.output.width.value == 512
    assert transform.output.height.value == 288
    assert encode.input.width.value == 512
    assert encode.input.height.value == 288


def test_omni_request_failure_does_not_mark_completed_upstream_stages_failed():
    store = PerceptionFlowSnapshotStore()
    diagnostic = _diagnostic(status=GraphStatus.ERROR)
    diagnostic.omni_sample_status = GraphStatus.OK
    diagnostic.media_transform_status = GraphStatus.OK
    diagnostic.media_encode_status = GraphStatus.OK
    diagnostic.omni_request_status = GraphStatus.ERROR
    store.merge_cycle("trace-1", 1_000, {"camera-1": diagnostic})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )
    statuses = {node.id: node.status for node in response.graph.nodes}

    assert statuses["omni.sample"] == GraphStatus.OK
    assert statuses["media.transform"] == GraphStatus.OK
    assert statuses["media.encode"] == GraphStatus.OK
    assert statuses["omni.request"] == GraphStatus.ERROR


def test_graph_does_not_invent_unobserved_processing_dimensions():
    store = PerceptionFlowSnapshotStore()
    store.merge_cycle("trace-1", 1_000, {"camera-1": _diagnostic()})

    response = build_perception_flow_graph(
        store,
        device_id="camera-1",
        generated_at=2_000,
        stale_after_sec=30,
    )

    nodes = {node.id: node for node in response.graph.nodes}
    assert nodes["gate.visual"].output.width.value == 1920
    assert nodes["gate.visual"].output.height.value == 1080
    reid_metrics = {
        metric.key: metric for metric in nodes["identity.track"].metrics
    }
    assert reid_metrics["detector_input_size"].value is None
    assert reid_metrics["reid_input_size"].value is None


def test_backend_rejects_unknown_producer_enums():
    with pytest.raises(ValidationError):
        GraphNode(
            id="node",
            kind="not-a-kind",
            label="Node",
            group=None,
            rank=None,
            order=0,
            status="ok",
            freshness="fresh",
            last_observed_status=None,
            standalone=False,
            metrics=[],
            input=None,
            output=None,
            warnings=[],
            evidence=None,
        )


def test_graph_rejects_duplicate_nodes_and_dangling_edges():
    node = GraphNode(
        id="node",
        kind=GraphKind.SOURCE,
        label="Node",
        group=None,
        rank=None,
        order=0,
        status=GraphStatus.UNKNOWN,
        freshness="unknown",
        last_observed_status=None,
        standalone=False,
        metrics=[],
        input=None,
        output=None,
        warnings=[],
        evidence=None,
    )
    with pytest.raises(ValueError, match="duplicate node"):
        GraphResponse.validate_graph(
            nodes=[node, node.model_copy()],
            edges=[],
        )
    with pytest.raises(ValueError, match="unknown node"):
        GraphResponse.validate_graph(
            nodes=[node],
            edges=[
                GraphEdge(
                    id="edge",
                    **{
                        "from": "node",
                        "to": "missing",
                    },
                    label=None,
                    active=True,
                    status=GraphStatus.UNKNOWN,
                    freshness="unknown",
                    last_observed_status=None,
                    media=None,
                    metrics=[],
                    warnings=[],
                    evidence=None,
                )
            ],
        )


def test_graph_rejects_disconnected_active_node():
    source = GraphNode(
        id="source",
        kind=GraphKind.SOURCE,
        label="Source",
        group=None,
        rank=0,
        order=0,
        status=GraphStatus.OK,
        freshness="fresh",
        last_observed_status=None,
        standalone=False,
        metrics=[],
        input=None,
        output=None,
        warnings=[],
        evidence=None,
    )
    sink = source.model_copy(
        update={
            "id": "sink",
            "kind": GraphKind.SINK,
            "label": "Sink",
            "rank": 1,
        }
    )
    disconnected = source.model_copy(
        update={"id": "disconnected", "kind": GraphKind.TRANSFORM}
    )
    edge = GraphEdge(
        id="source-to-sink",
        **{"from": "source", "to": "sink"},
        label=None,
        active=True,
        status=GraphStatus.OK,
        freshness="fresh",
        last_observed_status=None,
        media=None,
        metrics=[],
        warnings=[],
        evidence=None,
    )

    with pytest.raises(ValueError, match="source-to-sink path"):
        GraphResponse.validate_graph(
            nodes=[source, sink, disconnected],
            edges=[edge],
        )
