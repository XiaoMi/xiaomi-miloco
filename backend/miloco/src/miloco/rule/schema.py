# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Rule data models (V3)

V3 design source of truth: ~/work/newdoc/miloco-rule/v3-system-overview.md (§3.1 / §6.3).

Key V3 changes vs V1:
- RuleAction uses {did, iid, value/params, idempotent, cooldown_minutes} format,
  with idempotent check and cooldown dedup dispatched by the runner (§6.3).
- Rule has new fields: task_id, mode, lifecycle, on_enter_actions, on_enter_desc,
  on_exit_actions, on_exit_desc, terminate_when, exit_debounce_seconds.
- 每条 rule 在 fire 时按字段非空隐式选择执行路径——`actions` / `on_*_actions`
  走设备直控，`action_descriptions` / `on_*_desc` 走 Agent 回调；两者互斥。
- RuleExecuteResult adds event field (ENTERED / EXITED).
- New types: RuleMode, RuleLifecycle, RuleEvent, RuleLogKind, RuleTriggerCallback.

Validation (mode x type matrix, lifecycle constraints) is performed at the
service layer, not via pydantic validators -- this keeps the schema file
tidy and lets PATCH-style partial updates merge with the persisted Rule before
validation.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuleMode(str, Enum):
    """event: only on_enter (single trigger).
    state: on_enter + on_exit (paired, with debounce on exit)."""

    EVENT = "event"
    STATE = "state"


class RuleLifecycle(str, Enum):
    """permanent: until user deletes.
    temporary: agent evaluates terminate_when and self-deletes."""

    PERMANENT = "permanent"
    TEMPORARY = "temporary"


class RuleTriggerType(str, Enum):
    """Rule trigger source."""

    PERCEPTION = "perception"
    MIOT_EVENT = "miot_event"


class RuleEvent(str, Enum):
    """Frame-diff events emitted by RuleRunner."""

    ENTERED = "ENTERED"
    EXITED = "EXITED"
    STILL_IN = "STILL_IN"
    STILL_OUT = "STILL_OUT"
    # duration record 累计达标瞬间触发（rule engine 内部 timer 驱动，与 condition diff 无关）
    TARGET_FIRED = "TARGET_FIRED"


class RuleAction(BaseModel):
    """V3 action format (per latest v3-system-overview.md §6.3 / §5.5 Step 4c).

    Two shapes share the same model:

    - **Device control** (idempotent, e.g. light on / set temperature)::

          {"did": "<id>", "iid": "prop.<siid>.<piid>", "value": <val>,
           "idempotent": true}

    - **Notify / TTS** (non-idempotent, must declare a cooldown)::

          {"did": "<id>", "iid": "action.<siid>.<aiid>", "params": ["<text>"],
           "idempotent": false, "cooldown_minutes": 10}

    The two shapes are distinguished by ``iid`` prefix (``prop.`` vs
    ``action.``) and by which payload field is set (``value`` vs ``params``).
    There is no ``type`` field on RuleAction itself.

    Validation note: ``idempotent=False`` requires ``cooldown_minutes``.
    Service / cli layers enforce this; the schema keeps both fields optional
    so PATCH-style partial updates are not blocked when only one is sent.
    """

    did: str = Field(..., description="Device ID")
    iid: str = Field(
        ...,
        description="Property/action iid: prop.{siid}.{piid} or action.{siid}.{aiid}",
    )
    value: Any = Field(None, description="Property value (for prop.* iid)")
    params: list[Any] | None = Field(
        None, description="Action params (for action.* iid)"
    )
    idempotent: bool = Field(
        True,
        description="True: query current state and skip if already at target. "
        "False: use cooldown_minutes to deduplicate.",
    )
    cooldown_minutes: int | None = Field(
        None,
        description="Cooldown for non-idempotent actions; required when idempotent=False.",
    )


class RuleCondition(BaseModel):
    """Rule condition: which perception devices to watch + what to look for."""

    perceive_device_ids: list[str] = Field(
        default_factory=list,
        description="Perception device IDs (OR semantics: any match triggers)",
    )
    query: str = Field(..., description="Natural language condition description")
    source_ids: list[str] = Field(
        default_factory=list,
        description="MiOT event source IDs (device did / scene id)",
    )
    event_kinds: list[str] = Field(
        default_factory=list,
        description="MiOT event kinds, e.g. device_prop / scene",
    )
    property_filters: dict[str, Any] = Field(
        default_factory=dict,
        description="MiOT property filters keyed by property path/name",
    )
    mapping_ids: list[str] = Field(
        default_factory=list,
        description="Explicit mapping IDs used by miot_event trigger",
    )
    use_global_mapping: bool = Field(
        default=True,
        description="Whether the rule should use source-level global mapping",
    )


class Rule(BaseModel):
    """Rule data model (V3)."""

    id: str = Field("", description="Rule ID (UUID)")
    name: str = Field(..., description="Rule display name (free text)")
    task_id: str = Field(..., description="Task id (snake_case)")
    trigger_type: RuleTriggerType = Field(
        RuleTriggerType.PERCEPTION,
        description="Trigger source: perception or miot_event",
    )
    mode: RuleMode = Field(RuleMode.EVENT, description="event or state")
    lifecycle: RuleLifecycle = Field(
        RuleLifecycle.PERMANENT, description="permanent or temporary"
    )
    enabled: bool = Field(True, description="Whether the rule is enabled")
    condition: RuleCondition = Field(..., description="Trigger condition")

    # event mode fields (mutually exclusive; one of the two must be non-empty)
    actions: list[RuleAction] = Field(
        default_factory=list,
        description="event 模式设备直控动作；state 模式下忽略",
    )
    action_descriptions: list[str] = Field(
        default_factory=list,
        description="event 模式 Agent 回调提示文本；state 模式下忽略",
    )

    # state mode fields (on_enter / on_exit independent;
    # at most one of {actions, desc} per direction; at least one direction non-empty)
    on_enter_actions: list[RuleAction] = Field(
        default_factory=list,
        description="state on_enter 设备直控动作；event 模式下忽略",
    )
    on_enter_desc: str | None = Field(
        None,
        description="state on_enter Agent 回调提示文本；event 模式下忽略",
    )
    on_exit_actions: list[RuleAction] = Field(
        default_factory=list,
        description="state on_exit 设备直控动作；event 模式下忽略",
    )
    on_exit_desc: str | None = Field(
        None,
        description="state on_exit Agent 回调提示文本；event 模式下忽略",
    )
    on_target_desc: str | None = Field(
        None,
        description=(
            "state on_target Agent 回调提示文本（duration record 累计达标瞬间触发）。"
            "仅在 task 配 duration record + target_minutes 时有效；event 模式下忽略。"
        ),
    )

    # lifecycle / runtime tuning
    terminate_when: str | None = Field(
        None,
        description="Natural language terminate condition (lifecycle=temporary only)",
    )
    exit_debounce_seconds: int = Field(
        60, ge=0, description="state mode exit debounce in seconds"
    )
    duration_seconds: int | None = Field(
        None,
        ge=1,
        description=(
            "累计统计窗口（秒）。设置后窗口内 True 比例达 duration_ratio 才 fire。"
            "None=立即 fire（现状）。EVENT mode：fire 后清窗口走周期 fire。"
            "STATE mode：作为 ENTERED 前置确认门槛，达标 fire on_enter 一次，"
            "STILL_IN 期间不重复 fire；EXITED 走 exit_debounce_seconds，"
            "未达标就 EXITED 不 fire on_exit。"
        ),
    )
    duration_ratio: float | None = Field(
        None,
        gt=0.0,
        le=1.0,
        description=(
            "窗口内 True 比例阈值，仅 duration_seconds 设置时生效。"
            "None 时 service 创建/更新规则时用 settings.rule.default_duration_ratio "
            "回填（代码默认 0.6，可由 settings.yaml / config.json / env 覆盖）。"
            "1.0=必须全程 True。"
        ),
    )

    created_at: str | None = Field(None, description="Creation time (ISO 8601)")
    updated_at: str | None = Field(None, description="Last update time (ISO 8601)")


class RuleConditionUpdate(BaseModel):
    """Partial condition update -- both fields optional.

    Used by ``RuleUpdate.condition`` so PATCH can change one of
    ``perceive_device_ids`` / ``query`` without forcing the caller to resend
    the full RuleCondition. Service layer merges set fields into the
    persisted Rule.condition.
    """

    perceive_device_ids: list[str] | None = Field(None)
    query: str | None = Field(None)
    source_ids: list[str] | None = Field(None)
    event_kinds: list[str] | None = Field(None)
    property_filters: dict[str, Any] | None = Field(None)
    mapping_ids: list[str] | None = Field(None)
    use_global_mapping: bool | None = Field(None)


class RuleUpdate(BaseModel):
    """Partial update model -- all fields optional.

    Matrix validation is applied at service layer after merging with existing Rule.
    """

    name: str | None = Field(None)
    task_id: str | None = Field(None)
    trigger_type: RuleTriggerType | None = Field(None)
    mode: RuleMode | None = Field(None)
    lifecycle: RuleLifecycle | None = Field(None)
    enabled: bool | None = Field(None)
    condition: RuleConditionUpdate | None = Field(None)
    actions: list[RuleAction] | None = Field(None)
    action_descriptions: list[str] | None = Field(None)
    on_enter_actions: list[RuleAction] | None = Field(None)
    on_enter_desc: str | None = Field(None)
    on_exit_actions: list[RuleAction] | None = Field(None)
    on_exit_desc: str | None = Field(None)
    on_target_desc: str | None = Field(None)
    terminate_when: str | None = Field(None)
    exit_debounce_seconds: int | None = Field(None, ge=0)
    duration_seconds: int | None = Field(None, ge=1)
    duration_ratio: float | None = Field(None, gt=0.0, le=1.0)


class RuleTriggerRequest(BaseModel):
    """Manual trigger debug entry -- fires the rule's ENTER slot only.

    Debug-only: EXIT is not synthesized today (see RuleRunner.trigger_rule
    docstring for the state-bridging caveat). For state-mode rules, exercise
    the on_exit / debounce paths via real perception instead.
    """

    context: str = Field(default="", description="Trigger context from the caller")


class RuleTriggerCallback(BaseModel):
    """规则 fire 时 in-process 投递给 OpenClaw plugin runtime 的载荷
    （Agent 回调路径专用——`action_descriptions` / `on_*_desc` slot 命中时构造）。
    Not an HTTP webhook; lives within the same process.

    `trigger_kind="rule_dynamic"` 是与 OpenClaw 侧约定的协议字段值（外部契约），
    保留历史命名不动。

    Reference: v3-system-overview.md §6.6.2
    """

    trigger_kind: str = Field(default="rule_dynamic")
    rule_id: str = Field(...)
    rule_name: str = Field(...)
    event: RuleEvent = Field(..., description="ENTERED / EXITED / TARGET_FIRED")
    triggered_at: str = Field(..., description="ISO 8601 timestamp with timezone")
    source: list[str] = Field(..., description="did(s) responsible for the trigger")
    # 当前设计每个 rule 只对应一个感知设备，不存在同 cycle 多 room 命中
    # 同一 rule 的歧义——room_name 即该设备所在房间。
    room_name: str = Field(
        default="",
        description="Room name of the matched frame's device "
        "(ENTERED only; empty on EXITED)",
    )
    source_device_ids: list[str] = Field(
        default_factory=list,
        description="Device IDs of the matched frame (ENTERED only; empty on EXITED)",
    )
    prompt_text: str = Field(
        ...,
        description="action_descriptions / on_enter_desc / on_exit_desc / on_target_desc "
        "full text with tags / task_id / terminate_when metadata",
    )
    session: str = Field(default="isolated")
    caption: str = Field(default="", description="Caption from perception (if available)")
    trigger_reason: str = Field(default="", description="Why the rule fired (from MatchedRule.reason)")
    device_name: str = Field(default="", description="Source camera display name")
    rule_query: str = Field(default="", description="Rule condition query (from rule.condition.query)")


# ---- Execution result & log models ----


class RuleActionExecuteResult(BaseModel):
    """Single action execution result."""

    action: RuleAction = Field(..., description="The action that was executed")
    result: bool = Field(..., description="Whether execution succeeded")
    skipped: bool = Field(
        False,
        description="True when execution was skipped due to idempotent check "
        "(value already at target) or cooldown window not yet elapsed.",
    )
    error: str | None = Field(
        None, description="Error message when result=False"
    )


class RuleExecuteResult(BaseModel):
    """规则一次 fire 的汇总结果。执行路径由非空字段隐式表达：
    ``action_results`` 非空 → 走了设备直控；``dynamic_rule_event_sent=True`` →
    走了 Agent 回调。两者互斥。
    """

    event: RuleEvent = Field(..., description="Which diff event triggered execution")
    action_results: list[RuleActionExecuteResult] = Field(
        default_factory=list, description="设备直控逐 action 派发结果"
    )
    dynamic_rule_event_sent: bool = Field(
        False, description="是否已向 Agent 投递回调"
    )


class RuleLogKind(str, Enum):
    """Log kind for rule_log entries (v3-system-overview.md §11.2)."""

    RULE_TRIGGER_SUCCESS = "RULE_TRIGGER_SUCCESS"
    RULE_TRIGGER_FAILURE = "RULE_TRIGGER_FAILURE"


class RuleLog(BaseModel):
    """Rule execution log."""

    id: str | None = Field(None, description="Log ID (UUID)")
    timestamp: int = Field(..., description="Trigger time (millisecond Unix timestamp)")
    kind: RuleLogKind = Field(
        default=RuleLogKind.RULE_TRIGGER_SUCCESS, description="Log kind"
    )
    rule_id: str = Field(..., description="Rule ID")
    rule_name: str = Field(..., description="Rule name")
    rule_query: str = Field(..., description="Rule condition query")
    trigger_context: str = Field("", description="Trigger context from the caller")
    execute_result: RuleExecuteResult | None = Field(
        None, description="Execution result"
    )
