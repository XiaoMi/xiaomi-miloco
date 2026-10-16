# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iid 的形态判定与命名空间路由。

严格版和宽松版共用一份形态判定，两边只在失败出口上不同；`event.` 前缀则是
「这条 iid 指哪棵子树」的唯一判据 —— 规则条件项、容器叶、spec 键三处认的是同一个串。
"""

from __future__ import annotations

import pytest
from miloco.miot.iid import (
    EVENT_IID_PREFIX,
    event_iid,
    is_event_iid,
    leaf_path,
    try_parse_iid,
)


@pytest.mark.parametrize(
    "iid, expected",
    [
        ("prop.2.1", (2, 1)),
        ("prop.0.0", (0, 0)),
        ("prop.2", None),
        ("prop.2.1.3", None),
        ("action.2.1", None),
        ("prop.a.1", None),
        ("prop..1", None),
        ("", None),
    ],
)
def test_try_parse_iid(iid, expected):
    assert try_parse_iid(iid, "prop") == expected


@pytest.mark.parametrize("prefix", ["prop", "action"])
@pytest.mark.parametrize("bad", ["{p}.2", "{p}.a.1", "{p}.2.1.3", "other.2.1", ""])
def test_the_strict_parsers_raise_on_a_bad_shape(prefix, bad):
    """严格版和宽松版共用形态判定，两边只在失败出口上不同 —— 这条钉严格版那个出口。"""
    from miloco.middleware.exceptions import ValidationException

    strict = _strict(prefix)
    with pytest.raises(ValidationException):
        strict(bad.format(p=prefix))


@pytest.mark.parametrize("prefix", ["prop", "action"])
def test_the_strict_parsers_return_the_pair(prefix):
    assert (
        _strict(prefix)(f"{prefix}.2.1")
        == try_parse_iid(f"{prefix}.2.1", prefix)
        == (2, 1)
    )


@pytest.mark.parametrize("prefix", ["prop", "action"])
def test_the_strict_parsers_reject_the_other_prefix(prefix):
    """两个前缀共用一份形态判定，但各自只认自己那个前缀。"""
    from miloco.middleware.exceptions import ValidationException

    other = "action" if prefix == "prop" else "prop"
    with pytest.raises(ValidationException):
        _strict(prefix)(f"{other}.2.1")


def _strict(prefix: str):
    from miloco.miot.service import _parse_action_iid, _parse_prop_iid

    return _parse_prop_iid if prefix == "prop" else _parse_action_iid


# ── 命名空间：`event.` 前缀 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "iid, expected",
    [
        ("event.2.1020.unlock-operation-position", True),
        ("event.2.1020", True),
        # 属性侧的历史形态不带前缀，所以「不以 event. 开头」就是属性，不需要再判
        ("prop.2.1", False),
        ("2.1", False),
        ("action.2.1", False),
        # 前缀是 `event.`（带点）而不是 `event`：裸这个串不是任何合法的 iid 段
        ("event", False),
        ("", False),
    ],
)
def test_is_event_iid(iid, expected):
    assert is_event_iid(iid) is expected


def test_the_event_iid_is_the_string_every_consumer_shares():
    """这张表的键、规则条件项的 iid、容器叶的末段认的是同一个串。

    参数名里带点时它是名字的一部分（spec 原名允许），所以这条形态不是「三段点分」。
    """
    assert event_iid(2, 1020, "unlock-operation-position") == (
        "event.2.1020.unlock-operation-position"
    )
    assert event_iid(2, 1020, "a.b.c") == "event.2.1020.a.b.c"


def test_leaf_path_routes_by_namespace():
    """`event.` 走事件子树，其余一律当属性。

    事件叶的路径段里**没有**前缀（那一段由 `/event/` 承载），规则侧的 iid 有 ——
    两边不是同一个串，反查和订阅都各按各的形态。
    """
    assert (
        leaf_path("d1", "event.2.1020.unlock-operation-position")
        == "iot/device/d1/event/2.1020.unlock-operation-position"
    )
    # 属性侧是历史形态：裸 `<siid>.<piid>`，不带 `prop.` 前缀
    assert leaf_path("d1", "2.1") == "iot/device/d1/prop/2.1"


@pytest.mark.parametrize("iid", ["eventx.2.1", "events.2.1", "eventually.2.1"])
def test_a_merely_event_looking_name_is_not_the_event_namespace(iid):
    """前缀是 `event.`（带点），不是 `event`。

    判据松一个字符，名字里恰好以 event 开头的属性（`eventx`）就会被路由到事件
    子树上去 —— 那条叶永远不会有人写，规则静默地永不触发。
    """
    assert EVENT_IID_PREFIX == "event."
    assert is_event_iid(iid) is False
