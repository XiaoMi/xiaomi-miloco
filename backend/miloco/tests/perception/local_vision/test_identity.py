"""本地认人层。

这一层的价值全在**边界**上,不在顺利路径上:顺利路径无非「算个余弦」。真正会
伤到用户的是那几种失败 —— 库读不到时整窗感知一起死、同一个人被安到画面两处、
远处一个影子拿到 0.8 的相似度被叫成家人。逐条钉死。
"""

from __future__ import annotations

import time
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


# ── 一对一指派 ────────────────────────────────────────────────────────────
#
# 逐框独立取 argmax 会让两个人同时最像同一个成员:一个被安上别人的名字,另一个整个
# 从名册里消失。线上真实发生过(男的判成阳阳 0.85、女的判成小亮 0.81)。


def test_two_people_are_not_both_given_the_same_name(tmp_path):
    """两个框都最像小亮时,不能都叫小亮,也不能把另一个人丢掉。

    这是老实现的确切失效方式:argmax 让两框都指向小亮,随后"同名只留分高的"把
    另一个人从名册里删掉 —— 画面里两个人,名册里一个。
    """
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.9, 0.436)])   # 与小亮很像
    a, b = _Det(100, 20, 50, 150), _Det(600, 20, 60, 200)
    r = _resolver_with(tmp_path, [a, b], {
        150 + 2 * 7: _unit(1.0),          # 更像小亮
        200 + 2 * 10: _unit(0.97, 0.24),  # 也最像小亮,但没那么像
    })
    hits = r.resolve([_Frame(_img())])
    assert len(hits) == 2, "两个人都该在名册里"
    assert {h.name for h in hits} == {"小亮", "阳阳"}, "一个名字不能发两次"
    # 分高的那个框拿到它最像的名字
    assert next(h for h in hits if h.name == "小亮").bbox[0] < 200


def test_threshold_is_applied_before_assignment_not_after(tmp_path):
    """**顺序不能反。** 先指派再卡阈值会凭空制造错名字。

    场景是本机位每窗都在发生的:1 个真人 + 1 个电视屏幕里的人。先指派的话,指派
    被迫把两个成员都发出去,电视框系统性地更像某一个成员,于是真人被挤到另一个
    名字上;随后阈值把电视那一对丢掉,**真人却留着错名字**。
    """
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    real, tv = _Det(100, 20, 50, 150), _Det(600, 20, 60, 200)
    r = _resolver_with(tmp_path, [real, tv], {
        150 + 2 * 7: _unit(1.0),                 # 真人,像小亮 1.00
        200 + 2 * 10: _unit(0.55, 0.835),        # 电视,像阳阳 0.835 但像小亮只有 0.55
    })
    # 把阈值抬到电视那一对之上:阈值必须先把它封死,而不是等指派完再丢
    r.threshold = 0.9
    hits = r.resolve([_Frame(_img())])
    assert [h.name for h in hits] == ["小亮"], "真人必须保住自己的名字"
    assert hits[0].bbox[0] < 200


def test_assignment_never_emits_a_below_threshold_pair(tmp_path):
    """指派会把每一行都配出去(含被封死的格子),必须逐对再验一次阈值。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    _person(tmp_path, "p2", "阳阳", [_unit(0.0, 1.0)])
    a, b = _Det(100, 20, 50, 150), _Det(600, 20, 60, 200)
    r = _resolver_with(tmp_path, [a, b], {
        150 + 2 * 7: _unit(1.0),            # 过阈值
        200 + 2 * 10: _unit(0.4, 0.4, 0.82),  # 对两个成员都只有 ~0.4,远低于阈值
    })
    hits = r.resolve([_Frame(_img())])
    assert [h.name for h in hits] == ["小亮"]


# ── 库变更检测 ────────────────────────────────────────────────────────────


def test_renaming_a_person_takes_effect_without_restart(tmp_path):
    """用户在面板上把认错的名字改对,必须立刻生效。

    断言的是**用户可见的契约**,不是缓存实现 —— 老实现的指纹盯的是 tier_a 图文件,
    而这一层一张图都不读,于是改名在本进程生命周期内永远不生效。
    """
    import json as _json

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = _resolver_with(tmp_path, [_Det(100, 20, 50, 150)], {150 + 2 * 7: _unit(1.0)})
    assert [h.name for h in r.resolve([_Frame(_img())])] == ["小亮"]

    (tmp_path / "persons" / "p1" / "meta.json").write_text(
        _json.dumps({"name": "亮亮", "last_seen_ts": 0.0}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert [h.name for h in r.resolve([_Frame(_img())])] == ["亮亮"]


def test_embeddings_written_after_startup_are_picked_up(tmp_path):
    """启动时的特征补算(main.py::_backfill_tier_a_reid_embeddings)与这里的
    eager 加载是竞态。补算只动 .npy —— 察觉不到的话,那个成员在整个进程生命周期里
    都不参与比对,而他的检测框会被 argmax 分给**别的成员**。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    d = tmp_path / "persons" / "p2" / "tier_a"
    d.mkdir(parents=True)
    (d / "body_001.png").write_bytes(b"")
    import json as _json
    (tmp_path / "persons" / "p2" / "meta.json").write_text(
        _json.dumps({"name": "阳阳"}, ensure_ascii=False), encoding="utf-8")

    r = LocalIdentityResolver(tmp_path)
    r.refresh_gallery()
    assert r.gallery_size == 1, "还没有特征,不该进库"

    np.save(d / "body_001.npy", _unit(0.0, 1.0))   # 补算落盘
    assert r.refresh_gallery() is True
    assert r.gallery_size == 2


def test_empty_library_does_not_rescan_every_window(tmp_path, caplog):
    """identity_enabled 默认开,所以"还没登记任何人"是每个新装用户的初始状态。
    每窗重扫 + 打一条 INFO ≈ 7000 行/天/相机。"""
    import logging

    (tmp_path / "persons").mkdir()
    r = LocalIdentityResolver(tmp_path)
    with caplog.at_level(logging.INFO):
        assert r.refresh_gallery() is True      # 首次加载
        for _ in range(5):
            assert r.refresh_gallery() is False, "库没变不该重扫"
    assert len([m for m in caplog.messages if "身份库已加载" in m]) == 1


def test_unreadable_library_recovers_once_it_becomes_readable(tmp_path, monkeypatch):
    """读库失败后必须能自愈。

    哨兵若用 () 而不是 None,会与「库为空」的**合法**指纹撞上 —— 一次读盘失败之后
    就再也不会重载了,而这个状态在日志上与"库本来就是空的"完全同形。
    """
    from miloco.perception.local_vision import identity as m

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = LocalIdentityResolver(tmp_path)
    boom = {"on": True}
    real = m._library_fingerprint

    def flaky(root):
        if boom["on"]:
            raise OSError("transient io error")
        return real(root)

    monkeypatch.setattr(m, "_library_fingerprint", flaky)
    assert r.refresh_gallery() is False
    assert r.load_error is not None
    boom["on"] = False
    assert r.refresh_gallery() is True, "读盘恢复之后必须能重新加载"
    assert r.gallery_size == 1 and r.load_error is None


def test_missing_library_directory_is_an_empty_library_not_an_error(tmp_path):
    """目录还不存在 = 用户还没登记任何人,不是故障 —— 不该往 load_error 里写东西。"""
    r = LocalIdentityResolver(tmp_path / "not-yet")
    r.refresh_gallery()
    assert r.gallery_size == 0
    assert r.load_error is None


def test_member_without_embeddings_is_reported_not_silent(tmp_path, caplog):
    """没有特征的成员会静默不参与比对,而他的框随后被分给别人 —— 表现是"叫错名字"。"""
    import json as _json
    import logging

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    d = tmp_path / "persons" / "p2" / "tier_a"
    d.mkdir(parents=True)
    (d / "body_001.png").write_bytes(b"")
    (tmp_path / "persons" / "p2" / "meta.json").write_text(
        _json.dumps({"name": "阳阳"}, ensure_ascii=False), encoding="utf-8")
    r = LocalIdentityResolver(tmp_path)
    with caplog.at_level(logging.INFO):
        r.refresh_gallery()
    assert any("阳阳" in m and "跳过" in m for m in caplog.messages)


# ── 库龄 ──────────────────────────────────────────────────────────────────


def test_stale_library_warns(tmp_path, caplog):
    """库过期是唯一被证实的失效根因,而且表现是**高置信度认错**,不是认不出。
    代码侧无解 —— 所以至少要喊出来。"""
    import logging
    import os

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    old = time.time() - 40 * 86400
    for img in (tmp_path / "persons").glob("*/tier_a/body_*.png"):
        os.utime(img, (old, old))
    r = LocalIdentityResolver(tmp_path)
    with caplog.at_level(logging.WARNING):
        r.refresh_gallery()
    assert r.library_age_days is not None and r.library_age_days > 39
    assert any("重新登记" in m for m in caplog.messages)


def test_library_age_ignores_embedding_files(tmp_path):
    """库龄必须取自登记**图**。取 .npy 的话,启动时的特征补算会重写每一个 .npy,
    一个 36 天的旧库在补算之后显示成"全新" —— 亲手把唯一可靠的过期信号废掉。"""
    import os

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    old = time.time() - 40 * 86400
    for img in (tmp_path / "persons").glob("*/tier_a/body_*.png"):
        os.utime(img, (old, old))
    # .npy 刚被重写(模拟 backfill)
    for npy in (tmp_path / "persons").glob("*/tier_a/body_*.npy"):
        os.utime(npy, None)
    r = LocalIdentityResolver(tmp_path)
    r.refresh_gallery()
    assert r.library_age_days > 39, "补算不该让库龄归零"


# ── 并发 ──────────────────────────────────────────────────────────────────


def test_lazy_init_builds_one_detector_under_concurrency(tmp_path):
    """resolve 从多台相机的协程经 to_thread 并发进来。裸的 check-then-act 实测让
    6 个并发窗构造出 6 个 Detector(5 个立刻丢弃),而这台机器上还跑着视觉边车。"""
    import threading as _t

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = LocalIdentityResolver(tmp_path)
    built = []

    class _Slow:
        def __init__(self):
            built.append(1)
            time.sleep(0.05)  # 放大竞态窗口

        def detect(self, img):
            return []

    r._detector_locked = (
        lambda: setattr(r, "_detector", r._detector or _Slow()) or r._detector
    )
    ts = [_t.Thread(target=r._get_detector) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(built) == 1, f"构造了 {len(built)} 个检测器"


def test_miss_log_is_throttled_per_camera(tmp_path, caplog):
    """共用一个计时器的话,多相机库都过期时只有第一台报得出来,其余永远静默。"""
    import logging

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = _resolver_with(tmp_path, [_Det(100, 20, 50, 150)], {150 + 2 * 7: _unit(0.6, 0.8)})
    with caplog.at_level(logging.INFO):
        r.resolve([_Frame(_img())], source="cam1")
        r.resolve([_Frame(_img())], source="cam2")
        r.resolve([_Frame(_img())], source="cam1")   # 该被节流
    msgs = [m for m in caplog.messages if "都没认出来" in m]
    assert len(msgs) == 2
    assert any("cam1" in m for m in msgs) and any("cam2" in m for m in msgs)


def test_resolve_still_accepts_a_single_argument(tmp_path):
    """resolver 是注入的。加位置参数会让只实现 resolve(frames) 的替身 TypeError,
    落进兜底 → 名册恒空 —— 一个"改签名把认人静默关掉"的故障。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    r = _resolver_with(tmp_path, [_Det(100, 20, 50, 150)], {150 + 2 * 7: _unit(1.0)})
    assert [h.name for h in r.resolve([_Frame(_img())])] == ["小亮"]


def test_change_detection_does_not_rely_on_mtime_resolution(tmp_path):
    """两次改动落在同一个文件系统时间戳里,也必须被察觉。

    这不是理论顾虑:实测这台机器上 st_mtime_ns 没有纳秒精度,相继两次写拿到**完全
    相同**的时间戳;而「小亮」→「亮亮」字节数又恰好一样。(mtime, size) 那套
    在这里是瞎的,所以指纹哈希的是内容。
    """
    import json as _json
    import os

    from miloco.perception.local_vision.identity import _library_fingerprint

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    meta = tmp_path / "persons" / "p1" / "meta.json"
    fp1 = _library_fingerprint(tmp_path)
    before = meta.stat()
    meta.write_text(_json.dumps({"name": "亮亮", "last_seen_ts": 0.0}, ensure_ascii=False),
                    encoding="utf-8")
    os.utime(meta, ns=(before.st_atime_ns, before.st_mtime_ns))   # 时间戳强制不变
    assert meta.stat().st_size == before.st_size, "构造前提:字节数也一样"
    assert _library_fingerprint(tmp_path) != fp1, "内容变了就必须察觉"


def test_recomputed_embeddings_are_detected(tmp_path):
    """启动时的特征补算会把 .npy 重写成**不同的值、相同的大小**。
    盯 (mtime, size) 会漏掉,而漏掉意味着该成员继续用旧向量比对。"""
    import os

    from miloco.perception.local_vision.identity import _library_fingerprint

    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    npy = next((tmp_path / "persons" / "p1" / "tier_a").glob("body_*.npy"))
    fp1 = _library_fingerprint(tmp_path)
    before = npy.stat()
    np.save(npy, _unit(0.0, 1.0).astype(np.float32))
    os.utime(npy, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert npy.stat().st_size == before.st_size
    assert _library_fingerprint(tmp_path) != fp1


def test_reading_the_library_never_creates_directories(tmp_path):
    """读库**不该有副作用**。

    此前是构造 IdentityLibrary 来列成员,而它的 __init__ 会 _ensure_dirs() ——
    库路径配错时会在错误位置默默建出一副空目录骨架,把「目录不存在」这个最直接的
    排障信号抹掉:用户以为路径对了,实际是我们刚给他造了个空的。
    """
    root = tmp_path / "typo-in-config"
    r = LocalIdentityResolver(root)
    r.refresh_gallery()
    assert not root.exists(), f"读一次库就把 {root} 建出来了"
    assert r.gallery_size == 0


def test_a_corrupt_meta_does_not_hide_the_other_members(tmp_path):
    """单个坏 meta.json 不该让整库读不出来。"""
    _person(tmp_path, "p1", "小亮", [_unit(1.0)])
    bad = tmp_path / "persons" / "p2"
    (bad / "tier_a").mkdir(parents=True)
    (bad / "meta.json").write_text("{ 这不是 json", encoding="utf-8")
    r = LocalIdentityResolver(tmp_path)
    r.refresh_gallery()
    assert r.gallery_size == 1
