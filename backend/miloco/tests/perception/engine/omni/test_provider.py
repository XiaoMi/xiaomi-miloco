"""Tests for Omni Provider Adapter."""

import base64

from miloco.perception.engine.omni import provider
from miloco.perception.engine.omni.provider import (
    GeminiAdapter,
    LocalMediaInfo,
    MiMoAdapter,
    MiniMaxAdapter,
    OpenAICompatAdapter,
    QwenOmniAdapter,
    build_request_headers,
    get_adapter,
)

_VIDEO_MEDIA = LocalMediaInfo(
    video_width=910, video_height=512, fps=1, frame_count=4,
    has_audio=True, audio_sample_rate=16000,
)
_AUDIO_MEDIA = LocalMediaInfo(
    video_width=0, video_height=0, fps=0, frame_count=0,
    has_audio=True, audio_sample_rate=16000,
)
_MESSAGES = [{"role": "user", "content": "test"}]


class TestBuildRequestHeaders:
    adapter = MiMoAdapter()

    def test_opencode_go_gets_stable_session_header(self):
        first = build_request_headers(
            self.adapter, "https://opencode.ai/zen/go/v1", "secret-key"
        )
        second = build_request_headers(
            self.adapter, "https://opencode.ai/zen/go/v1/", "secret-key"
        )

        assert first["x-opencode-session"] == second["x-opencode-session"]
        assert first["x-opencode-session"].startswith("ses_")
        assert len(first["x-opencode-session"]) == 36
        assert "secret-key" not in first["x-opencode-session"]
        assert first["User-Agent"].startswith("xiaomi-miloco/")
        assert first["Authorization"] == "Bearer secret-key"

    def test_other_endpoints_do_not_get_opencode_header(self):
        for base_url in (
            "https://api.openai.com/v1",
            "https://opencode.ai/zen/v1",
            "https://opencode.ai.evil.example/zen/go/v1",
            "http://opencode.ai/zen/go/v1",
        ):
            headers = build_request_headers(self.adapter, base_url, "secret-key")
            assert "x-opencode-session" not in headers


class TestGetAdapter:
    def test_mimo_default(self):
        assert isinstance(get_adapter("mimo-v2.6-flash"), MiMoAdapter)

    def test_mimo_unknown(self):
        assert isinstance(get_adapter("some-unknown-model"), MiMoAdapter)

    def test_mimo_empty(self):
        assert isinstance(get_adapter(""), MiMoAdapter)

    def test_qwen_flash(self):
        assert isinstance(get_adapter("qwen3.5-omni-flash"), QwenOmniAdapter)

    def test_qwen_plus(self):
        assert isinstance(get_adapter("qwen3.5-omni-plus"), QwenOmniAdapter)

    def test_qwen_case_insensitive(self):
        assert isinstance(get_adapter("Qwen3.5-Omni-Flash"), QwenOmniAdapter)

    def test_gemini(self):
        assert isinstance(get_adapter("gemini-3-flash-preview"), GeminiAdapter)

    def test_gemini_36_flash(self):
        assert isinstance(get_adapter("gemini-3.6-flash"), GeminiAdapter)

    def test_gemini_case_insensitive(self):
        assert isinstance(get_adapter("Gemini-3-Pro"), GeminiAdapter)

    def test_minimax_m3(self):
        assert isinstance(get_adapter("MiniMax-M3"), MiniMaxAdapter)

    def test_minimax_m3_flash(self):
        assert isinstance(get_adapter("MiniMax-M3.1-Flash"), MiniMaxAdapter)

    def test_minimax_case_insensitive(self):
        assert isinstance(get_adapter("minimax-m3.1-flash"), MiniMaxAdapter)

    def test_openai_compat_family(self):
        # MiMo / Qwen / MiniMax 都归 OpenAI 兼容族；Gemini 不是。
        assert isinstance(get_adapter("mimo-v2.6-flash"), OpenAICompatAdapter)
        assert isinstance(get_adapter("qwen3.5-omni-flash"), OpenAICompatAdapter)
        assert isinstance(get_adapter("MiniMax-M3.1-Flash"), OpenAICompatAdapter)
        assert not isinstance(get_adapter("gemini-3-flash-preview"), OpenAICompatAdapter)

    def test_singleton(self):
        assert get_adapter("mimo-v2.6-flash") is get_adapter("mimo-v2.6-flash")
        assert get_adapter("qwen3.5-omni-flash") is get_adapter("qwen3.5-omni-plus")
        assert get_adapter("gemini-3-flash") is get_adapter("gemini-3-pro")
        assert get_adapter("MiniMax-M3") is get_adapter("MiniMax-M3.1-Flash")


class TestMiMoAdapter:
    adapter = MiMoAdapter()

    def test_video_block_has_fps_and_media_resolution(self):
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block["type"] == "video_url"
        assert block["fps"] == 1
        assert block["media_resolution"] == "max"
        assert block["video_url"]["url"].startswith("data:video/mp4;base64,")

    def test_audio_block_has_format(self):
        """根因回归守护:audio 块缺 format 是 2026-07-29 422×3 → OPEN_CONFIG 停摆的起点。"""
        block = self.adapter.build_audio_block("BBBB", _AUDIO_MEDIA)
        assert block["type"] == "input_audio"
        assert block["input_audio"]["format"] == "m4a"
        assert block["input_audio"]["data"].startswith("data:audio/m4a;base64,")

    def test_image_block_default_jpeg_data_uri(self):
        # image 块各 provider 同构,走基类默认实现;入参是原始 JPEG 字节,方法内部 base64。
        block = self.adapter.build_image_block(b"\xff\xd8rawjpeg")
        assert block["type"] == "image_url"
        prefix = "data:image/jpeg;base64,"
        url = block["image_url"]["url"]
        assert url.startswith(prefix)
        assert base64.b64decode(url[len(prefix):]) == b"\xff\xd8rawjpeg"

    def test_request_body_non_stream(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="mimo-v2.6-flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        assert body["stream"] is False
        assert body["thinking"] == {"type": "disabled"}
        assert "stream_options" not in body

    def test_request_body_stream(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="mimo-v2.6-flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=True,
        )
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["thinking"] == {"type": "disabled"}


class TestMiniMaxAdapter:
    adapter = MiniMaxAdapter()

    def test_request_body_has_no_thinking(self):
        """根因回归守护:M3 要求 adaptive thinking,显式 thinking.disabled 会被上游拒(400/2013)。"""
        body = self.adapter.build_request_body(
            _MESSAGES, model="MiniMax-M3.1-Flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        assert "thinking" not in body

    def test_request_body_stream_has_no_thinking(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="MiniMax-M3.1-Flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=True,
        )
        assert "thinking" not in body
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}

    def test_request_body_otherwise_same_as_mimo(self):
        """除 thinking 外与 MiMoAdapter 逐字段一致。"""
        minimax = self.adapter.build_request_body(
            _MESSAGES, model="MiniMax-M3.1-Flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        mimo = MiMoAdapter().build_request_body(
            _MESSAGES, model="mimo-v2.6-flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        assert set(minimax) == set(mimo) - {"thinking"}
        assert minimax["max_tokens"] == mimo["max_tokens"]
        assert minimax["temperature"] == mimo["temperature"]
        assert minimax["top_p"] == mimo["top_p"]
        assert minimax["stream"] == mimo["stream"]

    def test_video_block_same_as_mimo(self):
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block["type"] == "video_url"
        assert block["fps"] == 1
        assert block["media_resolution"] == "max"
        assert block["video_url"]["url"].startswith("data:video/mp4;base64,")

    def test_audio_block_same_as_mimo(self):
        block = self.adapter.build_audio_block("BBBB", _AUDIO_MEDIA)
        assert block["type"] == "input_audio"
        assert block["input_audio"]["format"] == "m4a"
        assert block["input_audio"]["data"].startswith("data:audio/m4a;base64,")

    def test_image_block_default_jpeg_data_uri(self):
        block = self.adapter.build_image_block(b"\xff\xd8rawjpeg")
        assert block["type"] == "image_url"
        prefix = "data:image/jpeg;base64,"
        url = block["image_url"]["url"]
        assert url.startswith(prefix)
        assert base64.b64decode(url[len(prefix):]) == b"\xff\xd8rawjpeg"


class TestQwenOmniAdapter:
    adapter = QwenOmniAdapter()

    def test_video_block_no_fps_no_media_resolution(self):
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block["type"] == "video_url"
        assert "fps" not in block
        assert "media_resolution" not in block
        assert block["video_url"]["url"].startswith("data:;base64,")

    def test_audio_block_has_format(self):
        block = self.adapter.build_audio_block("BBBB", _AUDIO_MEDIA)
        assert block["type"] == "input_audio"
        assert block["input_audio"]["format"] == "m4a"
        assert block["input_audio"]["data"].startswith("data:;base64,")

    def test_request_body_forces_stream(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="qwen3.5-omni-flash",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["modalities"] == ["text"]
        assert "thinking" not in body

    def test_request_body_no_thinking(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="qwen3.5-omni-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        assert "thinking" not in body


class TestOpenAICompatProtocol:
    """OpenAI 兼容族的协议默认实现（endpoint / headers / 反解析）。"""

    adapter = MiMoAdapter()

    def test_endpoint_same_for_stream(self):
        assert self.adapter.endpoint("https://x/v1", "m", stream=False) == "https://x/v1/chat/completions"
        assert self.adapter.endpoint("https://x/v1", "m", stream=True) == "https://x/v1/chat/completions"

    def test_auth_headers_bearer(self):
        assert self.adapter.auth_headers("KEY") == {"Authorization": "Bearer KEY"}

    def test_parse_response_passthrough(self):
        raw = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3}}
        assert self.adapter.parse_response(raw) is raw

    def test_parse_stream_chunk_content(self):
        delta, usage = self.adapter.parse_stream_chunk(
            {"choices": [{"delta": {"content": "ab"}}]}
        )
        assert delta == "ab"
        assert usage is None

    def test_parse_stream_chunk_usage(self):
        delta, usage = self.adapter.parse_stream_chunk({"usage": {"prompt_tokens": 5}})
        assert delta is None
        assert usage == {"prompt_tokens": 5}

    def test_parse_stream_chunk_empty_choices(self):
        delta, usage = self.adapter.parse_stream_chunk({"choices": []})
        assert delta is None
        assert usage is None


class TestGeminiAdapter:
    adapter = GeminiAdapter()

    def test_endpoint_generate_content(self):
        assert (
            self.adapter.endpoint("https://g/v1beta", "gemini-3-flash", stream=False)
            == "https://g/v1beta/models/gemini-3-flash:generateContent"
        )

    def test_endpoint_stream(self):
        assert (
            self.adapter.endpoint("https://g/v1beta", "gemini-3-flash", stream=True)
            == "https://g/v1beta/models/gemini-3-flash:streamGenerateContent?alt=sse"
        )

    def test_auth_headers_goog_key(self):
        assert self.adapter.auth_headers("KEY") == {"x-goog-api-key": "KEY"}

    def test_video_block_openai_shape(self):
        # 内部 IR 恒为 OpenAI 形态（带 fps + video/mp4 mime），转换在 build_request_body 完成。
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block["type"] == "video_url"
        assert block["fps"] == 1
        assert block["video_url"]["url"].startswith("data:video/mp4;base64,")

    def test_request_body_system_instruction(self, monkeypatch):
        monkeypatch.setattr(provider, "_gemini_media_resolution", lambda: "")
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]
        body = self.adapter.build_request_body(
            messages, model="gemini-3-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        assert body["system_instruction"] == {"parts": [{"text": "你是助手"}]}
        assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
        gc = body["generationConfig"]
        assert gc["maxOutputTokens"] == 512
        assert gc["temperature"] == 0.1
        assert gc["topP"] == 0.95
        # 默认关思考；暂不启用 JSON 模式；default media_resolution 不发字段；stream 不进 body
        assert gc["thinkingConfig"] == {"thinkingBudget": 0}
        assert "mediaResolution" not in gc
        assert "responseMimeType" not in gc
        assert "stream" not in body

    def test_media_resolution_default_omitted(self, monkeypatch):
        # ""/"low" → 不发 mediaResolution（= Gemini 默认 low）
        for val in ("", "low", "LOW"):
            monkeypatch.setattr(provider, "_gemini_media_resolution", lambda v=val: v)
            body = self.adapter.build_request_body(
                [{"role": "user", "content": "x"}], model="gemini-3-flash",
                max_tokens=512, temperature=0.1, top_p=0.95,
            )
            assert "mediaResolution" not in body["generationConfig"], val

    def test_media_resolution_high(self, monkeypatch):
        monkeypatch.setattr(provider, "_gemini_media_resolution", lambda: "high")
        body = self.adapter.build_request_body(
            [{"role": "user", "content": "x"}], model="gemini-3-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_HIGH"

    def test_request_body_video_metadata_at_part_level(self):
        video_block = self.adapter.build_video_block("VIDEOB64", _VIDEO_MEDIA)
        messages = [{"role": "user", "content": [{"type": "text", "text": "看"}, video_block]}]
        body = self.adapter.build_request_body(
            messages, model="gemini-3-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        parts = body["contents"][0]["parts"]
        # text part
        assert parts[0] == {"text": "看"}
        # video part：inline_data + video_metadata 在 Part 级（非 inline_data 内）
        video_part = parts[1]
        assert video_part["inline_data"] == {"mime_type": "video/mp4", "data": "VIDEOB64"}
        assert video_part["video_metadata"] == {"fps": 1}
        assert "video_metadata" not in video_part["inline_data"]

    def test_request_body_multiple_images_and_audio_inline_data(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG1"}},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG2"}},
            self.adapter.build_audio_block("AUD", _AUDIO_MEDIA),
        ]}]
        body = self.adapter.build_request_body(
            messages, model="gemini-3.6-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        parts = body["contents"][0]["parts"]
        assert parts[0]["inline_data"] == {"mime_type": "image/jpeg", "data": "IMG1"}
        assert parts[1]["inline_data"] == {"mime_type": "image/jpeg", "data": "IMG2"}
        assert parts[2]["inline_data"] == {"mime_type": "audio/mp4", "data": "AUD"}

    def test_image_block_round_trips_through_request_body_to_inline_data(self):
        # image 块下沉 adapter 的前提:build_image_block 产出的通用 image_url 块,经 Gemini
        # 原生转换(_content_to_parts)能正确落成 inline_data。端到端钉住这条路,防止将来
        # 有人改 build_image_block 形态却漏了 Gemini 侧转换。
        block = self.adapter.build_image_block(b"\xff\xd8rawjpeg")
        messages = [{"role": "user", "content": [block]}]
        body = self.adapter.build_request_body(
            messages, model="gemini-3.6-flash",
            max_tokens=512, temperature=0.1, top_p=0.95,
        )
        part = body["contents"][0]["parts"][0]
        assert part["inline_data"]["mime_type"] == "image/jpeg"
        assert base64.b64decode(part["inline_data"]["data"]) == b"\xff\xd8rawjpeg"

    def test_parse_response_to_openai_shape(self):
        raw = {
            "candidates": [{"content": {"parts": [{"text": "hello "}, {"text": "world"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "totalTokenCount": 120,
                "cachedContentTokenCount": 10,
                "promptTokensDetails": [
                    {"modality": "VIDEO", "tokenCount": 60},
                    {"modality": "AUDIO", "tokenCount": 8},
                ],
            },
        }
        out = self.adapter.parse_response(raw)
        assert out["choices"][0]["message"]["content"] == "hello world"
        assert out["usage"]["prompt_tokens"] == 100
        assert out["usage"]["completion_tokens"] == 20
        assert out["usage"]["total_tokens"] == 120
        assert out["usage"]["prompt_tokens_details"] == {
            "cached_tokens": 10, "video_tokens": 60, "audio_tokens": 8,
        }

    def test_parse_response_no_candidates_safe(self):
        # 空 candidates（被 promptFeedback 拦）→ choices 空，供下游走 fallback。
        out = self.adapter.parse_response({"promptFeedback": {"blockReason": "SAFETY"}})
        assert out["choices"] == []

    def test_parse_response_empty_parts_fallback(self):
        # candidate 有但 parts 空（如 finishReason=MAX_TOKENS 无文本）→ choices 空走 fallback，
        # 不产空串 content(与真·空回答区分)。
        out = self.adapter.parse_response(
            {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        )
        assert out["choices"] == []

    def test_parse_response_non_dict_passthrough(self):
        # 服务端偶发返回非 dict → 原样透传，交给上游 shape 守卫。
        assert self.adapter.parse_response(["x"]) == ["x"]

    def test_parse_stream_chunk(self):
        delta, usage = self.adapter.parse_stream_chunk(
            {"candidates": [{"content": {"parts": [{"text": "ab"}]}}]}
        )
        assert delta == "ab"
        assert usage is None

    def test_parse_stream_chunk_usage(self):
        delta, usage = self.adapter.parse_stream_chunk(
            {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}
        )
        assert delta is None
        assert usage == {"prompt_tokens": 5, "completion_tokens": 2}
