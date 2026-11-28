# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task schema 校验测试 (v2): task_id 字符集 / description 长度 /
TaskCreateRequest body 收窄。v2 后 LinkKind / TaskLinkAddRequest 已删除。
"""

import pytest
from miloco.task.schema import TaskCreateRequest
from pydantic import ValidationError


def test_task_id_must_be_snake_case_ascii():
    with pytest.raises(ValidationError):
        TaskCreateRequest(task_id="DrinkWater", description="x")
    with pytest.raises(ValidationError):
        TaskCreateRequest(task_id="喝水", description="x")
    with pytest.raises(ValidationError):
        TaskCreateRequest(task_id="", description="x")
    with pytest.raises(ValidationError):
        TaskCreateRequest(task_id="a" * 33, description="x")
    # OK
    TaskCreateRequest(task_id="drink_water", description="x")


def test_description_max_200():
    with pytest.raises(ValidationError):
        TaskCreateRequest(task_id="t", description="x" * 201)
    TaskCreateRequest(task_id="t", description="x" * 200)


def test_refs_fields_rejected_as_unknown():
    """refs 字段全部移除, body 含 refs 返 422 unknown_field。"""
    for forbidden in ("rule_refs", "cron_refs", "memory_refs"):
        with pytest.raises(ValidationError):
            TaskCreateRequest.model_validate(
                {"task_id": "t", "description": "x", forbidden: ["r1"]}
            )


def test_empty_task_now_allowed():
    TaskCreateRequest(task_id="t", description="x")


def test_boundary_actions_default_mode_is_sequential_per_slot():
    """老调用方不传这些字段时三个槽仍是全串行——新增开关不改变存量行为。"""
    from miloco.task.schema import TaskBoundaryActions

    actions = TaskBoundaryActions()
    assert actions.on_enter_execution_mode == "sequential"
    assert actions.on_exit_execution_mode == "sequential"
    assert actions.on_target_execution_mode == "sequential"


def test_actions_update_modes_are_enum_checked_per_slot():
    from miloco.task.schema import TaskActionsUpdateRequest

    req = TaskActionsUpdateRequest(
        on_enter_execution_mode="parallel_by_device",
        on_exit_execution_mode="sequential",
    )
    assert {"on_enter_execution_mode", "on_exit_execution_mode"} <= (
        req.model_fields_set
    )
    with pytest.raises(ValidationError):
        TaskActionsUpdateRequest(on_exit_execution_mode="parallel")
