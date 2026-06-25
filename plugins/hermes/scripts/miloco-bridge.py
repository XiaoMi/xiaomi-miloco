#!/usr/bin/env python3
"""
Miloco → Hermes 桥接 HTTP 服务器

替代原 OpenClaw 插件的 webhook 端点。接收 miloco-backend 的 HTTP 回调，
通过 Hermes 的消息平台（微信/QQ）将 agent 任务注入，并管理 trace 元数据。

启动方式:
    python3 miloco-bridge.py --port 1811 --config /path/to/config.json

API:
    POST /miloco/webhook
    {
        "action": "agent",
        "payload": {
            "message": "感知引擎推送消息...",
            "sessionKey": "main",
            "idempotencyKey": "uuid",
            "traceId": "backend-trace-123",
            "timeoutMs": 180000,
            "extraSystemPrompt": "额外系统提示词"
        }
    }

    POST /miloco/webhook
    {
        "action": "get_trace",
        "payload": {"runId": "..."}
    }
"""

import json
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional


# ─── 配置 ──────────────────────────────────────────────────────────────────

def expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def resolve_miloco_home(config: dict | None = None) -> str:
    """Resolve the Miloco shared home path.

    Keep this aligned with backend / CLI / OpenClaw defaults. Hermes runtime
    files live under ``~/.hermes``; Miloco config and logs stay in
    ``$MILOCO_HOME`` so all frontends point at the same backend instance.
    """
    if os.environ.get("MILOCO_HOME"):
        return expand_path(os.environ["MILOCO_HOME"])
    configured = (config or {}).get("hermes", {}).get("miloco_home")
    if isinstance(configured, str) and configured:
        return expand_path(configured)
    return expand_path("~/.openclaw/miloco")


MILOCO_HOME = resolve_miloco_home()
DEFAULT_PORT = 1811
DEFAULT_CONFIG = os.path.join(MILOCO_HOME, "config.json")
DEFAULT_HERMES_BASE = "http://127.0.0.1:18789"  # Hermes gateway
DEFAULT_HERMES_HOME = expand_path(os.environ.get("HERMES_HOME", "~/.hermes"))
DEFAULT_INCOMING_DIR = os.path.join(DEFAULT_HERMES_HOME, "messages", "incoming")


def resolve_bridge_auth_token(config: dict) -> str:
    """Token expected on backend → bridge webhook calls.

    Backend sends ``settings.agent.auth_bearer`` to the agent webhook. The
    previous Hermes draft checked ``server.token`` instead, which is the
    frontend/backend API token and does not match webhook auth.
    """
    agent_token = config.get("agent", {}).get("auth_bearer")
    if isinstance(agent_token, str) and agent_token:
        return agent_token
    hermes_token = config.get("hermes", {}).get("auth_bearer")
    if isinstance(hermes_token, str) and hermes_token:
        return hermes_token
    legacy = config.get("server", {}).get("token")
    return legacy if isinstance(legacy, str) else ""


# ─── Trace 管理 ────────────────────────────────────────────────────────────

class TraceStore:
    """内存中的 agent turn 元数据存储，供 backend 反向查询。"""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._turns: dict = {}       # runId → TurnMeta
        self._links: dict = {}       # runId → traceId
        # 自动过期
        self._done_ttl = 120  # 秒
        self._stuck_ttl = 900  # 秒
        self._last_gc = time.time()
    
    def link(self, run_id: str, trace_id: str):
        with self._lock:
            self._links[run_id] = trace_id
            if run_id not in self._turns:
                self._turns[run_id] = {"started_at": time.time(), "done": None}
    
    def start_turn(self, run_id: str, query: str = "", trace_id: str = ""):
        with self._lock:
            self._turns[run_id] = {
                "started_at": time.time(),
                "query": query,
                "trace_id": trace_id,
                "done": None,
            }
    
    def finish_turn(self, run_id: str, success: bool, error: str = "", duration_ms: float = 0):
        with self._lock:
            if run_id in self._turns:
                t = self._turns[run_id]
                t["done"] = {
                    "runId": run_id,
                    "success": success,
                    "errorCount": 0 if success else 1,
                    "errorMsg": error or None,
                    "durationMs": duration_ms,
                    "trace_id": t.get("trace_id", ""),
                    "query": t.get("query", ""),
                    "llmCallCount": 0,
                    "toolCallCount": 0,
                    "llmTotalMs": 0.0,
                    "toolTotalMs": 0.0,
                    "toolMaxMs": 0.0,
                    "slowestToolName": None,
                    "jsonlPath": None,
                    "finished_at": time.time(),
                }
        self._gc()
    
    def get_status(self, run_id: str) -> str:
        with self._lock:
            t = self._turns.get(run_id)
            if not t:
                return "unknown"
            return "done" if t.get("done") else "in_progress"
    
    def pop_done(self, run_id: str) -> Optional[dict]:
        with self._lock:
            t = self._turns.get(run_id)
            if not t or not t.get("done"):
                return None
            meta = t.pop("done")
            if not t.get("done"):
                del self._turns[run_id]
            return meta
    
    def _gc(self):
        now = time.time()
        if now - self._last_gc < 60:
            return
        self._last_gc = now
        with self._lock:
            to_delete = []
            for rid, t in self._turns.items():
                if t.get("done") and t.get("done", {}).get("finished_at", 0) < now - self._done_ttl:
                    to_delete.append(rid)
                elif not t.get("done") and t.get("started_at", 0) < now - self._stuck_ttl:
                    to_delete.append(rid)
            for rid in to_delete:
                del self._turns[rid]
                self._links.pop(rid, None)


trace_store = TraceStore()


# ─── 消息注入 ──────────────────────────────────────────────────────────────

class MessageInjector:
    """Inject Miloco events into Hermes.

    The least-coupled path is an incoming message spool directory. Deployments
    can move it with ``hermes.incoming_dir`` or ``HERMES_INCOMING_DIR`` without
    changing Miloco backend settings.
    """

    def __init__(
        self,
        base_url: str,
        incoming_dir: str = DEFAULT_INCOMING_DIR,
        platform: str = "weixin",
        user_id: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.incoming_dir = expand_path(incoming_dir)
        self.platform = platform
        self.user_id = user_id

    def inject(self, message: str, system_prompt: str = "") -> str:
        """Write one incoming message file for Hermes to consume."""
        os.makedirs(self.incoming_dir, exist_ok=True)
        
        msg_id = str(uuid.uuid4())
        msg_file = os.path.join(self.incoming_dir, f"miloco-bridge-{msg_id}.json")
        
        payload = {
            "id": msg_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "source": "miloco-bridge",
                "system_prompt_inject": system_prompt or "",
            }
        }
        
        with open(msg_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        
        return msg_id

    def inject_via_cron(self, message: str, system_prompt: str = "") -> Optional[str]:
        """Backward-compatible alias for the first Hermes draft."""
        return self.inject(message, system_prompt)


# ─── HTTP Handler ──────────────────────────────────────────────────────────

class MilocoBridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    injector: Optional[MessageInjector] = None
    auth_token: str = ""
    hermes_base: str = ""

    def log_message(self, format, *args):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] miloco-bridge: {format % args}\n")

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _check_auth(self) -> bool:
        if not self.auth_token:
            return True  # 无 token 配置则放行
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        return auth == expected

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path != "/miloco/webhook":
            self._send_json(404, {"code": 404, "message": "not found"})
            return

        if not self._check_auth():
            self._send_json(401, {"code": 401, "message": "unauthorized"})
            return

        # 读取请求体
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"code": 1001, "message": "Invalid JSON body"})
            return

        action = data.get("action", "")
        payload = data.get("payload", {})

        if action == "agent":
            self._handle_agent(payload)
        elif action == "get_trace":
            self._handle_get_trace(payload)
        else:
            self._send_json(404, {"code": 2001, "message": f"Action '{action}' not found"})

    def _handle_agent(self, payload: dict):
        message = payload.get("message", "")
        extra_system = payload.get("extraSystemPrompt", "")
        trace_id = payload.get("traceId", "")
        if not message:
            self._send_json(400, {"code": 400, "message": "message required"})
            return

        run_id = str(uuid.uuid4())
        trace_store.start_turn(run_id, query=message[:100], trace_id=trace_id)

        # 注入消息到 Hermes（通过 WeChat 频道）
        try:
            if self.injector:
                msg_id = self.injector.inject(message, extra_system)
                trace_store.finish_turn(run_id, success=True, duration_ms=0)
                self._send_json(200, {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "runId": run_id,
                        "status": "ok",
                        "msgId": msg_id,
                    }
                })
            else:
                self._send_json(500, {"code": 3000, "message": "injector not configured"})
        except Exception as e:
            trace_store.finish_turn(run_id, success=False, error=str(e))
            self._send_json(500, {"code": 3000, "message": str(e)})

    def _handle_get_trace(self, payload: dict):
        run_id = payload.get("runId", "")
        if not run_id:
            self._send_json(400, {"code": 400, "message": "runId required"})
            return

        status = trace_store.get_status(run_id)
        if status == "done":
            meta = trace_store.pop_done(run_id)
            self._send_json(200, {"code": 0, "message": "ok", "data": {"status": "done", **meta}} if meta else {"code": 0, "message": "ok", "data": {"status": "unknown"}})
        else:
            self._send_json(200, {"code": 0, "message": "ok", "data": {"status": status}})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "uptime": time.time() - server_start_time if 'server_start_time' in globals() else 0})
        else:
            self._send_json(404, {"code": 404, "message": "not found"})


# ─── 服务器 ────────────────────────────────────────────────────────────────

server_start_time = 0


def run_server(port: int, config: dict, injector: MessageInjector):
    global server_start_time
    server_start_time = time.time()

    MilocoBridgeHandler.injector = injector
    MilocoBridgeHandler.auth_token = resolve_bridge_auth_token(config)
    MilocoBridgeHandler.hermes_base = config.get("hermes", {}).get("gateway_url", "http://127.0.0.1:18789")

    server = HTTPServer(("0.0.0.0", port), MilocoBridgeHandler)
    server.timeout = 30  # 30s keepalive

    print(f"[miloco-bridge] Starting on 0.0.0.0:{port}", file=sys.stderr)
    print(f"[miloco-bridge] Hermes gateway: {MilocoBridgeHandler.hermes_base}", file=sys.stderr)
    print(f"[miloco-bridge] Auth: {'enabled' if MilocoBridgeHandler.auth_token else 'disabled'}", file=sys.stderr)

    def shutdown(sig, frame):
        print(f"\n[miloco-bridge] Shutting down...", file=sys.stderr)
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Miloco → Hermes bridge server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG, help=f"Config file path")
    parser.add_argument("--weixin-user", type=str, default="", help="Weixin user ID for message delivery")
    args = parser.parse_args()

    # 加载配置
    config = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            config = json.load(f)

    # Weixin user ID: 命令行 > 配置 > 环境变量
    weixin_user = args.weixin_user or config.get("notify", {}).get("weixin_user_id", "") or os.environ.get("WEIXIN_USER_ID", "")
    platform = config.get("notify", {}).get("default_channel", "weixin")
    incoming_dir = (
        os.environ.get("HERMES_INCOMING_DIR")
        or config.get("hermes", {}).get("incoming_dir")
        or DEFAULT_INCOMING_DIR
    )
    hermes_base = config.get("hermes", {}).get("gateway_url", DEFAULT_HERMES_BASE)
    injector = MessageInjector(
        base_url=hermes_base,
        incoming_dir=incoming_dir,
        platform=platform,
        user_id=weixin_user,
    )

    run_server(args.port, config, injector)


if __name__ == "__main__":
    main()
