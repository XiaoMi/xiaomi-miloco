# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""omni 下采时逐帧框必须与帧同步抽稀。

这条不变式是单帧人像注入的正确性前提：注入侧把 ``all_frames[j]`` 与 ``per_frame_track_boxes[j]``
配对裁图，两边下标一旦错位，就是拿另一个时刻的框去裁当前帧 —— 人已走开则裁到背景，多人场景则
裁到隔壁那个人，而那张图是绑 track_id 的，等于给模型喂一个错的身份证据。

只在直接构造 packet 的单测里是照不到的（那条路不经下采），故单独钉在这里。
"""

from __future__ import annotations

import numpy as np
from miloco.perception.engine.pipeline import _downsample_for_omni
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    IdentityPacket,
    MotionState,
)


def _packet(n: int, *, boxes: list | None = None) -> IdentityPacket:
    """n 帧；第 j 帧填充值 = j，便于用像素值判定"裁的是哪一帧"。"""
    frames = [np.full((48, 64, 3), j, dtype=np.uint8) for j in range(n)]
    return IdentityPacket(
        packet_id="p", room_name="r", timestamp=0.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=1000, fps=3),
        targets=[], scene_motion=MotionState.STATIC, frames=[], all_frames=frames,
        audio_clip=np.zeros(10, dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
        per_frame_track_boxes=[{1: (j, j, j + 10, j + 20)} for j in range(n)] if boxes is None else boxes,
    )


class TestDownsampleKeepsBoxesAligned:
    def test_boxes_subsampled_with_same_indices(self):
        """出厂配置 fps=3 / omni_fps=1：12 帧抽成 4 帧，框必须跟着抽同样那 4 个。"""
        out = _downsample_for_omni(_packet(12), 3, 1)
        assert len(out.all_frames) == 4
        assert len(out.per_frame_track_boxes) == 4
        # 以末帧为锚向前每 3 帧 → 原始下标 2/5/8/11
        assert [int(f[0, 0, 0]) for f in out.all_frames] == [2, 5, 8, 11]
        assert [b[1][0] for b in out.per_frame_track_boxes] == [2, 5, 8, 11]

    def test_frame_and_box_stay_paired(self):
        """逐对校验：第 j 帧的像素值必须等于第 j 个框的坐标（构造时二者同源于原始下标）。"""
        out = _downsample_for_omni(_packet(12), 3, 1)
        for frame, box in zip(out.all_frames, out.per_frame_track_boxes):
            assert int(frame[0, 0, 0]) == box[1][0]

    def test_no_downsample_keeps_boxes_untouched(self):
        """omni_fps >= src_fps 时原样返回，框不动。"""
        p = _packet(6)
        assert _downsample_for_omni(p, 3, 3) is p
        assert _downsample_for_omni(p, 3, 0) is p

    def test_empty_boxes_stay_empty(self):
        """mock 跟踪服务不产逐帧框 → 抽稀后仍为空（注入侧退末帧兜底，不报错）。"""
        out = _downsample_for_omni(_packet(12, boxes=[]), 3, 1)
        assert out.per_frame_track_boxes == []
        assert len(out.all_frames) == 4

    def test_length_mismatch_discards_boxes_rather_than_misalign(self):
        """长度不符时整份弃用而非按下标截断——截断只是换一种错位。"""
        out = _downsample_for_omni(_packet(12, boxes=[{1: (0, 0, 1, 1)}] * 5), 3, 1)
        assert out.per_frame_track_boxes == []
        assert len(out.all_frames) == 4

    def test_original_packet_untouched(self):
        """下采返回副本，原 packet 仍供 PipelineResult / 下游使用。"""
        p = _packet(12)
        _downsample_for_omni(p, 3, 1)
        assert len(p.all_frames) == 12
        assert len(p.per_frame_track_boxes) == 12
