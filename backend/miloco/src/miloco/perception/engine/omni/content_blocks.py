"""Shared OpenAI-compatible multimodal content blocks."""

from __future__ import annotations


def video_url_block(video_base64: str) -> dict:
    """Build a provider-compatible MP4 video content block."""
    return {
        "type": "video_url",
        "video_url": {
            "url": f"data:video/mp4;base64,{video_base64}",
            "detail": "default",
        },
    }


def image_url_block(media_type: str, image_base64: str) -> dict:
    """Build a provider-compatible image content block."""
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media_type};base64,{image_base64}",
        },
    }
