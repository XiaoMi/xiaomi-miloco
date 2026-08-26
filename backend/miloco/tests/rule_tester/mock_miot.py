"""Mock MiotProxy for the rule tester.

The real MiotProxy talks to Xiaomi MIoT cloud and requires login. The tester
needs the property / action / scene entry points the rule runner dispatches to
(``prop.*`` / ``action.*`` / ``iid=scene``); we stub them out and record each
call so the UI can show what the rule "would have" done.

This is a development tool and lives outside the production code path.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MockMiotCall:
    """One recorded MIoT call."""

    method: str          # set_device_properties / call_device_action /
                         # get_device_properties / execute_miot_scene
    payload: Any         # the params object(s) the runner sent
    result: Any          # what the mock returned
    ts_ms: int


class MockMiotProxy:
    """Stand-in for ``miloco.miot.client.MiotProxy``.

    All write operations succeed (``code == 0``); reads return ``None`` for
    ``value`` so the runner's device-dispatch handlers see "value differs, dispatch".
    Each call is appended to :attr:`history` (capped) for the UI.
    """

    HISTORY_CAP = 200

    # 场景动作（iid=scene）执行前要过家庭白名单，白名单读的是 KV。
    HOME_ID = "mock-home"

    def __init__(self) -> None:
        self.history: deque[MockMiotCall] = deque(maxlen=self.HISTORY_CAP)
        # rule runner 的场景分支经 miot.service._trigger_scene 走白名单校验，
        # 那里直接读 miot_proxy._kv_repo，所以这里给一个内存版。
        self._kv_repo = _MockKVRepo({"HOME_WHITE_LIST_KEY": f'["{self.HOME_ID}"]'})
        self.scenes: dict[str, Any] = {}

    def register_scene(self, scene_id: str, scene_name: str) -> None:
        """登记一个可触发的场景；未登记的 scene_id 会按「场景不存在」失败。"""
        self.scenes[scene_id] = SimpleNamespace(
            home_id=self.HOME_ID, scene_name=scene_name
        )

    # ---- methods used by miot.set_property / miot.call_action handlers ----

    async def set_device_properties(self, params: list) -> list:
        results = [{"code": 0, "did": p.did, "siid": p.siid, "piid": p.piid} for p in params]
        self._record("set_device_properties", [_dump(p) for p in params], results)
        return results

    async def call_device_action(self, param) -> dict:
        result = {"code": 0, "did": param.did, "siid": param.siid, "aiid": param.aiid}
        self._record("call_device_action", _dump(param), result)
        return result

    async def get_device_properties(self, params: list) -> list:
        # Return code=0 with value=None so idempotent paths (if any) treat the
        # current state as "unset" and proceed to dispatch.
        results = [
            {"code": 0, "did": p.did, "siid": p.siid, "piid": p.piid, "value": None}
            for p in params
        ]
        self._record("get_device_properties", [_dump(p) for p in params], results)
        return results

    # ---- scene（iid=scene 走 miot.service._trigger_scene）----

    async def get_all_scenes(self) -> dict:
        return self.scenes

    async def execute_miot_scene(self, scene_id: str) -> bool:
        self._record("execute_miot_scene", {"scene_id": scene_id}, True)
        return True

    # ---- introspection ----

    def recent(self, n: int = 50) -> list[dict]:
        items = list(self.history)[-n:]
        return [asdict(c) for c in items]

    def clear(self) -> None:
        self.history.clear()

    # ---- internal ----

    def _record(self, method: str, payload: Any, result: Any) -> None:
        import time

        call = MockMiotCall(
            method=method,
            payload=payload,
            result=result,
            ts_ms=int(time.time() * 1000),
        )
        self.history.append(call)
        logger.info("MockMiotProxy.%s payload=%s -> %s", method, payload, result)


def _dump(obj: Any) -> Any:
    """Best-effort serialize a pydantic / dataclass / dict to plain dict."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return obj


class _MockKVRepo:
    """`allowed_home_ids` 只用到 ``get``；写入路径 tester 不涉及。"""

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True
