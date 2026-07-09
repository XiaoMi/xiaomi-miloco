"""Omni Layer — MiMo API Client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import httpx

from miloco.database.token_usage_repo import fire_record
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni.circuit_breaker import (
    CircuitOpenError,
    get_omni_circuit_breaker,
)
from miloco.perception.engine.omni.constants import MILOCO_USER_AGENT
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
    classify_exception,
    classify_response,
)
from miloco.perception.engine.omni.provider import OmniProviderAdapter, get_adapter
from miloco.perception.snapshot_context import push_omni_trace

logger = logging.getLogger(__name__)

_ENV_KEY = "MILOCO_MODEL__OMNI__API_KEY"

# 三元组变化触发的 fire-and-forget reset task 强引用集合;asyncio 只对 task 持弱引用,
# 不持强引用短协程可能被 GC 提前回收。done_callback 里自动 discard。
_RESET_TASKS: set[asyncio.Task] = set()


class OmniError(Exception):
    """omni API 调用失败的统一异常包装。

    包装 httpx/httpcore 网络错误、4xx/5xx 状态码、JSON 解析失败等 omni 阶段错误。
    上游（processor）用它跟 gate/identity/convert 等其他 pipeline 阶段失败区分开，
    把这类错误算进 omni_error_count。

    ``partial_timing`` 由 pipeline 在 raise 前填入，含 gate/identity/omni 各阶段
    已知的耗时;client.py 把它写进失败 placeholder result 的 timing,让失败 cycle
    在 trace 表里也能反映真实墙钟分布。
    """

    def __init__(
        self,
        message: str,
        *,
        original: Exception | None = None,
        partial_timing: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.original = original
        self.partial_timing = partial_timing

    @property
    def code(self) -> str:
        """原始异常类名（ReadTimeout / ConnectError 等），作为 error_code 上报。

        HTTPStatusError 附带 status_code，形如 ``HTTPStatusError:429``，让上层
        区分限流（429）/服务端错误（5xx）/其他非 200。这是 dashboard 错误分类的基础。
        """
        if self.original is None:
            return self.__class__.__name__
        name = self.original.__class__.__name__
        if isinstance(self.original, httpx.HTTPStatusError):
            try:
                return f"{name}:{self.original.response.status_code}"
            except Exception:
                return name
        return name


@dataclass(frozen=True)
class OmniCallMeta:
    """omni 单次调用的元数据(latency / retry / token / error)。"""

    latency_ms: float
    retry_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    audio_tokens: int | None = None
    video_tokens: int | None = None
    error_code: str | None = None

    @classmethod
    def from_raw(
        cls,
        raw_response: dict[str, Any],
        latency_ms: float,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> "OmniCallMeta":
        usage = raw_response.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return cls(
            latency_ms=latency_ms,
            retry_count=retry_count,
            input_tokens=int(usage["prompt_tokens"])
            if usage.get("prompt_tokens") is not None
            else None,
            output_tokens=int(usage["completion_tokens"])
            if usage.get("completion_tokens") is not None
            else None,
            cached_tokens=int(details["cached_tokens"])
            if details.get("cached_tokens") is not None
            else None,
            audio_tokens=int(details["audio_tokens"])
            if details.get("audio_tokens") is not None
            else None,
            video_tokens=int(details["video_tokens"])
            if details.get("video_tokens") is not None
            else None,
            error_code=error_code,
        )


def resolve_omni_api_key(api_key_from_config: str = "") -> str:
    """Resolve omni API key: use explicit value if provided, else fall back to env."""
    return api_key_from_config or os.environ.get(_ENV_KEY, "")


def resolve_api_key(config: OmniConfig) -> str:
    """Resolve API key from config or environment variable."""
    return resolve_omni_api_key(config.api_key)


def resolve_live_omni_config(base: OmniConfig) -> OmniConfig:
    """Refresh the user-configurable omni fields (model / base_url / api_key) from
    the current settings, keeping the engine snapshot's other fields
    (max_completion_tokens / temperature / top_p / timeout / stream).

    感知引擎启动时把 OmniConfig 当快照持有,故 web 改配置默认要重启才生效。
    在每次 omni 调用前用本函数取一次当前 settings(``update_shared_config`` 写完已
    ``reset_settings()`` 清缓存),即可让新模型在**下一个推理周期**自动生效,无需重启
    进程、不重建引擎。api_key 为空时退回快照值,最终调用点 ``resolve_api_key`` 仍会兜底环境变量。

    副作用:三元组 (model, base_url, api_key) 变化时清熔断状态到 CLOSED。覆盖所有配置源
    (web PUT/activate / CLI set / env / 直接改 config.json)——只要 settings 变了就自动重置。
    """
    from dataclasses import replace

    from miloco.config import get_settings

    o = get_settings().model.omni
    resolved = replace(
        base,
        model=o.model,
        base_url=o.base_url,
        api_key=o.api_key or base.api_key,
    )
    _maybe_reset_breaker_on_config_change(resolved)
    return resolved


def _maybe_reset_breaker_on_config_change(resolved: OmniConfig) -> None:
    """检测 (model, base_url, api_key) 三元组变化,变了就清熔断。跨调用状态保存在
    函数属性 ``._last_triple`` 上——比 module-level global 更内聚。"""
    triple = (resolved.model, resolved.base_url, resolve_omni_api_key(resolved.api_key))
    prev = getattr(_maybe_reset_breaker_on_config_change, "_last_triple", None)
    if prev is not None and prev != triple:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(get_omni_circuit_breaker().reset_on_config_change())
            _RESET_TASKS.add(task)
            task.add_done_callback(_RESET_TASKS.discard)
    _maybe_reset_breaker_on_config_change._last_triple = triple  # type: ignore[attr-defined]


async def call_omni(
    payload: dict, config: OmniConfig, type: str = "realtime"
) -> dict[str, Any]:
    """Call the omni model via MiMo API platform.

    `type` is either ``"realtime"`` (perception-loop driven, default) or
    ``"on_demand"`` (user-initiated query).
    """
    api_key = resolve_api_key(config)
    if not api_key:
        raise ValueError(
            f"{_ENV_KEY} is not set. Provide it via config or environment variable."
        )

    adapter = get_adapter(config.model)
    messages = _build_messages(payload, adapter)

    body = adapter.build_request_body(
        messages,
        model=config.model,
        max_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
        stream=False,
    )

    forced_stream = body.get("stream", False)

    cb = get_omni_circuit_breaker()
    t0 = time.monotonic()
    raw: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    short_circuited = False
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": MILOCO_USER_AGENT,
    }
    try:
        await cb.before_call()  # 熔断 OPEN → 直接抛 CircuitOpenError
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            if not forced_stream:
                resp = await client.post(
                    f"{config.base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                classified = classify_response(resp)
                if classified is not None:
                    await cb.record_failure(classified)
                    logger.error(
                        "Omni API error %d: %s", resp.status_code, resp.text[:500]
                    )
                    resp.raise_for_status()
                raw = resp.json()
            else:
                # forced-stream (Qwen 等 adapter 强制 stream=True) 走 _collect_stream_response,
                # 该函数内部对非 200 直接 raise_for_status → HTTPStatusError。下方 except
                # 守卫 `not isinstance(e, HTTPStatusError)` 会跳过 record_failure(防非流路径
                # 双重计数),导致 forced-stream 路径的 4xx/5xx 熔断器完全看不到。此处显式
                # 补一次 classify + record_failure 后再抛,与非流路径行为对齐。
                try:
                    raw = await _collect_stream_response(
                        client, config.base_url, headers, body
                    )
                except httpx.HTTPStatusError as e:
                    classified = classify_response(e.response)
                    if classified is not None:
                        await cb.record_failure(classified)
                        logger.error(
                            "Omni API error %d (stream): %s",
                            e.response.status_code,
                            e.response.text[:500],
                        )
                    raise
            if not isinstance(raw, dict):
                # 用 __class__.__name__ 而非 type(...) 避免遮盖问题(参数名 type)
                raw_cls = raw.__class__.__name__
                await cb.record_failure(
                    ClassifiedError(
                        "bad_response",
                        f"non-dict body ({raw_cls})",
                        ErrorCategory.RECOVERABLE,
                    )
                )
                raise OmniError(f"omni response is not a dict (got {raw_cls})")
            await cb.record_success()
            fire_record(config.model, raw.get("usage", {}), type)
        return raw
    except CircuitOpenError as ce:
        short_circuited = True
        error = {"code": ce.code, "msg": ce.message[:512]}
        raise OmniError(f"call_omni short-circuited: {ce.message}", original=ce) from ce
    except OmniError:
        raise
    except Exception as e:
        # HTTP 响应异常已在 record_failure 里记过;这里补记 exception 类。
        if not isinstance(e, httpx.HTTPStatusError):
            await cb.record_failure(classify_exception(e))
        error = {"code": e.__class__.__name__, "msg": str(e)[:512]}
        raise OmniError(
            f"call_omni failed: {e.__class__.__name__}: {e}", original=e
        ) from e
    finally:
        # 熔断短路 latency=0(不占实际墙钟),便于 dashboard 区分"实际尝试失败"和"熔断跳过"
        latency_ms = 0.0 if short_circuited else (time.monotonic() - t0) * 1000
        push_omni_trace(
            request_messages=messages,
            response_raw=raw,
            latency_ms=latency_ms,
            error=error,
            model=config.model,
            inference_params={
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_completion_tokens,
            },
        )


async def _iter_sse_chunks(resp) -> AsyncGenerator[dict, None]:
    """从 SSE 响应逐行解析 JSON chunks，跳过空行、非 data 行和畸形 JSON。"""
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


async def _collect_stream_response(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    """读取 SSE 流并拼接为等效的非流式响应 dict。

    用于 adapter 强制 stream=True（如 Qwen）但调用方期望同步返回的场景。
    """
    content_parts: list[str] = []
    usage: dict[str, Any] = {}
    async with client.stream(
        "POST", f"{base_url}/chat/completions", headers=headers, json=body,
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()
            logger.error("Omni stream error %d: %s", resp.status_code, resp.text[:500])
            if resp.status_code == 400:
                from miloco.perception.engine.omni.omni import (
                    _summarize_multimodal_payload,
                )
                logger.error(
                    "[omni] stream 400 payload 摘要 | %s",
                    _summarize_multimodal_payload(body.get("messages", [])),
                )
            resp.raise_for_status()
        async for chunk in _iter_sse_chunks(resp):
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            try:
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            except (IndexError, KeyError):
                delta = None
            if delta:
                content_parts.append(delta)
    return {
        "choices": [{"message": {"content": "".join(content_parts)}}],
        "usage": usage,
    }


def _build_messages(payload: dict, adapter: OmniProviderAdapter) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": payload["system_prompt"]}]

    content: list[dict] = [{"type": "text", "text": payload["user_content"]}]

    media_info = payload.get("media_info")

    if payload.get("video_base64"):
        content.append(adapter.build_video_block(payload["video_base64"], media_info))
    elif payload.get("audio_base64"):
        content.append(adapter.build_audio_block(payload["audio_base64"], media_info))

    # Crop images (from tracker)
    for crop in payload.get("crops", []):
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{crop['media_type']};base64,{crop['data']}"
                },
            }
        )

    messages.append({"role": "user", "content": content})
    return messages


def extract_usage(raw_response: dict) -> dict[str, int]:
    """从 MiMo 响应里抽 usage 信息，归一化字段。

    MiMo 是 OpenAI 兼容协议：
      usage = {
        prompt_tokens, completion_tokens, total_tokens,
        prompt_tokens_details: {audio_tokens, cached_tokens, video_tokens},
        completion_tokens_details: {reasoning_tokens}
      }
    """
    usage = raw_response.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "audio_tokens": int(details.get("audio_tokens") or 0),
        "video_tokens": int(details.get("video_tokens") or 0),
    }


async def call_omni_stream(
    payload: dict,
    config: OmniConfig,
    usage_out: dict | None = None,
    type: str = "realtime",
) -> AsyncGenerator[str, None]:
    """Streaming call to omni model via MiMo API platform, yields content delta tokens.

    Args:
        usage_out: 可选 dict，调用方传入；流式过程中如果在 chunk 里抽到 usage 信息，
                   会以 {input_tokens, output_tokens, cached_tokens} 写回到这个 dict。
        type: 给 ``fire_record`` 的调用类型标签，默认 ``"realtime"``，跟 ``call_omni`` 对齐。
    """
    api_key = resolve_api_key(config)
    if not api_key:
        raise ValueError(
            f"{_ENV_KEY} is not set. Provide it via config or environment variable."
        )

    adapter = get_adapter(config.model)
    messages = _build_messages(payload, adapter)

    body = adapter.build_request_body(
        messages,
        model=config.model,
        max_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
        stream=True,
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": MILOCO_USER_AGENT,
    }

    # 累积本次调用最后一次见到的 raw usage（OpenAI 字段），循环结束后统一上报一次，
    # 跟 call_omni / _call_omni_messages 的非 stream 路径完全对齐。
    raw_usage_seen: dict | None = None
    response_chunks: list[str] = []
    error: dict[str, Any] | None = None
    short_circuited = False
    cb = get_omni_circuit_breaker()
    t0 = time.monotonic()
    try:
        await cb.before_call()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout, connect=10.0)
        ) as client:
            async with client.stream(
                "POST",
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=body,
            ) as resp:
                classified = classify_response(resp)
                if classified is not None:
                    await resp.aread()
                    await cb.record_failure(classified)
                    logger.error(
                        "Omni stream error %d: %s", resp.status_code, resp.text[:500]
                    )
                    resp.raise_for_status()
                async for chunk in _iter_sse_chunks(resp):
                    if isinstance(chunk.get("usage"), dict):
                        raw_usage_seen = chunk["usage"]
                        if usage_out is not None:
                            usage_out.update(extract_usage({"usage": raw_usage_seen}))

                    try:
                        delta = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content")
                        )
                    except (IndexError, KeyError):
                        delta = None
                    if delta:
                        response_chunks.append(delta)
                        yield delta
        await cb.record_success()
    except CircuitOpenError as ce:
        short_circuited = True
        error = {"code": ce.code, "msg": ce.message[:512]}
        raise OmniError(
            f"call_omni_stream short-circuited: {ce.message}", original=ce
        ) from ce
    except OmniError:
        raise  # 不重复包装
    except Exception as e:
        if not isinstance(e, httpx.HTTPStatusError):
            await cb.record_failure(classify_exception(e))
        error = {"code": e.__class__.__name__, "msg": str(e)[:512]}
        raise OmniError(
            f"call_omni_stream failed: {e.__class__.__name__}: {e}", original=e
        ) from e
    finally:
        # generator close (正常 / 异常 / 消费方提前 break) 时统一上报一次
        if raw_usage_seen is not None:
            fire_record(config.model, raw_usage_seen, type)
        # stream 路径没有原生 raw response,只能用累积的 chunks + usage 拼一份伪 raw
        # 让 push_omni_trace 用同一 _pick_response_fields 路径抽 content/usage.
        # error 路径 raw_for_trace 仍传 (拼出 content="" usage={}),让 trace 行包含
        # error 字段而不是被丢弃.
        raw_for_trace: dict[str, Any] = {
            "choices": [{"message": {"content": "".join(response_chunks)}}],
            "usage": raw_usage_seen or {},
        }
        latency_ms = 0.0 if short_circuited else (time.monotonic() - t0) * 1000
        push_omni_trace(
            request_messages=messages,
            response_raw=raw_for_trace,
            latency_ms=latency_ms,
            error=error,
            model=config.model,
            inference_params={
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_completion_tokens,
            },
        )
