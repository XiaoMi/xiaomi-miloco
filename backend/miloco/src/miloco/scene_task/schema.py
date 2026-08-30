# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Scene rule task (场景联动任务) data models.

场景联动 = 一条 state 模式 rule 的 on_enter / on_exit 各挂一个米家自动化场景
(trigger_scene) 的「规则任务」：感知引擎 realtime_perceive 拿到 omni 结果后命中
rule 直接触发场景，完全不经过 agent（执行路径是 runner 的 static slot，见
RuleRunner._execute_scene_action）。本模块是 web「场景联动」tab 的后端——用
task + rule 两张现有表组合实现，完整复用 rule 的命中 / 抗抖 / 冷却机制：

  - enter：条件 query 从假变真并持续 enter_debounce_seconds（进入确认时间）
           → 触发 enter_scene_id；0 = 立即触发。实现上复用 Rule 的
           duration_seconds + duration_ratio=1.0（STATE 模式 ENTERED 前置
           确认门槛，见 rule/schema.py 的 duration_seconds 注释）
  - exit ：条件真变假并持续 exit_debounce_seconds → 触发 exit_scene_id；
          或配置 max_dwell_seconds 后「到期自动退出」→ 同样触发 exit_scene_id

任务启停 / 删除复用 TaskService（disable 同步 rule.enabled，delete 走 FK CASCADE）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SceneTaskCreateRequest(BaseModel):
    """创建场景联动任务。enter / exit 至少配一个场景。"""

    description: str = Field(..., min_length=1, max_length=200, description="任务名（同时作为 rule.name）")
    perceive_device_ids: list[str] = Field(..., description="感知设备 did 列表（任一命中即触发）")
    query: str = Field(..., description="进入条件（自然语言，进行时状态描述）")
    enter_scene_id: str | None = Field(None, description="进入时触发的米家场景 id；不配 = 只退出联动")
    exit_scene_id: str | None = Field(None, description="退出时触发的米家场景 id；不配 = 只进入联动")
    cooldown_minutes: int = Field(5, ge=1, le=1440, description="场景触发冷却（分钟），进入/退出共用")
    enter_debounce_seconds: int = Field(0, ge=0, le=86400, description="进入确认时间（秒）：条件持续满足这么久才触发进入场景；0=立即触发（现状）")
    exit_debounce_seconds: int = Field(60, ge=0, le=86400, description="退出确认时间（秒）：条件持续不满足这么久才触发退出场景")
    max_dwell_seconds: int | None = Field(None, ge=1, le=86400, description="最长驻留（秒）：进入后到时强制退出并触发退出场景（到期自动退出）")
    enabled: bool = Field(True, description="创建后是否立即启用")

    model_config = {'extra': 'forbid'}

    @model_validator(mode='after')
    def _at_least_one_scene(self):
        if not self.enter_scene_id and not self.exit_scene_id:
            raise ValueError('enter_scene_id / exit_scene_id 至少配置一个')
        return self


class SceneTaskUpdateRequest(BaseModel):
    """部分更新——全部字段可选。scene_id 类字段置 None = 清空该方向（但不能两个
    方向同时清空）；enter_debounce_seconds / max_dwell_seconds 置 None 或 0 =
    清除该行为（进入立即触发 / 不自动退出）；其余字段 None = 不动。
    """

    description: str | None = Field(None, min_length=1, max_length=200)
    perceive_device_ids: list[str] | None = None
    query: str | None = None
    enter_scene_id: str | None = None
    exit_scene_id: str | None = None
    cooldown_minutes: int | None = Field(None, ge=1, le=1440)
    enter_debounce_seconds: int | None = Field(None, ge=0, le=86400)
    exit_debounce_seconds: int | None = Field(None, ge=0, le=86400)
    max_dwell_seconds: int | None = Field(None, ge=1, le=86400)
    enabled: bool | None = None

    model_config = {'extra': 'forbid'}


class SceneTaskView(BaseModel):
    """场景联动任务视图（task + rule 合体）。"""

    task_id: str
    description: str
    status: Literal["active", "paused"]
    paused_at: str | None = None
    created_at: str
    updated_at: str | None = None
    rule_id: str
    enabled: bool
    query: str
    perceive_device_ids: list[str]
    enter_scene_id: str | None = None
    enter_scene_name: str | None = None
    exit_scene_id: str | None = None
    exit_scene_name: str | None = None
    cooldown_minutes: int | None = None
    enter_debounce_seconds: int = 0
    exit_debounce_seconds: int = 60
    max_dwell_seconds: int | None = None