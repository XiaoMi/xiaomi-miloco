"""Omni Provider Adapter — 不同模型的协议适配层。

两层分离的 Layer 2：把 Miloco 内部「受控 OpenAI messages 子集」翻译成各 provider 的
线上协议，并把响应反解析回 OpenAI 形态，使下游解析（response_parser / extract_usage /
fire_record / trace）与 provider 无关。

adapter 职责：
  - build_video_block / build_audio_block —— 构建 OpenAI 规范的多模态 content block
    （内部 IR 恒为 OpenAI 形态，供 trace / 摘要复用）。
  - build_request_body —— 把 OpenAI messages 转成 provider 的线上请求体。
  - endpoint / auth_headers —— provider 的请求 URL 与鉴权头。
  - parse_response / parse_stream_chunk —— 把 provider 响应反解析回 OpenAI 形态
    ``{choices:[{message:{content}}], usage:{...}}``。

OpenAI 兼容族（MiMo / Qwen）继承 ``OpenAICompatAdapter``，协议方法走默认实现，只覆写各自
的 block / body 差异。Gemini 走原生 ``generateContent`` 协议（OpenAI 兼容端点不支持视频输入）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 已对哪些非 flash 的 gemini model 打过 thinkingBudget=0 告警——进程内按 model 去重,
# 避免每个推理窗口刷屏(build_request_body 在热路径上每窗调一次)。
_warned_non_flash_gemini: set[str] = set()


@dataclass(frozen=True)
class LocalMediaInfo:
    """本地编码后的视频/音频实际参数。"""

    video_width: int
    video_height: int
    fps: int
    frame_count: int
    has_audio: bool
    audio_sample_rate: int


class OmniProviderAdapter(ABC):

    @abstractmethod
    def build_video_block(self, video_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        """构建 video content block（进 messages[].content[]，恒为 OpenAI 形态）。"""

    @abstractmethod
    def build_audio_block(self, audio_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        """构建 audio-only content block（恒为 OpenAI 形态）。"""

    @abstractmethod
    def build_request_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        """把 OpenAI messages 转为 provider 线上请求 body。"""

    @abstractmethod
    def endpoint(self, base_url: str, model: str, *, stream: bool) -> str:
        """provider 的 chat/completions 请求 URL（含 stream 分歧）。"""

    @abstractmethod
    def auth_headers(self, api_key: str) -> dict[str, str]:
        """provider 的鉴权头（不含 Content-Type / User-Agent，由调用方补齐）。"""

    @abstractmethod
    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把 provider 非流式响应反解析回 OpenAI 形态 ``{choices, usage}``。"""

    @abstractmethod
    def parse_stream_chunk(
        self, chunk: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        """把单个流式 chunk 反解析成 ``(content_delta, usage)``。

        无内容/无 usage 时对应位返回 None；usage 一旦返回即为 OpenAI 形态。
        """


class OpenAICompatAdapter(OmniProviderAdapter):
    """OpenAI 兼容协议默认实现（MiMo / Qwen 等继承）。

    请求走 ``{base_url}/chat/completions`` + ``Authorization: Bearer``；响应本就是
    OpenAI 形态，parse_response 原样返回、parse_stream_chunk 抽 ``choices[].delta.content``。
    子类只需覆写 build_video_block / build_audio_block / build_request_body 的差异。
    """

    def endpoint(self, base_url: str, model: str, *, stream: bool) -> str:
        return f"{base_url}/chat/completions"

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def parse_stream_chunk(
        self, chunk: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        usage = chunk["usage"] if isinstance(chunk.get("usage"), dict) else None
        try:
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
        except (IndexError, KeyError):
            delta = None
        return delta, usage


class MiMoAdapter(OpenAICompatAdapter):
    """MiMo-v2.5 API adapter。

    - video block: ``video_url`` + ``fps`` + ``media_resolution: "max"``
    - audio block: ``input_audio``（m4a AAC）
    - request body: 含 ``thinking: {"type": "disabled"}``
    """

    def build_video_block(self, video_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        return {
            "type": "video_url",
            "video_url": {"url": f"data:video/mp4;base64,{video_base64}"},
            "fps": media.fps,
            "media_resolution": "max",
        }

    def build_audio_block(self, audio_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        audio_data_url = f"data:audio/m4a;base64,{audio_base64}"
        return {
            "type": "input_audio",
            "input_audio": {
                "data": audio_data_url,
                "format": audio_data_url.split(";")[0].split(":")[1],
            },
        }

    def build_request_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": stream,
            "thinking": {"type": "disabled"},
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body


class QwenOmniAdapter(OpenAICompatAdapter):
    """Qwen3.5-Omni 系列 API adapter（qwen3.5-omni-plus / qwen3.5-omni-flash）。

    仅支持 Qwen3.5-Omni 系列——fused 模式需要视频+图片+文本组合输入，
    旧版 qwen3-omni-flash 只支持文本+单一模态，无法满足。

    - video block: ``video_url``（不传 fps / media_resolution 字段，Qwen 从 mp4 本身读帧率）
    - audio block: ``input_audio`` + ``format``
    - request body: 强制 ``stream: true``（Qwen-Omni 非流式会报错）、``modalities: ["text"]``
    """

    def build_video_block(self, video_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        return {
            "type": "video_url",
            "video_url": {"url": f"data:;base64,{video_base64}"},
        }

    def build_audio_block(self, audio_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        return {
            "type": "input_audio",
            "input_audio": {
                "data": f"data:;base64,{audio_base64}",
                "format": "m4a",
            },
        }

    def build_request_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
            "stream_options": {"include_usage": True},
            "modalities": ["text"],
        }


def _parse_data_uri(url: str) -> tuple[str, str]:
    """把 ``data:<mime>;base64,<payload>`` 拆成 ``(mime_type, base64_payload)``。

    mime 缺省（``data:;base64,`` —— Qwen 风格）时回退 ``application/octet-stream``，
    由调用方按块类型兜底具体 mime。非 data URI（理论不出现）时 payload 原样返回。
    """
    header, sep, payload = url.partition(",")
    if not sep or not header.startswith("data:"):
        return "application/octet-stream", url
    mime = header[len("data:"):].split(";", 1)[0]
    return (mime or "application/octet-stream"), payload


def _gemini_usage_to_openai(usage_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """把 Gemini ``usageMetadata`` 归一化成 OpenAI ``usage`` 形态，供 extract_usage /
    fire_record 直接消费。

    映射：``promptTokenCount→prompt_tokens``、``candidatesTokenCount→completion_tokens``、
    ``totalTokenCount→total_tokens``、``cachedContentTokenCount→prompt_tokens_details.cached_tokens``；
    ``promptTokensDetails[]`` 按 modality 抽 AUDIO/VIDEO token 数。
    """
    if not usage_metadata:
        return {}
    usage: dict[str, Any] = {}
    if usage_metadata.get("promptTokenCount") is not None:
        usage["prompt_tokens"] = usage_metadata["promptTokenCount"]
    if usage_metadata.get("candidatesTokenCount") is not None:
        usage["completion_tokens"] = usage_metadata["candidatesTokenCount"]
    if usage_metadata.get("totalTokenCount") is not None:
        usage["total_tokens"] = usage_metadata["totalTokenCount"]

    details: dict[str, Any] = {}
    if usage_metadata.get("cachedContentTokenCount") is not None:
        details["cached_tokens"] = usage_metadata["cachedContentTokenCount"]
    for entry in usage_metadata.get("promptTokensDetails") or []:
        if not isinstance(entry, dict):
            continue
        count = entry.get("tokenCount")
        if count is None:
            continue
        modality = entry.get("modality")
        if modality == "AUDIO":
            details["audio_tokens"] = count
        elif modality == "VIDEO":
            details["video_tokens"] = count
    if details:
        usage["prompt_tokens_details"] = details
    return usage


def _gemini_media_resolution() -> str:
    """读 Gemini media_resolution 档位配置（``""`` / ``"low"`` / ``"high"``）。

    与 ``prompt_builder._get_video_short_edge`` 同款：每次调用实时读 settings，CLI 改后
    下一推理周期生效；读失败回退 ``""``（= Gemini 默认 low / 66 tok 每帧）。
    """
    try:
        from miloco.config import get_settings

        val = get_settings().perception.engine.get("input", {}).get("media_resolution", "")
        return str(val or "")
    except Exception:
        return ""


def _gemini_extract_text(payload: dict[str, Any]) -> str | None:
    """从 Gemini 响应体（非流式 raw 或流式 chunk）抽 ``candidates[0].content.parts[].text`` 拼接。

    无 candidates / parts 空 / 拼出空串 → 返回 ``None``，让上游走「无内容」fallback，
    与「真·空回答」区分开。非 dict（服务端偶发返回 list/str）同样返回 None。
    """
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text or None


class GeminiAdapter(OmniProviderAdapter):
    """Gemini 原生 ``generateContent`` 协议 adapter。

    Gemini 的 OpenAI 兼容端点不支持视频输入，而 Miloco 主链路是视频感知，故走原生协议：
    ``contents/parts`` + ``inline_data`` + Part 级 ``video_metadata.fps``。

    内部 IR（build_video_block/build_audio_block 产出）仍是 OpenAI 形态，转换在
    build_request_body 一次性完成；响应经 parse_response/parse_stream_chunk 反解析回
    OpenAI 形态，下游无感。

    - URL: ``{base_url}/models/{model}:generateContent``（流式 ``:streamGenerateContent?alt=sse``），
      base_url 需指向 Gemini 原生根（如 ``https://generativelanguage.googleapis.com/v1beta``）。
    - 鉴权: ``x-goog-api-key`` 头。
    - 视频 base64 走 inline_data，受 ~20MB 单请求上限约束（Miloco clip 远低于）。
    """

    def build_video_block(self, video_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        # 产 OpenAI 规范块（同 MiMo 形态，带真实 mime + fps），转换在 build_request_body 完成，
        # 保证内部 IR / trace / _summarize_multimodal_payload 仍是 OpenAI 形态。
        return {
            "type": "video_url",
            "video_url": {"url": f"data:video/mp4;base64,{video_base64}"},
            "fps": media.fps,
        }

    def build_audio_block(self, audio_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        # m4a(AAC) 容器 mime 记为 audio/mp4；Gemini 原生 inline audio 对 m4a 的接受度需实测，
        # 不达标属编码层问题（见 prompt_builder._encode_audio_only_mp4），不在 adapter 范围。
        audio_data_url = f"data:audio/mp4;base64,{audio_base64}"
        return {
            "type": "input_audio",
            "input_audio": {
                "data": audio_data_url,
                "format": audio_data_url.split(";")[0].split(":")[1],
            },
        }

    def _content_to_parts(self, content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 OpenAI content（str 或 block 列表）转成 Gemini ``parts``。"""
        if isinstance(content, str):
            return [{"text": content}] if content else []
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append({"text": block.get("text", "")})
            elif btype == "image_url":
                mime, data = _parse_data_uri(block.get("image_url", {}).get("url", ""))
                parts.append({"inline_data": {"mime_type": mime, "data": data}})
            elif btype == "video_url":
                mime, data = _parse_data_uri(block.get("video_url", {}).get("url", ""))
                if mime == "application/octet-stream":
                    mime = "video/mp4"
                # video_metadata 是 Part 成员（非 inline_data 成员）——放错位置 Gemini 报错。
                part: dict[str, Any] = {"inline_data": {"mime_type": mime, "data": data}}
                fps = block.get("fps")
                if fps:
                    part["video_metadata"] = {"fps": fps}
                parts.append(part)
            elif btype == "input_audio":
                mime, data = _parse_data_uri(block.get("input_audio", {}).get("data", ""))
                if mime == "application/octet-stream":
                    mime = "audio/mp4"
                parts.append({"inline_data": {"mime_type": mime, "data": data}})
        return parts

    def build_request_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        # system 消息 → system_instruction；其余 → contents。stream 不进 body（由 endpoint 决定），
        # 故 body.get("stream") 天然为 False，调用方走同步 generateContent 路径。
        system_texts: list[str] = []
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    if content:
                        system_texts.append(content)
                else:
                    system_texts.extend(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                continue
            parts = self._content_to_parts(content)
            if parts:
                grole = "model" if role == "assistant" else "user"
                contents.append({"role": grole, "parts": parts})

        # get_adapter 用子串匹配路由,任何含 "gemini" 的 model 都到这里;但 thinkingBudget=0
        # 只对 gemini-3-flash 系列验证过。非 flash 的 gemini(如强制思考的 gemini-2.5-pro,
        # 最小 budget 128)会因 budget=0 直接 400——打一条 warning 让失败可读、点名本字段。
        # build_request_body 在每个推理窗口热路径上,故按 model 进程内去重、只打一次:既避免
        # 刷屏,也弱化"名字不含 flash"这个粗判据对 gemini-3-pro 等大概率可用模型的误报噪音。
        if "flash" not in model.lower() and model not in _warned_non_flash_gemini:
            _warned_non_flash_gemini.add(model)
            # model 来自用户配置,记日志前清掉 CR/LF 防日志注入(CodeQL log-injection)。
            safe_model = model.replace("\r", " ").replace("\n", " ")
            logger.warning(
                "[omni] GeminiAdapter 默认发 thinkingConfig.thinkingBudget=0,仅对 gemini-3-flash "
                "系列验证过;model=%s 若为强制思考模型(如 gemini-2.5-pro)会 400,需按模型放开此项",
                safe_model,
            )
        gen_cfg: dict[str, Any] = {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
            "topP": top_p,
            # 关思考：感知要直给结构化结果（对标 MiMo 的 thinking:disabled）。省 token，
            # 且防思考挤占 maxOutputTokens 导致可见输出被截断。
            # 【假设面向 gemini-3 系列】gemini-3 用 thinkingBudget=0 可彻底关闭（thinkingLevel
            # "low" 关不掉），实测 gemini-3-flash-preview / gemini-3.5-flash 均接受。注意强制思考
            # 的模型（如 gemini-2.5-pro，最小 budget 128）会因 budget=0 直接 400——本 adapter
            # 面向 gemini-3-flash，不覆盖那类模型；若未来要接，需按模型放开此项。
            "thinkingConfig": {"thinkingBudget": 0},
        }
        # media_resolution 档位：仅 "high" 显式请求高预算(264 tok/帧)；其余(""/"low")不发该
        # 字段即 Gemini 默认 low(66 tok/帧)。默认 low 最省，identity 等细节场景可经 CLI 切 high。
        if _gemini_media_resolution().lower() == "high":
            gen_cfg["mediaResolution"] = "MEDIA_RESOLUTION_HIGH"

        body: dict[str, Any] = {"contents": contents, "generationConfig": gen_cfg}
        if system_texts:
            body["system_instruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
        return body

    def endpoint(self, base_url: str, model: str, *, stream: bool) -> str:
        verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{base_url}/models/{model}:{verb}"

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key}

    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        # 非 dict（服务端偶发返回 list/str）原样透传，交给 _call_omni_messages 的
        # ``isinstance(raw, dict)`` 守卫 dump 诊断，与 OpenAICompat 的 passthrough 行为对齐。
        if not isinstance(raw, dict):
            return raw
        text = _gemini_extract_text(raw)
        usage = _gemini_usage_to_openai(raw.get("usageMetadata"))
        # text 为 None（无 candidates / parts 空 / 空串）→ choices 空 → _extract_content 得
        # None 走「无内容」fallback，与真·空回答区分。
        choices = [{"message": {"content": text}}] if text is not None else []
        return {"choices": choices, "usage": usage}

    def parse_stream_chunk(
        self, chunk: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        delta = _gemini_extract_text(chunk)
        usage_meta = chunk.get("usageMetadata") if isinstance(chunk, dict) else None
        usage = _gemini_usage_to_openai(usage_meta) if usage_meta else None
        return delta, usage


def adjust_fps_for_omni(fps: int, omni_fps: int) -> int:
    """当 fps % omni_fps != 0 时，返回 omni_fps 的最小整数倍 >= fps。"""
    if omni_fps <= 0 or fps % omni_fps == 0:
        return fps
    if omni_fps > fps:
        return omni_fps
    return omni_fps * -(-fps // omni_fps)


_DEFAULT_ADAPTER = MiMoAdapter()
_QWEN_ADAPTER = QwenOmniAdapter()
_GEMINI_ADAPTER = GeminiAdapter()


def get_adapter(model: str) -> OmniProviderAdapter:
    """按 model 字符串返回对应 adapter，默认 MiMo。

    Qwen 侧仅支持 Qwen3.5-Omni 系列（qwen3.5-omni-plus / qwen3.5-omni-flash），
    旧版 qwen3-omni-flash 不支持多模态组合输入，无法满足 fused 模式需求。

    Gemini 走原生 generateContent 协议（OpenAI 兼容端点不支持视频输入）。
    """
    name = model.lower()
    if "qwen" in name:
        return _QWEN_ADAPTER
    if "gemini" in name:
        return _GEMINI_ADAPTER
    return _DEFAULT_ADAPTER
