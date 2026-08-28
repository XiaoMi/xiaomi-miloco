# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""作用域代号：切换账号或家庭之后，旧作用域的写入一律作废。"""

from __future__ import annotations

import miloco.manager as manager_module
import pytest
from miloco.manager import Manager


@pytest.fixture
def manager() -> Manager:
    """Manager 是单例，不重置的话代号会跨用例带过去。"""
    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    yield Manager()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None


def test_a_fresh_manager_has_not_aligned_anything(manager):
    assert manager.scope_is_aligned() is False


def test_marking_the_current_scope_makes_it_aligned(manager):
    manager.mark_scope_aligned(manager.current_scope())

    assert manager.scope_is_aligned() is True


def test_switching_invalidates_the_alignment_without_anyone_resetting_it(manager):
    """存代号不存布尔，就没有「谁负责重置」这个问题。"""
    manager.mark_scope_aligned(manager.current_scope())

    manager.begin_scope_switch()

    assert manager.scope_is_aligned() is False


def test_a_late_alignment_from_an_old_scope_cannot_erase_the_current_one(manager):
    """旧对齐迟到只该被忽略，不该把新对齐的成果抹掉。

    「标记旧代号等于没标记」是 `scope_is_aligned` 自己就成立的，不用谁去挡；真正
    要挡的是它覆盖掉一个更新的标记，那会让已经对齐好的这一代退回未对齐。
    """
    stale = manager.current_scope()
    manager.begin_scope_switch()
    manager.mark_scope_aligned(manager.current_scope())

    manager.mark_scope_aligned(stale)

    assert manager.scope_is_aligned() is True


def test_each_switch_gives_a_scope_nobody_has_seen_before(manager):
    """代号不能循环利用，否则迟到的旧写入会撞上一个恰好相等的新代号。"""
    seen = {manager.current_scope()}

    for _ in range(3):
        assert manager.begin_scope_switch() not in seen
        seen.add(manager.current_scope())


def test_the_align_task_handle_starts_empty(manager):
    """切换编排要靠它取消上一轮对齐，所以句柄挂在 manager 上而不是 lifespan 的局部变量。"""
    assert manager.state_align_task is None
