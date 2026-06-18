"""aiohttp web app：暴露 miloco webhook 契约 ``POST /miloco/webhook``。

复刻 OpenClaw 插件 ``plugins/openclaw/src/webhooks/index.ts`` 的路由与错误码：
- ``action == "agent"``     → 调 hermes_client.run_turn，返回 ``{code:0, data:{runId,status,...}}``
- ``action == "get_trace"`` → 同步 chat 已完成，observability 降级直接回 ``{code:0, data:{status:"done"}}``
- 未知 action               → ``{code:2001, message:"Action '<a>' not found"}``
- JSON 解析失败             → ``{code:1001, message:"bad json"}``
- 内部异常                  → ``{code:3000, message:str(e)}``
- Bearer 鉴权失败           → HTTP 401（非业务码，对齐 OpenClaw ``auth:"gateway"``）

幂等：payload.idempotencyKey 命中内存缓存(TTL 1h)时直接回缓存结果，避免 miloco
后端 HTTP 真断重试时在 Hermes 侧起第二个 turn（对齐 OpenClaw idempotencyKey 去重语义）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

from .hermes_client import HermesClient

logger = logging.getLogger(__name__)

# 幂等缓存 TTL：miloco 后端的重试窗口由批次调度决定，1h 足够覆盖单批重试；
# 进程内 dict + 时间戳即可，无需持久化（适配进程重启后 miloco 也会重发）。
_IDEM_TTL_S = 3600.0
_IDEM_MAX_ENTRIES = 10_000  # 防 OOM 上限，LRU 由 _purge 按 ts 淘汰


class _IdempotencyCache:
    """进程内幂等缓存：key=idempotencyKey → (result, ts)。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], float]] = {}

    def _purge(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._store.items() if now - ts > _IDEM_TTL_S]
        for k in expired:
            self._store.pop(k, None)
        # 超 cap 时按 ts 升序淘汰最旧
        if len(self._store) > _IDEM_MAX_ENTRIES:
            for k, _ in sorted(self._store.items(), key=lambda kv: kv[1][1])[
                : len(self._store) - _IDEM_MAX_ENTRIES
            ]:
                self._store.pop(k, None)

    def get(self, key: str) -> dict[str, Any] | None:
        self._purge()
        item = self._store.get(key)
        if item is None:
            return None
        result, ts = item
        if time.time() - ts > _IDEM_TTL_S:
            self._store.pop(key, None)
            return None
        return result

    def set(self, key: str, result: dict[str, Any]) -> None:
        self._purge()
        self._store[key] = (result, time.time())


def _ok(data: Any) -> dict[str, Any]:
    return {"code": 0, "message": "ok", "data": data}


def _fail(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def create_app(
    *,
    auth_bearer: str,
    hermes_client: HermesClient,
) -> web.Application:
    """构建 aiohttp app。

    ``auth_bearer``: miloco 调本适配器时用的 Bearer token；空串则跳过鉴权
    （对齐 miloco ``AgentSettings``：auth_bearer 可空）。
    """
    idem = _IdempotencyCache()

    async def _handle_webhook(request: "web.Request") -> "web.Response":
        # --- Bearer 鉴权 ---
        if auth_bearer:
            auth = request.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if token != auth_bearer:
                logger.warning("adapter webhook rejected: invalid bearer")
                return web.json_response(
                    {"code": 3000, "message": "unauthorized"}, status=401
                )

        # --- 解析 body ---
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_fail(1001, "bad json"), status=400)
        if not isinstance(body, dict):
            return web.json_response(_fail(1001, "bad json"), status=400)

        action = body.get("action")
        if not action or not isinstance(action, str):
            return web.json_response(_fail(1001, "missing action"), status=400)

        payload = body.get("payload") or {}

        # --- 幂等去重（仅 agent 动作，按 idempotencyKey 缓存结果）---
        idem_key: str | None = None
        if action == "agent" and isinstance(payload, dict):
            raw_key = payload.get("idempotencyKey")
            if isinstance(raw_key, str) and raw_key:
                idem_key = raw_key
                cached = idem.get(idem_key)
                if cached is not None:
                    logger.info("adapter webhook idem hit key=%s", idem_key)
                    return web.json_response(_ok(cached))

        # --- 路由 ---
        try:
            if action == "agent":
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                result = await hermes_client.run_turn(payload)
                if idem_key is not None:
                    idem.set(idem_key, result)
                return web.json_response(_ok(result))
            if action == "get_trace":
                # Hermes 同步 chat 无独立 run 概念，turn 已在 webhook 返回时完成。
                # observability 降级：直接告之 done，miloco 侧不阻塞。
                return web.json_response(_ok({"status": "done"}))
            return web.json_response(
                _fail(2001, f"Action '{action}' not found"), status=404
            )
        except Exception as e:
            logger.exception("adapter webhook action '%s' failed", action)
            return web.json_response(_fail(3000, str(e)), status=500)

    app = web.Application()
    app.router.add_post("/miloco/webhook", _handle_webhook)

    # 健康检查，便于 miloco 侧/部署探活
    async def _health(request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok"})

    app.router.add_get("/health", _health)
    return app
