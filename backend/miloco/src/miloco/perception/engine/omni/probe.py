"""omni provider 连通性探测。

统一被 web preflight (admin/router.py) 与运行时 circuit_breaker HALF_OPEN 复用。
两阶段探测：GET /models 验鉴权+可达；再 max_tokens=1 chat 真校验模型。

返回统一形状 {ok, code, status?, latency_ms?, message}。code 集合与 spec §2 一致。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_ALLOWED_SCHEMES = ("http", "https")


def _normalize_base_url(base_url: str) -> tuple[str | None, str | None]:
    """校验 base_url 并归一化(去尾斜杠)。

    只挡非 http/https scheme(拒 file/gopher/ftp/data 等)。**不挡内网/链路本地 IP**——
    家用场景的自建 LLM (Ollama http://127.0.0.1:11434 / vLLM http://192.168.x.x:8000
    / Tailscale http://100.64.x.x) 就是常见 base_url,禁内网 = 禁自建。

    防"key 通过 base_url 外泄"靠的是 admin/router.py::_key_by_label 的跨 URL 凭证隔离
    (base_url 变了不沿用旧 key),不靠这里的 IP 黑名单。docstring 明说这点避免后续读者
    误以为这层做了 SSRF 防护。

    返回 (normalized, error_message);合法时 error 为 None。
    """
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            None,
            f"Base URL 协议非法（仅支持 http/https，实际: {scheme or 'empty'}）",
        )
    if not parsed.netloc:
        return None, "Base URL 缺少主机名"
    return base_url.rstrip("/"), None


async def probe_reachable(base_url: str) -> dict | None:
    """无 key 时判 Base URL 是否明显有问题;使 URL 错优先于「缺 key」暴露。

    - scheme 非法 / 网络错 → {code: unreachable, ...}
    - 2xx/3xx 或 401/403 → None(URL 没问题,问题在缺 key)
    - 其他 4xx/5xx → {code: http_error, ...}
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"code": "unreachable", "message": err}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models")
    except Exception as e:  # noqa: BLE001
        return {
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code < 400 or r.status_code in (401, 403):
        return None
    return {"code": "http_error", "message": f"服务返回异常（HTTP {r.status_code}）"}


async def fetch_models(base_url: str, api_key: str) -> dict[str, Any]:
    """拉取 provider 模型列表(GET /models)。

    模型下拉在「选定 model 之前」拉取,没有 model 可路由 adapter,故按 base_url 判 provider:
    Gemini 原生根(generativelanguage)用 ``x-goog-api-key`` 鉴权、响应形态 ``{models:[{name}]}``
    (需剥 "models/" 前缀);其余按 OpenAI 兼容 ``{data:[{id}]}`` + ``Bearer`` 解析。
    (经代理转发的 Gemini 不含该域名时,仍走 OpenAI 兼容分支——用户可手填 model 名兜底。)
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "models": [], "message": err}
    # 解析出主机名精确匹配,不用子串判断(``"…" in base_url`` 会被
    # ``https://evil.com/generativelanguage.googleapis.com`` 之类绕过——CodeQL 报的
    # incomplete URL substring sanitization)。
    is_gemini = (
        (urlparse(base).hostname or "").lower() == "generativelanguage.googleapis.com"
    )
    headers = (
        {"x-goog-api-key": api_key}
        if is_gemini
        else {"Authorization": f"Bearer {api_key}"}
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models", headers=headers)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "models": [],
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code == 200:
        try:
            if is_gemini:
                ids = [
                    (m.get("name") or "").removeprefix("models/")
                    for m in (r.json().get("models") or [])
                    if m.get("name")
                ]
            else:
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:  # noqa: BLE001
            ids = []
        return {"ok": True, "models": sorted(i for i in ids if i)}
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "models": [],
            "message": "API Key 无效或无权限",
        }
    return {
        "ok": False,
        "code": "http_error",
        "models": [],
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


class _FakeStatusResp:
    """占位:probe_chat 流式路径下,非 200 场景把 status_code 塞进"看起来像 httpx.Response"
    的最小对象里,复用下方 status_code 分支代码。仅用 status_code / json / text / headers
    四个属性。

    headers 包成 httpx.Headers 而非 plain dict:非流式路径的真实 Response.headers 是
    httpx.Headers(大小写不敏感);_probe_stream_chat 传上来的 dict(resp.headers) 已经把
    header 名小写化了(httpx 语义),若这里存成 plain dict,下方 429 分支的
    r.headers.get("Retry-After")(大写 R/A)会大小写敏感 miss → 恒 None,与非流式路径的
    大小写不敏感 hit 行为不一致,Qwen 撞 429 时会丢掉 server 明示的 Retry-After。
    """

    def __init__(self, status_code: int, json_body: dict, text: str, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = httpx.Headers(headers or {})

    def json(self) -> Any:
        return self._json


async def _probe_stream_chat(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    t0: float,
) -> tuple[int, int, bool, dict[str, str]]:
    """流式探测:开 SSE stream,读第一条 data 行就视为可达。返回
    (status_code, latency_ms, ok, resp_headers)。非 200 status 回带原始 response
    headers,让上层 429 分支能读 Retry-After —— 否则 forced-stream provider (Qwen)
    撞 429 时熔断退避会丢掉 server 明示的等待时长,与非流式路径 (MiMo) 行为不一致。"""
    async with client.stream(
        "POST", url, headers=headers, json=body,
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()  # 允许连接释放
            return resp.status_code, 0, False, dict(resp.headers)
        # 读到任一 data 行即算可达 (200 已经通过);不等 [DONE] 避免 max_tokens=1 下
        # provider 拖延 keep-alive 直到 _TIMEOUT。
        async for line in resp.aiter_lines():
            line = line.strip()
            if line.startswith("data: "):
                latency_ms = round((time.monotonic() - t0) * 1000)
                return 200, latency_ms, True, {}
        # 流开完无 data 行 → 视为 http_error(RECOVERABLE),返 500 让上层 http_error
        # 兜底分支处理(与 bad_response 都归 RECOVERABLE、cap 同为 _default 600s,
        # 运行时行为无差;此处选 http_error 是让 code 与状态码语义一致 —— 无 payload
        # 更像上游异常而非结构错)。
        return 500, 0, False, {}


async def probe_chat(model: str, base_url: str, api_key: str) -> dict[str, Any]:
    """极简 chat 探测(max_tokens=1)真校验模型是否可用。

    走 provider adapter 生成 body,兼容不同 provider 的强制要求(Qwen 强制
    stream=True + modalities=["text"])。之前硬编码非流式 body 打 Qwen 会被
    400/422 判成 rejected_authed,合法配置反而进 OPEN_CONFIG。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    # 延迟 import 避免 probe 被 wire 时循环拉起 provider (provider 只依赖标准库,
    # 但保险起见延后到函数内)。
    from miloco.perception.engine.omni.provider import get_adapter

    adapter = get_adapter(model)
    body = adapter.build_request_body(
        [{"role": "user", "content": "ping"}],
        model=model,
        max_tokens=1,
        temperature=0.0,
        top_p=1.0,
        stream=False,  # 请求非流式;adapter 若强制 stream=True (Qwen) 会覆盖
    )
    forced_stream = body.get("stream", False)
    url = adapter.endpoint(base, model, stream=forced_stream)
    # adapter.auth_headers 走 provider 特化 —— Gemini 用 ``x-goog-api-key`` 头,
    # OpenAI 兼容族用 ``Authorization: Bearer``。硬编码 Bearer 会对合法 Gemini
    # 配置误报失败(401)。
    headers = {
        **adapter.auth_headers(api_key),
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if forced_stream:
                # forced-stream provider (Qwen):走 SSE 流,读第一条有效 data chunk 就
                # 视为可达(不等 [DONE],避免 max_tokens=1 下 provider 拖延 keep-alive
                # 撑到超时)。任何一条 status_code / auth / model 错都会在开 stream 时
                # 直接抛,与非流式行为对齐。
                status_code, latency_ms, ok, resp_headers = await _probe_stream_chat(
                    client, url, headers, body, t0
                )
                if ok:
                    return {
                        "ok": True,
                        "code": "ok",
                        "status": status_code,
                        "latency_ms": latency_ms,
                        "message": "连接正常",
                    }
                # 非 200: 复用下方 status_code 分支;把 headers 一起塞进 _FakeStatusResp,
                # 429 分支能读 Retry-After,行为与非流式路径对齐。
                r = _FakeStatusResp(status_code, {}, "", resp_headers)
            else:
                r = await client.post(  # type: ignore[assignment]
                    url,
                    headers=headers,
                    json=body,
                )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    latency_ms = round((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        # status 200 不代表 body 合法:mock/中间层可能返 200 + 非 JSON。运行时 omni_client
        # 走 json.loads + 非 dict → bad_response,probe 需对齐,否则 mock/异常网关下 probe
        # 误判 ok,熔断状态被 record_probe_result(True) 复位 CLOSED,与真实调用行为背离。
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001 — 任何解码错都归 bad_response
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "omni 响应格式异常",
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "omni 响应格式异常",
            }
        return {
            "ok": True,
            "code": "ok",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "连接正常",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code == 404:
        return {
            "ok": False,
            "code": "not_found",
            "status": 404,
            "message": "模型或地址不存在",
        }
    if r.status_code in (400, 422):
        return {
            "ok": False,
            "code": "rejected_authed",
            "status": r.status_code,
            "latency_ms": latency_ms,
            "message": "已连接，但拒绝了模型请求（模型名可能错误）",
        }
    if r.status_code == 429:
        # 429 不加分支时会掉到 http_error 兜底,后果:上层用 http_error 走 _default cap
        # (600s)而非 rate_limited cap(60s),且丢 Retry-After header → backoff 无法尊重
        # server 明示的等待时长,可能过快复触发限流或过慢恢复。
        retry_after: float | None = None
        rah = r.headers.get("Retry-After")
        if rah:
            try:
                retry_after = float(rah)
            except ValueError:
                # HTTP-date 格式不解析,靠默认 backoff 兜底
                retry_after = None
        payload: dict[str, Any] = {
            "ok": False,
            "code": "rate_limited",
            "status": 429,
            "latency_ms": latency_ms,
            "message": "被 provider 限流",
        }
        if retry_after is not None:
            payload["retry_after_seconds"] = retry_after
        return payload
    return {
        "ok": False,
        "code": "http_error",
        "status": r.status_code,
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


async def probe_omni(model: str, base_url: str, api_key: str) -> dict[str, Any]:
    """两阶段探测:GET /models 预检 → 极简 chat 真校验。

    - GET /models 网络错 → unreachable
    - GET /models 401/403 → bad_key
    - GET /models 5xx → http_error
    - 其他(含 200 / 404 等) → 回退到 chat,以其结论为准

    非 OpenAI 兼容族(Gemini 等原生协议)没有等价的 GET /models 预检语义,直接走
    adapter 化的 chat 探测(``probe_chat`` 已按 provider 取 endpoint / 鉴权)。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    # 延迟 import 避免顶层循环依赖。
    from miloco.perception.engine.omni.provider import (
        OpenAICompatAdapter,
        get_adapter,
    )

    if not isinstance(get_adapter(model), OpenAICompatAdapter):
        return await probe_chat(model, base, api_key)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code >= 500:
        return {
            "ok": False,
            "code": "http_error",
            "status": r.status_code,
            "message": f"服务返回异常（HTTP {r.status_code}）",
        }
    return await probe_chat(model, base, api_key)
