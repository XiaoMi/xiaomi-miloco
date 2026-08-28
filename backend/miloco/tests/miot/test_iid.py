# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""prop iid 的形态判定。严格版和宽松版共用它，两边只在失败出口上不同。"""

from __future__ import annotations

import pytest
from miloco.miot.iid import try_parse_prop_iid


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
def test_try_parse_prop_iid(iid, expected):
    assert try_parse_prop_iid(iid) == expected


@pytest.mark.parametrize("iid", ["prop.2", "action.2.1", "prop.a.1", ""])
def test_the_strict_parser_raises_on_a_bad_shape(iid):
    """严格版和宽松版共用形态判定，两边只在失败出口上不同 —— 这条钉严格版那个出口。"""
    from miloco.middleware.exceptions import ValidationException
    from miloco.miot.service import _parse_prop_iid

    with pytest.raises(ValidationException):
        _parse_prop_iid(iid)


def test_the_strict_parser_returns_the_same_pair():
    from miloco.miot.service import _parse_prop_iid

    assert _parse_prop_iid("prop.2.1") == try_parse_prop_iid("prop.2.1") == (2, 1)
