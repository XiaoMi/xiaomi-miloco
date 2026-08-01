"""local-vision 边车的 HTTP 客户端。

契约刻意与模型无关(送视频段 + 提问 + 规则 → 拿描述 + 逐条判定 + 门控概率),
任何满足它的服务都能替换参考实现,miloco 侧不需要改动。
"""

from __future__ import annotations

import base64
import logging

import httpx

logger = logging.getLogger(__name__)


class LocalVisionError(RuntimeError):
    """边车不可达 / 返回异常。调用方按「本窗口感知失败」处理。"""


class LocalVisionClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health_sync(self) -> dict:
        """同步探活。引擎在 ``__init__``(同步上下文)里判就绪,不能 await;
        这里用同步客户端而不是临时起 event loop,免得和调用方的 loop 打架。"""
        try:
            with httpx.Client(timeout=min(self.timeout, 10.0)) as c:
                r = c.get(f"{self.base_url}/health")
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            raise LocalVisionError(f"health check failed: {e}") from e

    async def health(self) -> dict:
        """探活。供「测试连接」与引擎就绪判定使用;不需要 token。"""
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, 10.0)) as c:
                r = await c.get(f"{self.base_url}/health")
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            raise LocalVisionError(f"health check failed: {e}") from e

    async def perceive(
        self,
        video: bytes,
        rules: list[dict],
        scene_ask: str | None = None,
        max_new_tokens: int = 256,
        want_gate: bool = True,
    ) -> dict:
        payload = {
            "video_b64": base64.b64encode(video).decode("ascii"),
            "rules": rules,
            "max_new_tokens": max_new_tokens,
            "want_gate": want_gate,
        }
        if scene_ask:
            payload["scene_ask"] = scene_ask
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(
                    f"{self.base_url}/v1/perceive", json=payload, headers=self._headers()
                )
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", "")
            except Exception:  # noqa: BLE001
                detail = e.response.text[:200]
            raise LocalVisionError(
                f"sidecar returned {e.response.status_code}: {detail}"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise LocalVisionError(f"sidecar request failed: {e}") from e
