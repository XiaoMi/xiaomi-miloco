"""Tests for Omni Provider Adapter."""

import base64

import pytest
from miloco.perception.engine.omni import provider
from miloco.perception.engine.omni.provider import (
    GeminiAdapter,
    KimiAdapter,
    LocalMediaInfo,
    MiMoAdapter,
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

    def test_kimi(self):
        assert isinstance(get_adapter("kimi-k2.6"), KimiAdapter)

    def test_kimi_case_insensitive(self):
        assert isinstance(get_adapter("Kimi-K2.6"), KimiAdapter)
    def test_openai_compat_family(self):
        # MiMo / Qwen / Kimi 都归 OpenAI 兼容族；Gemini 不是。
        assert isinstance(get_adapter("mimo-v2.6-flash"), OpenAICompatAdapter)
        assert isinstance(get_adapter("qwen3.5-omni-flash"), OpenAICompatAdapter)
        assert isinstance(get_adapter("kimi-k2.6"), OpenAICompatAdapter)
        assert not isinstance(get_adapter("gemini-3-flash-preview"), OpenAICompatAdapter)

    def test_singleton(self):
        assert get_adapter("mimo-v2.6-flash") is get_adapter("mimo-v2.6-flash")
        assert get_adapter("qwen3.5-omni-flash") is get_adapter("qwen3.5-omni-plus")
        assert get_adapter("gemini-3-flash") is get_adapter("gemini-3-pro")
        assert get_adapter("kimi-k2.6") is get_adapter("kimi-k2-turbo-preview")


def test_fused_capability_error_avoids_duplicate_error_log(caplog):
    """Fused cleanup re-raises capability rejection for pipeline classification."""
    import asyncio
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni.omni import run_omni_fused
    from miloco.perception.engine.types import OmniContext

    deliver_fused_failure = AsyncMock()
    identity_engine = SimpleNamespace(
        library=SimpleNamespace(list_persons=lambda: []),
        take_fused_pending=lambda: SimpleNamespace(
            candidates=[object()], gallery_snapshot={}
        ),
        deliver_fused_failure=deliver_fused_failure,
    )
    capability_error = provider.OmniCapabilityError(
        "Kimi does not support audio input"
    )

    caplog.set_level(logging.WARNING, logger="miloco.perception.engine.omni.omni")
    with patch(
        "miloco.perception.engine.omni.omni.build_fused_payload",
        side_effect=capability_error,
    ), pytest.raises(provider.OmniCapabilityError):
        asyncio.run(
            run_omni_fused(
                [],
                OmniContext(),
                OmniConfig(model="kimi-k2.6"),
                identity_engine,
            )
        )

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    deliver_fused_failure.assert_awaited_once_with(
        "Kimi does not support audio input"
    )


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


class TestKimiAdapter:
    adapter = KimiAdapter()

    def test_video_block_matches_moonshot_documented_shape(self):
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block == {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAAA"},
        }

    def test_audio_block_is_rejected_before_request_assembly(self):
        with pytest.raises(ValueError, match="Kimi.*does not support audio"):
            self.adapter.build_audio_block("BBBB", _AUDIO_MEDIA)

    def test_kimi_k2_6_omits_non_modifiable_sampling_parameters(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="kimi-k2.6",
            max_tokens=512, temperature=0.1, top_p=1.0, stream=False,
        )
        assert "temperature" not in body
        assert "top_p" not in body
        assert body["thinking"] == {"type": "disabled"}

    def test_other_kimi_models_passthrough_sampling(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="kimi-k2-turbo-preview",
            max_tokens=512, temperature=0.1, top_p=0.9, stream=False,
        )
        assert body["temperature"] == 0.1
        assert body["top_p"] == 0.9
        assert "thinking" not in body

    def test_stream_keeps_usage_options_and_disables_thinking(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="kimi-k2.6",
            max_tokens=512, temperature=0.1, top_p=1.0, stream=True,
        )
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert "temperature" not in body
        assert "top_p" not in body
        assert body["thinking"] == {"type": "disabled"}


class TestKimiAdapterWireShape:
    """感知主链路真实组包（``_build_messages`` → ``build_request_body``）的线格式断言。

    上面的 ``TestKimiAdapter`` 只单测 block 构造，本类补上「block 进到messages 后、
    最终发给 Moonshot 的那个 body 长什么样」这一段——block 单测过不代表组包后形状正确
    （例如MiMo 私有fps 被带进 video_url.url、或 K2.6 未显式关闭思考）。

    ⚠️ 能力边界：这些断言证明 **本仓组包逻辑** 产出预期形状（含 data URI 承载方式），
    **不能证明 Moonshot 线上接受该形状**。后者需真实 Moonshot key + 真实视频/音频的
    非 probe 请求，见 PR #575 描述「未验证项」。仓内无 mock 可替代该证据。
    """

    adapter = KimiAdapter()

    def _wire_body(self, payload: dict) -> dict:
        """走生产同款路径组包：payload → messages → request body。"""
        from miloco.perception.engine.omni.omni_client import _build_messages

        adapter = get_adapter(payload.get("model", "kimi-k2.6"))
        messages = _build_messages(payload, adapter)
        return adapter.build_request_body(
            messages,
            model=payload.get("model", "kimi-k2.6"),
            max_tokens=512,
            temperature=0.1,
            top_p=1.0,
            stream=False,
        )

    def test_fused_video_payload_data_uri_survives_assembly(self):
        """视频窗口（video_base64 命中，audio 分支不进）——data URI 必须原样进 wire body。"""
        body = self._wire_body({
            "system_prompt": "你是助手",
            "user_content": "看这段视频",
            "video_base64": "VIDEOB64",
            "audio_base64": "AUD",
            "media_info": _VIDEO_MEDIA,
            "crops": [],
        })
        content = body["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": "看这段视频"}
        assert content[1] == {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,VIDEOB64"},
        }
        # MiMo 私有字段不能顺着 group 漏进 Kimi 请求
        assert "fps" not in content[1]
        assert "media_resolution" not in content[1]
        # audio_base64 与 video_base64 同时在场时走 video 分支（生产 elif 语义）
        assert len(content) == 2
        assert content[1]["type"] == "video_url"

    def test_audio_only_payload_is_rejected_before_wire_body(self):
        """纯音频窗口不能退化成 text-only，也不能生成 Moonshot 不支持的块。"""
        with pytest.raises(ValueError, match="Kimi.*does not support audio"):
            self._wire_body({
                "system_prompt": "你是助手",
                "user_content": "听这段音频",
                "audio_base64": "AUD",
                "media_info": _AUDIO_MEDIA,
                "crops": [],
            })

    def test_wire_body_pairs_media_blocks_with_normalized_sampling(self):
        """多模态与 K2.6 默认采样必须同时成立：媒体块就位且受管字段省略。"""
        body = self._wire_body({
            "system_prompt": "你是助手",
            "user_content": "看这段视频",
            "video_base64": "VIDEOB64",
            "media_info": _VIDEO_MEDIA,
            "crops": [],
        })
        assert "temperature" not in body
        assert "top_p" not in body
        assert body["thinking"] == {"type": "disabled"}
        # 多模态走 OpenAI 兼容族 endpoint，不该被转成 Gemini 原生协议
        assert "contents" not in body
        assert body["messages"][1]["content"][1]["type"] == "video_url"

    def test_mimo_media_blocks_absent_from_kimi_wire_body(self):
        """反向对照：MiMo 私有扩展不得出现在 Kimi body 的任何 content block 里。

        顺序按 ``_build_messages`` 生产语义：text → video/audio → crops（图片）。
        """
        body = self._wire_body({
            "system_prompt": "你是助手",
            "user_content": "看这段视频",
            "video_base64": "VIDEOB64",
            "media_info": _VIDEO_MEDIA,
            "crops": [{"media_type": "image/png", "data": "IMG"}],
        })
        blocks = body["messages"][1]["content"][1:]
        assert [b["type"] for b in blocks] == ["video_url", "image_url"]
        for b in blocks:
            assert "fps" not in b
            assert "media_resolution" not in b
        assert blocks[0]["video_url"]["url"].startswith("data:video/mp4;base64,")


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
