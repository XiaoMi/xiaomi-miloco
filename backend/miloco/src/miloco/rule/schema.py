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

from collections.abc import Iterable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuleMode(str, Enum):
    """event: only on_enter (single trigger).
    state: on_enter + on_exit (paired, with debounce on exit)."""

    EVENT = "event"
    STATE = "state"


class RuleDirection(str, Enum):
    """rule 的边沿如何映射成 task 的进/出 —— 取代 ``mode``。

    expand-contract 阶段 A 期间两者并存: 本字段为空时按 ``mode`` 推导
    (``_MODE_TO_DIRECTION``), 读侧一律走 ``Rule.resolved_direction``。
    """

    ENTER = "enter"
    EXIT = "exit"
    SESSION = "session"
    MILESTONE = "milestone"


_MODE_TO_DIRECTION: dict[str, RuleDirection] = {
    RuleMode.EVENT.value: RuleDirection.ENTER,
    RuleMode.STATE.value: RuleDirection.SESSION,
}


class RuleLifecycle(str, Enum):
    """permanent: until user deletes.
    temporary: agent evaluates terminate_when and self-deletes."""

    PERMANENT = "permanent"
    TEMPORARY = "temporary"


class RuleEvent(str, Enum):
    """Frame-diff events emitted by RuleRunner."""

    ENTERED = "ENTERED"
    EXITED = "EXITED"
    STILL_IN = "STILL_IN"
    STILL_OUT = "STILL_OUT"
    # duration record 累计达标瞬间触发（rule engine 内部 timer 驱动，与 condition diff 无关）
    TARGET_FIRED = "TARGET_FIRED"


class TriggerOutcome(str, Enum):
    """一次 ``update_state`` 判定的「触发结论」——供住户日志展示「本周期是否真触发」。

    反映的是**状态机是否派发了一次触发**，不含 agent / 设备下游执行成败。中性枚举：
    不含住户可见文案（中文标签由展示层 event_text_builder 映射）。
    """

    # 注：FIRED 严格是「决策层到达 fire 点」，**不保证住户那边有任何可感知的结果**——下游
    # 有三种情形会让它落空，都在本枚举语义之外（也判不出，故不在展示层区分）：
    #   ① 该方向 slot 为空（exit-only 规则的进入方向）：slot 空要到异步 _fire 才发现；
    #   ② 动作被冷却 / 幂等压制：**仅静态直控 slot**——_execute_action 走 skipped 分支、
    #      设备不动作（dynamic agent 回调那条路 _execute_dynamic 没有 cooldown，不受本条影响）；
    #   ③ agent / 设备执行失败：合批 + agent 不回报，miloco 侧拿不到结果。
    # 故「空 slot」归 FIRED、不归 NOT_FIRED。
    FIRED = "FIRED"          # 本周期到达 fire 决策点（ENTER 边沿 / 计时达标；含空 slot）
    STILL_IN = "STILL_IN"    # 已在态内、条件持续满足，不重复触发（含吸收伪退出/抖动）
    COUNTING = "COUNTING"    # duration 规则累积中：窗口未满 / 比例未达 / 同 round 去重
    NOT_FIRED = "NOT_FIRED"  # 抗抖观察 / 其它未触发


# 聚合优先级 FIRED > COUNTING > STILL_IN > NOT_FIRED。模块级常量，避免每次访问重建字典。
_OUTCOME_PRIORITY: dict[TriggerOutcome, int] = {
    TriggerOutcome.FIRED: 3,
    TriggerOutcome.COUNTING: 2,
    TriggerOutcome.STILL_IN: 1,
    TriggerOutcome.NOT_FIRED: 0,
}


def aggregate_outcomes(outcomes: Iterable[TriggerOutcome]) -> TriggerOutcome | None:
    """同一 rule 本周期多摄像头的结论聚合：取「最强」信号
    （FIRED > COUNTING > STILL_IN > NOT_FIRED）。空输入返回 None。

    未在 ``_OUTCOME_PRIORITY`` 里映射的成员按最弱（-1）处理、不抛 KeyError：本函数在感知
    client 的 ``finally`` 落库路径上被调用，抛异常会顶替掉 ``try`` 里的原始异常、并打掉
    「循环抛异常本 cycle 仍能落库」那条韧性设计。缺映射成员**单独**出现时聚合原样返回它，
    由展示层 ``_OUTCOME_LABEL.get(outcome, "")`` 兜底成空串 → 省略整行；但同 rule 另有已
    映射成员时，缺映射成员会因 -1 落选，日志展示的是那个**已映射（更弱）**的标签而非省略。
    故新增枚举成员必须同步补进 ``_OUTCOME_PRIORITY`` 与 ``_OUTCOME_LABEL``——两张表的覆盖度
    由 test_rule.py / test_event_text_builder.py 的完整性测试在 CI 拦住（fail-safe 的代价是
    漏补映射运行时不再报错，只能靠那两条测试暴露）。
    """
    return max(outcomes, key=lambda o: _OUTCOME_PRIORITY.get(o, -1), default=None)


SCENE_IID = "scene"
"""``iid`` 哨兵值：这条 action 触发一个米家场景，``did`` 位置放 scene_id。

场景没有 siid/aiid 可拆，也读不到现值，所以既不能走 ``prop.``/``action.`` 的
iid 解析，也没法做幂等比对——只能靠冷却去重。``did`` 借位放 scene_id 与
``MiotService.trigger_scene`` 落台账的既有做法一致，同时让冷却键
``(did, iid)`` 能按场景隔离；``did`` 若留空，同一条规则的多个场景会共用一个
冷却槽、互相把对方压掉。
"""


def parse_device_iid(iid: str) -> tuple[bool, int, int] | None:
    """拆 ``prop.<siid>.<piid>`` / ``action.<siid>.<aiid>``。

    返回 ``(是否属性, siid, piid/aiid)``；不是这两种形态（含 ``scene`` 和
    ``prop.2`` 这类缺段写法）返回 ``None``。CRUD 校验和执行分流共用这一份,
    两侧判定不会漂移。
    """
    is_prop = iid.startswith("prop.")
    if not is_prop and not iid.startswith("action."):
        return None
    parts = iid.split(".")
    if len(parts) != 3:
        return None
    try:
        return is_prop, int(parts[1]), int(parts[2])
    except ValueError:
        return None


class RuleAction(BaseModel):
    """V3 action format (per latest v3-system-overview.md §6.3 / §5.5 Step 4c).

    Three shapes share the same model:

    - **Device control** (idempotent, e.g. light on / set temperature)::

          {"did": "<id>", "iid": "prop.<siid>.<piid>", "value": <val>,
           "idempotent": true}

    - **Notify / TTS** (non-idempotent, must declare a cooldown)::

          {"did": "<id>", "iid": "action.<siid>.<aiid>", "params": ["<text>"],
           "idempotent": false, "cooldown_minutes": 10}

    - **Scene trigger** (non-idempotent, must declare a cooldown)::

          {"did": "<scene_id>", "iid": "scene",
           "idempotent": false, "cooldown_minutes": 5}

    The shapes are distinguished by ``iid`` (``prop.`` / ``action.`` prefix, or
    the bare ``scene`` sentinel) and by which payload field is set (``value`` /
    ``params`` / neither). There is no ``type`` field on RuleAction itself.

    Validation note: ``idempotent=False`` requires ``cooldown_minutes``, and
    ``iid=scene`` requires ``idempotent=False``. Service / cli layers enforce
    both; the schema keeps the fields optional so PATCH-style partial updates
    are not blocked when only one is sent.
    """

    did: str = Field(
        ..., description="Device ID; scene_id when iid is 'scene'"
    )
    iid: str = Field(
        ...,
        description=(
            "prop.{siid}.{piid} / action.{siid}.{aiid} / 'scene' (did=scene_id)"
        ),
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
        ..., description="Perception device IDs (OR semantics: any match triggers)"
    )
    query: str = Field(..., description="Natural language condition description")


class ConditionItem(BaseModel):
    """一个触发源上的一个条件。

    ``source_type`` 不叫 ``source``: CLI 现有 ``--source <DID>`` 指感知摄像头,
    这个字段指触发源类型, 撞名会让 agent 把摄像头 did 写成类型串且不报错。

    ``spec`` 按源多态, 本次不上多态模型 —— 求值未落地, 存进来只需原样round-trip。
    """

    source_type: str = Field("omni", description="omni / record / iot / presence")
    spec: dict[str, Any] = Field(default_factory=dict)
    negate: bool = Field(False, description="本次校验强制 false")


class RuleConditionDNF(BaseModel):
    """外层 OR、内层 AND。本次校验强制 1×1, 与旧 ``RuleCondition`` 完全等价。"""

    any_of: list[list[ConditionItem]] = Field(default_factory=list)


class Rule(BaseModel):
    """Rule data model (V3)."""

    id: str = Field("", description="Rule ID (UUID)")
    name: str = Field(..., description="Rule display name (free text)")
    task_id: str = Field(..., description="Task id (snake_case)")
    mode: RuleMode = Field(RuleMode.EVENT, description="event or state")
    lifecycle: RuleLifecycle = Field(
        RuleLifecycle.PERMANENT, description="permanent or temporary"
    )
    enabled: bool = Field(True, description="Whether the rule is enabled")
    condition: RuleCondition = Field(..., description="Trigger condition")

    # expand-contract 阶段 A 新增, 与 mode / condition 并存。为空 = 该 rule 还没
    # 迁移过 (存量库跑迁移前, 或内存里直接构造的), 读侧回退到旧字段。
    direction: RuleDirection | None = Field(None, description="取代 mode, 见 §4.1")
    condition_dnf: RuleConditionDNF | None = Field(None, description="DNF 结构")

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

    @property
    def resolved_direction(self) -> RuleDirection:
        """读侧唯一入口: 有 direction 用它, 没有按 mode 推。

        阶段 A 期间两个字段并存, 直接读 ``direction`` 会在存量数据和内存构造的
        Rule 上拿到 None; 直接读 ``mode`` 则表达不了 exit / milestone。
        """
        if self.direction is not None:
            return self.direction
        return _MODE_TO_DIRECTION.get(self.mode.value, RuleDirection.ENTER)


class RuleConditionUpdate(BaseModel):
    """Partial condition update -- both fields optional.

    Used by ``RuleUpdate.condition`` so PATCH can change one of
    ``perceive_device_ids`` / ``query`` without forcing the caller to resend
    the full RuleCondition. Service layer merges set fields into the
    persisted Rule.condition.
    """

    perceive_device_ids: list[str] | None = Field(None)
    query: str | None = Field(None)


class RuleUpdate(BaseModel):
    """Partial update model -- all fields optional.

    Matrix validation is applied at service layer after merging with existing Rule.
    """

    name: str | None = Field(None)
    task_id: str | None = Field(None)
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
