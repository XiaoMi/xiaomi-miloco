# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""诊断出口里有分支的那几处。

本 codebase 没有 FastAPI TestClient，照 person/router.py 的 _validate_pool_dump_path
那个路数，把可测的部分从 web 层摘出来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from miloco.miot.router import build_state_dump, build_state_stats
from miloco.rule.iot_source import IotSource
from miloco.rule.router import build_iot_diagnostics
from miloco.state import StateStore


def _store_with(leaves: int) -> StateStore:
    store = StateStore()
    for i in range(leaves):
        store.set(f"iot/device/d{i}/prop/2.1", i, source="test")
    return store


def test_stats_without_a_writer_gives_an_empty_dict():
    """摘成纯函数就是为了不起整个 Manager 也能覆盖到这个分支。"""
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
    """源没接上来时给一份「没在跑」而不是抛。"""
    data = build_iot_diagnostics(None)

    assert data["consumer_alive"] is False
    assert data["consumer_exit"]


def test_iot_diagnostics_keeps_one_shape_whether_the_source_is_up():
    """两个分支同形。少几个键的那一份会让按固定键取值的调用方拿到 KeyError，
    而拿不到的正是「源根本没起来」这一刻。"""
    source = IotSource(
        store=StateStore(),
        feed=lambda *a, **k: None,
        mark_unknown=lambda *a: None,
        iot_refs=lambda: [],
        ref_of_rule=lambda _rule_id: None,
    )

    assert set(build_iot_diagnostics(None)) == set(source.diagnostics())


def test_iot_diagnostics_passes_the_source_report_through():
    source = SimpleNamespace(diagnostics=lambda: {"consumer_alive": True, "rules": {}})

    assert build_iot_diagnostics(source)["consumer_alive"] is True


# ── 错误路径 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_invalid_pattern_becomes_a_400(monkeypatch):
    """本模块的 HTTPException 是 miloco 自己那个 `(message, status_code)`，不是
    FastAPI 的 `(status_code, detail)` —— 传错会 TypeError 变 500。

    这条错误路径此前没有任何测试走过：`build_state_dump` 的单测只喂合法 pattern。
    """
    from miloco.middleware.exceptions import BadRequestException
    from miloco.miot import router as miot_router

    # state_store 是只读 property，改它背后那个字段
    monkeypatch.setattr(miot_router.manager, "_state_store", _store_with(1))

    # 末段落在中间节点、一片叶子都没收到 —— snapshot 对这种 pattern 抛 ValueError
    with pytest.raises(BadRequestException) as excinfo:
        await miot_router.get_state_dump(
            pattern="iot/device/d0", limit=10, current_user="u"
        )

    assert excinfo.value.http_status == 400


def test_safe_log_strips_newlines():
    """用户可控的值里带换行符就能伪造出一整行日志。"""
    from miloco.utils.common import safe_log

    assert safe_log("a\r\nERROR fake line") == "a ERROR fake line"
