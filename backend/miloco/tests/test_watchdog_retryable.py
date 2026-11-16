"""Exercise the shipped watchdog without starting the application manager."""
import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


def run_watchdog(nat_blocked=False, frames=(False, False)):
    source = Path(__file__).parents[1] / "src/miloco/miot/router.py"
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body
             if isinstance(node, ast.AsyncFunctionDef)
             and node.name == "_first_frame_watchdog"]
    frame_values = iter(frames)
    namespace = {
        "WebSocket": object, "asyncio": SimpleNamespace(sleep=AsyncMock()),
        "json": json, "logger": logging.getLogger(__name__),
        "safe_log": str, "_cam_tag": lambda cam, ch: f"{cam}.{ch}",
        "_truncate_ws_reason": str, "_nat_blocked": lambda _: nat_blocked,
        "_FIRST_FRAME_TIMEOUT_S": 12, "_GRACE_EXTENSION_S": 60,
        "UNREACHABLE_MESSAGES": {
            "camera_unreachable": "No camera frames yet",
            "camera_unreachable_cross_subnet": "NAT blocked",
        },
        "miot_video_stream_manager": SimpleNamespace(
            has_emitted_frame=lambda *_: next(frame_values)
        ),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    ws = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    asyncio.run(namespace["_first_frame_watchdog"](ws, "cam", 0))
    return ws


def test_unproven_no_frame_timeout_is_retryable():
    ws = run_watchdog()
    payload = json.loads(ws.send_text.await_args.args[0])
    assert payload["retryable"] is True
    assert payload["reason"] == "camera_unreachable"


def test_conclusive_nat_block_is_not_retried():
    ws = run_watchdog(nat_blocked=True)
    payload = json.loads(ws.send_text.await_args.args[0])
    assert payload["retryable"] is False


def test_frame_during_grace_avoids_closing_recovered_stream():
    ws = run_watchdog(frames=(False, True))
    ws.send_text.assert_not_awaited()
    ws.close.assert_not_awaited()
