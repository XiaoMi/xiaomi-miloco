# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 数据模型 — task SSOT (v2)。

v2 起 task_link 表已 DROP: rule 关联走 rule.task_id FK CASCADE, cron 关联
走 cron.task_id FK CASCADE。前端 / agent 从 rule_briefs + cron_refs 两个独立
字段读归属。
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

_TASK_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class TaskCreateRequest(BaseModel):
    """``POST /tasks`` 入参 (v2)。

    body 收窄为 ``{task_id, description}`` — task create 仅建占位 task 行,
    rule / cron / record 关联挂载由后续 endpoint 完成:

    - rule create endpoint 内部 INSERT rule (rule.task_id FK 挂载)
    - ``POST /crons`` internal cron 装配 (阶段 3)
    - ``POST /tasks/{id}/record`` record 直连 task (FK CASCADE)
    """

    model_config = {"extra": "forbid"}

    task_id: str = Field(..., description="snake_case，[a-z0-9_]{1,32}")
    description: str = Field(..., max_length=200, description="≤200 字符")

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, v: str) -> str:
        if not _TASK_ID_RE.match(v):
            raise ValueError(
                f"task_id 必须匹配 [a-z0-9_]{{1,32}}，收到: {v!r}"
            )
        return v


class TaskUpdateRequest(BaseModel):
    """`PATCH /tasks/{task_id}` 改 description。"""

    description: str = Field(..., max_length=200)


class RuleBrief(BaseModel):
    """`task get` / `task list` 中的实时 rule 摘要。

    两组「动作」字段并列, 用途不同, 都要保留:

    - ``actions_desc``: 压平后的人话摘要 (含 ``on_enter:`` / ``on_exit:`` 前缀,
      设备直控与 agent 文案混在一列)。给 agent 去重比对 / CLI 展示用, 紧凑但
      不可逆解析 —— **别拿它回填编辑框**。
    - ``mode`` + ``action_descriptions`` / ``on_*_desc`` / ``device_actions``:
      结构化原值, 给 Web 就地编辑用。``device_actions`` 是设备直控摘要, 只读
      —— 设备动作是结构化 JSON, 不在 Web 上手改。

    哪几个 desc 槽位**能改**由 ``editable_desc_slots`` 说了算, 前端别自己按 mode
    推: V3 校验矩阵 (``rule/service.py:_validate_rule_consistency``) 规定同一槽位
    的「设备直控」与「agent 文案」互斥 —— 走设备直控的槽位再填 desc 会被 422 挡下。
    该判定与校验矩阵同源, 放在 backend 才不会两边漂移。

    ``name`` 随 rule 一并回传: 它跟 ``query`` 一起进 omni system prompt
    (见 ``perception/engine/api.py`` 装 ``RuleCondition``), 是「名称」里唯一
    真正影响感知判定的字段, 故也开放编辑。
    """

    rule_id: str
    name: str = ""
    mode: Literal["event", "state"] = "event"
    query: str
    actions_desc: list[str] = Field(default_factory=list)
    action_descriptions: list[str] = Field(default_factory=list)
    on_enter_desc: str | None = None
    on_exit_desc: str | None = None
    on_target_desc: str | None = None
    device_actions: list[str] = Field(default_factory=list)
    editable_desc_slots: list[
        Literal[
            "action_descriptions",
            "on_enter_desc",
            "on_exit_desc",
            "on_target_desc",
        ]
    ] = Field(default_factory=list)


class CronRef(BaseModel):
    """task 名下的 cron 引用 (v2 新增, 与 rule_briefs 并列)。"""

    ref: str
    dispatch_owner: Literal["internal", "external"]


class TaskFullView(BaseModel):
    """`GET /tasks/{task_id}` 返回。"""

    task_id: str
    description: str
    status: Literal["active", "paused"]
    paused_at: str | None = None
    created_at: str
    rule_briefs: list[RuleBrief] = Field(default_factory=list)
    cron_refs: list[CronRef] = Field(default_factory=list)


class PendingOp(BaseModel):
    """agent 待执行的 cron 操作。source 缺省 openclaw, 保留供未来接别的 agent。"""

    kind: Literal["cron"]
    ref: str
    action: Literal[
        "disable",
        "enable",
        "remove",
    ]
    source: Literal["openclaw"] = "openclaw"


class BackendSyncRuleResult(BaseModel):
    rule_id: str
    result: Literal["ok", "fail", "not_found"]


class BackendSyncResult(BaseModel):
    meta_status: Literal["ok", "noop"]
    rules: list[BackendSyncRuleResult] = Field(default_factory=list)


class TaskDisableResult(BaseModel):
    task_id: str
    status: Literal["active", "paused"]
    backend_synced: BackendSyncResult
    agent_pending: list[PendingOp] = Field(default_factory=list)


class TaskDeleteBackendSynced(BaseModel):
    rules_deleted: list[str] = Field(default_factory=list)


class TaskDeleteResult(BaseModel):
    task_id: str
    backend_synced: TaskDeleteBackendSynced
    agent_pending: list[PendingOp] = Field(default_factory=list)


# ── task summary 视图(spec 2026-06-11) ──────────────────────────────────────


class WindowRemaining(BaseModel):
    """距当前 window 边界(当日 24:00 上海时区)的剩余时间。

    window='all' 时上层传 None,本对象不构造。
    """

    seconds: int
    display: str


class ActiveSession(BaseModel):
    """duration kind 当前活跃 session;非 duration kind 恒为 None。"""

    started_at: str
    elapsed_minutes: int


class RecordSummary(BaseModel):
    """summary 接口里单个 task 的 record 摘要。

    derived 字段按 kind 不同形态不同(progress/duration/event),
    由 ``TaskRecordService.list_active_summaries`` 拼装。
    """

    kind: Literal["progress", "duration", "event"]
    completed: bool
    active_session: ActiveSession | None
    window_remaining: WindowRemaining | None
    derived: dict[str, Any]


class TaskSummaryView(TaskFullView):
    """summary 接口返回的单条 view,继承 TaskFullView 全部字段 + 追加 record。"""

    record: RecordSummary | None = None
