import pytest
from miloco.observability.types import (
    ActionLedgerRecord,
    AgentRunRecord,
    CycleTraceRecord,
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
    IdentityTrace,
    OmniTrace,
)


def test_gate_trace_skipped_consistency():
    with pytest.raises(ValueError, match="skipped"):
        GateTrace(ms=1.0, video_ms=0.5, audio_ms=0.5,
                  video_pass=True, audio_pass=False, skipped=True)


def test_gate_trace_passed_property():
    g = GateTrace(ms=1.0, video_ms=0.5, audio_ms=0.5,
                  video_pass=False, audio_pass=False, skipped=True)
    assert g.passed is False


def test_device_trace_record_to_row_minimal():
    record = DeviceTraceRecord(
        device_trace_id="dt-1", cycle_id="c-1", timestamp=100, device_id="did-1",
        room_name="客厅",
        decode=DecodeTrace(video_avg_ms=1.0, audio_avg_ms=2.0,
                           video_frame_count=10, audio_frame_count=5),
        gate=GateTrace(ms=1.0, video_ms=0.5, audio_ms=0.5,
                       video_pass=True, audio_pass=False, skipped=False),
    )
    row = record.to_row()
    assert row["device_trace_id"] == "dt-1"
    assert row["cycle_id"] == "c-1"
    assert row["device_id"] == "did-1"
    assert row["gate_video_pass"] == 1
    assert row["gate_audio_pass"] == 0
    assert row["gate_skipped"] == 0
    assert "identity_ms" not in row
    assert "omni_ms" not in row


def test_device_trace_record_to_row_with_omni():
    record = DeviceTraceRecord(
        device_trace_id="dt-1", cycle_id="c-1", timestamp=100, device_id="did-1",
        room_name="客厅",
        decode=DecodeTrace(video_avg_ms=1.0, audio_avg_ms=2.0,
                           video_frame_count=10, audio_frame_count=5),
        gate=GateTrace(ms=1.0, video_ms=0.5, audio_ms=0.5,
                       video_pass=True, audio_pass=True, skipped=False),
        identity=IdentityTrace(ms=2.0),
        omni=OmniTrace(ms=100.0, retry_count=1),
    )
    row = record.to_row()
    assert row["identity_ms"] == 2.0
    assert row["omni_ms"] == 100.0
    assert row["omni_retry_count"] == 1


def test_cycle_trace_record_to_row():
    record = CycleTraceRecord(
        trace_id="c-1", timestamp=100, device_count=2, skipped=False,
        in_delay_ms=10.0, out_delay_ms=20.0, decode_ms=1.0, collect_ms=2.0,
        convert_ms=3.0, log_ms=4.0, cycle_total_ms=100.0,
        pipeline_total_ms=90.0, window_duration_ms=3000.0,
        window_first_frame_recv_ms=99000, stream_lag_ms=500.0,
        gate_ms=2.0, gate_video_ms=1.0, gate_audio_ms=1.0,
        gate_video_pass=True, gate_audio_pass=False,
        identity_ms=4.0, omni_ms=180.0,
        omni_call_count=2, omni_error_count=0,
    )
    row = record.to_row()
    assert row["trace_id"] == "c-1"
    assert row["gate_video_pass"] == 1
    # agent_* 列已搬到 agent_runs 表,traces 行不再持有
    assert "run_id" not in row
    assert "agent_query" not in row
    assert "has_agent_turn" not in row


def test_agent_run_record_to_row():
    record = AgentRunRecord(
        run_id="r-1", trace_id="c-1", timestamp=1000, source="interaction",
        webhook_rtt_ms=15.0,
        query="hello", duration_ms=1000.0,
        llm_call_count=2, tool_call_count=3,
        llm_total_ms=820.0, tool_total_ms=312.0, tool_max_ms=200.0,
        slowest_tool_name="miot_call",
        success=True, error_count=0, error_msg=None, jsonl_path=None,
    )
    row = record.to_row()
    assert row["run_id"] == "r-1"
    assert row["trace_id"] == "c-1"
    assert row["source"] == "interaction"
    assert row["webhook_rtt_ms"] == 15.0
    assert row["llm_total_ms"] == 820.0
    assert row["slowest_tool_name"] == "miot_call"
    assert row["success"] == 1


def test_action_ledger_record_to_row_carries_link():
    """台账行的 to_row 带上链路两列(v5)。

    落库那条 INSERT 的列名由 row 的键推出来,故「record 少一个键」= 「那一列永远
    写不进去」——而且不会报错,只会静默留 NULL。这条把两个键钉住。
    """
    base = dict(
        id="a-1", timestamp=1000, action_type="set_property", did="d-1",
        device_name="灯", room="客厅", iid="prop.2.1", value_json="true",
        result_code=0, result_msg=None, success=True, error=None,
    )
    row = ActionLedgerRecord(**base).to_row()
    assert row["phase"] is None
    assert row["trigger_event_id"] is None

    row = ActionLedgerRecord(
        **base, source="rule", source_id="r-1", phase="enter", trigger_event_id="ev-1"
    ).to_row()
    assert row["phase"] == "enter"
    assert row["trigger_event_id"] == "ev-1"


class TestGateTraceHoldPass:
    """Section 7.5 — GateTrace hold_pass 字段 + __post_init__ 兼容 hold 拉起的 packet。"""

    def test_hold_pass_default_false(self):
        g = GateTrace(
            ms=1.0, video_ms=0.5, audio_ms=0.5,
            video_pass=True, audio_pass=False, skipped=False,
        )
        assert g.hold_pass is False

    def test_hold_pass_field(self):
        g = GateTrace(
            ms=1.0, video_ms=0.5, audio_ms=0.5,
            video_pass=False, audio_pass=False, skipped=False,
            hold_pass=True,
        )
        assert g.hold_pass is True

    def test_post_init_allows_hold_pulled_packet(self):
        """visual=F + audio=F + hold_pass=T 时,skipped=False(packet 生成)。"""
        g = GateTrace(
            ms=1.0, video_ms=0.5, audio_ms=0.5,
            video_pass=False, audio_pass=False, skipped=False,
            hold_pass=True,
        )
        assert g.skipped is False

    def test_post_init_rejects_inconsistent_skipped(self):
        """全不通过 + 非 hold → 必须 skipped=True。"""
        with pytest.raises(ValueError):
            GateTrace(
                ms=1.0, video_ms=0.5, audio_ms=0.5,
                video_pass=False, audio_pass=False, skipped=False,
                hold_pass=False,
            )
