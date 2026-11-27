from __future__ import annotations

import logging
import socket
import struct
import sys
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_DARWIN_TCP_CONNECTION_INFO = 0x106
_DARWIN_TCP_CONNECTION_INFO_SIZE = 112


def _empty_tcp_info(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "srtt_ms": None,
        "rttvar_ms": None,
        "total_retrans": None,
    }


def read_tcp_info(stream: Any) -> dict[str, Any]:
    if sys.platform == "linux":
        option = getattr(socket, "TCP_INFO", None)
        size = 104
        rtt_offset, rttvar_offset, retrans_offset = 68, 72, 100
        divisor = 1000
        retrans_format = "=I"
    elif sys.platform == "darwin":
        option = getattr(socket, "TCP_CONNECTION_INFO", _DARWIN_TCP_CONNECTION_INFO)
        size = _DARWIN_TCP_CONNECTION_INFO_SIZE
        rtt_offset, rttvar_offset, retrans_offset = 44, 48, 104
        divisor = 1
        retrans_format = "=Q"
    else:
        return _empty_tcp_info("unsupported_platform")

    if option is None:
        return _empty_tcp_info("unsupported_platform")
    try:
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            return _empty_tcp_info("socket_unavailable")
        raw = sock.getsockopt(socket.IPPROTO_TCP, option, size)
        if len(raw) < size:
            return _empty_tcp_info("short_tcp_info")
        srtt = struct.unpack_from("=I", raw, rtt_offset)[0]
        if srtt == 0:
            return _empty_tcp_info("no_rtt_sample")
        return {
            "status": "ok",
            "srtt_ms": srtt / divisor,
            "rttvar_ms": struct.unpack_from("=I", raw, rttvar_offset)[0] / divisor,
            "total_retrans": struct.unpack_from(retrans_format, raw, retrans_offset)[0],
        }
    except Exception:
        return _empty_tcp_info("read_failed")


class OmniCallTelemetry:
    """Capture caller-observed timing and connection-level TCP statistics.

    ``ttft_ms`` is measured from the start of the Omni HTTP call through the
    first non-empty streamed content delta. TCP RTT values describe the active
    transport connection, not an individual HTTP request.
    """
    def __init__(self, *, streaming: bool, started_at: float | None = None):
        self.streaming = streaming
        self.started_at = time.monotonic() if started_at is None else started_at
        self.first_content_at: float | None = None
        self.http_requests: list[dict[str, Any]] = []

    def record_response(self, response: Any) -> None:
        self.http_requests.append(
            {
                "status_code": getattr(response, "status_code", None),
                "tcp_info": read_tcp_info(
                    getattr(response, "extensions", {}).get("network_stream")
                ),
            }
        )

    def observe_content(self, content: str | None) -> None:
        if content and self.first_content_at is None:
            self.first_content_at = time.monotonic()

    def to_dict(self, *, duration_ms: float | None = None) -> dict[str, Any]:
        if duration_ms is None:
            duration_ms = (time.monotonic() - self.started_at) * 1000
        ttft_ms = None
        if self.first_content_at is not None:
            ttft_ms = round((self.first_content_at - self.started_at) * 1000, 3)
        return {
            "streaming": self.streaming,
            "duration_ms": round(duration_ms, 3),
            "ttft_ms": ttft_ms,
            "http_requests": self.http_requests,
        }


def publish_omni_telemetry(*, model: str, telemetry: dict[str, Any]) -> None:
    try:
        from miloco.observability.context import get_device_context
        from miloco.observability.metrics_client import get_metrics_client

        client = get_metrics_client()
        if client is None:
            return
        context = get_device_context()
        client.publish_event(
            event_type="omni_llm_telemetry",
            source=context.device_id if context is not None else "omni",
            payload={"model": model, **telemetry},
        )
    except Exception:
        _LOGGER.debug("Failed to publish Omni telemetry", exc_info=True)
