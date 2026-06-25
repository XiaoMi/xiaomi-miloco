"""音视频质量预设参数。

根据 perception.quality.preset 配置返回对应的音视频参数。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityParams:
    """音视频质量参数。"""

    video_short_edge: int  # omni 视频短边像素
    audio_sample_rate: int  # 音频采样率 (Hz)
    camera_video_quality: int  # 摄像头流画质 (1=LOW, 3=HIGH)


# 预设参数表
_PRESETS: dict[str, QualityParams] = {
    "default": QualityParams(
        video_short_edge=512,
        audio_sample_rate=16000,
        camera_video_quality=1,  # LOW
    ),
    "high": QualityParams(
        video_short_edge=1080,
        audio_sample_rate=48000,
        camera_video_quality=3,  # HIGH
    ),
}

DEFAULT_PRESET = "default"


def get_quality_params(preset: str | None = None) -> QualityParams:
    """获取音视频质量参数。

    Args:
        preset: 预设名称，None 时使用默认预设。

    Returns:
        QualityParams 实例。
    """
    if preset is None:
        preset = DEFAULT_PRESET
    return _PRESETS.get(preset, _PRESETS[DEFAULT_PRESET])


def list_presets() -> dict[str, dict]:
    """列出所有可用预设。"""
    return {
        name: {
            "video_short_edge": p.video_short_edge,
            "audio_sample_rate": p.audio_sample_rate,
            "camera_video_quality": "HIGH" if p.camera_video_quality == 3 else "LOW",
        }
        for name, p in _PRESETS.items()
    }
