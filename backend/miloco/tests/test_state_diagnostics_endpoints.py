# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""诊断出口里有分支的那几处。

本 codebase 没有 FastAPI TestClient，照 person/router.py 的 _validate_pool_dump_path
那个路数，把可测的部分从 web 层摘出来。
"""

from __future__ import annotations

from types import SimpleNamespace

from miloco.miot.router import build_state_dump, build_state_stats
from miloco.rule.router import build_iot_diagnostics
from miloco.state import StateStore


def _store_with(leaves: int) -> StateStore:
    store = StateStore()
    for i in range(leaves):
        store.set(f"iot/device/d{i}/prop/2.1", i, source="test")
    return store


def test_stats_without_a_writer_gives_an_empty_dict():
    """接线在 initialize() 里，端点在那之前就可达 —— 抛 AttributeError 会让诊断接口
    在最需要它的时候（启动异常）反而用不了。"""
    data = build_state_stats(_store_with(1), None)

    assert data["push"] == {}
    assert data["store"]["leaves"] == 1


def test_stats_includes_the_writer_counters():
    writer = SimpleNamespace(stats=lambda: {"prop_written": 9})

    assert build_state_stats(_store_with(0), writer)["push"] == {"prop_written": 9}


def test_dump_reports_the_pre_truncation_line_count():
    """total_lines 必须是截断前的真实行数 —— 否则调用方会把截断后的 lines 当成整棵树。"""
    data = build_state_dump(_store_with(10), "**", 3)

    assert len(data["lines"]) == 3
    assert data["total_lines"] > 3
    assert data["truncated"] is True


def test_dump_does_not_claim_truncation_when_it_fits():
    """判据用 > 不用 >=：恰好等于 limit 时没有截断。"""
    lines = build_state_dump(_store_with(4), "**", 5000)["total_lines"]
    data = build_state_dump(_store_with(4), "**", lines)

    assert data["truncated"] is False
    assert len(data["lines"]) == lines


def test_iot_diagnostics_without_a_source_says_it_is_not_running():
    """源没接上来时给一份「没在跑」而不是抛 —— 同上一条的理由。"""
    data = build_iot_diagnostics(None)

    assert data["consumer_alive"] is False
    assert data["consumer_exit"]


def test_iot_diagnostics_passes_the_source_report_through():
    source = SimpleNamespace(diagnostics=lambda: {"consumer_alive": True, "rules": {}})

    assert build_iot_diagnostics(source)["consumer_alive"] is True
