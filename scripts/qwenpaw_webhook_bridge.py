#!/usr/bin/env python3
"""Miloco → QwenPaw Webhook Bridge.

Replaces the OpenClaw plugin's ``/miloco/webhook`` HTTP route.  The Miloco
Python backend POSTs to ``http://127.0.0.1:18789/miloco/webhook``, and this
bridge translates those calls into QwenPaw inter-agent chat via
``qwenpaw agents chat``.

Actions
-------
agent
    Forward ``message`` to the ``miloco`` agent, wait for the response, cache
    trace metadata, and return ``{runId, status, error?, recovered?}`` in the
    ``data`` field — matching the OpenClaw ``waitForRun`` payload.

get_trace
    Look up a previously cached agent turn by ``runId``.  Returns the cached
    trace dict or ``data: null`` if not found.

Environment
-----------
``QWENPAW_BIN``      — path to the ``qwenpaw`` CLI (default: from venv).
``QWENPAW_BASE_URL`` — host:port of the QwenPaw API (default: localhost:8088).
``AGENT_TIMEOUT``    — max seconds to wait for ``agents chat`` (default: 300).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional

QWENPAW_BIN = os.environ.get("QWENPAW_BIN", "/app/venv/bin/qwenpaw")
QWENPAW_BASE_URL = os.environ.get("QWENPAW_BASE_URL", "http://localhost:8088")
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "300"))

FROM_AGENT = "miloco-bridge"
TO_AGENT = "miloco"

TRACE_CACHE: dict[str, dict[str, Any]] = {}

LOG_DIR = Path.home() / ".openclaw" / "miloco" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_DIR / "qwenpaw_bridge.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _call_agent(
    text: str,
    *,
    session_key: str = "main",
    lane: str = "",
    trace_id: str = "",
    timeout_ms: int = 180_000,
) -> dict[str, Any]:
    """Call ``qwenpaw agents chat`` and return a dict like OpenClaw's waitForRun.

    Returns ``{runId, status, error?, recovered?}``.  The ``data`` field
    (agent response text) is stored in the trace cache and returned via
    the ``get_trace`` action.
    """
    run_id = str(uuid.uuid4())
    started = _now_ms()

    try:
        result = subprocess.run(
            [
                QWENPAW_BIN, "agents", "chat",
                "--from-agent", FROM_AGENT,
                "--to-agent", TO_AGENT,
                "--text", text,
                "--base-url", QWENPAW_BASE_URL,
                "--timeout", str(max(10, timeout_ms // 1000)),
            ],
            capture_output=True,
            text=True,
            timeout=max(30, timeout_ms // 1000 + 15),
            env={**os.environ},
            cwd=os.environ.get("QWENPAW_WORKING_DIR", "/app/working"),
        )

        elapsed_ms = _now_ms() - started
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0 and stdout:
            # Parse "[SESSION: ...]\n\ncontent" format
            if stdout.startswith("[SESSION:"):
                _, _, content = stdout.partition("\n")
                content = content.strip()
            else:
                content = stdout

            # Cache trace for get_trace polling
            TRACE_CACHE[run_id] = {
                "runId": run_id,
                "status": "completed",
                "success": True,
                "data": content,
                "elapsedMs": elapsed_ms,
            }
            _log(f"agent ok runId={run_id} elapsed={elapsed_ms}ms")
            return {"runId": run_id, "status": "ok"}

        if result.returncode != 0:
            error_msg = stderr or "agent call failed"
            TRACE_CACHE[run_id] = {
                "runId": run_id,
                "status": "failed",
                "success": False,
                "errorMsg": error_msg,
                "elapsedMs": elapsed_ms,
            }
            _log(f"agent error runId={run_id}: {error_msg}")
            return {"runId": run_id, "status": "error", "error": error_msg}

        # Empty response
        TRACE_CACHE[run_id] = {
            "runId": run_id,
            "status": "completed",
            "success": True,
            "data": "",
            "elapsedMs": elapsed_ms,
        }
        return {"runId": run_id, "status": "ok"}

    except subprocess.TimeoutExpired:
        TRACE_CACHE[run_id] = {
            "runId": run_id,
            "status": "timeout",
            "success": False,
            "errorMsg": f"agent call timed out after {timeout_ms}ms",
        }
        _log(f"agent timeout runId={run_id}")
        return {"runId": run_id, "status": "timeout", "error": "agent call timed out"}
    except FileNotFoundError:
        return {"runId": run_id, "status": "error", "error": f"qwenpaw binary not found: {QWENPAW_BIN}"}
    except Exception as e:
        return {"runId": run_id, "status": "error", "error": str(e)}


def _get_trace(run_id: str) -> Optional[dict[str, Any]]:
    """Look up a previously cached agent turn by runId."""
    return TRACE_CACHE.get(run_id)


def _send_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    if handler.wfile.closed:
        return
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):

    def _handle_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message", "")
        if not message:
            return {"code": 1002, "message": "Missing message in payload"}

        session_key = payload.get("sessionKey", "main")
        lane = payload.get("lane", "")
        trace_id = payload.get("traceId", str(uuid.uuid4()))
        timeout_ms = payload.get("timeoutMs", 180_000)

        result = _call_agent(
            message,
            session_key=session_key,
            lane=lane,
            trace_id=trace_id,
            timeout_ms=timeout_ms,
        )

        if result.get("status") in ("ok", "error", "timeout"):
            return {"code": 0, "data": result}
        return {"code": 2001, "message": result.get("error", "unknown error")}

    def _handle_get_trace(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload.get("runId", "")
        if not run_id:
            return {"code": 1002, "message": "Missing runId in payload"}
        data = _get_trace(run_id)
        return {"code": 0, "data": data}

    def do_POST(self) -> None:
        if self.path != "/miloco/webhook":
            _send_json(self, 404, {"code": 404, "message": "Not Found"})
            return

        cl = int(self.headers.get("Content-Length", 0))
        if cl == 0:
            _send_json(self, 400, {"code": 1001, "message": "Empty body"})
            return

        try:
            body = json.loads(self.rfile.read(cl))
        except json.JSONDecodeError:
            _send_json(self, 400, {"code": 1001, "message": "Invalid JSON"})
            return

        if "action" not in body:
            _send_json(self, 400, {"code": 1001, "message": "Missing action field"})
            return

        action = body["action"]
        payload = body.get("payload", {})

        if action == "agent":
            response = self._handle_agent(payload)
        elif action == "get_trace":
            response = self._handle_get_trace(payload)
        else:
            response = {"code": 2001, "message": f"Unknown action: {action}"}

        code = response.get("code", 500)
        _send_json(self, 200 if code == 0 else (400 if code < 2000 else 500), response)

    def log_message(self, fmt: str, *args: Any) -> None:
        _log(fmt % args)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    host = "127.0.0.1"
    port = 18789

    print(f"[Miloco Bridge] Starting on {host}:{port}")
    print(f"[Miloco Bridge] QwenPaw: {QWENPAW_BASE_URL}")
    print(f"[Miloco Bridge] Agents: {FROM_AGENT} → {TO_AGENT}")
    sys.stdout.flush()

    server = HTTPServer((host, port), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Miloco Bridge] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
