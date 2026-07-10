"""Omni Provider Adapter — 不同模型的 API 请求构建适配层。

两层分离的 Layer 2：根据本地编码后的视频参数，生成让 API 端不二次修改输入的请求参数。
只管请求构建，不管 response 解析（所有 provider 遵循 OpenAI 兼容协议）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


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
        """构建 video content block（进 messages[].content[]）。"""

    @abstractmethod
    def build_audio_block(self, audio_base64: str, media: LocalMediaInfo) -> dict[str, Any]:
        """构建 audio-only content block。"""

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
        """构建完整 HTTP request body。"""


class MiMoAdapter(OmniProviderAdapter):
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
        return {
            "type": "input_audio",
            "input_audio": {"data": f"data:audio/m4a;base64,{audio_base64}"},
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


class QwenOmniAdapter(OmniProviderAdapter):
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


def adjust_fps_for_omni(fps: int, omni_fps: int) -> int:
    """当 fps % omni_fps != 0 时，返回 omni_fps 的最小整数倍 >= fps。"""
    if omni_fps <= 0 or fps % omni_fps == 0:
        return fps
    if omni_fps > fps:
        return omni_fps
    return omni_fps * -(-fps // omni_fps)


_DEFAULT_ADAPTER = MiMoAdapter()
_QWEN_ADAPTER = QwenOmniAdapter()


def get_adapter(model: str) -> OmniProviderAdapter:
    """按 model 字符串返回对应 adapter，默认 MiMo。

    Qwen 侧仅支持 Qwen3.5-Omni 系列（qwen3.5-omni-plus / qwen3.5-omni-flash），
    旧版 qwen3-omni-flash 不支持多模态组合输入，无法满足 fused 模式需求。
    """
    name = model.lower()
    if "qwen" in name:
        return _QWEN_ADAPTER
    return _DEFAULT_ADAPTER
