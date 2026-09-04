# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""`register/preview` 的 media_kind 复核回归。

服务端原先无条件相信客户端自报的 ``media_kind``。HEIF/AVIF 与 mp4/mov 共用 ISO BMFF 容器，
一张被报成 ``"video"`` 的 HEIC 会进 ``extract_from_video`` → ffmpeg 不拼 HEIC 的 tile grid、
只暴露 512x512 瓦片 → DeepSORT 在一块瓦片上跑 → 200「no valid subject」，零提示。

这条路今天真实可达：CLI 的 ftyp 判据把 HEIC 判成视频，报错文案又引导 agent 改用 ``--video``。
本测试钉住服务端这一侧：**视频抽取器不得被调到**。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException
from miloco.person import router as prouter
from miloco.person.router import RegisterPreviewPayload, register_preview

from tests.test_image_decode import HEIC_BYTES


@pytest.fixture
def no_video_extractor(monkeypatch):
    """把视频抽取器换成"一旦被调就炸"，图片抽取器换成"返回空候选"。"""
    from miloco.perception.engine.identity import extractor as ex

    def _boom(*a, **k):
        raise AssertionError("extract_from_video 被调到了——HEIC 又走进视频分支")

    monkeypatch.setattr(ex, "extract_from_video", _boom)
    monkeypatch.setattr(ex, "extract_from_image", lambda *a, **k: [])
    monkeypatch.setattr(prouter, "_load_detector", lambda: object())
    monkeypatch.setattr(
        prouter,
        "manager",
        type(
            "M",
            (),
            {
                "perception_service": type(
                    "P", (), {"get_reid_extractor": staticmethod(lambda: None)}
                )()
            },
        )(),
    )


async def test_heic_declared_as_video_does_not_reach_video_extractor(no_video_extractor):
    body = RegisterPreviewPayload(
        media_b64=base64.b64encode(HEIC_BYTES).decode(), media_kind="video"
    )
    # 关键是**没有**触发 extract_from_video 的 AssertionError。原先这里裸吞 HTTPException，
    # 连「HEIC 根本没解开」也会被一并吞掉——那样即便撤销本 PR 的解码接线，用例仍然全绿。
    # 现在改成：只容忍「空候选」那一类 4xx，并额外断言这张 HEIC 确实解得开。
    from miloco.perception.engine.identity._image_utils import decode_image

    assert decode_image(HEIC_BYTES) is not None, "HEIC 解不开，掰回图片路径也没意义"
    try:
        await register_preview(body, current_user="t")
    except HTTPException as e:
        assert e.status_code in (400, 422), f"意外的错误码 {e.status_code}"
    assert body.media_kind == "image"  # 已被掰回图片路径


async def test_real_video_still_goes_to_video_extractor(monkeypatch):
    """反向：真视频容器必须仍走视频分支，别把护栏做成"一律当图片"。"""
    from miloco.perception.engine.identity import extractor as ex

    seen = {}

    def _video(*a, **k):
        seen["called"] = True
        return {}

    monkeypatch.setattr(ex, "extract_from_video", _video)
    monkeypatch.setattr(prouter, "_load_detector", lambda: object())
    monkeypatch.setattr(
        prouter,
        "manager",
        type(
            "M",
            (),
            {
                "perception_service": type(
                    "P",
                    (),
                    {
                        "get_reid_extractor": staticmethod(lambda: None),
                        "deep_sort_config": None,
                    },
                )()
            },
        )(),
    )
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    body = RegisterPreviewPayload(
        media_b64=base64.b64encode(mp4).decode(), media_kind="video"
    )
    try:
        await register_preview(body, current_user="t")
    except HTTPException:
        pass
    assert seen.get("called") is True
    assert body.media_kind == "video"  # 未被改写
