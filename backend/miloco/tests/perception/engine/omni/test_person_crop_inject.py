# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""单帧人像注入（omni/person_crop_inject.py）。

选帧口径、兜底、降级语义与「绝不抛」都在这里钉住。图用纯色帧构造，解回来看颜色即可判定
**选中的是哪一帧**——比断言尺寸更能区分「取了面积最大那帧」和「碰巧取了第一帧」。
"""

from __future__ import annotations

import base64
import re
from unittest.mock import patch

import cv2
import numpy as np
from miloco.perception.engine.config import PersonCropInjectConfig
from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
from miloco.perception.engine.omni import person_crop_inject as pci

_NOTE_HEAD = "【识别辅助】下方为每个待识别 track 的"


def _cfg(**kw):
    """替换热读配置（被测函数按模块全局名调用它，patch 模块属性即可）。"""
    return patch.object(
        pci, "person_crop_inject_config_from_settings",
        return_value=PersonCropInjectConfig(enabled=True, **kw),
    )


def _frame(fill: int, h: int = 480, w: int = 640):
    return np.full((h, w, 3), fill, dtype=np.uint8)


def _decode(block: dict) -> np.ndarray:
    """image_url 块 → BGR ndarray。顺带校验它确实是 PNG（无损，规格要求）。"""
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), url[:40]
    raw = base64.b64decode(url.split(",", 1)[1])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def _images(content: list[dict]) -> list[np.ndarray]:
    return [_decode(b) for b in content if b.get("type") == "image_url"]


def _build(candidates, frames, per_frame_boxes, **cfgkw):
    with _cfg(**cfgkw):
        return pci.build_person_crop_content(
            candidates=candidates, frames=frames, per_frame_boxes=per_frame_boxes,
        )


class TestGate:
    def test_disabled_injects_nothing(self):
        """关闸即完全不注入——止损开关必须真的能止损。"""
        with patch.object(
            pci, "person_crop_inject_config_from_settings",
            return_value=PersonCropInjectConfig(enabled=False),
        ):
            out = pci.build_person_crop_content(
                candidates=[IdentityQueryItem(track_id=1, body_crop=_frame(9, 100, 60))],
                frames=[_frame(9)], per_frame_boxes=[{1: (0, 0, 100, 200)}],
            )
        assert out == []

    def test_no_candidates_injects_nothing(self):
        assert _build([], [_frame(9)], [{1: (0, 0, 100, 200)}]) == []


class TestFrameSelection:
    def test_picks_largest_area_frame(self):
        """跨帧取**裁出面积最大**的那一帧，不是第一帧、也不是末帧。"""
        frames = [_frame(30), _frame(200), _frame(90)]
        boxes = [
            {1: (0, 0, 60, 60)},      # 面积最小
            {1: (0, 0, 220, 320)},    # 最大 → 应选中（fill=200）
            {1: (0, 0, 100, 120)},
        ]
        out = _build([IdentityQueryItem(track_id=1)], frames, boxes)
        imgs = _images(out)
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 200) <= 2

    def test_min_bbox_height_skips_small_boxes(self):
        """原生框高不足的帧不参与选帧——哪怕它是唯一的一帧。

        此处唯一的框高 30 < 40，逐帧路径应一张都取不出，从而落到 body_crop 兜底。
        """
        frames = [_frame(30)]
        boxes = [{1: (0, 0, 300, 30)}]  # 高 30 < min_bbox_height_px=40
        cand = IdentityQueryItem(track_id=1, body_crop=_frame(150, 100, 60))
        imgs = _images(_build([cand], frames, boxes))
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 150) <= 2  # 用的是 body_crop，不是那帧糊图

    def test_larger_but_too_short_box_loses_to_valid_one(self):
        """面积更大但框高不合格的帧，不能赢过面积较小但合格的帧。"""
        frames = [_frame(40), _frame(210)]
        boxes = [
            {1: (0, 0, 600, 35)},    # 面积 21000，但高 35 不合格
            {1: (0, 0, 80, 100)},    # 面积 8000，合格 → 应选中（fill=210）
        ]
        imgs = _images(_build([IdentityQueryItem(track_id=1)], frames, boxes))
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 210) <= 2


    def test_answer_moves_with_the_box_index(self):
        """把最大框换到另一个下标，选出的帧必须跟着换 —— 钉住"帧 j 配框 j"这层对应。

        若实现里帧与框的下标错开（上游下采只抽帧不抽框就出过这种事），赢家摆在正中间的用例
        会歪打正着；所以这里把赢家分别放在首位和末位各验一次。
        """
        frames = [_frame(11), _frame(22), _frame(33)]
        big, small = {1: (0, 0, 220, 320)}, {1: (0, 0, 60, 60)}
        first = _images(_build([IdentityQueryItem(track_id=1)], frames, [big, small, small]))
        last = _images(_build([IdentityQueryItem(track_id=1)], frames, [small, small, big]))
        assert abs(int(first[0].mean()) - 11) <= 2   # 大框在下标 0 → 取第 0 帧
        assert abs(int(last[0].mean()) - 33) <= 2    # 大框在下标 2 → 取第 2 帧

    def test_length_mismatch_gives_up_per_frame_path(self):
        """帧数与框数不等时整条逐帧路径放弃、退末帧兜底 —— **不**按较短者截断。

        截断只是换一种错位（用另一时刻的框裁这一帧），而图是绑 track_id 的。上游 omni 下采曾
        只抽帧不抽框，当时正是靠这里的静默截断把错位藏住、无任何日志。
        """
        frames = [_frame(11), _frame(22), _frame(33)]
        cand = IdentityQueryItem(track_id=1, body_crop=_frame(160, 100, 60))
        imgs = _images(_build([cand], frames, [{1: (0, 0, 220, 320)}]))  # 3 帧 vs 1 框
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 160) <= 2   # 用的是 body_crop 兜底

    def test_min_bbox_height_follows_config(self):
        """小框门槛跟随配置，不是硬编码的 40。"""
        frames = [_frame(70), _frame(230)]
        boxes = [{1: (0, 0, 600, 35)}, {1: (0, 0, 80, 100)}]
        # 门槛降到 30：面积更大的那帧（高 35）重新入选
        loose = _images(_build([IdentityQueryItem(track_id=1)], frames, boxes,
                               min_bbox_height_px=30))
        assert abs(int(loose[0].mean()) - 70) <= 2
        # 门槛抬到 120：两帧都不合格 → 无兜底 → 整段不注入
        assert _build([IdentityQueryItem(track_id=1)], frames, boxes,
                      min_bbox_height_px=120) == []


class TestFallbackAndDegrade:
    def test_falls_back_to_body_crop_without_per_frame_boxes(self):
        """拿不到逐帧框（mock 跟踪服务等）时用候选自带的末帧 body_crop。"""
        cand = IdentityQueryItem(track_id=7, body_crop=_frame(120, 200, 90))
        out = _build([cand], [_frame(30)], [])
        imgs = _images(out)
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 120) <= 2
        assert any("track_id=7" in b.get("text", "") for b in out)

    def test_skips_only_the_track_that_has_nothing(self):
        """单个 track 裁不出图只跳过它自己——与 gallery 的「全或无」相反。"""
        ok = IdentityQueryItem(track_id=1, body_crop=_frame(180, 100, 60))
        bad = IdentityQueryItem(track_id=2)  # 无逐帧框、无 body_crop
        out = _build([ok, bad], [], [])
        assert len(_images(out)) == 1
        texts = " ".join(b.get("text", "") for b in out)
        assert "track_id=1" in texts
        assert "track_id=2" not in texts

    def test_all_tracks_unusable_yields_nothing(self):
        """全都裁不出 → 整段不注入（连说明块也不留，避免悬空的「下方为…」）。"""
        out = _build([IdentityQueryItem(track_id=1), IdentityQueryItem(track_id=2)], [], [])
        assert out == []


class TestSpec:
    def test_one_image_per_track_in_candidate_order(self):
        """每个 track 恰好一张，且顺序与 candidates 一致（对齐「待识别 track」文本段）。"""
        cands = [
            IdentityQueryItem(track_id=3, body_crop=_frame(60, 100, 60)),
            IdentityQueryItem(track_id=1, body_crop=_frame(60, 100, 60)),
            IdentityQueryItem(track_id=2, body_crop=_frame(60, 100, 60)),
        ]
        out = _build(cands, [], [])
        assert len(_images(out)) == 3
        order = [b["text"] for b in out if b.get("type") == "text" and "track_id=" in b["text"]]
        assert order == [
            "待识别 track_id=3 的外观单帧：",
            "待识别 track_id=1 的外观单帧：",
            "待识别 track_id=2 的外观单帧：",
        ]

    def test_note_is_emitted_once_and_first(self):
        cands = [IdentityQueryItem(track_id=i, body_crop=_frame(60, 100, 60)) for i in (1, 2)]
        out = _build(cands, [], [])
        notes = [b for b in out if b.get("type") == "text" and _NOTE_HEAD in b["text"]]
        assert len(notes) == 1
        assert out[0] is notes[0]

    def test_note_wording_is_verbatim(self):
        """文案是已验证配置的一部分（去掉 track_id 绑定的对照臂显著变差），逐字钉住。

        整串相等防任何改写；另外单挑两处**承重**措辞，让它们即便在整串被重排时也不会悄悄丢：
        「上方 gallery」是插入位的语义前提（人像必须在 gallery 之后，否则这句是事实错误），
        「identity_assignments 时照旧用 track_id」是输出契约。
        """
        expected = "【识别辅助】下方为每个待识别 track 的“外观单帧”：从本段视频中裁出的该 track 最大最清晰的一帧。请优先把每个 track 的外观单帧与上方 gallery 成员参考图逐一比对来判定身份；track 的 bbox 数字坐标仍可用于在视频画面中交叉核对位置。输出 identities 时照旧用 track_id 数字。"
        assert pci._INJECT_NOTE == expected
        assert "与上方 gallery 成员参考图" in pci._INJECT_NOTE
        assert "输出 identities 时照旧用 track_id 数字" in pci._INJECT_NOTE
        assert "\n" not in pci._INJECT_NOTE  # 别为源码行宽在句中断行

    def test_normalized_to_configured_height(self):
        """归一高度是硬保证：宽高比再离谱也不能被 768 宽帽连高一起缩。"""
        wide = np.full((40, 900, 3), 77, dtype=np.uint8)  # 22.5:1，必然触发宽帽
        out = _build([IdentityQueryItem(track_id=1, body_crop=wide)], [], [])
        img = _images(out)[0]
        assert img.shape[0] == 256

    def test_height_follows_config(self):
        out = _build(
            [IdentityQueryItem(track_id=1, body_crop=_frame(60, 100, 60))], [], [],
            crop_height=128,
        )
        assert _images(out)[0].shape[0] == 128

    def test_padding_expands_box(self):
        """抠图外扩 5%：框 100×200 应裁出 110×220（未触边）。"""
        frames = [_frame(50, 800, 800)]
        boxes = [{1: (300, 300, 400, 500)}]  # w=100 h=200
        with _cfg():
            crop = pci._pick_largest_crop(frames, boxes, 1, 40)
        assert crop.shape[:2] == (220, 110)


class TestNeverRaises:
    def test_garbage_boxes_degrade_to_empty(self):
        """逐帧热路径的硬约束：payload 构造抛异常会被上游折成整相机本窗 skipped。"""
        out = _build([IdentityQueryItem(track_id=1)], [_frame(9, 10, 10)], [{1: ("a", "b", "c", "d")}])
        assert out == []

    def test_bad_config_read_degrades_to_empty(self):
        with patch.object(
            pci, "person_crop_inject_config_from_settings", side_effect=RuntimeError("boom"),
        ):
            assert pci.build_person_crop_content(
                candidates=[IdentityQueryItem(track_id=1, body_crop=_frame(60, 100, 60))],
                frames=[], per_frame_boxes=[],
            ) == []


class TestConfigFromSettings:
    def test_non_mapping_fails_closed(self):
        """config.json 里手写 `"person_crop_inject": true` 这类结构错必须退成关闸，不能抛。"""
        fake = type("S", (), {"perception": type("P", (), {"engine": {"person_crop_inject": True}})()})()
        with patch("miloco.config.get_settings", return_value=fake):
            assert pci.person_crop_inject_config_from_settings().enabled is False

    def test_unknown_keys_filtered(self):
        fake = type("S", (), {"perception": type("P", (), {
            "engine": {"person_crop_inject": {"enabled": True, "crop_height": 320, "nope": 1}}
        })()})()
        with patch("miloco.config.get_settings", return_value=fake):
            cfg = pci.person_crop_inject_config_from_settings()
        assert cfg.enabled is True and cfg.crop_height == 320

    def test_non_bool_gate_fails_closed(self):
        fake = type("S", (), {"perception": type("P", (), {
            "engine": {"person_crop_inject": {"enabled": "yes"}}
        })()})()
        with patch("miloco.config.get_settings", return_value=fake):
            assert pci.person_crop_inject_config_from_settings().enabled is False


class TestShippedDefault:
    def test_shipped_settings_enable_injection(self):
        """随包默认是**开**的。

        这条单独钉住，是为了让上面那些 `test_not_injected_*` 的阴性结论有意义 —— 若哪天把随包
        默认改成关，失败会定位到这一条并说明原因，而不是让一堆阴性用例"照样绿"地失去判别力。
        """
        from miloco.config import get_settings

        raw = get_settings().perception.engine.get("person_crop_inject", {})
        assert raw.get("enabled") is True
        cfg = pci.person_crop_inject_config_from_settings()
        assert cfg.enabled is True
        assert cfg.crop_height == 256
        assert cfg.min_bbox_height_px == 40


class TestConfigNumericValidation:
    """数值字段的 fail-closed。

    这些字段来自用户可写的 config.json。类型/范围错时若放行，异常要到 cv2.resize 才抛，而本模块
    外层有宽 except → 被吞成"整段不注入"，线上表现为"召回悄悄回落到基线"，日志里看不出根因是
    配置写错。所以必须在读配置这一步就拦住并报出字段名。同 crop_enhance 的口径。
    """

    @staticmethod
    def _cfg_from(raw):
        fake = type("S", (), {"perception": type("P", (), {
            "engine": {"person_crop_inject": raw}
        })()})()
        with patch("miloco.config.get_settings", return_value=fake):
            return pci.person_crop_inject_config_from_settings()

    def test_string_number_fails_closed(self):
        cfg = self._cfg_from({"enabled": True, "crop_height": "256"})
        assert cfg.enabled is False

    def test_bool_as_number_fails_closed(self):
        """bool 是 int 的子类，True 会当 1 通过朴素的 isinstance 检查。"""
        cfg = self._cfg_from({"enabled": True, "crop_height": True})
        assert cfg.enabled is False

    def test_non_positive_height_fails_closed(self):
        for h in (0, -1):
            assert self._cfg_from({"enabled": True, "crop_height": h}).enabled is False

    def test_negative_min_height_fails_closed(self):
        cfg = self._cfg_from({"enabled": True, "min_bbox_height_px": -5})
        assert cfg.enabled is False

    def test_valid_numbers_pass(self):
        cfg = self._cfg_from({"enabled": True, "crop_height": 320, "min_bbox_height_px": 20})
        assert cfg.enabled is True and cfg.crop_height == 320 and cfg.min_bbox_height_px == 20


class TestTermConsistency:
    def test_label_reuses_the_term_from_the_note(self):
        """绑定文本里的名词必须是说明块里定义过的那一个。

        说明块用「的“X”：」给这个概念下定义，绑定文本再引用它。两处若不一致，模型会看到两个名字
        （说明块讲 A、图前标 B），指代就断了。而两处各有各的字面量断言，改一处忘改另一处时谁也发现
        不了 —— 这条从说明块里**反解**出名词再去校验绑定文本，把这个空档堵上。
        """
        term = re.search(r'的“(.+?)”：', pci._INJECT_NOTE).group(1)
        out = _build([IdentityQueryItem(track_id=1, body_crop=_frame(60, 100, 60))], [], [])
        label = next(b["text"] for b in out if "track_id=1" in b.get("text", ""))
        assert term in label, f"说明块定义的是「{term}」，绑定文本却写成「{label}」"

    def test_note_defines_the_term_in_quotes(self):
        """说明块必须保留「的“X”：」这个下定义的形式 —— 上面那条守卫依赖它才能反解出名词。"""
        assert re.search(r'的“(.+?)”：', pci._INJECT_NOTE) is not None

    def test_label_carries_no_frame_count(self):
        """绑定文本不带数量括注。

        「单帧」已经说明了是一帧，再括注「（1 帧）」是显式重言；那是评测 harness 里多帧拼图臂与
        单帧臂共用一个 f-string 的残留（拼图臂标「（4 帧）」是真信息）。另外本 prompt 里既有的五个
        图块标签一律不标数量，括注一个计数还会让"帧数"看起来像可变维度或序号槽。
        """
        out = _build([IdentityQueryItem(track_id=1, body_crop=_frame(60, 100, 60))], [], [])
        label = next(b["text"] for b in out if "track_id=1" in b.get("text", ""))
        # 只断言"没有数量括注"这一件事：整串相等由 test_one_image_per_track_in_candidate_order
        # 负责，名词一致性由上面那条负责。一条测试只为一件事变红，红了才指得准方向。
        assert not re.search(r"[（(]\s*\d+\s*帧\s*[）)]", label), label


class TestFallbackHeightGate:
    """末帧兜底同样要过最小框高这道闸。

    不过的话，这道闸恰好在**最需要它的窗口**（全窗都是小框）失效：逐帧路径把小框全滤掉、返回
    None，兜底无条件接管同一个小框在末帧裁出的图，再放大到归一高度送进去——清晰度一点没变，
    净效果只是把选帧从「窗内最大」降级成「末帧」，比不做这个过滤更差。
    """

    def test_small_body_crop_also_blocked(self):
        """全窗小框 + 兜底图也小 → 整段不注入（该 track 落进 skipped）。"""
        frames = [_frame(40)]
        boxes = [{1: (0, 0, 300, 30)}]                    # 原生框高 30 < 40
        small = np.full((33, 20, 3), 90, dtype=np.uint8)  # 兜底图 33px，也不过闸
        assert _build([IdentityQueryItem(track_id=1, body_crop=small)], frames, boxes) == []

    def test_tall_body_crop_still_used(self):
        """反向守卫：人够大时兜底照旧生效，别因为加了闸把正常兜底路径也堵死。"""
        frames = [_frame(40)]
        boxes = [{1: (0, 0, 300, 30)}]
        imgs = _images(_build(
            [IdentityQueryItem(track_id=1, body_crop=_frame(170, 200, 90))], frames, boxes))
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 170) <= 2

    def test_gate_follows_config(self):
        """闸用的是配置值，不是硬编码 40。"""
        frames, boxes = [_frame(40)], [{1: (0, 0, 300, 30)}]
        def cand():
            return IdentityQueryItem(track_id=1, body_crop=_frame(120, 60, 40))  # 兜底图 60px 高

        assert len(_images(_build([cand()], frames, boxes, min_bbox_height_px=50))) == 1
        assert _build([cand()], frames, boxes, min_bbox_height_px=80) == []

    def test_other_fallback_paths_unaffected_when_person_is_large(self):
        """闸只看图的高度，不看为什么走到兜底。

        兜底有三条触发路径（无逐帧框 / 全窗 coasting / 全窗小框）。人够大时三条都该照常兜底——
        这里用「压根没有逐帧框」这条验证，确认加闸没有连带堵掉它。
        """
        imgs = _images(_build(
            [IdentityQueryItem(track_id=1, body_crop=_frame(200, 150, 70))], [], []))
        assert len(imgs) == 1
        assert abs(int(imgs[0].mean()) - 200) <= 2
