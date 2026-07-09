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
    """拉取 provider 模型列表(GET /models)。"""
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "models": [], "message": err}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "models": [],
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code == 200:
        try:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:  # noqa: BLE001
            ids = []
        return {"ok": True, "models": sorted(ids)}
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


async def probe_chat(model: str, base_url: str, api_key: str) -> dict[str, Any]:
    """极简 chat 探测(max_tokens=1)真校验模型是否可用。"""
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
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
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
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
