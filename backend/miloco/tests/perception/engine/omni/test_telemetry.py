import struct

import pytest
from miloco.perception.engine.omni import telemetry


class FakeSocket:
    def __init__(self, data):
        self.data = data

    def getsockopt(self, level, option, size):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeStream:
    def __init__(self, sock):
        self.sock = sock

    def get_extra_info(self, name):
        return self.sock if name == "socket" else None


def test_read_linux_tcp_info_converts_to_milliseconds(monkeypatch):
    monkeypatch.setattr(telemetry.sys, "platform", "linux")
    raw = bytearray(104)
    struct.pack_into("=I", raw, 68, 21_762)
    struct.pack_into("=I", raw, 72, 10_881)
    struct.pack_into("=I", raw, 100, 3)

    assert telemetry.read_tcp_info(FakeStream(FakeSocket(bytes(raw)))) == {
        "status": "ok",
        "srtt_ms": 21.762,
        "rttvar_ms": 10.881,
        "total_retrans": 3,
    }


def test_read_darwin_tcp_connection_info(monkeypatch):
    monkeypatch.setattr(telemetry.sys, "platform", "darwin")
    raw = bytearray(112)
    struct.pack_into("=I", raw, 44, 23)
    struct.pack_into("=I", raw, 48, 7)
    struct.pack_into("=Q", raw, 104, 2)

    assert telemetry.read_tcp_info(FakeStream(FakeSocket(bytes(raw)))) == {
        "status": "ok",
        "srtt_ms": 23,
        "rttvar_ms": 7,
        "total_retrans": 2,
    }


@pytest.mark.parametrize(
    ("platform", "stream", "expected"),
    [
        ("freebsd", FakeStream(FakeSocket(b"")), "unsupported_platform"),
        ("linux", None, "socket_unavailable"),
        ("darwin", FakeStream(FakeSocket(b"short")), "short_tcp_info"),
        ("linux", FakeStream(FakeSocket(OSError())), "read_failed"),
    ],
)
def test_read_tcp_info_unavailable_states(monkeypatch, platform, stream, expected):
    monkeypatch.setattr(telemetry.sys, "platform", platform)
    result = telemetry.read_tcp_info(stream)

    assert result == {
        "status": expected,
        "srtt_ms": None,
        "rttvar_ms": None,
        "total_retrans": None,
    }


def test_ttft_records_first_non_empty_content(monkeypatch):
    times = iter([1.0, 1.05, 1.08])
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: next(times))
    call = telemetry.OmniCallTelemetry(streaming=True)
    call.observe_content("")
    call.observe_content("first")
    call.observe_content("later")

    assert call.to_dict()["ttft_ms"] == pytest.approx(50)


def test_non_streaming_call_has_no_ttft():
    call = telemetry.OmniCallTelemetry(streaming=False, started_at=1.0)

    assert call.to_dict(duration_ms=200)["ttft_ms"] is None


def test_publish_omni_telemetry_writes_traceable_event_without_content(monkeypatch):
    from miloco.observability import context, metrics_client

    class FakeMetricsClient:
        def __init__(self):
            self.events = []

        def publish_event(self, **event):
            self.events.append(event)

    client = FakeMetricsClient()
    monkeypatch.setattr(metrics_client, "get_metrics_client", lambda: client)
    monkeypatch.setattr(
        context,
        "get_device_context",
        lambda: context.DeviceContext(
            device_trace_id="device-trace", device_id="camera-1", room_name="living"
        ),
    )

    telemetry.publish_omni_telemetry(
        model="test-model",
        telemetry={
            "streaming": True,
            "duration_ms": 123.0,
            "ttft_ms": 45.0,
            "http_requests": [],
        },
    )

    assert len(client.events) == 1
    event = client.events[0]
    assert event["event_type"] == "omni_llm_telemetry"
    assert event["source"] == "camera-1"
    assert event["payload"] == {
        "model": "test-model",
        "streaming": True,
        "duration_ms": 123.0,
        "ttft_ms": 45.0,
        "http_requests": [],
    }
    assert not {"prompt", "body", "response"} & event["payload"].keys()


def test_push_omni_trace_stores_telemetry():
    from miloco.perception.snapshot_context import (
        OmniEventArtifacts,
        event_artifacts_scope,
        push_omni_trace,
    )

    artifacts = OmniEventArtifacts()
    telemetry_data = {
        "streaming": False,
        "duration_ms": 20.0,
        "ttft_ms": None,
        "http_requests": [],
    }
    with event_artifacts_scope(artifacts):
        push_omni_trace(
            request_messages=[{"role": "user", "content": "prompt text"}],
            response_raw={"choices": [{"message": {"content": "response text"}}]},
            latency_ms=20.0,
            error=None,
            model="test-model",
            telemetry=telemetry_data,
        )

    assert artifacts.trace["calls"][0]["telemetry"] == telemetry_data
