"""omni 调用异常/响应到统一错误码集合的映射。

映射规则见 spec §2。CODES 与 web 前端 omniHealth.codes 一一对应(10 个 code),
前端直接复用 i18n。前端的 OMNI_CODE_KEY 是「测试连接」与「模型下拉」**两条路共用**的
机器码 → i18n 表,为本集合的裁剪+增补变体:去掉 probe 路径不会产生的 timeout(probe 把
所有异常统一归 unreachable)、加上成功码 ok、再加上只由 fetch_models 产出的
list_unsupported —— 后者不参与熔断,故**不**属于本集合。

新增任何面向前端的机器码,**不论走哪条路径**,都要同步登记进 OMNI_CODE_KEY 并补中英文案;
漏登记时界面会回退到后端硬编码的中文 message、污染英文界面。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

import httpx


class ErrorCategory(Enum):
    RECOVERABLE = "recoverable"  # 进指数退避熔断
    CONFIG = "config"  # 直接软停,等用户改配置


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    category: ErrorCategory
    retry_after_seconds: float | None = (
        None  # 仅 rate_limited 且 Retry-After 存在时非空
    )


CODES: set[str] = {
    "unreachable",
    "timeout",
    "http_error",
    "rate_limited",
    "bad_key",
    "no_key",
    "not_found",
    "rejected_authed",
    "bad_response",
    "cancelled",
}


_MESSAGES: dict[str, str] = {
    "unreachable": "无法连接 omni 服务",
    "timeout": "omni 服务响应超时",
    "http_error": "omni 服务返回异常",
    "rate_limited": "被 provider 限流",
    "bad_key": "API Key 无效或无权限",
    "no_key": "未配置 API Key",
    "not_found": "模型或地址不存在",
    "rejected_authed": "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
    "bad_response": "omni 响应格式异常",
    "cancelled": "重试被中断",
}


def classify_exception(exc: BaseException) -> ClassifiedError:
    """httpx 异常/本地异常 → ClassifiedError。未知异常保守归 unreachable。"""
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return ClassifiedError(
            "timeout", _MESSAGES["timeout"], ErrorCategory.RECOVERABLE
        )
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.NetworkError)):
        return ClassifiedError(
            "unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE
        )
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return ClassifiedError(
            "bad_response", _MESSAGES["bad_response"], ErrorCategory.RECOVERABLE
        )
    return ClassifiedError(
        "unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE
    )


def classify_response(resp: httpx.Response) -> ClassifiedError | None:
    """HTTP 响应 → ClassifiedError；2xx 返 None(调用方按成功处理)。"""
    s = resp.status_code
    if 200 <= s < 300:
        return None
    if s in (401, 403):
        return ClassifiedError("bad_key", _MESSAGES["bad_key"], ErrorCategory.CONFIG)
    if s == 404:
        return ClassifiedError(
            "not_found", _MESSAGES["not_found"], ErrorCategory.CONFIG
        )
    if s in (400, 422):
        return ClassifiedError(
            "rejected_authed", _MESSAGES["rejected_authed"], ErrorCategory.CONFIG
        )
    if s == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        return ClassifiedError(
            "rate_limited",
            _MESSAGES["rate_limited"],
            ErrorCategory.RECOVERABLE,
            retry_after,
        )
    if s >= 500:
        return ClassifiedError(
            "http_error",
            f"{_MESSAGES['http_error']}（HTTP {s}）",
            ErrorCategory.RECOVERABLE,
        )
    return ClassifiedError(
        "http_error",
        f"{_MESSAGES['http_error']}（HTTP {s}）",
        ErrorCategory.RECOVERABLE,
    )


def _parse_retry_after(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None  # HTTP-date 格式不解析,交给默认 backoff
