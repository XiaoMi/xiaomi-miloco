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
from miloco.perception.engine.omni.constants import MILOCO_USER_AGENT
from miloco.perception.snapshot_context import push_omni_trace

logger = logging.getLogger(__name__)

_ENV_KEY = "MILOCO_MODEL__OMNI__API_KEY"

# ── shared httpx.AsyncClient ──────────────────────────────────────────────
# 统一 omni_client.py 与 omni.py 中原本独立的两套客户端实现为一个共享
# httpx.AsyncClient（池化 + keepalive），消除三个调用路径的重复代码与行为差异。
# AsyncClient 绑定到创建时所在的 event loop；感知每窗 asyncio.run() 起新 loop，client 每窗重建。
# 客户端不显式关闭（进程退出由 OS 回收）。
_http_client: "httpx.AsyncClient | None" = None
# _http_client_loop: CodeQL "is unused" alert is a false positive.
# This is the sole trigger for rebuilding the client when the event loop
# changes — removing it reuses a client bound to a dead loop and causes
# "Event loop is closed". Do not delete.
_http_client_loop: "asyncio.AbstractEventLoop | None" = None


def _get_http_client(timeout: float) -> httpx.AsyncClient:
    global _http_client, _http_client_loop
    loop = asyncio.get_running_loop()
    if (
        _http_client is None
        or _http_client_loop is not loop
        or _http_client.is_closed
    ):
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),  # matches max camera count per window (≤1 omni call per camera via room batches)
        )
        _http_client_loop = loop
    return _http_client


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
            input_tokens=int(usage["prompt_tokens"]) if usage.get("prompt_tokens") is not None else None,
            output_tokens=int(usage["completion_tokens"]) if usage.get("completion_tokens") is not None else None,
            cached_tokens=int(details["cached_tokens"]) if details.get("cached_tokens") is not None else None,
            audio_tokens=int(details["audio_tokens"]) if details.get("audio_tokens") is not None else None,
            video_tokens=int(details["video_tokens"]) if details.get("video_tokens") is not None else None,
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
    """
    from dataclasses import replace

    from miloco.config import get_settings

    o = get_settings().model.omni
    return replace(
        base,
        model=o.model,
        base_url=o.base_url,
        api_key=o.api_key or base.api_key,
    )


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

    messages = _build_messages(payload)

    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_completion_tokens,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "stream": False,
        "thinking": {"type": "disabled"},
    }

    t0 = time.monotonic()
    raw: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        client = _get_http_client(config.timeout)
        resp = await client.post(
            f"{config.base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": MILOCO_USER_AGENT,
            },
            json=body,
        )
        if resp.status_code != 200:
            logger.error(
                "Omni API error %d: %s", resp.status_code, resp.text[:500]
            )
        resp.raise_for_status()
        raw = resp.json()
        fire_record(config.model, raw.get("usage", {}), type)
        return raw
    except OmniError:
        raise  # 不重复包装
    except Exception as e:
        error = {"code": e.__class__.__name__, "msg": str(e)[:512]}
        raise OmniError(
            f"call_omni failed: {e.__class__.__name__}: {e}", original=e
        ) from e
    finally:
        push_omni_trace(
            request_messages=messages,
            response_raw=raw,
            latency_ms=(time.monotonic() - t0) * 1000,
            error=error,
            model=config.model,
            inference_params={
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_completion_tokens,
            },
        )


def _build_messages(payload: dict) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": payload["system_prompt"]}]

    content: list[dict] = [{"type": "text", "text": payload["user_content"]}]

    # Video (frames + audio merged into mp4)；与 audio_base64 互斥（上游 _build_payload 保证）
    if payload.get("video_base64"):
        content.append(
            {
                "type": "video_url",
                "video_url": {
                    "url": f"data:video/mp4;base64,{payload['video_base64']}"
                },
                "fps": payload.get("video_fps", 3),
                "media_resolution": "max",
            }
        )
    # Audio-only route：独立 input_audio 块（仅当无 video_base64 时启用）
    elif payload.get("audio_base64"):
        content.append(
            {
                "type": "input_audio",
                "input_audio": {
                    "data": f"data:audio/m4a;base64,{payload['audio_base64']}"
                },
            }
        )

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

    messages = _build_messages(payload)

    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_completion_tokens,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }
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
    t0 = time.monotonic()
    try:
        client = _get_http_client(config.timeout)
        async with client.stream(
            "POST",
            f"{config.base_url}/chat/completions",
            headers=headers,
            json=body,
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                logger.error(
                    "Omni stream error %d: %s", resp.status_code, resp.text[:500]
                )
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # usage 在最后一个 chunk 的顶层
                if isinstance(chunk.get("usage"), dict):
                    raw_usage_seen = chunk["usage"]
                    if usage_out is not None:
                        usage_out.update(extract_usage({"usage": raw_usage_seen}))

                # content delta：choices[0].delta.content
                try:
                    delta = (
                        chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    )
                except (IndexError, KeyError):
                    delta = None
                if delta:
                    response_chunks.append(delta)
                    yield delta
    except OmniError:
        raise  # 不重复包装
    except Exception as e:
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
        push_omni_trace(
            request_messages=messages,
            response_raw=raw_for_trace,
            latency_ms=(time.monotonic() - t0) * 1000,
            error=error,
            model=config.model,
            inference_params={
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_completion_tokens,
            },
        )
