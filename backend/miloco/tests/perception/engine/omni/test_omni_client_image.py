"""Omni image/video message assembly regression tests."""

import base64

import pytest
from miloco.perception.engine.omni.omni_client import _build_messages
from miloco.perception.engine.omni.prompt_builder import EncodedImageFrame


def _frames(*payloads: bytes) -> list[EncodedImageFrame]:
    """按生产侧的形态构造帧 —— _encode_image_frames 产出的就是这个 dataclass。"""
    last = len(payloads) - 1
    return [
        EncodedImageFrame(data=data, sequence_index=i, is_last=i == last)
        for i, data in enumerate(payloads)
    ]


class _FakeAdapter:
    def build_video_block(self, video_base64, media):
        return {"type": "video_url", "video_url": {"url": video_base64}}

    def build_audio_block(self, audio_base64, media):
        return {"type": "input_audio", "input_audio": {"data": audio_base64}}


def _payload(**overrides):
    payload = {
        "system_prompt": "system",
        "user_content": "user",
    }
    payload.update(overrides)
    return payload


def _content(messages):
    return messages[1]["content"]


def _jpeg_payload(block) -> bytes:
    prefix = "data:image/jpeg;base64,"
    url = block["image_url"]["url"]
    assert url.startswith(prefix), url[:32]
    return base64.b64decode(url[len(prefix):])


def test_image_mode_requires_visual_frames_without_audio():
    with pytest.raises(ValueError, match="requires image_frames"):
        _build_messages(
            _payload(
                visual_input_mode="image",
            ),
            _FakeAdapter(),
        )


def test_image_mode_audio_only_sends_independent_audio():
    messages = _build_messages(
        _payload(
            visual_input_mode="image",
            audio_base64="audio",
        ),
        _FakeAdapter(),
    )
    assert [block["type"] for block in _content(messages)] == [
        "text", "input_audio"
    ]


def test_image_mode_sends_all_images_and_independent_audio():
    messages = _build_messages(
        _payload(
            visual_input_mode="image",
            image_frames=_frames(b"one", b"two"),
            audio_base64="audio",
        ),
        _FakeAdapter(),
    )
    content = _content(messages)
    assert [block["type"] for block in content] == [
        "text", "image_url", "image_url", "input_audio"
    ]
    # 顺序即语义(序列按拍摄时间从早到晚),所以要逐个对回原始字节, 不能只看有几个块
    assert _jpeg_payload(content[1]) == b"one"
    assert _jpeg_payload(content[2]) == b"two"


def test_video_mode_does_not_duplicate_audio_embedded_in_video():
    messages = _build_messages(
        _payload(
            visual_input_mode="video",
            video_base64="video",
            audio_base64="audio",
        ),
        _FakeAdapter(),
    )
    assert [block["type"] for block in _content(messages)] == ["text", "video_url"]


def test_audio_only_message_still_sends_audio_in_video_mode():
    messages = _build_messages(
        _payload(
            visual_input_mode="video",
            audio_base64="audio",
        ),
        _FakeAdapter(),
    )
    assert [block["type"] for block in _content(messages)] == ["text", "input_audio"]
