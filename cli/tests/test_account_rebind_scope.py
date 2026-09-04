"""绑回同一账号时，CLI 不能再跑一遍选家流程。

后端保住了家庭白名单，CLI 的 `_submit_authorize` 若照旧无条件
`api_put("/api/miot/scope/homes", ...)`，转头就把它覆盖掉——后端那一半修复
对 CLI 主路径等于没做。
"""

from __future__ import annotations

from unittest.mock import patch

from miloco_cli.commands.account import _submit_authorize


def _run(auth_data):
    """跑一次 _submit_authorize，返回 (是否拉过家庭列表, 是否写过启用家庭)。"""
    with (
        patch("miloco_cli.client.api_post", return_value=auth_data) as post,
        patch("miloco_cli.client.api_get", return_value={"data": [
            {"home_id": "h1", "home_name": "客厅"},
            {"home_id": "h2", "home_name": "父母家"},
        ]}) as get,
        patch("miloco_cli.client.api_put") as put,
    ):
        _submit_authorize("code", "state", pretty=False)
        assert post.called
        return get.called, put.called


def test_same_account_skips_home_selection():
    listed, wrote = _run({
        "code": 0,
        "data": {"account_changed": False, "scope_preserved": True},
    })
    assert not listed, "同账号重绑不该再拉家庭列表"
    assert not wrote, "同账号重绑绝不能覆写家庭白名单"


def test_account_changed_still_selects_home():
    listed, wrote = _run({
        "code": 0,
        "data": {"account_changed": True, "scope_preserved": False},
    })
    assert listed, "换账号仍要走选家流程"
    assert wrote


def test_old_backend_without_field_keeps_previous_behaviour():
    """老后端返回 data=None，取不到字段就按原行为走。"""
    listed, wrote = _run({"code": 0, "data": None})
    assert listed
    assert wrote
