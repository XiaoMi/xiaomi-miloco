# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""快照结果的形状转换。"""

from __future__ import annotations

from miloco.state import Entry, StateStore, flatten


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
