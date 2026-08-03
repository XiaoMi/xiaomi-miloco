"""Tests for Smart Crop / 自适应分辨率 —— crop_enhance 纯函数(合成帧确定性)。"""

import numpy as np
from miloco.perception.engine.config import CropEnhanceConfig
from miloco.perception.engine.omni.crop_enhance import (
    _body_boxes,
    compute_crop_region,
    compute_motion_blocks,
    crop_frames,
    remap_bbox_norm_to_crop,
)
from miloco.perception.engine.types import (
    IdentityTarget,
    ObjectType,
    TrackingBoxInfo,
)

CFG = CropEnhanceConfig()


def _black(h=300, w=300):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _target(
    boxes: dict[str, tuple[int, int, int, int]],
    *,
    person_id: str = "p",
    no_person: bool = False,
) -> IdentityTarget:
    return IdentityTarget(
        type=ObjectType.HUMAN_BODY,
        person_id=person_id,
        track_id=1,
        needs_omni_verify=False,
        box_info=[TrackingBoxInfo(frame_index=0, boxes=boxes)],
        no_person=no_person,
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

    def test_block_axis_order_is_x_then_y(self):
        """锁死 (x1,y1,x2,y2) 轴序:x 取 CC_STAT_LEFT、y 取 CC_STAT_TOP。

        上面那条用例是 300x300 **正方形**帧 + 对角线对称的方块,把实现里的
        blocks.append((bx,by,...)) 转置成 (by,bx,...) 也照样绿——轴序写反会让 crop 区域
        整体跑到镜像位置,却没有任何测试会响。这里用非方形帧 + 不对称位置钉住。
        """
        f0, f1 = _black(h=240, w=400), _black(h=240, w=400)
        f0[20:70, 300:370] = 255     # 右上
        f1[150:200, 40:110] = 255    # 移到左下(不重叠)
        blocks = compute_motion_blocks([f0, f1], CFG)
        assert len(blocks) >= 1
        for x1, y1, x2, y2 in blocks:
            assert 0 <= x1 < x2 <= 400 and 0 <= y1 < y2 <= 240
        # 右上方块的 x 落在 300 附近;若 x/y 转置则最大 x1 只有 150,断言失败
        assert any(x1 >= 250 for x1, _, _, _ in blocks)

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

    def test_expand_clamped_to_frame_upper_bound(self):
        """真正触发 _clamp 的 min(w,x2)/min(h,y2) 上界。

        上面那条用例名字带 clamp,但区域 (2,4,38,36) 在帧内、压根没截断:把 _clamp 的
        min(w,x2)/min(h,y2) 退回裸 x2,y2 它照样绿。越界坐标会写进 trace,前端按
        frame_size_wh 当 viewBox 画框会画到图外,所以这个上界必须有测试守住。

        100x100 帧,human_body xywh=(60,60,39,39) → xyxy(60,60,99,99);uw=uh=39 →
        ex=int(39*0.4)=15, ey=int(39*0.3)=11 → 扩展后 (45,49,114,110) 越界 → 截回
        (45,49,100,100)(面积 28% 在 [10%,49%] 内,不再被最小/最大面积改动)。
        """
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"human_body": (60, 60, 39, 39)})], frames, CFG)
        assert region == (45, 49, 100, 100)

    def test_face_excluded(self):
        # 只有 human_face 框 + 静态帧 → 无主体框、无运动 → None(face 不参与 crop)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)] * 2
        assert _body_boxes([_target({"human_face": (10, 10, 20, 20)})]) == []
        assert compute_crop_region([_target({"human_face": (10, 10, 20, 20)})], frames, CFG) is None

    def test_no_person_box_excluded(self):
        # 已落定 no_person 的静物误检框不进并集(否则位置固定的误检会把区域撑过面积上限,
        # 让整个房间永久回退全景)。
        t = _target({"human_body": (10, 10, 20, 20)}, person_id="none", no_person=True)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)] * 2
        assert _body_boxes([t]) == []
        assert compute_crop_region([t], frames, CFG) is None

    def test_unrecognized_person_box_kept(self):
        # person_id == "none" 但**不是** no_person(有人、只是没识别出身份)→ 框照常参与,
        # 证明排除的是状态位而非 "none" 这个渲染值(陌生人不能被一起排掉)。
        t = _target({"human_body": (10, 10, 20, 20)}, person_id="none")
        assert _body_boxes([t]) == [(10, 10, 30, 30)]

    def test_no_person_does_not_hide_real_target(self):
        # 一真一误检:并集只按真人框算,与单独给真人框的结果一致。
        real = _target({"human_body": (10, 10, 20, 20)})
        ghost = _target({"human_body": (80, 80, 15, 15)}, person_id="none", no_person=True)
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        assert compute_crop_region([real, ghost], frames, CFG) == compute_crop_region(
            [real], frames, CFG
        )

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

    def test_edge_hugging_small_box_falls_back(self):
        # 紧贴画面角的极小框:_enforce_min_area 绕中心放大被 clamp 截断、达不到下限 →
        # 复检兜底返回 None(或至少面积 >= 下限),不放行等效分辨率无收益的裁切。
        frames = [np.zeros((100, 100, 3), dtype=np.uint8)]
        region = compute_crop_region([_target({"human_body": (0, 0, 4, 4)})], frames, CFG)
        if region is not None:
            x1, y1, x2, y2 = region
            assert (x2 - x1) * (y2 - y1) / (100 * 100) >= CFG.crop_min_area_ratio - 1e-6

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


# ---------------- remap_bbox_norm_to_crop ----------------

class TestRemapBbox:
    """全景 [0,1000] bbox → crop 坐标系 [0,1000]。

    crop 生效时主视频是局部放大画面,而名册 bbox 按全景整帧归一化。不换算就直接锚视频
    是**方向性错误**(会把姓名贴到 crop 中央那个人身上),不是精度损失。
    """

    def test_half_frame_region_doubles_coords(self):
        # 帧 1000x1000、区域取左上 1/4(500x500):区域内的框归一化值翻倍
        assert remap_bbox_norm_to_crop((100, 100, 200, 200), (0, 0, 500, 500), (1000, 1000)) == (
            200, 200, 400, 400,
        )

    def test_offset_region_subtracts_origin(self):
        # 区域原点非 0:先平移再按区域尺寸归一化
        # px 300..500 → -200 → /400*1000 = 250..750
        assert remap_bbox_norm_to_crop((300, 300, 500, 500), (200, 200, 600, 600), (1000, 1000)) == (
            250, 250, 750, 750,
        )

    def test_full_frame_region_is_identity(self):
        # 区域=整帧 → 换算应为恒等(取整误差不得引入偏移)。
        # 用 640x480 非方形帧,顺带盯住 x 用宽、y 用高(轴搞反这条会挂)。
        assert remap_bbox_norm_to_crop((156, 208, 281, 458), (0, 0, 640, 480), (640, 480)) == (
            156, 208, 281, 458,
        )

    def test_box_outside_region_returns_none(self):
        # 框与区域无交集 → clamp 后零宽 → None(调用方退化为"只给姓名不给位置")
        assert remap_bbox_norm_to_crop((100, 100, 200, 200), (500, 500, 1000, 1000), (1000, 1000)) is None

    def test_partial_overflow_is_clamped(self):
        # 框超出区域右下 → 夹到 1000,不吐出 >1000 的坐标
        out = remap_bbox_norm_to_crop((100, 100, 800, 800), (0, 0, 500, 500), (1000, 1000))
        assert out == (200, 200, 1000, 1000)

    def test_degenerate_inputs_return_none(self):
        assert remap_bbox_norm_to_crop((0, 0, 100, 100), (100, 100, 100, 300), (500, 500)) is None  # 零宽区域
        assert remap_bbox_norm_to_crop((0, 0, 100, 100), (0, 0, 100, 100), (0, 0)) is None  # 零尺寸帧

    def test_real_region_contains_its_own_det_box(self):
        """契约锚点:compute_crop_region 由检测框并集算出 → 该框换算后必不退化。

        生产里名册 bbox 与 crop 区域同源(都读 box_info[-1].boxes["human_body"]),所以
        remap 正常不该返回 None。这条把"同源 ⇒ 包含"钉住:若哪天 _body_boxes 的取框口径
        与 identity 侧漂移,这里先挂。
        """
        frames = [_black(480, 640) for _ in range(2)]
        box_xywh = (100, 100, 80, 120)  # xyxy = (100, 100, 180, 220)
        region = compute_crop_region([_target({"human_body": box_xywh})], frames, CFG)
        assert region is not None
        # 直接调 identity 侧的归一化,而不是手抄它的公式 —— 这条测试的立意就是跨模块契约,
        # 抄公式会让口径漂移(如 round 改成截断)时它仍然绿灯,守不住自称要守的东西。
        from miloco.perception.engine.identity.engine import _normalize_bbox_to_1000

        bbox_norm = _normalize_bbox_to_1000((100, 100, 180, 220), frames[-1])
        assert bbox_norm is not None
        out = remap_bbox_norm_to_crop(bbox_norm, region, (640, 480))
        assert out is not None
        assert all(0 <= v <= 1000 for v in out)
        assert out[2] > out[0] and out[3] > out[1]


# ---------------- crop_enhance_config_from_settings ----------------

class TestConfigFromSettings:
    """yaml 键名 ↔ dataclass 字段的契约锚点。

    这是生产里唯一读 `perception.engine.crop_enhance` 的地方,也是
    「settings.yaml 键名 ↔ CropEnhanceConfig 字段 ↔ admin 写入路径」三方的唯一交汇点:
    整个能力开不开全压在它身上。此前测试都绕开了它(算法测试直接构 cfg、
    prompt_builder 测试把它整个 patch 掉、admin 测试只走读写不走这里),
    结果是把 yaml 里的 `crop_enhance:` 改个名字全套测试仍全绿,而生产静默 dark。
    """

    @staticmethod
    def _with_engine_config(tmp_path, monkeypatch, crop_enhance: dict) -> None:
        import json

        from miloco.config.settings import reset_settings

        monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
        monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"perception": {"engine": {"crop_enhance": crop_enhance}}}),
            encoding="utf-8",
        )
        reset_settings()

    def test_reads_both_gates_and_ratios(self, tmp_path, monkeypatch):
        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        self._with_engine_config(
            tmp_path,
            monkeypatch,
            {"enabled": True, "user_enabled": True, "expand_ratio_h": 0.25},
        )
        try:
            cfg = crop_enhance_config_from_settings()
            assert cfg.enabled is True
            assert cfg.user_enabled is True
            assert cfg.expand_ratio_h == 0.25
            # 未给的键仍取默认
            assert cfg.crop_short_edge == CropEnhanceConfig().crop_short_edge
        finally:
            reset_settings()

    def test_non_bool_gate_does_not_fail_open(self, tmp_path, monkeypatch):
        """字符串 "false" 不能把 ops 灰度闸绕开。

        dataclass 不校验值类型,`enabled: "false"`(yaml 里加了引号就是这个)会得到非空
        字符串 → truthy → 双闸判定通过、继续裁切,而运维以为已经压死。闸是 enabled=true
        发版后唯一的止损手段,必须只认真 bool。

        admin GET 的 smart_crop_available / smart_crop_enabled 也经本函数取值,故这里退
        禁用 = GET 侧一起退,两侧不会分裂;端到端断言见
        tests/admin/test_perception_config_dispatch.py::test_smart_crop_gates_non_bool_match_runtime。
        """
        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        for bad in ("false", "no", "0", 1, "true"):
            self._with_engine_config(
                tmp_path, monkeypatch, {"enabled": bad, "user_enabled": True}
            )
            try:
                cfg = crop_enhance_config_from_settings()
                # 退默认(enabled=False/user_enabled=False)→ 双闸相与必然不裁
                assert not (cfg.enabled and cfg.user_enabled), f"闸被 {bad!r} 绕开"
            finally:
                reset_settings()

    def test_non_numeric_ratio_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """数值字段写成字符串 → 退默认,而不是等到窗口内比较时抛 TypeError。

        否则会被主流程的宽 except 吞成 reason=exception,日志里看不出是配置写错了。
        """
        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        self._with_engine_config(
            tmp_path,
            monkeypatch,
            {"enabled": True, "user_enabled": True, "crop_min_area_ratio": "0.1"},
        )
        try:
            cfg = crop_enhance_config_from_settings()
            assert cfg.crop_min_area_ratio == CropEnhanceConfig().crop_min_area_ratio
            assert not (cfg.enabled and cfg.user_enabled)
        finally:
            reset_settings()

    def test_inverted_area_ratios_fall_back_to_defaults(self, tmp_path, monkeypatch):
        """min > max 的矛盾配置:每窗静默回退、日志只说 area_rejected,查不出根因。"""
        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        self._with_engine_config(
            tmp_path,
            monkeypatch,
            {"enabled": True, "crop_min_area_ratio": 0.6, "crop_max_area_ratio": 0.2},
        )
        try:
            cfg = crop_enhance_config_from_settings()
            assert cfg.crop_min_area_ratio == CropEnhanceConfig().crop_min_area_ratio
            assert cfg.crop_max_area_ratio == CropEnhanceConfig().crop_max_area_ratio
        finally:
            reset_settings()

    def test_unknown_key_ignored_not_raised(self, tmp_path, monkeypatch):
        """schema 演进 / 手写错键不该让整块配置作废(只过滤该键)。"""
        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        self._with_engine_config(
            tmp_path, monkeypatch, {"enabled": True, "no_such_key": 1}
        )
        try:
            assert crop_enhance_config_from_settings().enabled is True
        finally:
            reset_settings()

    def test_missing_block_inherits_shipped_defaults_and_still_does_not_crop(
        self, tmp_path, monkeypatch
    ):
        """整块缺失(老配置)→ 继承随包 settings.yaml 的默认值,而非 dataclass 默认值。

        随包 yaml 是合并的基础层,所以 ops 闸拿到的是产品默认 enabled=true(不是 dataclass
        的 False —— 后者只在 yaml 自身缺 key 时才生效,而我们随包就带着它)。真正保护老用户
        的是 user_enabled=false:**双闸相与,所以不裁**。断言落在这个行为不变量上,
        它不随 ops 闸的灰度策略变动。
        """
        import json

        from miloco.config.settings import reset_settings
        from miloco.perception.engine.omni.crop_enhance import (
            crop_enhance_config_from_settings,
        )

        monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
        monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"perception": {"engine": {"input": {"omni_fps": 1}}}}),
            encoding="utf-8",
        )
        reset_settings()
        try:
            cfg = crop_enhance_config_from_settings()
            assert cfg.user_enabled is False
            assert not (cfg.enabled and cfg.user_enabled)  # 双闸相与 → 老配置不会突然开始裁
        finally:
            reset_settings()

    def test_shipped_settings_yaml_keys_match_dataclass(self):
        """随包 settings.yaml 里 crop_enhance 的每个键都必须是 dataclass 字段。

        否则运维照着 yaml 改参数,值被 __dataclass_fields__ 过滤掉、静默不生效。
        """
        from pathlib import Path

        import yaml

        import miloco

        raw = yaml.safe_load(
            (Path(miloco.__file__).parent / "config" / "settings.yaml").read_text(
                encoding="utf-8"
            )
        )
        block = raw["perception"]["engine"]["crop_enhance"]
        unknown = set(block) - set(CropEnhanceConfig.__dataclass_fields__)
        assert not unknown, f"settings.yaml 里这些键不会被读到: {unknown}"
        assert block["enabled"] is True  # ops 闸已放开(团队决定)
        # 用户开关仍默认关:随包配置本身不能让任何存量用户开始裁切,必须由 UI/admin API 显式打开。
        assert block["user_enabled"] is False
