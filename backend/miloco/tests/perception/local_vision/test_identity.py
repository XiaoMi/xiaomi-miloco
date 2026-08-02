"""本地认人层。

这一层的价值全在**边界**上,不在顺利路径上:顺利路径无非「算个余弦」。真正会
伤到用户的是那几种失败 —— 库读不到时整窗感知一起死、同一个人被安到画面两处、
远处一个影子拿到 0.8 的相似度被叫成家人。逐条钉死。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from miloco.perception.local_vision.identity import (
    LocalIdentityResolver,
    PersonHit,
    _load_embeddings,
    _norm_bbox,
    _sample,
    render_roster,
)


class _Det:
    """检测框替身。字段名与 tracker.detector.Detection 对齐。"""

    def __init__(self, x, y, w, h, class_id=0, confidence=0.9):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.class_id, self.confidence = class_id, confidence


class _Frame:
    def __init__(self, img):
        self.data = img


def _img(h=480, w=848):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _person(root: Path, pid: str, name: str, vecs: list[np.ndarray], role: str = "") -> None:
    d = root / "persons" / pid / "tier_a"
    d.mkdir(parents=True, exist_ok=True)
    for i, v in enumerate(vecs, 1):
        np.save(d / f"body_{i:03d}.npy", v.astype(np.float32))
        (d / f"body_{i:03d}.png").write_bytes(b"")  # list_persons 按图文件计数
    meta = {"name": name, "last_seen_ts": 0.0}
    if role:
        meta["role"] = role
    import json

    (root / "persons" / pid / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _unit(*vals) -> np.ndarray:
    v = np.zeros(128, dtype=np.float32)
    for i, x in enumerate(vals):
        v[i] = x
    return v / np.linalg.norm(v)


# ── 库加载 ────────────────────────────────────────────────────────────────


def test_missing_library_yields_empty_roster_not_an_exception(tmp_path):
    """库目录不存在时必须静默降级。

    抛出去会让这台相机整窗没有输出 —— 而用户失去的本来只是"名字"这一项。
    """
    r = LocalIdentityResolver(tmp_path / "nope")
    assert r.resolve([_Frame(_img())]) == []


def test_gallery_reloads_only_when_the_library_changed(tmp_path):
    """每窗重扫目录是浪费;但用户重新登记后不重启也必须生效。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = LocalIdentityResolver(tmp_path)
    assert r.refresh_gallery() is True
    assert r.refresh_gallery() is False, "库没变不该重载"

    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    assert r.refresh_gallery() is True, "新登记的成员必须被看到"
    assert r.gallery_size == 2


def test_person_without_a_name_is_skipped(tmp_path):
    """没名字的成员进名册只能渲染成空串,不如不进。"""
    _person(tmp_path, "p1", "", [_unit(1.0)])
    r = LocalIdentityResolver(tmp_path)
    r.refresh_gallery()
    assert r.gallery_size == 0


def test_embeddings_of_mixed_dimensions_disable_that_person(tmp_path):
    """换过 ReID 模型、库里混着两代特征时,点积得到的是无意义的数。

    宁可这个人整个不参与比对 —— 让一个数值上"能算出来"但语义上错的相似度进入
    判定,会稳定地把他认成别人。
    """
    d = tmp_path / "persons" / "p1" / "tier_a"
    d.mkdir(parents=True)
    np.save(d / "body_001.npy", np.ones(128, dtype=np.float32))
    np.save(d / "body_002.npy", np.ones(256, dtype=np.float32))
    assert _load_embeddings(d) is None


def test_zero_and_corrupt_vectors_are_dropped_not_fatal(tmp_path):
    """单个坏文件不该让该成员整个失效。"""
    d = tmp_path / "persons" / "p1" / "tier_a"
    d.mkdir(parents=True)
    np.save(d / "body_001.npy", np.zeros(128, dtype=np.float32))  # 模长 0
    np.save(d / "body_002.npy", _unit(1.0))
    (d / "body_003.npy").write_bytes(b"not a npy file")
    embs = _load_embeddings(d)
    assert embs is not None and embs.shape == (1, 128)


def test_embeddings_are_renormalised_on_load(tmp_path):
    """比对用点积 —— 没归一化的向量会靠模长而不是方向拿高分。"""
    d = tmp_path / "persons" / "p1" / "tier_a"
    d.mkdir(parents=True)
    v = np.zeros(128, dtype=np.float32)
    v[0] = 7.0
    np.save(d / "body_001.npy", v)
    embs = _load_embeddings(d)
    assert embs is not None
    assert np.isclose(np.linalg.norm(embs[0]), 1.0)


# ── 匹配 ──────────────────────────────────────────────────────────────────


def _resolver_with(tmp_path, dets, feats, threshold=0.70):
    """构造一个把检测器与 ReID 都换成替身的 resolver。

    替身按**检测框**返回特征,而不是按调用顺序 —— 顺序耦合的测试会在实现改成
    并发抽特征时假绿。
    """
    r = LocalIdentityResolver(tmp_path, threshold=threshold)

    class _FakeDetector:
        def detect(self, img):
            return dets

    class _FakeReID:
        def extract_feature(self, crop):
            # 用 crop 的高度反查是哪个框(测试里各框高度互不相同)
            return feats[crop.shape[0]]

    r._detector = _FakeDetector()
    r._reid = _FakeReID()
    return r


def test_names_the_most_similar_member(tmp_path):
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    d = _Det(100, 50, 60, 200)
    r = _resolver_with(tmp_path, [d], {200 + 2 * 10: _unit(1.0)})
    hits = r.resolve([_Frame(_img())])
    assert [h.name for h in hits] == ["小亮"]
    assert hits[0].score == pytest.approx(1.0)


def test_below_threshold_produces_no_entry_rather_than_a_stranger(tmp_path):
    """不够像就不进名册,**不产出「陌生人」条目**。

    凭空多一个"陌生人"会让模型在描述里写出画面上并不存在的人 —— 云端 prompt 对
    这条有专门约束,本地不引入这个风险面。这也是电视屏幕里的人被挡住的方式。
    """
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    d = _Det(100, 50, 60, 200)
    r = _resolver_with(tmp_path, [d], {220: _unit(0.5, 0.87)})  # ≈0.5 相似度
    assert r.resolve([_Frame(_img())]) == []


def test_same_person_matched_twice_keeps_only_the_better_box(tmp_path):
    """名册里说「小亮同时在画面两处」会让模型写出自相矛盾的描述。

    这不是理论顾虑:电视屏幕里的人与真人常常都匹配到同一个成员,只是分数不同。
    """
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    weak, strong = _Det(10, 10, 40, 150), _Det(500, 20, 60, 200)
    r = _resolver_with(
        tmp_path, [weak, strong],
        {150 + 2 * 7: _unit(0.8, 0.6), 200 + 2 * 10: _unit(1.0)},
    )
    hits = r.resolve([_Frame(_img())])
    assert len(hits) == 1
    assert hits[0].bbox[0] > 500 * 1000 // 848 - 20, "该留下分高的那个框"


def test_tiny_detections_never_get_a_name(tmp_path):
    """远处一个 20 像素高的影子拿到高相似度并被叫成家人 —— ReID 输入是 192x96,
    小框喂进去只有噪声。这一刀在检测层就切掉。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = _resolver_with(tmp_path, [_Det(10, 10, 8, 20)], {})
    assert r.resolve([_Frame(_img())]) == []


def test_non_human_detections_are_ignored(tmp_path):
    """检测器同时识别猫/狗/人头/人脸。把人脸框也拿去比人体 ReID 会得到垃圾。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    face = _Det(100, 50, 60, 200, class_id=4)
    r = _resolver_with(tmp_path, [face], {220: _unit(1.0)})
    assert r.resolve([_Frame(_img())]) == []


def test_reid_failure_on_one_crop_does_not_lose_the_others(tmp_path):
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    bad, good = _Det(10, 10, 40, 150), _Det(500, 20, 60, 200)
    r = LocalIdentityResolver(tmp_path)

    class _FakeDetector:
        def detect(self, img):
            return [bad, good]

    class _FakeReID:
        def extract_feature(self, crop):
            if crop.shape[0] < 200:
                raise RuntimeError("onnx blew up")
            return _unit(0.0, 1.0)

    r._detector, r._reid = _FakeDetector(), _FakeReID()
    assert [h.name for h in r.resolve([_Frame(_img())])] == ["阳阳"]


def test_empty_gallery_short_circuits_before_loading_models(tmp_path):
    """一个成员都没登记时,不该白白吃掉两个 ONNX 模型的内存。

    断言的是"没碰过检测器",不是"结果为空" —— 后者用一个坏掉的实现也能通过。
    """
    r = LocalIdentityResolver(tmp_path)
    called = []
    r._get_detector = lambda: called.append(1)  # type: ignore[method-assign]
    assert r.resolve([_Frame(_img())]) == []
    assert called == []


# ── 坐标与渲染 ────────────────────────────────────────────────────────────


def test_bbox_is_normalised_to_the_cloud_convention(tmp_path):
    """两条通路必须说同一种坐标语言,提示词模板才能共用。"""
    assert _norm_bbox(_Det(424, 240, 424, 240), 848, 480) == (500, 500, 1000, 1000)


def test_bbox_is_clamped_into_range(tmp_path):
    """检测框可以越界(padding / 边缘目标)。越界坐标会被边车整条丢弃。"""
    x1, y1, x2, y2 = _norm_bbox(_Det(-10, -10, 900, 500), 848, 480)
    assert (x1, y1) == (0, 0)
    assert x2 <= 1000 and y2 <= 1000


def test_roster_payload_shape():
    hits = [PersonHit("p1", "小亮", "男主人", (10, 20, 30, 40), 0.9)]
    assert render_roster(hits) == [{"name": "小亮", "bbox": [10, 20, 30, 40]}]


def test_frame_sampling_includes_both_ends():
    """窗口末尾最可能含事件;只取中间会把它整段丢掉。"""
    frames = list(range(100))
    got = _sample(frames, 3)
    assert got[0] == 0 and got[-1] == 99


def test_frame_sampling_handles_short_windows():
    assert _sample([1, 2], 5) == [1, 2]
    assert _sample([], 3) == []


def test_roster_is_ordered_left_to_right(tmp_path):
    """名册顺序与画面一致,读日志的人不用在脑子里重排。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    right, left = _Det(600, 20, 60, 200), _Det(100, 20, 50, 150)
    r = _resolver_with(
        tmp_path, [right, left],
        {200 + 2 * 10: _unit(1.0), 150 + 2 * 7: _unit(0.0, 1.0)},
    )
    assert [h.name for h in r.resolve([_Frame(_img())])] == ["阳阳", "小亮"]


def test_unrecognised_people_are_reported_once_not_every_window(tmp_path, caplog):
    """「有人但都没认出来」必须说出来,而且只说一次。

    这个情形与"屋里没人"在日志上完全同形,但处置南辕北辙(一个该去重新登记,
    一个什么都不用做)—— 所以必须报。又因为它一旦发生往往是**持续**的(库过期),
    每窗一条会把日志刷没 —— 所以必须节流。
    """
    import logging

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    d = _Det(100, 50, 60, 200)
    r = _resolver_with(tmp_path, [d], {220: _unit(0.6, 0.8)})  # 0.6 < 阈值
    with caplog.at_level(logging.INFO):
        assert r.resolve([_Frame(_img())]) == []
        assert r.resolve([_Frame(_img())]) == []
    msgs = [m for m in caplog.messages if "都没认出来" in m]
    assert len(msgs) == 1, "第二窗必须被节流掉"
    assert "0.600" in msgs[0], "必须带上最高相似度,否则没法判断是不是库过期"


def test_silence_when_nobody_is_in_frame(tmp_path, caplog):
    """画面里本来就没人时不该产生这条日志 —— 那会把真正的信号淹掉。"""
    import logging

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = _resolver_with(tmp_path, [], {})
    with caplog.at_level(logging.INFO):
        assert r.resolve([_Frame(_img())]) == []
    assert not [m for m in caplog.messages if "都没认出来" in m]
