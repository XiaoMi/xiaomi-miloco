# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P4b：pet:observe 选 crop + omni 编排（mock detector + 真 SortTracker + mock omni）。

不依赖真 onnx：detector 用 mock（返回 Detection 列表）；omni 用 monkeypatch 桩。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity.tracker.detector import Detection
from miloco.pet import observe as obs


def _frame(h=200, w=200):
    # 噪声帧：compute_sharpness > 0，使评分有意义
    return np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)


def _det(x, y, w, h, conf=0.9, cls=Detection.CLASS_CAT) -> Detection:
    return Detection(x=x, y=y, w=w, h=h, confidence=conf, class_id=cls)


# ── 单图选最大 crop ──────────────────────────────────────────────────────────


def test_largest_pet_crop_picks_biggest():
    frame = _frame()
    detector = SimpleNamespace(
        detect_pets=lambda f: [_det(5, 5, 20, 20), _det(40, 40, 80, 80)]
    )
    out = obs._largest_pet_crop(frame, detector)
    assert out is not None
    assert out["class_id"] == Detection.CLASS_CAT
    # 取了大框（约 80×80 + 扩边），明显大于小框 20×20
    assert out["crop"].shape[0] >= 80 and out["crop"].shape[1] >= 80


def test_largest_pet_crop_none_when_no_pets():
    detector = SimpleNamespace(detect_pets=lambda f: [])
    assert obs._largest_pet_crop(_frame(), detector) is None


def test_largest_pet_crop_ignores_non_pet_class():
    # 即便 detect_pets 误带 HUMAN，也按宠物类过滤
    detector = SimpleNamespace(
        detect_pets=lambda f: [_det(10, 10, 50, 50, cls=Detection.CLASS_HUMAN)]
    )
    assert obs._largest_pet_crop(_frame(), detector) is None


# ── 视频：SORT 分 track + 每 track 选优 ──────────────────────────────────────


def test_video_single_pet_one_track():
    frames = [(i, _frame(300, 300)) for i in range(8)]
    detector = SimpleNamespace(detect_pets=lambda f: [_det(50, 50, 60, 60)])
    crops = obs._video_pet_crops(frames, detector, fps=1)
    assert len(crops) >= 1
    assert crops[0]["class_id"] == Detection.CLASS_CAT
    assert crops[0]["crop"].size > 0


def test_video_multi_pet_picks_dominant_track():
    # 两只相距很远的宠物 → SORT 分两 track；新语义只取主体 track（不再一 track 一候选）
    frames = [(i, _frame(400, 400)) for i in range(8)]
    detector = SimpleNamespace(
        detect_pets=lambda f: [
            _det(20, 20, 60, 60, cls=Detection.CLASS_CAT),
            _det(300, 300, 60, 60, cls=Detection.CLASS_DOG),
        ]
    )
    selected, n_tracks = obs._select_video_crops(frames, detector, fps=1)
    assert n_tracks == 2  # 检测到两只（供 observe_pet 出 multiple_pets 提示）
    assert 1 <= len(selected) <= 3  # 只取主体 track 的 ≤3 张
    assert len({c["class_id"] for c in selected}) == 1  # 同一 track → 物种单一


def test_gate_select_hard_exclude_returns_empty():
    # 全部被硬排除（这里用 crowded 标记）→ 返回 []（退纯描述，绝不放宽）
    cands = [
        {"crop": _frame(40, 40), "conf": 0.9, "sharpness": 999.0, "area_ratio": 0.3,
         "class_id": Detection.CLASS_CAT, "crowded": True},
        {"crop": _frame(40, 40), "conf": 0.9, "sharpness": 999.0, "area_ratio": 0.001,
         "class_id": Detection.CLASS_CAT, "crowded": False},  # 面积过小
    ]
    assert obs._gate_select(cands) == []


def test_gate_select_dedups_and_caps_at_3():
    # 5 张过门槛的候选 → gate_score 排序 + dHash 多样性 → ≤3
    cands = [
        {"crop": _frame(60, 60), "conf": 0.9, "sharpness": 800.0 + i, "area_ratio": 0.2,
         "class_id": Detection.CLASS_CAT, "crowded": False}
        for i in range(5)
    ]
    out = obs._gate_select(cands)
    assert len(out) <= 3
    assert all("gate_score" in c for c in out)  # 已打加权分


# ── omni 编排 ────────────────────────────────────────────────────────────────


def _stub_omni(monkeypatch, payload_holder=None, *, with_head=False):
    desc = {
        "species": "猫",
        "breed": "",
        "size_build": "中等体型",
        "coat_color_pattern": "黑色",
        "coat_length_texture": "短毛",
        "distinctive_markings": ["尾尖一撮白"],
        "accessories": "",
        "summary": "中等体型的黑色短毛猫，尾巴尖有一撮白毛",
    }
    if with_head:
        desc["head_bbox"] = [0.3, 0.1, 0.4, 0.4]
    content = json.dumps(desc, ensure_ascii=False)

    async def _fake_call_omni(payload, config, type="realtime"):
        if payload_holder is not None:
            payload_holder["payload"] = payload
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(obs, "call_omni", _fake_call_omni)


@pytest.mark.asyncio
async def test_omni_describe_parses_description(monkeypatch):
    holder = {}
    _stub_omni(monkeypatch, holder)
    # 合并后签名：入参 crops:list，返回 (dict, head_bboxes:list, refs_inconsistent, body_bbox)
    desc, head_bboxes, refs, body_bbox = await obs._omni_describe(
        [_frame()], grounding=False
    )
    assert desc["species"] == "猫"
    assert desc["summary"].startswith("中等体型")
    assert head_bboxes == [None]  # grounding 关 → 每 crop 对齐一个 None
    assert refs is None  # 单图无 refs_inconsistent
    assert body_bbox is None  # 未开 body_grounding
    # crop 以 image/jpeg 进 payload 的 crops
    assert holder["payload"]["crops"][0]["media_type"] == "image/jpeg"
    # 单图不含 multi 片段（与改造前逐字节等价）：grounding/whole_frame/multi/body 全关 → 恰是基础 prompt
    assert holder["payload"]["user_content"] == obs._SINGLE_USER_CONTENT
    assert holder["payload"]["system_prompt"] == obs.OBSERVE_SYSTEM_PROMPT
    assert "head_bbox" not in obs.OBSERVE_SYSTEM_PROMPT  # 关闭时 prompt 不含 grounding 段


@pytest.mark.asyncio
async def test_omni_describe_grounding_returns_head_bbox(monkeypatch):
    _stub_omni(monkeypatch, with_head=True)
    desc, head_bboxes, refs, body_bbox = await obs._omni_describe(
        [_frame()], grounding=True
    )
    assert head_bboxes == [[0.3, 0.1, 0.4, 0.4]]  # 单图 → [单框]
    assert refs is None
    assert body_bbox is None
    assert "head_bbox" not in desc  # head_bbox 已从 description 弹出


@pytest.mark.asyncio
async def test_omni_describe_multi_common_description(monkeypatch):
    """多图（≥2 crop）→ 一次调用、一条共性描述 + 每图 head_bbox + refs_inconsistent。"""
    holder = {}

    async def _fake_call_omni(payload, config, type="realtime"):
        holder["payload"] = payload
        desc = {
            "species": "猫",
            "summary": "黑色短毛猫，尾尖一撮白",
            "distinctive_markings": ["尾尖一撮白"],
            "head_bboxes": [[0.3, 0.1, 0.4, 0.4], None],  # 第二张头部不可见
            "refs_inconsistent": True,
        }
        return {"choices": [{"message": {"content": json.dumps(desc, ensure_ascii=False)}}]}

    monkeypatch.setattr(obs, "call_omni", _fake_call_omni)
    desc, head_bboxes, refs, _body = await obs._omni_describe(
        [_frame(), _frame()], grounding=True
    )
    # 一次调用送 2 张
    assert len(holder["payload"]["crops"]) == 2
    assert holder["payload"]["user_content"] == obs._MULTI_USER_CONTENT
    assert "同一只" in holder["payload"]["system_prompt"]  # 走了 multi 片段
    assert head_bboxes == [[0.3, 0.1, 0.4, 0.4], None]  # 与 crops 对齐
    assert refs is True
    assert "head_bboxes" not in desc and "refs_inconsistent" not in desc  # 已弹出


# ── observe_pet 端到端（mock detector + mock omni）──────────────────────────


@pytest.mark.asyncio
async def test_observe_pet_image_detected(monkeypatch):
    _stub_omni(monkeypatch)
    monkeypatch.setattr(
        obs,
        "default_detector",
        lambda: SimpleNamespace(detect_pets=lambda f: [_det(20, 20, 80, 80)]),
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet([buf.tobytes()], is_video=False, grounding=False)
    assert res["detected"] is True
    assert res["description"]["species"] == "猫"
    assert res["primary_crop_b64"]
    assert len(res["candidates"]) == 1


@pytest.mark.asyncio
async def test_observe_pet_fallback_when_detector_misses(monkeypatch):
    # 检测器框不到猫/狗（兔/鸟/仓鼠等其他物种，或角度/遮挡）→ 回退整幅画面交 omni，仍出描述
    holder = {}
    _stub_omni(monkeypatch, holder)
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet([buf.tobytes()], is_video=False, grounding=False)
    assert res["detected"] is True
    assert res["description"]["species"]  # omni 回填了物种
    assert res["primary_crop_b64"]  # 整幅画面作 crop
    assert res["candidates"] == []  # 回退无检测器候选
    assert "整幅画面" in holder["payload"]["system_prompt"]  # 走了整幅画面 prompt 段


@pytest.mark.asyncio
async def test_observe_pet_fallback_body_grounding_crops_body(monkeypatch):
    # 回退路径（框不到猫狗）+ omni 框出本体 → 裁本体作 1 张参考 crop（D7；非猫狗物种也有参考图）
    async def _fake(payload, config, type="realtime"):
        desc = {"species": "兔", "summary": "白色垂耳兔", "body_bbox": [0.2, 0.2, 0.5, 0.5]}
        return {"choices": [{"message": {"content": json.dumps(desc, ensure_ascii=False)}}]}

    monkeypatch.setattr(obs, "call_omni", _fake)
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    ok, buf = cv2.imencode(".jpg", _frame(200, 200))
    res = await obs.observe_pet(
        [buf.tobytes()], is_video=False, grounding=False, body_grounding=True
    )
    assert res["detected"] is True
    assert len(res["candidates"]) == 1  # 本体 crop 作参考
    assert res["candidates"][0]["species_guess"] == "其他"  # 非检测器候选
    assert res["candidates"][0]["bbox"] == [40, 40, 100, 100]  # 0.2/0.2/0.5/0.5 × 200
    assert res["primary_crop_b64"]
    assert "body_bbox" not in res["description"]  # 已从描述弹出、不污染落库


@pytest.mark.asyncio
async def test_observe_pet_fallback_body_grounding_off_no_crop(monkeypatch):
    # body_grounding=False → 即便 omni 多嘴回了 body_bbox 也不裁、不产参考 crop（走整幅回退）
    async def _fake(payload, config, type="realtime"):
        desc = {"species": "兔", "summary": "白色垂耳兔", "body_bbox": [0.2, 0.2, 0.5, 0.5]}
        return {"choices": [{"message": {"content": json.dumps(desc, ensure_ascii=False)}}]}

    monkeypatch.setattr(obs, "call_omni", _fake)
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    ok, buf = cv2.imencode(".jpg", _frame(200, 200))
    res = await obs.observe_pet(
        [buf.tobytes()], is_video=False, grounding=False, body_grounding=False
    )
    assert res["detected"] is True
    assert res["candidates"] == []  # 未开 body grounding → 不裁本体
    assert "body_bbox" not in res["description"]  # 始终从描述弹出


@pytest.mark.asyncio
async def test_observe_pet_no_animal_at_all(monkeypatch):
    # 检测器空 + omni 也判定画面无动物（species/summary 皆空）→ detected=False
    async def _empty_omni(payload, config, type="realtime"):
        return {"choices": [{"message": {"content": '{"species":"","summary":""}'}}]}

    monkeypatch.setattr(obs, "call_omni", _empty_omni)
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet([buf.tobytes()], is_video=False, grounding=False)
    assert res["detected"] is False
    assert res["description"] is None
    assert res["candidates"] == []


# ── P0 契约：候选质量分（conf/sharpness/area_ratio/bbox/frame_idx，零额外 omni）──


def test_largest_pet_crop_has_quality_fields():
    detector = SimpleNamespace(detect_pets=lambda f: [_det(20, 20, 80, 80, conf=0.9)])
    out = obs._largest_pet_crop(_frame(160, 160), detector)
    assert out is not None
    assert 0.0 <= out["conf"] <= 1.0
    assert out["sharpness"] >= 0.0
    assert 0.0 < out["area_ratio"] <= 1.0
    assert list(out["bbox"]) == [20, 20, 80, 80]
    assert out["frame_idx"] is None  # 单图无帧序号


def test_video_crop_has_quality_fields():
    frames = [(i, _frame(300, 300)) for i in range(6)]
    detector = SimpleNamespace(detect_pets=lambda f: [_det(50, 50, 60, 60)])
    crops = obs._video_pet_crops(frames, detector, fps=1)
    assert crops
    c = crops[0]
    assert 0.0 <= c["conf"] <= 1.0
    assert c["sharpness"] >= 0.0
    assert 0.0 < c["area_ratio"] <= 1.0
    assert len(c["bbox"]) == 4
    assert isinstance(c["frame_idx"], int)  # 视频帧序号为整数


def test_candidate_out_exposes_quality_without_score():
    c = {
        "track_id": 3,
        "class_id": Detection.CLASS_DOG,
        "crop": _frame(40, 40),
        "score": 999.0,  # 内部临时分，不应外露
        "conf": 0.8,
        "sharpness": 12.3,
        "area_ratio": 0.25,
        "bbox": (10, 20, 30, 40),
        "frame_idx": 5,
    }
    out = obs._candidate_out(c)
    assert out["species_guess"] == "狗"
    assert out["conf"] == 0.8
    assert out["sharpness"] == 12.3
    assert out["area_ratio"] == 0.25
    assert out["bbox"] == [10, 20, 30, 40]  # tuple → list
    assert out["frame_idx"] == 5
    assert "score" not in out  # 内部分不进对外候选契约
    assert out["crop_b64"]


@pytest.mark.asyncio
async def test_observe_pet_image_candidate_quality(monkeypatch):
    _stub_omni(monkeypatch)
    monkeypatch.setattr(
        obs,
        "default_detector",
        lambda: SimpleNamespace(detect_pets=lambda f: [_det(20, 20, 80, 80)]),
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet([buf.tobytes()], is_video=False, grounding=False)
    cand = res["candidates"][0]
    for k in ("conf", "sharpness", "area_ratio", "bbox", "frame_idx"):
        assert k in cand
    assert 0.0 <= cand["conf"] <= 1.0
    assert 0.0 < cand["area_ratio"] <= 1.0
    assert cand["frame_idx"] is None
    assert "score" not in cand  # 对外候选不含内部分


# ── P1a-A 新契约：多图 / primary_index / warnings / head_bbox ──────────────────


def _multi_det(cls=Detection.CLASS_CAT):
    return SimpleNamespace(detect_pets=lambda f: [_det(20, 20, 90, 90, cls=cls)])


@pytest.mark.asyncio
async def test_observe_pet_result_has_new_contract_keys(monkeypatch):
    _stub_omni(monkeypatch)
    monkeypatch.setattr(obs, "default_detector", lambda: _multi_det())
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet([buf.tobytes()], is_video=False, grounding=False)
    for k in ("primary_index", "refs_inconsistent", "warnings"):
        assert k in res
    assert res["primary_index"] == 0
    assert res["refs_inconsistent"] is None  # 单图无
    assert isinstance(res["warnings"], list)
    assert res["candidates"][0]["head_bbox"] is None  # grounding 关 → None


@pytest.mark.asyncio
async def test_observe_pet_multi_image_one_description(monkeypatch):
    # 3 张图 → 一次 describe（不逐张）、candidates 三张、primary_index=0
    calls = {"n": 0}

    async def _fake_call_omni(payload, config, type="realtime"):
        calls["n"] += 1
        calls["crops"] = len(payload["crops"])
        desc = {"species": "猫", "summary": "黑色短毛猫，尾尖一撮白",
                "distinctive_markings": ["尾尖一撮白"]}
        return {"choices": [{"message": {"content": json.dumps(desc, ensure_ascii=False)}}]}

    monkeypatch.setattr(obs, "call_omni", _fake_call_omni)
    monkeypatch.setattr(obs, "default_detector", lambda: _multi_det())
    imgs = [cv2.imencode(".jpg", _frame(160, 160))[1].tobytes() for _ in range(3)]
    res = await obs.observe_pet(imgs, is_video=False, grounding=False)
    assert calls["n"] == 1  # 一次成型：只调 1 次 omni
    assert calls["crops"] == 3  # 三张一起送
    assert len(res["candidates"]) == 3
    assert res["primary_index"] == 0
    assert res["detected"] is True


def test_build_warnings_species_mismatch_and_generic():
    # detector 判猫、描述判狗 → species_mismatch；无区分特征 → generic_look
    selected = [{"class_id": Detection.CLASS_CAT}]
    desc = {"species": "狗", "distinctive_markings": [], "breed": "", "accessories": ""}
    types = {w["type"] for w in obs._build_warnings(desc, selected, None, 1)}
    assert "species_mismatch" in types
    assert "generic_look" in types


def test_build_warnings_refs_inconsistent_and_multiple_pets():
    desc = {"species": "猫", "distinctive_markings": ["尾尖白"], "breed": "", "accessories": ""}
    selected = [{"class_id": Detection.CLASS_CAT}]
    types = {w["type"] for w in obs._build_warnings(desc, selected, True, 2)}
    assert "refs_inconsistent" in types
    assert "multiple_pets" in types
    assert "generic_look" not in types  # 有区分性花纹 → 不报大众脸


def test_build_warnings_species_alias_no_false_positive():
    # 描述"猫咪"与检测"猫"经别名归一化后一致 → 不误报 species_mismatch
    selected = [{"class_id": Detection.CLASS_CAT}]
    desc = {"species": "猫咪", "distinctive_markings": ["尾尖白"], "breed": "", "accessories": ""}
    types = {w["type"] for w in obs._build_warnings(desc, selected, None, 1)}
    assert "species_mismatch" not in types


# ── 复核补测（对抗验证 workflow findings）──────────────────────────────────────


def test_gate_select_backfills_near_duplicates_without_crash():
    # 回归：近重复帧（同图 4 份）→ dHash 只留 1 → 触发 top-up 回填（曾因 `c not in out` 比 ndarray 崩溃）
    shared = _frame(60, 60)  # 高频噪声 → sharpness 高、过硬门槛；内容相同 → dHash 相同
    cands = [
        {
            "crop": shared.copy(),
            "conf": 0.9,
            "sharpness": 800.0,
            "area_ratio": 0.2,
            "class_id": Detection.CLASS_CAT,
            "crowded": False,
        }
        for _ in range(4)
    ]
    out = obs._gate_select(cands)
    assert len(out) == 3  # 回填到 k=3、不抛 ValueError


def test_box_crowded_overlap_gate():
    assert obs._box_crowded((0, 0, 10, 10), [], 0.10) is False  # 无其它框
    assert obs._box_crowded((0, 0, 10, 10), [(100, 100, 110, 110)], 0.10) is False  # 不相交
    assert obs._box_crowded((0, 0, 100, 100), [(0, 0, 90, 90)], 0.10) is True  # 大幅重叠


def test_align_head_bboxes():
    assert obs._align_head_bboxes(None, 2) == [None, None]
    assert obs._align_head_bboxes([[0, 0, 1, 1]], 2) == [[0, 0, 1, 1], None]  # 补齐
    assert obs._align_head_bboxes(
        [[0, 0, 1, 1], [0, 0, 1, 1], [0, 0, 1, 1]], 2
    ) == [[0, 0, 1, 1], [0, 0, 1, 1]]  # 截断
    assert obs._align_head_bboxes(["bad", [0, 0, 1, 1]], 2) == [None, [0, 0, 1, 1]]  # 非法项→None
    assert obs._align_head_bboxes([[0, 1]], 1) == [None]  # 长度不对→None


def test_omni_config_for_token_bump():
    assert obs._omni_config_for(1).max_completion_tokens == 512  # 单图默认（字节等价锚点）
    assert obs._omni_config_for(2).max_completion_tokens == 640
    assert obs._omni_config_for(3).max_completion_tokens == 768


def test_crop_from_norm_bbox_edges():
    f = _frame(100, 100)
    assert obs._crop_from_norm_bbox(f, None) == (None, None)
    assert obs._crop_from_norm_bbox(f, [0.1, 0.2]) == (None, None)  # 非 4 元素
    assert obs._crop_from_norm_bbox(f, [0.5, 0.5, 0.0, 0.5]) == (None, None)  # w=0
    # 越界 → 夹紧，像素框不越界（area_ratio 才不会 >1）
    crop, px = obs._crop_from_norm_bbox(f, [0.5, 0.5, 0.9, 0.9])  # x+w=1.4 → 夹到 w=0.5
    assert crop is not None
    x, y, w, h = px
    assert x + w <= 100 and y + h <= 100


def test_video_dominant_track_is_longer_lived():
    # 猫贯穿全片、狗只闪 1 帧 → 主体应是猫（帧数多者胜）
    calls = {"n": 0}

    def _dets(f):
        calls["n"] += 1
        cat = _det(20, 20, 60, 60, cls=Detection.CLASS_CAT)
        if calls["n"] == 1:  # 仅首次出现狗
            return [cat, _det(320, 320, 60, 60, cls=Detection.CLASS_DOG)]
        return [cat]

    frames = [(i, _frame(400, 400)) for i in range(8)]
    selected, _n = obs._select_video_crops(frames, SimpleNamespace(detect_pets=_dets), fps=1)
    assert selected
    assert {c["class_id"] for c in selected} == {Detection.CLASS_CAT}  # 主体=猫


def test_build_warnings_dog_alias_and_cross_mismatch():
    dog = [{"class_id": Detection.CLASS_DOG}]
    for sp in ("犬类", "狗狗", "犬"):  # 归一化 == 狗 → 不误报
        desc = {"species": sp, "distinctive_markings": ["x"], "breed": "", "accessories": ""}
        assert "species_mismatch" not in {
            w["type"] for w in obs._build_warnings(desc, dog, None, 1)
        }
    # 检测猫、描述犬类 → 真不一致 → 报（覆盖非漏报方向）
    cat = [{"class_id": Detection.CLASS_CAT}]
    desc = {"species": "犬类", "distinctive_markings": ["x"], "breed": "", "accessories": ""}
    assert "species_mismatch" in {w["type"] for w in obs._build_warnings(desc, cat, None, 1)}


def test_build_warnings_generic_suppressed_by_breed_or_accessories():
    sel = [{"class_id": Detection.CLASS_CAT}]
    d_breed = {"species": "猫", "distinctive_markings": [], "breed": "英短", "accessories": ""}
    assert "generic_look" not in {w["type"] for w in obs._build_warnings(d_breed, sel, None, 1)}
    d_acc = {"species": "猫", "distinctive_markings": [], "breed": "", "accessories": "红色项圈"}
    assert "generic_look" not in {w["type"] for w in obs._build_warnings(d_acc, sel, None, 1)}


@pytest.mark.asyncio
async def test_observe_video_multiple_pets_surfaced_when_gated_out(monkeypatch):
    # 回归：视频多 track（n=2）但主体被门控全灭 → 落回退，仍要透出 multiple_pets（别静默丢）
    _stub_omni(monkeypatch)  # omni 回填物种 → has_animal
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    monkeypatch.setattr(obs, "_decode_and_sample", lambda b, mf: ([(0, _frame(200, 200))], 1))
    monkeypatch.setattr(obs, "_select_video_crops", lambda *a, **k: ([], 2))  # 空选中、2 track
    res = await obs.observe_pet([b"fakevideo"], is_video=True, grounding=False)
    assert res["detected"] is True
    assert res["candidates"] == []  # 门控全灭 → 无参考 crop
    assert "multiple_pets" in {w["type"] for w in res["warnings"]}  # 仍透出多宠提示
