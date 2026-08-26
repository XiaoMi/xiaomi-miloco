# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""快照结果的形状转换与调试打印。"""

from __future__ import annotations

import pytest
from miloco.state import Entry, StateStore, flatten
from miloco.state.utils import format_dump


def make_store() -> StateStore:
    store = StateStore()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")
    store._commit("iot/dev5/prop/3.2", True, source="miot")
    return store


def test_flatten_restores_full_paths():
    store = make_store()

    assert flatten(store.snapshot("iot/**")) == {
        "iot/dev1/prop/2.1": 26,
        "iot/dev5/prop/3.2": True,
    }


def test_flatten_keeps_entries_when_snapshot_carries_meta(monkeypatch):
    """带元数据时叶子是 `Entry`，摊平不拆开它——退出模式时要读 `last_changed`。"""
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: 1754890000000)
    store = StateStore()
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    assert flatten(store.snapshot("iot/**", with_meta=True)) == {
        "iot/dev1/prop/2.1": Entry(26, 1754890000000, 1754890000000, "miot")
    }


def test_flatten_does_not_walk_into_tuple_values():
    """元组是叶子值不是子树。按「不是 dict 就停」来认，元组才不会被当成一层展开。"""
    store = StateStore()
    store._commit("omni/cam0/box", [1, 2, 3], source="omni")

    assert flatten(store.snapshot("omni/**")) == {"omni/cam0/box": (1, 2, 3)}


def test_flatten_of_empty_snapshot_is_empty():
    store = make_store()

    assert flatten(store.snapshot("nothing/**")) == {}


def test_flatten_round_trips_the_paths_asked_for():
    """模式保存的用法：拿一组精确路径取快照，摊平后 key 应当原样还是那组路径。"""
    store = make_store()
    targets = ["iot/dev1/prop/2.1", "iot/dev5/prop/3.2"]

    assert sorted(flatten(store.snapshot(targets))) == sorted(targets)


def test_flatten_skips_paths_that_do_not_exist():
    """取不到的路径直接不出现，不是给个哨兵值——恢复时遍历会把哨兵当成真值发给设备。"""
    store = make_store()

    saved = flatten(store.snapshot(["iot/dev1/prop/2.1", "iot/dev404/prop/9.9"]))

    assert saved == {"iot/dev1/prop/2.1": 26}


NOW = 1754890000000


@pytest.fixture
def clock(monkeypatch):
    """写入时间可控。dump 打的是相对时间，两条叶子得能有不同的年龄。"""
    current = {"ms": NOW}
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: current["ms"])
    return current


def dump_rows(dumped: str) -> dict[str, str]:
    """把 dump 的正文拆成 `{路径: 等号右边}`，跳过头一行。"""
    rows = {}
    for line in dumped.splitlines()[1:]:
        path, _, rest = line.partition(" = ")
        rows[path.strip()] = rest
    return rows


def test_dump_gives_value_source_and_two_ages_for_each_leaf(clock):
    store = StateStore()
    clock["ms"] = NOW - 12_300
    store._commit("iot/dev1/prop/2.1", 26, source="miot")

    rows = dump_rows(format_dump(store.snapshot("**", with_meta=True), NOW))

    assert rows == {"iot/dev1/prop/2.1": "26  src=miot changed=12.3s reported=12.3s"}


def test_dump_header_counts_the_leaves(clock):
    store = make_store()

    dumped = format_dump(store.snapshot("**", with_meta=True), NOW)

    assert dumped.splitlines()[0] == "state dump: 2 leaves"


def test_dump_tells_a_number_from_a_string(clock):
    """值打 repr。容器按精确类型判等，看不出类型就查不了「同一个数写成两种类型」。"""
    store = StateStore()
    store._commit("a/number", 42, source="s")
    store._commit("a/text", "42", source="s")

    rows = dump_rows(format_dump(store.snapshot("**", with_meta=True), NOW))

    assert rows["a/number"].startswith("42 ")
    assert rows["a/text"].startswith("'42' ")


def test_dump_sorts_paths_so_two_dumps_can_be_diffed(clock):
    store = StateStore()
    store._commit("z/leaf", 1, source="s")
    store._commit("a/leaf", 1, source="s")

    dumped = format_dump(store.snapshot("**", with_meta=True), NOW)

    assert list(dump_rows(dumped)) == ["a/leaf", "z/leaf"]


def test_dump_switches_age_units_so_a_long_idle_leaf_stays_readable(clock):
    store = StateStore()
    clock["ms"] = NOW - 3 * 86_400_000
    store._commit("iot/dev1/online", True, source="miot")

    rows = dump_rows(format_dump(store.snapshot("**", with_meta=True), NOW))

    assert rows["iot/dev1/online"] == "True  src=miot changed=3d reported=3d"


def test_dump_truncates_and_says_how_many_are_left(clock, monkeypatch):
    """叶子上限远高于人能读的行数，全打出来会把日志撑爆。"""
    monkeypatch.setattr("miloco.state.utils.DUMP_MAX_LEAVES", 2)
    store = StateStore()
    for index in range(5):
        store._commit(f"a/leaf{index}", index, source="s")

    lines = format_dump(store.snapshot("**", with_meta=True), NOW).splitlines()

    assert len(lines) == 4
    assert lines[-1] == "... 3 more leaves not shown"


def test_dump_of_empty_snapshot_says_so_instead_of_returning_nothing():
    assert format_dump({}, NOW) == "state dump: empty"


def test_dump_rejects_a_snapshot_taken_without_meta():
    """不带元数据的快照叶子是裸值，没有来源和时间可打。"""
    store = make_store()

    with pytest.raises(TypeError, match="with_meta"):
        format_dump(store.snapshot("**"), NOW)


def test_store_dump_walks_the_whole_tree():
    store = make_store()

    assert set(dump_rows(store.dump())) == {
        "iot/dev1/prop/2.1",
        "iot/dev5/prop/3.2",
    }


def test_store_dump_takes_a_pattern_to_narrow_the_output():
    store = make_store()

    assert set(dump_rows(store.dump("iot/dev1/**"))) == {"iot/dev1/prop/2.1"}
