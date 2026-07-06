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


_DEFAULT_ADAPTER = MiMoAdapter()


def get_adapter(model: str) -> OmniProviderAdapter:
    """按 model 字符串返回对应 adapter，默认 MiMo。"""
    return _DEFAULT_ADAPTER
