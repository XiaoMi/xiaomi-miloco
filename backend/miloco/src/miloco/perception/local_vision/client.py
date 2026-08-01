"""local-vision 边车的 HTTP 客户端。

契约刻意与模型无关(送视频段 + 提问 + 规则 → 拿描述 + 逐条判定 + 门控概率),
任何满足它的服务都能替换参考实现,miloco 侧不需要改动。
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx

logger = logging.getLogger(__name__)


class LocalVisionError(RuntimeError):
    """边车不可达 / 返回异常。调用方按「本窗口感知失败」处理。"""


# /health 的响应会经 admin 端点回显给调用方,而 base_url 是用户可填的 —— 若原样
# 回显整个 body,这个端点就成了一个能读任意 URL 响应体的探针(SSRF 回显)。只放行
# 已知字段,把回显面钉死成固定形状。
_HEALTH_FIELDS = (
    "status", "model_loaded", "gate_available", "gate_error", "load_error",
    "device", "backend",
    # 边车侧用与推理同一套比较得出的鉴权结论。凭证不对时 /health 仍返 200,
    # 靠这两个字段区分「服务没起来」和「起来了但 token 不匹配」——后者若不看,
    # 探活会一路绿灯而每一窗推理 401,感知静默停摆。
    "auth_required", "auth_ok",
)
#: 这几个字段语义上是布尔,允许边车用 JSON int 表达。
_BOOLEAN_FIELDS = ("model_loaded", "gate_available", "auth_required", "auth_ok")
_HEALTH_ERROR_MAXLEN = 300
# 探活超时刻意远小于推理超时:它在 tick 自愈里被高频调用,长超时会卡住调用线程。
_HEALTH_TIMEOUT = 3.0
_HEALTH_CONNECT_TIMEOUT = 1.5


def _sanitize_health(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in _HEALTH_FIELDS:
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, int) and k in _BOOLEAN_FIELDS:
            # JSON 里 0/1 表示布尔是完全自然的写法,而契约对第三方实现开放。
            # 丢掉它会让 auth_ok=0 变成"字段缺失",而缺失被读作"不需要鉴权" ——
            # 一个 fail-open 的判定,恰恰是这套字段存在的意义的反面。
            out[k] = bool(v)
        elif isinstance(v, str):
            out[k] = v[:_HEALTH_ERROR_MAXLEN]
    return out


class LocalVisionClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # 连接池复用一份。每次推理新建再销毁的话,4 台相机 × 4 秒窗 = 每秒一次
        # TCP 握手,永远拿不到 keep-alive。惰性建:探活(health_sync)是同步的,
        # 构造期不该顺手创建一个属于别的事件循环的 async client。
        self._async_client: httpx.AsyncClient | None = None
        self._async_client_loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> httpx.AsyncClient:
        """取连接池,**按 event loop 缓存**。

        AsyncClient 绑定到创建时所在的 loop;那个 loop 关闭后继续用会抛
        "Event loop is closed"。而 InferenceWorker 每次 start() 都建一个新 loop —— 
        仓库为同一件事写过 _get_fused_http_client(omni/omni.py),这里照同一条来。
        此前只靠 close() 在换代之前把它置空,那是一条隔着三个模块的不变量,不是
        这个类自己的性质。
        """
        loop = asyncio.get_running_loop()
        if (
            self._async_client is None
            or self._async_client_loop is not loop
            or self._async_client.is_closed
        ):
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout,
                # 与 fused 通路同档:相机数上限 4,留一点余量给主动查询。
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
            self._async_client_loop = loop
        return self._async_client

    async def aclose(self) -> None:
        """引擎 close() 时释放连接池。"""
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.aclose()
        self._async_client = None
        self._async_client_loop = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health_sync(self) -> dict:
        """同步探活。引擎在 ``__init__``(同步上下文)里判就绪,不能 await;
        这里用同步客户端而不是临时起 event loop,免得和调用方的 loop 打架。

        **超时必须短**:本方法会被 tick 自愈每个感知周期(默认 4s)调一次,而
        同步 httpx 会占住调用线程。边车地址若被防火墙 DROP(或远端 GPU 机休眠),
        长超时会把主事件循环按秒级卡住 —— 相机取帧、SSE、HTTP API 全部跟着停。
        连接阶段给 1.5s 足够判定"起没起",整体也只给 ``_HEALTH_TIMEOUT``。
        """
        timeout = httpx.Timeout(_HEALTH_TIMEOUT, connect=_HEALTH_CONNECT_TIMEOUT)
        try:
            with httpx.Client(timeout=timeout) as c:
                # 必须带上凭证:边车会用它回 auth_ok,调用方据此在**切换那一刻**
                # 就发现 token 配错,而不是等每一窗推理 401。不带的话,任何配了
                # 凭证的部署都会被判成"凭证不被接受"。
                r = c.get(f"{self.base_url}/health", headers=self._headers())
                r.raise_for_status()
                return _sanitize_health(r.json())
        except Exception as e:  # noqa: BLE001
            raise LocalVisionError(f"health check failed: {e}") from e

    async def perceive(
        self,
        video: bytes,
        rules: list[dict],
        scene_ask: str | None = None,
        camera_note: str = "",
        max_new_tokens: int = 256,
        want_gate: bool = True,
        ngram_guard: int | None = None,
    ) -> dict:
        payload = {
            "video_b64": base64.b64encode(video).decode("ascii"),
            "rules": rules,
            "max_new_tokens": max_new_tokens,
            "want_gate": want_gate,
        }
        if ngram_guard is not None:
            payload["ngram_guard"] = ngram_guard
        if scene_ask:
            payload["scene_ask"] = scene_ask
        if camera_note:
            # 独立字段传:塞进 scene_ask 会顶掉任务提问本身,且会落在格式约定之前。
            payload["camera_note"] = camera_note
        try:
            r = await self._client().post(
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
