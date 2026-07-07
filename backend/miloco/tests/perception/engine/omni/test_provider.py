"""Tests for Omni Provider Adapter."""

from miloco.perception.engine.omni.provider import (
    LocalMediaInfo,
    MiMoAdapter,
    QwenOmniAdapter,
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


class TestGetAdapter:
    def test_mimo_default(self):
        assert isinstance(get_adapter("xiaomi/mimo-v2.5"), MiMoAdapter)

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

    def test_singleton(self):
        assert get_adapter("xiaomi/mimo-v2.5") is get_adapter("xiaomi/mimo-v2.5")
        assert get_adapter("qwen3.5-omni-flash") is get_adapter("qwen3.5-omni-plus")


class TestMiMoAdapter:
    adapter = MiMoAdapter()

    def test_video_block_has_fps_and_media_resolution(self):
        block = self.adapter.build_video_block("AAAA", _VIDEO_MEDIA)
        assert block["type"] == "video_url"
        assert block["fps"] == 1
        assert block["media_resolution"] == "max"
        assert block["video_url"]["url"].startswith("data:video/mp4;base64,")

    def test_audio_block(self):
        block = self.adapter.build_audio_block("BBBB", _AUDIO_MEDIA)
        assert block["type"] == "input_audio"
        assert block["input_audio"]["data"].startswith("data:audio/m4a;base64,")

    def test_request_body_non_stream(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="xiaomi/mimo-v2.5",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=False,
        )
        assert body["stream"] is False
        assert body["thinking"] == {"type": "disabled"}
        assert "stream_options" not in body

    def test_request_body_stream(self):
        body = self.adapter.build_request_body(
            _MESSAGES, model="xiaomi/mimo-v2.5",
            max_tokens=512, temperature=0.1, top_p=0.95, stream=True,
        )
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["thinking"] == {"type": "disabled"}


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
