"""person 删/改 → 家庭档案级联的护栏单测。

delete_person 删 DB + identity_lib 后，必须再调 home_profile_service.remove_subject
清掉绑定该成员的档案条目；update_person 改名后必须调 home_profile_service.commit 触发
重渲染（subject_name 在 commit 内按成员当前 name 自动纠偏）。这两条级联若回归，被删成员的
条目会残留漂移、改名后档案标题陈旧。

不起 TestClient：直接 await 路由协程，用 SimpleNamespace 桩掉 manager 各 service，
record 调用参数做断言。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from miloco.person import router as prouter
from miloco.person.router import delete_person, update_person
from miloco.person.schema import PersonUpdate

_PID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def calls(monkeypatch):
    """桩掉 manager + identity_lib，返回记录各级联调用的 dict。"""
    rec: dict = {"remove": [], "commit": [], "lib_delete": []}

    person_service = SimpleNamespace(
        delete_person=lambda pid: None,
        update_person=lambda pid, name, role: None,
    )
    home_profile_service = SimpleNamespace(
        remove_subject=lambda pid: rec["remove"].append(pid),
        commit=lambda: rec["commit"].append(True),
    )
    monkeypatch.setattr(
        prouter, "manager",
        SimpleNamespace(
            person_service=person_service,
            home_profile_service=home_profile_service,
        ),
    )
    monkeypatch.setattr(
        prouter, "_get_identity_library",
        lambda: SimpleNamespace(
            delete_person=lambda pid: rec["lib_delete"].append(pid),
            clear_person_avatar=lambda pid: rec["clear_avatar"].append(pid),
        ),
    )
    rec["clear_avatar"] = []
    return rec


async def test_delete_person_cascades_remove_subject(calls):
    res = await delete_person(_PID, current_user="t")
    assert res.code == 0
    assert calls["lib_delete"] == [_PID]
    assert calls["remove"] == [_PID]
    # 级联清显式头像（avatars/persons/<id>.*，不在 persons/<id>/ 内）
    assert calls["clear_avatar"] == [_PID]


async def test_update_person_cascades_commit(calls):
    res = await update_person(_PID, PersonUpdate(name="新名"), current_user="t")
    assert res.code == 0
    # 改名后触发一次 commit 重渲染（subject_name 在 commit 内自动纠偏，无需路由层传名）
    assert calls["commit"] == [True]


async def test_delete_cascade_failure_does_not_block(calls, monkeypatch):
    """home_profile 级联抛错不应让删除整体 500（DB 行已删，残留可后续清）。"""
    def _boom(pid):
        raise RuntimeError("home profile down")

    monkeypatch.setattr(prouter.manager.home_profile_service, "remove_subject", _boom)
    res = await delete_person(_PID, current_user="t")
    assert res.code == 0
    assert calls["lib_delete"] == [_PID]
