"""Tests for Smart Crop / 自适应分辨率 —— crop_enhance 纯函数(合成帧确定性)。"""

import numpy as np
from miloco.perception.engine.config import CropEnhanceConfig
from miloco.perception.engine.omni.crop_enhance import (
    _body_boxes,
    compute_crop_region,
    compute_motion_blocks,
    crop_frames,
)
from miloco.perception.engine.types import (
    IdentityTarget,
    ObjectType,
    TrackingBoxInfo,
)

CFG = CropEnhanceConfig()


def _black(h=300, w=300):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _target(boxes: dict[str, tuple[int, int, int, int]]) -> IdentityTarget:
    return IdentityTarget(
        type=ObjectType.HUMAN_BODY,
        person_id="p",
        track_id=1,
        needs_omni_verify=False,
        box_info=[TrackingBoxInfo(frame_index=0, boxes=boxes)],
    )


# ---------------- compute_motion_blocks ----------------

class TestMotionBlocks:
    def test_moving_square_produces_blocks(self):
        f0, f1 = _black(), _black()
        f0[20:60, 20:60] = 255      # 方块在 A
        f1[200:240, 200:240] = 255  # 方块移到 B(远离,不重叠)
        blocks = compute_motion_blocks([f0, f1], CFG)
        assert len(blocks) >= 1
        for x1, y1, x2, y2 in blocks:
            assert 0 <= x1 < x2 <= 300 and 0 <= y1 < y2 <= 300

    def test_tiny_mover_filtered_by_area(self):
        # 4x4 方块面积占比 16/90000 ≈ 0.018% < 0.5% → 被点状噪声过滤
        f0, f1 = _black(), _black()
        f0[10:14, 10:14] = 255
        f1[40:44, 40:44] = 255
        assert compute_motion_blocks([f0, f1], CFG) == []

    def test_global_drift_returns_empty(self):
        f0 = _black()
        f1 = np.full((300, 300, 3), 255, dtype=np.uint8)  # 整帧翻白 → changed_ratio=1.0 > 0.5
        assert compute_motion_blocks([f0, f1], CFG) == []

    def test_static_no_motion(self):
        f = _black()
        f[50:100, 50:100] = 128
        assert compute_motion_blocks([f, f.copy()], CFG) == []

    def test_single_frame_no_motion(self):
        assert compute_motion_blocks([_black()], CFG) == []

    def test_thin_streak_filtered_by_fill(self):
        # 细斜线移动 → 连通块外接框大但填充稀疏 → fill_ratio < 0.2 被过滤
        f0, f1 = _black(), _black()
        for i in range(200):
            f0[20 + i, 20 + i] = 255       # 斜线 A
            f1[20 + i, 60 + i] = 255       # 斜线 B(平移)
        blocks = compute_motion_blocks([f0, f1], CFG)
        # 斜线的外接框近似 200x200,实际亮像素极少 → fill 远小于 0.2,应被丢弃
        assert blocks == []


# ---------------- _body_boxes / compute_crop_region ----------------

class TestCropRegion:
    def test_body_box_expand_and_clamp(self):
        # 100x100 帧,human_body xywh=(10,10,20,20) → xyxy(10,10,30,30)
        # 扩展 h40%/v30%: uw=uh=20 → ex=8,ey=6 → (2,4,38,36),clamp 内
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"human_body": (10, 10, 20, 20)})], frames, CFG)
        assert region is not None
        x1, y1, x2, y2 = region
        assert x1 == 2 and y1 == 4 and x2 == 38 and y2 == 36

    def test_face_excluded(self):
        # 只有 human_face 框 + 静态帧 → 无主体框、无运动 → None(face 不参与 crop)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)] * 2
        assert _body_boxes([_target({"human_face": (10, 10, 20, 20)})]) == []
        assert compute_crop_region([_target({"human_face": (10, 10, 20, 20)})], frames, CFG) is None

    def test_no_boxes_no_motion_none(self):
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)] * 2
        assert compute_crop_region([_target({})], frames, CFG) is None
        assert compute_crop_region([], frames, CFG) is None

    def test_max_area_fallback_none(self):
        # 大主体框 → 扩展后面积 >49% → None(回退全景)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"human_body": (10, 10, 80, 80)})], frames, CFG)
        assert region is None

    def test_min_area_enlarged(self):
        # 极小主体框 → 应放大到 >= crop_min_area_ratio(10%)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"human_body": (48, 48, 4, 4)})], frames, CFG)
        assert region is not None
        x1, y1, x2, y2 = region
        area_ratio = (x2 - x1) * (y2 - y1) / (100 * 100)
        assert area_ratio >= CFG.crop_min_area_ratio - 1e-6
        assert area_ratio <= CFG.crop_max_area_ratio

    def test_pet_body_included(self):
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"pet_body": (20, 20, 15, 15)})], frames, CFG)
        assert region is not None

    def test_precomputed_boxes_passthrough(self):
        # 预算 det_boxes/motion_blocks 传入 → 结果与内部自算一致(免重复算 CV 的诊断路径)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        targets = [_target({"human_body": (10, 10, 20, 20)})]
        auto = compute_crop_region(targets, frames, CFG)
        passed = compute_crop_region(
            targets, frames, CFG,
            det_boxes=_body_boxes(targets),
            motion_blocks=compute_motion_blocks(frames, CFG),
        )
        assert auto == passed
        # 显式传空 → 无依据 → None(不回退到内部自算)
        assert compute_crop_region(
            targets, frames, CFG, det_boxes=[], motion_blocks=[]
        ) is None


# ---------------- crop_frames ----------------

class TestCropFrames:
    def test_crop_shapes(self):
        frames = [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(3)]
        out = crop_frames(frames, (10, 20, 40, 60))
        assert len(out) == 3
        for f in out:
            assert f.shape == (40, 30, 3)  # (y2-y1, x2-x1, 3)

    def test_crop_is_copy(self):
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        out = crop_frames(frames, (0, 0, 10, 10))
        out[0][:] = 255
        assert frames[0].sum() == 0  # 原帧不受影响
