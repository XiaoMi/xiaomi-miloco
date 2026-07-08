"""omni 调用异常/响应到统一错误码集合的映射。

映射规则见 spec §2。code 集合与 web 前端 OMNI_CODE_KEY 保持一致,前端可直接复用 i18n。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

import httpx


class ErrorCategory(Enum):
    RECOVERABLE = "recoverable"  # 进指数退避熔断
    CONFIG = "config"            # 直接软停,等用户改配置


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    category: ErrorCategory
    retry_after_seconds: float | None = None  # 仅 rate_limited 且 Retry-After 存在时非空


CODES: set[str] = {
    "unreachable", "timeout", "http_error", "rate_limited",
    "bad_key", "not_found", "rejected_authed", "bad_response",
}


_MESSAGES: dict[str, str] = {
    "unreachable": "无法连接 omni 服务",
    "timeout": "omni 服务响应超时",
    "http_error": "omni 服务返回异常",
    "rate_limited": "被 provider 限流",
    "bad_key": "API Key 无效或无权限",
    "not_found": "模型或地址不存在",
    "rejected_authed": "已连接，但拒绝了模型请求（模型名可能错误）",
    "bad_response": "omni 响应格式异常",
}


def classify_exception(exc: BaseException) -> ClassifiedError:
    """httpx 异常/本地异常 → ClassifiedError。未知异常保守归 unreachable。"""
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return ClassifiedError("timeout", _MESSAGES["timeout"], ErrorCategory.RECOVERABLE)
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.NetworkError)):
        return ClassifiedError("unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE)
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return ClassifiedError("bad_response", _MESSAGES["bad_response"], ErrorCategory.RECOVERABLE)
    return ClassifiedError("unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE)


def classify_response(resp: httpx.Response) -> ClassifiedError | None:
    """HTTP 响应 → ClassifiedError；2xx 返 None(调用方按成功处理)。"""
    s = resp.status_code
    if 200 <= s < 300:
        return None
    if s in (401, 403):
        return ClassifiedError("bad_key", _MESSAGES["bad_key"], ErrorCategory.CONFIG)
    if s == 404:
        return ClassifiedError("not_found", _MESSAGES["not_found"], ErrorCategory.CONFIG)
    if s in (400, 422):
        return ClassifiedError("rejected_authed", _MESSAGES["rejected_authed"], ErrorCategory.CONFIG)
    if s == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        return ClassifiedError("rate_limited", _MESSAGES["rate_limited"], ErrorCategory.RECOVERABLE, retry_after)
    if s >= 500:
        return ClassifiedError("http_error", f"{_MESSAGES['http_error']}（HTTP {s}）", ErrorCategory.RECOVERABLE)
    return ClassifiedError("http_error", f"{_MESSAGES['http_error']}（HTTP {s}）", ErrorCategory.RECOVERABLE)


def _parse_retry_after(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None  # HTTP-date 格式不解析,交给默认 backoff
