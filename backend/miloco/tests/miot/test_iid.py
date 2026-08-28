# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""prop iid 的形态判定。严格版和宽松版共用它，两边只在失败出口上不同。"""

from __future__ import annotations

import pytest
from miloco.miot.iid import try_parse_iid


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
