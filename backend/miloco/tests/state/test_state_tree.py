# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""StateStore 数据结构层：不 start()，不需要 event loop。"""

from __future__ import annotations

import itertools
import logging

import pytest
from miloco.state import MISSING, StateStore
from miloco.state.path import MAX_DEPTH
from miloco.state.store import Entry


def make_store() -> StateStore:
    return StateStore()


def paths(changes) -> list[str]:
    return [change.path for change in changes]


# ---- diff ----


def test_add_change_delete():
    store = make_store()

    added = store._commit("iot/dev1/online", True, source="miot")
    assert [(c.path, c.old, c.new) for c in added] == [
        ("iot/dev1/online", MISSING, True)
    ]

    modified = store._commit("iot/dev1/online", False, source="miot")
    assert [(c.path, c.old, c.new) for c in modified] == [
        ("iot/dev1/online", True, False)
    ]

    removed = store._commit_delete("iot/dev1/online", source="miot")
    assert [(c.path, c.old, c.new) for c in removed] == [
        ("iot/dev1/online", False, MISSING)
    ]


def test_same_value_produces_no_change():
    store = make_store()
    store._commit("iot/dev1/online", True, source="miot")
    assert store._commit("iot/dev1/online", True, source="miot") == []


def test_replace_deletes_uncovered_leaves():
    store = make_store()
    store._commit("iot/dev1", {"online": True, "prop": {"2.1": 26}}, source="miot")

    changes = store._commit("iot/dev1", {"prop": {"2.2": 50}}, source="miot")

    assert sorted(paths(changes)) == [
        "iot/dev1/online",
        "iot/dev1/prop/2.1",
        "iot/dev1/prop/2.2",
    ]
    assert store.get("iot/dev1") == {"prop": {"2.2": 50}}


def test_leaf_and_subtree_replace_each_other():
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 24, source="miot")

    to_subtree = store._commit("iot/dev1/prop/2.1", {"value": 24}, source="miot")
    assert [(c.path, c.new) for c in to_subtree] == [
        ("iot/dev1/prop/2.1", MISSING),
        ("iot/dev1/prop/2.1/value", 24),
    ]

    to_leaf = store._commit("iot/dev1/prop/2.1", 24, source="miot")
    assert [(c.path, c.new) for c in to_leaf] == [
        ("iot/dev1/prop/2.1/value", MISSING),
        ("iot/dev1/prop/2.1", 24),
    ]


def test_write_under_existing_leaf_deletes_it():
    store = make_store()
    store._commit("iot/dev1", 5, source="miot")

    changes = store._commit("iot/dev1/prop", 3, source="miot")

    assert [(c.path, c.old, c.new) for c in changes] == [
        ("iot/dev1", 5, MISSING),
        ("iot/dev1/prop", MISSING, 3),
    ]


def test_deletes_come_before_upserts():
    store = make_store()
    store._commit("iot/dev1", {"a": 1, "b": 2}, source="miot")

    changes = store._commit("iot/dev1", {"c": 3}, source="miot")

    assert changes[-1].path == "iot/dev1/c"
    assert all(change.new is MISSING for change in changes[:-1])


def test_delete_recurses_over_subtree():
    store = make_store()
    store._commit("iot/dev1", {"online": True, "prop": {"2.1": 26}}, source="miot")

    changes = store._commit_delete("iot/dev1", source="miot")

    assert sorted(paths(changes)) == ["iot/dev1/online", "iot/dev1/prop/2.1"]
    assert store.get("iot/dev1") is MISSING


def test_delete_under_a_leaf_is_noop():
    store = make_store()
    store._commit("iot/dev1", 5, source="t")

    assert store._commit_delete("iot/dev1/prop", source="t") == []
    assert store.get("iot/dev1") == 5


def test_delete_source_is_the_deleting_side():
    store = make_store()
    store._commit("omni/rule/r1/dev1", True, source="omni")

    changes = store._commit_delete("omni/rule/r1", source="rule_admin")

    assert [change.source for change in changes] == ["rule_admin"]


def test_delete_missing_path_is_noop():
    store = make_store()
    store._commit("iot/dev1/online", True, source="miot")

    assert store._commit_delete("iot/dev2", source="miot") == []
    assert store._commit_delete("iot/dev1/online", source="miot") != []
    assert store._commit_delete("iot/dev1/online", source="miot") == []
    assert store.get("iot/dev1/online") is MISSING


def test_empty_dict_deletes_subtree():
    store = make_store()
    store._commit("iot/dev1", {"online": True}, source="miot")

    changes = store._commit("iot/dev1", {}, source="miot")

    assert paths(changes) == ["iot/dev1/online"]
    assert store.get("iot/dev1") is MISSING


# ---- 判等 ----


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (True, 1),
        (26, 26.0),
        ((True,), (1,)),
        ((1, 2), (1, 2, 3)),
    ],
)
def test_equality_is_typed_and_elementwise(first, second):
    store = make_store()
    store._commit("x/y", first, source="t")

    assert store._commit("x/y", second, source="t") != []


# ---- 时间戳 ----


def test_one_timestamp_per_write(monkeypatch):
    """必须让 `now_ms` 每次调用都自增。

    直接断言批内时间戳相同是永绿的——同一毫秒内 `now_ms()` 本来就返回同一个数，每片叶子
    各取一次时间的实现照样通过。
    """
    store = make_store()
    ticks = itertools.count(1000)
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: next(ticks))

    changes = store._commit("iot/dev1", {"a": 1, "b": {"c": 2}}, source="miot")

    stamps = {change.at for change in changes}
    stamps |= {store.get_entry(path).last_changed for path in paths(changes)}
    stamps |= {store.get_entry(path).last_reported for path in paths(changes)}
    assert stamps == {1000}


def test_one_timestamp_per_delete(monkeypatch):
    store = make_store()
    store._commit("iot/dev1", {"a": 1, "b": 2}, source="miot")
    ticks = itertools.count(5000)
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: next(ticks))

    changes = store._commit_delete("iot/dev1", source="miot")

    assert {change.at for change in changes} == {5000}


def test_same_value_refreshes_only_last_reported(monkeypatch):
    store = make_store()
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: 1000)
    store._commit("iot/dev1/online", True, source="miot")

    monkeypatch.setattr("miloco.state.store.now_ms", lambda: 2000)
    store._commit("iot/dev1/online", True, source="cloud")

    entry = store.get_entry("iot/dev1/online")
    assert (entry.last_changed, entry.last_reported, entry.source) == (
        1000,
        2000,
        "cloud",
    )


# ---- 值规范化 ----


def test_list_is_stored_as_tuple():
    store = make_store()
    original = [1, 2]

    changes = store._commit("x/y", original, source="t")
    original.append(3)

    assert store.get("x/y") == (1, 2)
    assert store.get_entry("x/y").value == (1, 2)
    assert changes[0].new == (1, 2)
    assert store.snapshot("x/y") == {"x": {"y": (1, 2)}}


@pytest.mark.parametrize(
    "value",
    [
        {"a": [{"r": 255}]},
        {"a": [[1]]},
        {"a": b"bytes"},
        object(),
    ],
)
def test_non_json_values_are_rejected(value):
    store = make_store()
    with pytest.raises(TypeError):
        store._commit("x", value, source="t")


def test_numpy_scalars_are_rejected():
    """`np.float64` 是 `float` 的子类，收下来会让判等永远不成立，同一个值来回写产一串假事件。"""
    numpy = pytest.importorskip("numpy")
    store = make_store()

    for value in (
        numpy.float64(1.0),
        numpy.float32(1.0),
        numpy.int64(1),
        numpy.bool_(True),
    ):
        with pytest.raises(TypeError):
            store._commit("x/y", value, source="t")
        with pytest.raises(TypeError):
            store._commit("x/z", [value], source="t")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_inf_are_rejected(value):
    store = make_store()
    with pytest.raises(ValueError):
        store._commit("x/y", value, source="t")


def test_dict_key_must_be_a_valid_segment():
    store = make_store()

    with pytest.raises(TypeError):
        store._commit("iot", {1: "a"}, source="t")
    for key in ("a/b", "*", "", "c" * 129):
        with pytest.raises(ValueError):
            store._commit("iot", {key: 1}, source="t")


@pytest.mark.parametrize("path", ["", "iot//dev1", "iot/cam*", "a/" + "b" * 129])
def test_invalid_paths_are_rejected(path):
    store = make_store()
    with pytest.raises(ValueError):
        store._commit(path, 1, source="t")


def test_path_and_pattern_depth_limit():
    store = make_store()
    deep = "/".join(f"s{index}" for index in range(MAX_DEPTH + 1))

    with pytest.raises(ValueError):
        store._commit(deep, 1, source="t")
    with pytest.raises(ValueError):
        store.snapshot(deep)


@pytest.mark.parametrize("path", [None, 1, ("iot", "dev1")])
def test_non_string_path_is_rejected(path):
    store = make_store()
    with pytest.raises(TypeError):
        store._commit(path, 1, source="t")
    with pytest.raises(TypeError):
        store.subscribe(path, lambda change: None)


@pytest.mark.parametrize("pattern", [None, 1, ["iot/**", 1], [None]])
def test_non_string_pattern_is_rejected(pattern):
    """序列里的每一条也要逐个校验，不能只看最外层是不是序列。"""
    store = make_store()
    with pytest.raises(TypeError):
        store.snapshot(pattern)


def test_depth_limit_covers_payload_nesting():
    store = make_store()
    deep = 1
    for _ in range(16):
        deep = {"n": deep}

    with pytest.raises(ValueError):
        store._commit("root", deep, source="t")


def test_rejected_write_leaves_tree_untouched():
    store = make_store()
    store._commit("iot/dev1", {"online": True}, source="miot")

    with pytest.raises(TypeError):
        store._commit("iot/dev1", {"online": False, "bad": object()}, source="miot")

    assert store.get("iot/dev1") == {"online": True}


def test_leaf_limit_rejects_whole_write(monkeypatch, caplog):
    store = make_store()
    monkeypatch.setattr("miloco.state.store.MAX_LEAVES", 2)
    store._commit("iot", {"a": 1, "b": 2}, source="t")

    with caplog.at_level(logging.ERROR, logger="miloco.state.store"):
        for _ in range(5):
            assert store._commit("iot", {"a": 1, "b": 2, "c": 3}, source="t") == []

    assert store.get("iot") == {"a": 1, "b": 2}
    # 这条日志在锁内，按写入频率打会把所有写入方一起堵在锁上
    assert len(caplog.records) == 1


# ---- 读 ----


def test_missing_is_falsy():
    assert not MISSING
    assert repr(MISSING) == "MISSING"


def test_get_distinguishes_missing_from_none():
    store = make_store()
    store._commit("omni/cam0/last_error", None, source="omni")

    assert store.get("omni/cam0/last_error") is None
    assert store.get("omni/cam0/missing") is MISSING
    assert store.get("omni/cam0/missing", None) is None


def test_deleting_a_leaf_clears_the_nodes_above_it():
    """空节点不算叶子、`get` 和 `snapshot` 也看不见，留着就绕开了叶子总数上限。"""
    store = make_store()
    for index in range(100):
        store._commit(f"a/t{index}/room", 1, source="t")
        store._commit_delete(f"a/t{index}/room", source="t")

    assert store.get("a") is MISSING
    assert store.snapshot("a/**") == {}
    assert store._root == {}


def test_writing_an_empty_dict_clears_the_nodes_above_it():
    store = make_store()
    store._commit("a/b/c", 1, source="t")

    store._commit("a/b/c", {}, source="t")

    assert store._root == {}


def test_no_write_path_leaves_an_empty_node():
    """走遍每条会摘 key 的写入路径，断言树里没有一个不含叶子的中间节点。

    `get` / `snapshot` / `_plain` 都不再防空节点了，前提就是这一条；将来加了新的写入路径
    忘记清理，这里先红。
    """
    store = make_store()

    store._commit("a/b/c", 1, source="t")
    store._commit_delete("a/b/c", source="t")

    store._commit("d/e/f", 1, source="t")
    store._commit("d/e/f", {}, source="t")

    store._commit("g/h", {"i": {}, "j": {"k": {}}}, source="t")

    store._commit("m/n", 1, source="t")
    store._commit("m/n/o", 1, source="t")
    store._commit_delete("m/n/o", source="t")

    store._commit("p/q", {"r": 1}, source="t")
    store._commit("p/q", 2, source="t")
    store._commit_delete("p", source="t")

    assert _empty_nodes(store._root) == []
    assert store._leaf_count == 0
    assert store._root == {}


def _empty_nodes(node, prefix: str = "") -> list[str]:
    found = []
    for key, child in node.items():
        if isinstance(child, Entry):
            continue
        path = f"{prefix}/{key}" if prefix else key
        if child:
            found.extend(_empty_nodes(child, path))
        else:
            found.append(path)
    return found


def test_subtree_that_expands_to_nothing_is_not_created():
    store = make_store()
    store._commit("a", {"kept": 1, "empty": {}, "nested": {"deep": {}}}, source="t")

    assert store.get("a") == {"kept": 1}
    assert store._root == {"a": {"kept": store.get_entry("a/kept")}}


def test_get_entry_only_for_leaves():
    store = make_store()
    store._commit("a/b/c", 1, source="t")

    assert isinstance(store.get_entry("a/b/c"), Entry)
    assert store.get_entry("a/b") is None
    assert store.get_entry("a/x") is None


def test_reading_through_a_leaf_finds_nothing():
    store = make_store()
    store._commit("iot/dev1", 5, source="t")

    assert store.get("iot/dev1/prop") is MISSING
    assert store.get_entry("iot/dev1/prop") is None


def test_falsy_leaf_values_stay_visible():
    """`_plain` 和快照都用真值判断剪空节点，`()` / `0` / `""` 不能被一起剪掉。"""
    store = make_store()
    store._commit("x", {"empty": [], "zero": 0, "blank": ""}, source="t")

    assert store.get("x") == {"empty": (), "zero": 0, "blank": ""}
    assert store.snapshot("x/**") == {"x": {"empty": (), "zero": 0, "blank": ""}}


# ---- pattern ----


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("iot/dev1/prop/2.1", ["iot/dev1/prop/2.1"]),
        ("iot/*/prop/2.1", ["iot/dev1/prop/2.1", "iot/dev2/prop/2.1"]),
        ("iot/*/online", ["iot/dev1/online"]),
        ("iot/**", ["iot/dev1/online", "iot/dev1/prop/2.1", "iot/dev2/prop/2.1"]),
        ("iot/*/prop/**", ["iot/dev1/prop/2.1", "iot/dev2/prop/2.1"]),
        ("omni/**", []),
    ],
)
def test_snapshot_pattern(pattern, expected):
    store = make_store()
    store._commit("iot/dev1", {"online": True, "prop": {"2.1": 26}}, source="miot")
    store._commit("iot/dev2/prop/2.1", 24, source="miot")

    result = store.snapshot(pattern)

    assert sorted(flatten(result)) == sorted(expected)


def flatten(tree: dict, prefix: str = "") -> list[str]:
    found = []
    for key, value in tree.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(value, dict):
            found.extend(flatten(value, path))
        else:
            found.append(path)
    return found


def test_double_star_does_not_match_zero_segments():
    store = make_store()
    store._commit("iot", 1, source="t")

    assert store.snapshot("iot/**") == {}
    assert store.snapshot("iot") == {"iot": 1}


def test_snapshot_keeps_full_path():
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev2/prop/2.1", 24, source="miot")

    assert store.snapshot("iot/*/prop/2.1") == {
        "iot": {"dev1": {"prop": {"2.1": 26}}, "dev2": {"prop": {"2.1": 24}}}
    }


def test_snapshot_prunes_branches_without_matches():
    """中间节点匹配、但它下面一个叶子都没匹配上时，结果里不能留一个空壳。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev2/prop/2.3", 24, source="miot")

    assert store.snapshot("iot/*/prop/2.1") == {"iot": {"dev1": {"prop": {"2.1": 26}}}}


def test_snapshot_with_meta(monkeypatch):
    store = make_store()
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: 1754890000000)
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    assert store.snapshot("iot/**", with_meta=True) == {
        "iot": {
            "dev1": {
                "prop": {
                    "2.1": Entry(26, 1754890000000, 1754890000000, "miot"),
                }
            }
        }
    }


def test_snapshot_leaf_is_a_dict_only_for_subtrees():
    """认叶子看类型。带元数据的叶子若也是 dict，遍历结果的人就分不出它和子树了。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    result = store.snapshot("iot/**", with_meta=True)

    assert isinstance(result["iot"]["dev1"], dict)
    assert not isinstance(result["iot"]["dev1"]["prop"]["2.1"], dict)


# ---- 多 pattern ----


def test_snapshot_merges_several_patterns():
    """钉的是结果合并：另起一个子 dict 再赋值会把前一个 pattern 的结果整个盖掉。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev5/prop/3.2", True, source="miot")
    store._commit("iot/dev9/prop/4.1", 1, source="miot")

    assert store.snapshot(["iot/dev1/prop/2.1", "iot/dev5/prop/3.2"]) == {
        "iot": {"dev1": {"prop": {"2.1": 26}}, "dev5": {"prop": {"3.2": True}}}
    }


@pytest.mark.parametrize(
    "order", [("iot/**", "iot/dev1/prop/2.1"), ("iot/dev1/prop/2.1", "iot/**")]
)
def test_overlapping_patterns_merge_either_way(order):
    """宽窄两个 pattern 重叠时，谁先谁后结果都得一样。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev5/prop/3.2", True, source="miot")

    assert store.snapshot(list(order)) == store.snapshot("iot/**")


def test_snapshot_of_no_patterns_is_empty():
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    assert store.snapshot([]) == {}


def test_snapshot_validates_every_pattern():
    """一条非法就整个抛，不能悄悄按剩下的返回半份结果。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    with pytest.raises(ValueError):
        store.snapshot(["iot/dev1/prop/2.1", "iot/**/prop"])


@pytest.mark.parametrize(
    "pattern", ["iot/**/prop", "", "iot//dev1", "iot/cam*", "iot/*x"]
)
def test_invalid_patterns_raise(pattern):
    store = make_store()
    with pytest.raises(ValueError):
        store.snapshot(pattern)
    with pytest.raises(ValueError):
        store.subscribe(pattern, lambda change: None)


# ---- 末段落在中间节点 ----


@pytest.mark.parametrize("pattern", ["iot", "iot/dev1", "iot/*", "iot/*/prop"])
def test_pattern_ending_on_subtree_raises(pattern):
    """末段命中中间节点却收不到叶子时必须报出来，不能静默返回空。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    with pytest.raises(ValueError, match=r"\*\*"):
        store.snapshot(pattern)


def test_one_pattern_contributing_nothing_raises():
    """合并结果非空掩盖不了其中一条 pattern 一片叶子都没收到。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev2/online", False, source="miot")

    with pytest.raises(ValueError, match=r"\*\*"):
        store.snapshot(["iot/dev1", "iot/dev2/online"])


def test_pattern_matching_no_path_still_returns_empty():
    """路径不存在是正常情况，仍然返回空、不抛。"""
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    assert store.snapshot("nothing/**") == {}
    assert store.snapshot("iot/dev9/online") == {}
    assert store.snapshot(["iot/dev1/prop/2.1", "iot/dev9/online"]) == {
        "iot": {"dev1": {"prop": {"2.1": 26}}}
    }


def test_partly_matched_pattern_does_not_raise():
    """有设备没这条属性时照旧缺席——这一层收到过叶子，不算零贡献。"""
    store = make_store()
    store._commit("iot/dev1/online", True, source="miot")
    store._commit("iot/dev2/prop/2.1", 26, source="miot")

    assert store.snapshot("iot/*/online") == {"iot": {"dev1": {"online": True}}}


def test_leaf_beside_subtree_at_same_level_does_not_raise():
    """同一层既有叶子又有子树时，叶子收到了就不算零贡献，子树照旧不收。"""
    store = make_store()
    store._commit("iot/legacy", 1, source="miot")
    store._commit("iot/dev2/online", True, source="miot")

    assert store.snapshot("iot/*") == {"iot": {"legacy": 1}}


def test_subtree_reachable_with_double_star():
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev1/online", True, source="miot")

    assert store.snapshot("iot/dev1/**") == {
        "iot": {"dev1": {"prop": {"2.1": 26}, "online": True}}
    }


# ---- 叶子与子树互相翻转 ----


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("iot/dev1/prop", 26), ("iot/dev1/prop", {"value": 26})),
        (("a/b/c", 1), ("a/b", 2)),
        (("a/b", 1), ("a/b/c", 2)),
    ],
    ids=["leaf_to_subtree", "subtree_to_leaf", "leaf_split_by_deeper_write"],
)
def test_shape_flip_is_counted_and_logged(caplog, first, second):
    store = make_store()
    store._commit(first[0], first[1], source="miot")

    with caplog.at_level(logging.WARNING, logger="miloco.state.store"):
        store._commit(second[0], second[1], source="miot")

    assert store._shape_flips == 1
    assert "shape" in caplog.text


def test_shape_flip_warns_once_but_keeps_counting(caplog):
    """反复翻转不能把日志刷爆，但次数要留下来分得出抖动和一次性变更。"""
    store = make_store()

    with caplog.at_level(logging.WARNING, logger="miloco.state.store"):
        for value in [26, {"value": 26}, 26, {"value": 26}]:
            store._commit("iot/dev1/prop", value, source="miot")

    assert store._shape_flips == 3
    assert len([r for r in caplog.records if "shape" in r.getMessage()]) == 1


def test_stable_writes_do_not_count_as_flip():
    store = make_store()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev1/prop/2.1", 27, source="miot")
    store._commit("iot/dev1/prop/2.2", 1, source="miot")
    store._commit_delete("iot/dev1/prop/2.1", source="miot")

    assert store._shape_flips == 0
