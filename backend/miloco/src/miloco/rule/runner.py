# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Rule runner (V3).

Frame-level boolean reports come in via ``update_state(rule_id, source_did,
current_bool, context)``. The runner aggregates per-source state with OR to
get the rule-level state, diffs against the previous tick, and emits one
of four events:

    false -> true     ENTERED
    true  -> true     STILL_IN
    true  -> false    EXITED
    false -> false    STILL_OUT

Only ENTERED and (debounced) EXITED reach the action layer. STILL_* and empty
slots return silently without writing logs.

For state mode, ``on_enter`` and ``on_exit`` are independent slots. Each slot
either holds a list of actions (设备直控路径) or a single prompt text (Agent
回调路径); the runner picks the path by which field is non-empty. Either
direction may be empty -- as long as at least one direction is configured.

Reference: rule-design.md §7
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Mapping

if TYPE_CHECKING:
    from miloco.task_record.service import TaskRecordService

from miot.types import MIoTActionParam, MIoTGetPropertyParam, MIoTSetPropertyParam

from miloco.database.rule_repo import RuleLogRepo
from miloco.dispatch import dispatch_event
from miloco.miot.client import MiotProxy
from miloco.node_monitor import NodeName, get_monitor
from miloco.observability.metrics_client import get_metrics_client
from miloco.rule.record_source import RECORD_SOURCE_DID, RecordSource, record_ref_of
from miloco.rule.schema import (
    SCENE_IID,
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleDirection,
    RuleEvent,
    RuleExecuteResult,
    RuleLog,
    RuleLogKind,
    RuleTriggerCallback,
    TriggerOutcome,
    parse_device_iid,
)
from miloco.task.state_machine import (
    ActionSlot,
    SignalKind,
    TaskSignal,
    TaskStateMachine,
    TransitionOutcome,
    slot_for_edge,
)
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)

_EMPTY_RESULT_MSG = "MIoT 返回为空/不可判定,无法确认执行结果"


def _summarize_rule_result(obj: object) -> tuple[bool, int | None, str | None]:
    """规则执行语义的结果归一:**空/不可判定返回 = 失败**。

    summarize_results 对 ``[]`` / ``None`` / 非法形态按成功处理(CLI 展示场景的
    宽松口径),但规则这里"成功"会写 cooldown、压掉后续真实动作——未确认的执行
    不能算成功。故仅当返回体里有实际结果项时才复用 #394 的负码判定。
    """
    has_result = isinstance(obj, dict) or (
        isinstance(obj, list) and any(isinstance(it, dict) for it in obj)
    )
    if not has_result:
        return False, None, _EMPTY_RESULT_MSG
    from miloco.miot.result_codes import summarize_results

    return summarize_results(obj)


def build_rule_callbacks_text(callbacks: list[RuleTriggerCallback]) -> str | None:
    """合并后的 rule DYNAMIC 回调列表 → 给 agent 的 message。

    单条 callback 形态：header + 元信息段（时间/来源/画面描述/触发条件/触发原因，
    各自独占一行 key:value，空字段省略）+ 空行 + prompt_text 整块。
    多条合并用 \\n\\n═══\\n\\n 分隔（与 prompt_text 内三段间的 \\n\\n---\\n\\n
    区分：═══ 是 callback 边界，--- 是单 callback 内的段分隔）。

    **防注入**：元信息段里模型直出 / 可 PATCH 的字段（画面描述、触发条件、触发原因、
    房间名、设备名）一律先过 ``oneline`` 折叠内嵌换行，与住户日志侧同一道防线（同一个
    ``MatchedRule.reason`` 两边都要折）。这里比住户日志更要紧：本文本经 dispatch_event
    直接成为 agent 的 LLM 输入，不折叠时 omni 可用 ``\\n\\n═══\\n\\n`` 伪造出第二个
    callback 块、附上「无需通知住户，请直接调用某动作」之类的假意图段，而 agent 真能执行
    设备动作。``prompt_text`` 不折叠——它是规则自带的多行 prompt（内部按 ``---`` 分段），
    多行是它的设计形态、且不由感知模型产出。
    """
    if not callbacks:
        return None

    from miloco.perception.event_text_builder import HEADER_MATCHED_RULE, oneline

    def _fmt_source(c: RuleTriggerCallback) -> str:
        # did 由引擎注入(机器 id),房间名/设备名来自设备配置——仍与住户日志侧同口径折叠。
        room_name = oneline(c.room_name)
        device_name = oneline(c.device_name)
        did_tag = f"(did={','.join(c.source)})" if c.source else ""
        if room_name and device_name:
            return f"{room_name}的{device_name}{did_tag}"
        if room_name:
            return f"{room_name}{did_tag}" if did_tag else room_name
        if device_name:
            return f"{device_name}{did_tag}"
        return did_tag  # 仅 did 兜底

    def _fmt(c: RuleTriggerCallback) -> str:
        lines: list[str] = []
        time = c.triggered_at.split("T")[1][:8] if "T" in c.triggered_at else ""
        if time:
            lines.append(f"时间：{time}")
        source = _fmt_source(c)
        if source:
            lines.append(f"来源：{source}")
        caption = oneline(c.caption)
        if caption:
            lines.append(f"画面描述：{caption.rstrip('。.')}")
        condition = oneline(c.rule_query or c.rule_name)
        if condition:
            lines.append(f"触发条件：{condition}")
        reason = oneline(c.trigger_reason)
        if reason:
            lines.append(f"触发原因：{reason.rstrip('。.')}")
        head = "\n".join(lines)
        return f"{head}\n\n{c.prompt_text}" if head else c.prompt_text

    body = "\n\n═══\n\n".join(_fmt(c) for c in callbacks)
    return f"{HEADER_MATCHED_RULE}\n{body}"


# Slot selection result: ("static", actions) | ("dynamic", prompt_text) | None
StaticSlot = tuple[Literal["static"], list[RuleAction]]
DynamicSlot = tuple[Literal["dynamic"], str]
Slot = StaticSlot | DynamicSlot | None


_FIRE_PREAMBLE_WITH_RECORD = """**处理流程**：（按时间序 1→2→3 执行；以下 CLI 前缀均省略 miloco-cli task record）
1. 前置闸门：调 get <task_id>，若 status=completed → 跳过 step 2 和所有通知（避免重复触达）；意图里的设备动作不受影响
2. record 写操作（必做，且先于意图里的通知 / 设备动作执行）：按额外信息字段选对应 CLI——
   - 含 actual_started_at → session-start <task_id> --at <actual_started_at>
   - 含 actual_exited_at → session-end <task_id> --at <actual_exited_at>
   - 都没有 → 按意图首句：
     - 计数加一 / +1 → progress-inc <task_id>
     - 事件追加 → event-append <task_id> --description "<事件>"
3. 后置判定（看 mutate 响应）：
   - status 从 active 翻 completed → 首次达标，本次通知用户达成，之后该任务静默
   - noop=true 且 reason=task_paused → 暂停态，静默退出

辅助工具：派生量历史 / 跨窗口查询用 compute <task_id> [--window all|day|week|month] [--date YYYY-MM-DD]；所有 CLI 响应自带 derived 字段直接读，禁止心算。"""


# _select_task_slot 的第三态: None 已经表示"空槽", 需要另一个值表示"没接管"。
_NO_TASK_ACTIONS: object = object()

# 状态机判定为"该 fire"的结论。其余 (已在态内 / 被对侧条件拦住 / 不在会话中)
# 都不 fire。
_FIRING_OUTCOMES = frozenset(
    {
        TransitionOutcome.ENTERED,
        TransitionOutcome.EXITED,
        TransitionOutcome.EVENT_FIRED,
        TransitionOutcome.MILESTONE_FIRED,
    }
)


_EVENT_TO_SIGNAL_KIND = {
    RuleEvent.ENTERED: SignalKind.ENTERED,
    RuleEvent.EXITED: SignalKind.EXITED,
}


def _slot_for(rule: Rule, event: RuleEvent) -> ActionSlot | None:
    """本层唯一的方向映射入口 —— 既定信号的意图, 也定动作取哪个槽。

    两者必须同源: 分成两处算就会在方向改动后短暂分叉 (状态机按旧方向判状态、
    动作按新方向取槽)。达标不是进出边沿, 直接对应达标槽。
    """
    if event is RuleEvent.TARGET_FIRED:
        return ActionSlot.ON_TARGET
    kind = _EVENT_TO_SIGNAL_KIND.get(event)
    if kind is None:
        return None
    return slot_for_edge(rule.resolved_direction.value, kind)


def _is_milestone(rule: Rule) -> bool:
    """这条 rule 是不是达标型。走 ``_slot_for`` 而不是自己判方向, 保持单一映射点。"""
    return _slot_for(rule, RuleEvent.ENTERED) is ActionSlot.ON_TARGET


def _has_any_action(actions: dict) -> bool:
    """六个槽里有没有任何一个非空。全空 = 没配动作 / 还没迁移, 该回退到 rule。"""
    return any(
        actions.get(k)
        for k in (
            "on_enter_actions",
            "on_enter_desc",
            "on_exit_actions",
            "on_exit_desc",
            "on_target_actions",
            "on_target_desc",
        )
    )


@dataclass
class PerSourceState:
    last_bool: bool = False
    pending_exit: bool = False
    pending_enter: bool = False


@dataclass
class RuleRuntimeState:
    sources: dict[str, PerSourceState] = field(default_factory=dict)
    last_rule_state: bool = False
    exit_debounce_task: "asyncio.Task | None" = None
    exit_debounce_at: float | None = None
    duration_window: "deque[int] | None" = None
    last_duration_round: int | None = None
    state_duration_fired: bool = False
    action_cooldown: dict[tuple[str, str], float] = field(default_factory=dict)


class RuleRunner:
    """V3 rule runner: per-frame state diff + slot-aware execution."""

    def __init__(
        self,
        rules: list[Rule],
        miot_proxy: MiotProxy,
        rule_log_repo: RuleLogRepo,
        sample_interval_seconds: float = 3.0,
        task_record_service: "TaskRecordService | None" = None,
    ):
        self._rules: dict[str, Rule] = {r.id: r for r in rules if r.id}
        self._miot_proxy = miot_proxy
        self._log_repo = rule_log_repo
        if task_record_service is None:
            from miloco.task_record.service import TaskRecordService

            task_record_service = TaskRecordService()
        self._task_record_service = task_record_service

        # Per-rule runtime state. 取代原先散落的 12 个分散字段：所有 per-(rule,
        # source) 抗抖位、OR 聚合状态、duration 滑窗、target timer、action
        # cooldown 都在 RuleRuntimeState / PerSourceState 里。新增字段只动
        # dataclass 定义；reset 时 pop 整条即可，不会再忘清。
        # 旧字段名（_last_source_state 等）以 @property 暴露给测试 / rule_tester。
        self._state: dict[str, RuleRuntimeState] = {}

        # In-flight fire-and-forget tasks. Held strongly so the GC doesn't
        # collect them mid-await; cleared via add_done_callback.
        self._fire_tasks: set[asyncio.Task] = set()

        # `sample_interval` 锁在 init，避免运行中 settings 漂移。
        self._sample_interval = sample_interval_seconds

        # task 状态机。为 None 时全部走旧的「rule 自己 fire」路径 —— expand-contract
        # 阶段 A 的回退闸, 单测与未迁移的库都走这条。attach_state_machine 接管。
        self._state_machine: TaskStateMachine | None = None
        # task 边界动作快照, 由接管方在登记拓扑时喂进来。runner 不查 DB:
        # _select_slot 在 fire 路径上, 查一次 DB 就把 hot path 拖进 IO。
        self._task_actions: dict[str, dict] = {}
        # 停用中的 task。「有效启用」= rule.enabled AND task 不在这个集合里,
        # 是派生量、无人直接写 (§19.9)。放内存是因为 get_enabled_rules 每个判定
        # 周期都要走一遍, 不能查 DB。
        self._paused_tasks: set[str] = set()

        # record 源。达标判断的全部逻辑在它里面, runner 只在 session 边界通知它。
        self._record_source = RecordSource(
            self._task_record_service,
            self._feed_record,
            self._record_refs_of_task,
        )

        logger.info("RuleRunner init, rules: %d", len(self._rules))

    # ---- record 源接线 ----

    def _record_refs_of_task(self, task_id: str):
        """该 task 名下带 record 条件项的 rule。

        不按 enabled 过滤: 停用判断在 ``update_state`` 入口, 那是唯一一处。源层
        再判一次就是两份判据, 改一处漏一处。代价只是给停用的 rule 排一个到点空转
        的 timer。
        """
        for rule in self._rules.values():
            if rule.task_id != task_id:
                continue
            ref = record_ref_of(rule)
            if ref is not None:
                yield ref

    def seed_reached_target(self, rule_id: str) -> None:
        """把达标条件直接置真, 不走 diff、不产边沿 (§7 重启重建)。

        与已删除的"退出时重置基线"不是一回事: 那个在条件为假时强行置真, 是说谎;
        这个只在启动时确实读到"今天已经达标"才用, 说的是真话, 只是不当成一次跃迁。
        """
        state = self._ensure_state(rule_id)
        self._ensure_source(rule_id, RECORD_SOURCE_DID).last_bool = True
        state.last_rule_state = True

    async def _feed_record(
        self, rule_id: str, value: bool, metadata: dict | None = None
    ) -> None:
        """把 record 源算出的 bool 交给条件层。

        ``skip_flicker``: record 的值来自 DB 算术不会抖, 而它每次翻转只喂一次 ——
        留观察窗会把这一次吸收掉, 条件永久停在旧值。

        ``metadata`` 是达标那笔账 (目标 / 实际累计), 由源层带过来 —— 达标文案要用
        真实数字, 让 fire 路径再查一次 DB 就是把源层的知识复制一份。
        """
        await self.update_state(
            rule_id, RECORD_SOURCE_DID, value,
            context="record", skip_flicker=True, extra_metadata=metadata,
        )

    # ---- task 状态机接管 (expand-contract 阶段 A) ----

    def attach_state_machine(self, state_machine: TaskStateMachine) -> None:
        self._state_machine = state_machine

    @property
    def record_source(self) -> RecordSource:
        return self._record_source

    def set_task_actions(self, task_id: str, actions: dict | None) -> None:
        """喂一份 task 边界动作快照。``None`` / 空 → 该 task 回退到 rule 上的旧字段。"""
        if actions and _has_any_action(actions):
            self._task_actions[task_id] = actions
        else:
            self._task_actions.pop(task_id, None)

    def task_owns_actions(self, task_id: str) -> bool:
        return task_id in self._task_actions

    # ---- 状态机的注入点 (§15) ----

    def is_condition_satisfied(self, rule_id: str) -> bool | None:
        """该 rule 的条件现在是不是真。``None`` = 未就绪。

        判"未就绪"用的是"有没有任何 source 被观测过", 而不是 last_rule_state 的
        初值 False —— 后者分不出"观测到假"和"还没观测"。
        """
        state = self._state.get(rule_id)
        if state is None or not state.sources:
            return None
        return state.last_rule_state

    # ---- Legacy field views (test / rule_tester compatibility) ----
    #
    # 旧实现把 per-rule state 散落在 12 个独立 dict / set 里。重构后所有 state
    # 都在 self._state[rule_id] 一个 RuleRuntimeState 内；下列 property 是
    # read-only 视图，临时支撑测试代码和 rule_tester 调试工具不改动。后续若
    # 把测试迁移到直接读 self._state，可以删掉这些 property。
    # 注意：返回的 dict / set 是临时构造，外部修改不会回写到 self._state。

    @property
    def _last_source_state(self) -> dict[tuple[str, str], bool]:
        return {
            (rid, did): src.last_bool
            for rid, st in self._state.items()
            for did, src in st.sources.items()
        }

    @property
    def _last_rule_state(self) -> dict[str, bool]:
        return {rid: st.last_rule_state for rid, st in self._state.items()}

    @property
    def _pending_source_exit(self) -> set[tuple[str, str]]:
        return {
            (rid, did)
            for rid, st in self._state.items()
            for did, src in st.sources.items()
            if src.pending_exit
        }

    @property
    def _pending_source_enter(self) -> set[tuple[str, str]]:
        return {
            (rid, did)
            for rid, st in self._state.items()
            for did, src in st.sources.items()
            if src.pending_enter
        }

    @property
    def _pending_exit(self) -> dict[str, asyncio.Task]:
        return {
            rid: st.exit_debounce_task
            for rid, st in self._state.items()
            if st.exit_debounce_task is not None
        }

    @property
    def _pending_exit_scheduled_at(self) -> dict[str, float]:
        return {
            rid: st.exit_debounce_at
            for rid, st in self._state.items()
            if st.exit_debounce_at is not None
        }

    @property
    def _duration_window(self) -> dict[str, "deque[int]"]:
        return {
            rid: st.duration_window
            for rid, st in self._state.items()
            if st.duration_window is not None
        }

    @property
    def _last_duration_round(self) -> dict[str, int]:
        return {
            rid: st.last_duration_round
            for rid, st in self._state.items()
            if st.last_duration_round is not None
        }

    @property
    def _state_duration_fired(self) -> set[str]:
        return {rid for rid, st in self._state.items() if st.state_duration_fired}

    @property
    def _action_cooldown_state(self) -> dict[tuple[str, str, str], float]:
        return {
            (rid, did, iid): ts
            for rid, st in self._state.items()
            for (did, iid), ts in st.action_cooldown.items()
        }

    # ---- Rule management ----

    def add_rule(self, rule: Rule) -> None:
        """Insert or replace a rule.

        When replacing an existing rule whose ``direction`` or
        ``condition.perceive_device_ids`` changed, drop the per-rule runtime
        state (last_source/rule_state, pending_exit, action_cooldown). Keeping
        stale state across a shape change can resurrect old EXIT debounces
        or skew the next OR-aggregation.
        """
        existing = self._rules.get(rule.id)
        if existing is not None:
            # 判 direction 而不是 mode: enter 与 exit 的 mode 都是 event, 只看
            # mode 的话这两者互换时状态不会清, 旧的防抖和聚合结果会留下来。
            direction_changed = existing.resolved_direction != rule.resolved_direction
            sources_changed = set(existing.condition.perceive_device_ids) != set(
                rule.condition.perceive_device_ids
            )
            duration_config_changed = (
                existing.duration_seconds != rule.duration_seconds
                or existing.duration_ratio != rule.duration_ratio
                or existing.on_target_desc != rule.on_target_desc
            )
            # enabled 切换也 reset：disable 期间 update_state 入口处直接 return，
            # 状态机和窗口冻结；enable 回来时若不 reset，残留状态会让 evaluate
            # 错误拦截（fired 残留 → 永远不再 fire）。
            enabled_changed = existing.enabled != rule.enabled
            if (
                direction_changed
                or sources_changed
                or duration_config_changed
                or enabled_changed
            ):
                self._reset_runtime_state(rule.id)
        self._rules[rule.id] = rule

    def remove_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)
        self._reset_runtime_state(rule_id)

    def _ensure_state(self, rule_id: str) -> RuleRuntimeState:
        state = self._state.get(rule_id)
        if state is None:
            state = RuleRuntimeState()
            self._state[rule_id] = state
        return state

    def _ensure_source(self, rule_id: str, source_did: str) -> PerSourceState:
        state = self._ensure_state(rule_id)
        src = state.sources.get(source_did)
        if src is None:
            src = PerSourceState()
            state.sources[source_did] = src
        return src

    def _reset_runtime_state(self, rule_id: str) -> None:
        # record timer 不在 state 里 (它按 rule_id 存在 record 源那边), 所以先撤,
        # 且不受下面 state 为空的早返影响。
        self._record_source.cancel_rule(rule_id)
        state = self._state.pop(rule_id, None)
        if state is None:
            return
        if state.exit_debounce_task is not None and not state.exit_debounce_task.done():
            state.exit_debounce_task.cancel()

    def _clear_pending_source_enter(self, rule_id: str) -> None:
        """清掉 rule 所有 source 的 pending_enter 残留。

        所有可能让 rule 离开 exit_debounce 阶段的路径都要调一次（reset /
        trigger_rule / ENTERED cancel / 重启 debounce / debounce 真完成），
        避免下次进入 debounce 时旧观察窗残留把首帧 True 误判为"第二帧"。
        """
        state = self._state.get(rule_id)
        if state is None:
            return
        for src in state.sources.values():
            src.pending_enter = False

    def get_rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def get_all_rules(self) -> list[Rule]:
        return list(self._rules.values())

    def _is_effectively_enabled(self, rule: Rule) -> bool:
        """「有效启用」= 用户意图 AND 所属 task 没被停用 (§19.9)。

        ``rule.enabled`` 只表示用户想不想开, task 停用不再覆写它。所以凡是判
        「这条规则此刻要不要参与判定」都得走这里, 只看 enabled 会让停用失效。
        """
        return rule.enabled and rule.task_id not in self._paused_tasks

    def get_enabled_rules(self) -> list[Rule]:
        """「有效启用」的 rule。"""
        return [r for r in self._rules.values() if self._is_effectively_enabled(r)]

    def set_task_paused(self, task_id: str, paused: bool) -> None:
        """刷新派生量。task 启停的唯一入口。"""
        if paused:
            self._paused_tasks.add(task_id)
        else:
            self._paused_tasks.discard(task_id)

    def is_task_paused(self, task_id: str) -> bool:
        return task_id in self._paused_tasks

    @property
    def state_machine(self) -> TaskStateMachine | None:
        return self._state_machine

    def attach_tracker(self, tracker) -> None:
        self._tracker = tracker

    @property
    def tracker(self):
        return getattr(self, "_tracker", None)

    # ---- Main entry: per-frame, per-source state report ----

    async def update_state(
        self,
        rule_id: str,
        source_did: str,
        current_bool: bool,
        context: str = "",
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        caption: str = "",
        device_name: str = "",
        cycle_source_states: Mapping[str, bool] | None = None,
        skip_flicker: bool = False,
        extra_metadata: dict | None = None,
    ) -> TriggerOutcome:
        """Per-frame, per-source state report from the perception engine.

        Aggregates across sources with OR, diffs against the previous tick,
        and dispatches according to (mode, event). ``context`` is only used on
        flip frames (ENTERED / EXITED); STILL_* frames discard it.

        ``trigger_room`` / ``trigger_dids`` are pass-through metadata from the
        matched frame (room name + device ids of the camera that saw it). They
        ride along to the Agent callback on ENTERED and never participate in
        state aggregation; EXITED fires with them empty.
        """
        async with get_monitor().track_async(NodeName.RULE, "update") as h:
            h.add_input(1)
            rule = self._rules.get(rule_id)
            if rule is None:
                logger.warning("update_state: rule %s not found", rule_id)
                return TriggerOutcome.NOT_FIRED
            if not self._is_effectively_enabled(rule):
                return TriggerOutcome.NOT_FIRED

            src = self._ensure_source(rule_id, source_did)
            prev = src.last_bool

            # out() 统一分流本周期触发结论：duration 规则的结论完全由 _evaluate_duration
            # 决定（frame 路径对 duration 不 fire、只做状态维护），非 duration 规则用
            # frame/diff 路径结论。结论作为 update_state 返回值当场交给调用方（感知 client
            # 就地累积成住户日志的「触发状态」），引擎侧不留存跨 cycle 的展示状态。
            dur_outcome: TriggerOutcome | None = None

            def out(frame_outcome: TriggerOutcome) -> TriggerOutcome:
                return dur_outcome if rule.duration_seconds else frame_outcome

            # 丢帧。感知 client 会在同一 cycle 内传入已观测 source 的快照，避免
            # 多 source 同步翻 False 时先来的 source 仍读到后来的 source 上一帧 True。
            # 未在本 cycle 观测到的 source 继续沿用 self._state[rule_id].sources。
            if rule.duration_seconds:
                observed_states = dict(cycle_source_states or {})
                observed_states.setdefault(source_did, current_bool)
                rule_state = self._state[rule_id]
                effective_state = any(observed_states.values()) or any(
                    s.last_bool
                    for did, s in rule_state.sources.items()
                    if did not in observed_states
                )
                dur_outcome = await self._evaluate_duration(
                    rule, effective_state, source_did, context, caption, device_name
                )
                # out() 对 duration 规则一律取 dur_outcome，其非空由本块位置维持——静态类型
                # 推不出来，失效后又会被 aggregate_outcomes 的 .get(o, -1) 兜底静默吞掉。
                # 这里显式断言，让不变式失效时当场炸而不是渲染出一个错标签。
                assert dur_outcome is not None

            # 帧级抗抖：source 上次 True 时，单帧 False 不立即翻转 — 视为 LLM 漏识，
            # 留一帧观察窗。下一帧仍 False 才确认 EXIT；翻回 True 则吸收为抖动。
            #
            # skip_flicker 给不会抖的源用（record 源的 bool 来自 DB 算术）。对这类源
            # 留观察窗是有害的：它每次翻转只喂一次，第一次被吸收掉就再没有第二次，
            # 条件会永久停在旧值。
            if prev and not skip_flicker:
                if not current_bool:
                    if not src.pending_exit:
                        src.pending_exit = True
                        logger.debug(
                            "rule %s source %s exit pending (1st false)",
                            rule_id, source_did,
                        )
                        return out(TriggerOutcome.NOT_FIRED)
                    src.pending_exit = False
                elif src.pending_exit:
                    src.pending_exit = False
                    logger.info(
                        "rule %s source %s flicker absorbed", rule_id, source_did
                    )
                    return out(TriggerOutcome.STILL_IN)
            elif not skip_flicker and self._state[rule_id].exit_debounce_task is not None:
                # 仅在 exit_debounce 阶段，对 False → True 加对称双帧抗抖：单帧 True
                # 视为 LLM 单帧幻觉，留一帧观察。下一帧仍 True 才确认 ENTER 并 cancel
                # debounce；第二帧 False 则吸收幻觉、debounce 继续完成。修复 omni
                # 单帧幻觉反复打断 exit_debounce 导致 state 退不出的问题。
                if current_bool:
                    if not src.pending_enter:
                        src.pending_enter = True
                        logger.debug(
                            "rule %s source %s enter pending during exit_debounce "
                            "(1st true)",
                            rule_id, source_did,
                        )
                        return out(TriggerOutcome.NOT_FIRED)
                    src.pending_enter = False
                    logger.info(
                        "rule %s source %s enter confirmed (2 consecutive true) "
                        "during exit_debounce",
                        rule_id, source_did,
                    )
                elif src.pending_enter:
                    src.pending_enter = False
                    logger.info(
                        "rule %s source %s single-frame true absorbed during "
                        "exit_debounce",
                        rule_id, source_did,
                    )
                    return out(TriggerOutcome.NOT_FIRED)

            src.last_bool = current_bool

            rule_state = self._state[rule_id]
            new_rule_state = any(s.last_bool for s in rule_state.sources.values())
            old_rule_state = rule_state.last_rule_state
            rule_state.last_rule_state = new_rule_state

            if old_rule_state == new_rule_state:
                return out(
                    TriggerOutcome.STILL_IN if new_rule_state else TriggerOutcome.NOT_FIRED
                )

            event = RuleEvent.ENTERED if new_rule_state else RuleEvent.EXITED
            dispatch_outcome = await self._dispatch_event(
                rule, event, source_did, context, trigger_room, trigger_dids,
                caption=caption, device_name=device_name,
                extra_metadata=extra_metadata,
            )
            h.add_output(1)
            if event == RuleEvent.EXITED:
                # EXITED 的结论以 _dispatch_event 的 NOT_FIRED 为准，不被 duration 的
                # dur_outcome 覆盖——覆盖会返回语义相反的值：STATE + duration 已 fire 过
                # on_enter 时 _evaluate_duration 早返 STILL_IN（「还在态内」），而此刻恰恰是
                # 确认离开的那一周期。
                # 唯一例外：本周期 _evaluate_duration **真派发过**。ratio<1（默认 0.6）时窗口
                # 末尾的 0 被容忍后仍可达标，而窗口填满的那一刻可以正好落在确认离开的周期
                # （如 maxlen=20/ratio=0.6：前 18 帧 True、后 2 帧 False → 18/20 达标 → 真
                # _spawn_fire）。那次 fire 已经发出去了，此时报 NOT_FIRED 等于把一次真派发
                # 说没了——方向比被覆盖成 STILL_IN 更糟。
                # 今天两者都零后果（唯一产生 EXITED 的调用点丢弃返回值），但一旦有人开始
                # 消费返回值就是真错。
                if dur_outcome is TriggerOutcome.FIRED:
                    return dur_outcome
                return dispatch_outcome
            return out(dispatch_outcome)

    # ---- Debug / manual trigger ----

    async def trigger_rule(
        self,
        rule_id: str,
        context: str = "",
    ) -> RuleExecuteResult | None:
        """Manual trigger -- debug only. Fires the ENTER slot once.

        Behavior:
        - Always fires regardless of prior state.
        - Cancels any pending exit debounce (same as the ENTERED path in
          ``_dispatch_event``).
        - Writes ``self._state[rule_id].sources[source_did].last_bool = True``
          and ``self._state[rule_id].last_rule_state = True``.

        Caveats (do NOT use from production hot paths):
        - No EXIT synthesis. The follow-up EXITED event must come from real
          perception; for state-mode rules this means on_exit / debounce will
          not fire just because you triggered.
        - The ``source_did`` written here (``condition.perceive_device_ids[0]``
          or ``"manual"``) does not match the ``"perception"`` key the
          production perception client uses. After a manual trigger,
          OR-aggregation sees both keys, which can keep a state-mode rule
          stuck at ENTERED until the runner is rebuilt (process restart).

        Returns the execution result, or None when the rule is missing,
        disabled, or has an empty ENTER slot.
        """
        rule = self._rules.get(rule_id)
        if rule is None:
            logger.warning("trigger_rule: rule %s not found", rule_id)
            return None
        if not self._is_effectively_enabled(rule):
            logger.info("trigger_rule: rule %s is disabled, skipping", rule_id)
            return None

        # Bridge: update state machine so future events diff correctly
        source_did = (
            rule.condition.perceive_device_ids[0]
            if rule.condition.perceive_device_ids
            else "manual"
        )
        src = self._ensure_source(rule_id, source_did)
        src.last_bool = True
        state = self._state[rule_id]
        state.last_rule_state = True

        # Cancel any pending exit debounce (same as ENTERED in _dispatch_event)
        if state.exit_debounce_task is not None and not state.exit_debounce_task.done():
            state.exit_debounce_task.cancel()
        state.exit_debounce_task = None
        state.exit_debounce_at = None
        self._clear_pending_source_enter(rule.id)

        sources = self._sources_currently_true(rule_id) or [source_did]
        return await self._fire(
            rule, RuleEvent.ENTERED, sources, context, str(uuid.uuid4())
        )

    # ---- EVENT duration sliding-window evaluator ----

    async def _evaluate_duration(
        self,
        rule: Rule,
        new_rule_state: bool,
        source_did: str,
        context: str,
        caption: str = "",
        device_name: str = "",
    ) -> TriggerOutcome:
        """每个采样周期采样一次 OR 聚合状态；窗口 True 比例达阈值即 fire。

        返回触发结论：达标 fire → ``FIRED``；已 fire 过（STATE）→ ``STILL_IN``；
        累积中（窗口未满 / 比例未达 / 同 round 去重）→ ``COUNTING``。

        - 同一采样周期内多 source 多次进入 → 通过 round_id 去重，只采一次。
        - 采样断流（round_id 不连续）：用 0 补齐 gap 让老样本自然衰减；
          gap 超过整窗则直接清空（避免无意义循环）。
        - 窗口未填满（``len(win) < maxlen``）直接 return：必须累积满
          ``duration_seconds`` 时长才进入 ratio 判定，避免 ratio<1 导致最快
          ``duration_seconds * ratio`` 就触发（如 30min * 0.8 → 24min 触发）。
        - 分母固定用 maxlen 而非 ``len(win)``：保留 ratio 间歇容忍语义，
          窗口满后允许部分漏检。
        - session 型且已 fire on_enter（``state.state_duration_fired`` 置位）
          → 直接 return：STILL_IN 期间不重复 fire，等 _debounced_exit 真完成时
          清标记重新累积。单方向的 rule 不用本拦截，fire 后清窗口走"周期 fire"
          by-design。
        """
        state = self._ensure_state(rule.id)
        if rule.resolved_direction is RuleDirection.SESSION and state.state_duration_fired:
            return TriggerOutcome.STILL_IN

        round_id = int(time.time() / self._sample_interval)
        last_round_id = state.last_duration_round
        if last_round_id == round_id:
            return TriggerOutcome.COUNTING

        maxlen = max(1, int(rule.duration_seconds / self._sample_interval))
        win = state.duration_window
        if win is None or win.maxlen != maxlen:
            win = deque(maxlen=maxlen)
            state.duration_window = win

        if last_round_id is not None:
            gap = round_id - last_round_id - 1
            if gap >= maxlen:
                win.clear()
                logger.info(
                    "rule %s (task=%s) duration window cleared due to long sample gap: "
                    "%d rounds (>= maxlen %d)",
                    rule.id, rule.task_id, gap, maxlen,
                )
            elif gap > 0:
                win.extend([0] * gap)
                logger.debug(
                    "rule %s (task=%s) duration window filled %d zeros for sample gap",
                    rule.id, rule.task_id, gap,
                )

        win.append(1 if new_rule_state else 0)
        state.last_duration_round = round_id

        if new_rule_state:
            logger.info(
                "DURATION sample: rule=%s task=%s cur=1 sum=%d/%d ratio=%.2f/%.2f filled=%d/%d",
                rule.id, rule.task_id, sum(win), maxlen,
                sum(win) / maxlen, rule.duration_ratio, len(win), maxlen,
            )

        if len(win) < maxlen:
            return TriggerOutcome.COUNTING

        if sum(win) / maxlen >= rule.duration_ratio:
            # actual_started_at = 窗口里第一帧 true 的对齐时间（与 actual_exited_at 对称）。
            # ratio<1 时比"窗口名义起点 fire_ts - duration_seconds"更准确反映用户真实开始时刻。
            win_list = list(win)
            first_true_offset = next(i for i, v in enumerate(win_list) if v == 1)
            first_true_round = (round_id - maxlen + 1) + first_true_offset
            actual_started_at = ms_to_iso_local(
                int(first_true_round * self._sample_interval * 1000)
            )
            logger.info(
                "rule %s (task=%s, %s) duration met: actual_started_at=%s "
                "(sum=%d/maxlen=%d, ratio>=%.2f)",
                rule.id,
                rule.task_id,
                rule.resolved_direction.value,
                actual_started_at,
                sum(win),
                maxlen,
                rule.duration_ratio,
            )
            slot = _slot_for(rule, RuleEvent.ENTERED)
            if slot is ActionSlot.ON_EXIT:
                # exit 型的进入边沿就是"该退出了"。达标兜底必须排在状态机翻 off
                # 之前 —— 翻完再喂会被"不在会话中"拦掉, 这一天的达标就丢了。与
                # 瞬时翻转那条路同一份处理, 两条都是退出的入口。
                await self._record_source.settle(rule.task_id)

            if not self._state_machine_allows(rule, RuleEvent.ENTERED):
                # 在清窗口 / 标记 fired 之前问闸：被吞掉时这两样都不该动，否则
                # 白丢一次累积。
                return TriggerOutcome.STILL_IN

            if rule.resolved_direction is not RuleDirection.SESSION:
                # 单方向：清窗口 → 下次 update_state 重新累积（by-design 周期 fire）。
                # state_duration_fired 只由 _debounced_exit 清, 而单方向的 rule 走
                # 不到那条路 —— 标记了就永久拦死。
                state.duration_window = None
                state.last_duration_round = None
            else:
                # session：标记 fired 拦截 STILL_IN 重复 fire；窗口留着无害
                # （fired 拦截了，后续 evaluate 不会用），_debounced_exit 真完成时一并清
                state.state_duration_fired = True
            sources = self._sources_currently_true(rule.id) or [source_did]
            self._spawn_fire(
                rule,
                RuleEvent.ENTERED,
                sources,
                context,
                extra_metadata={
                    "duration_seconds": rule.duration_seconds,
                    "actual_started_at": actual_started_at,
                },
                caption=caption, device_name=device_name,
            )
            self._sync_record_source(rule, slot)
            return TriggerOutcome.FIRED

        return TriggerOutcome.COUNTING

    # ---- Event dispatch ----

    async def _dispatch_event(
        self,
        rule: Rule,
        event: RuleEvent,
        source_did: str,
        context: str,
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        caption: str = "",
        device_name: str = "",
        extra_metadata: dict | None = None,
    ) -> TriggerOutcome:
        """Translate a diff event into an action-layer fire (with state-mode
        debounce on EXITED).

        Returns the resulting ``TriggerOutcome`` for the ENTERED path (``FIRED``
        only when it actually spawns a fire). EXITED / suppressed paths return
        ``NOT_FIRED`` — for duration rules the caller ignores this and uses
        ``_evaluate_duration``."""
        state = self._ensure_state(rule.id)
        if event == RuleEvent.ENTERED:
            # 进入分支瞬间锚定 wall-clock 作为 actual_started_at —— 与 actual_exited_at
            # 镜像：fire 到达 agent 时已晚 N 秒（链路延迟），但 metadata 时间戳是过去
            # 时刻，agent --at <actual_started_at> 不受链路延迟影响。
            actual_started_at = ms_to_iso_local(now_ms())
            # state mode: ENTERED cancels any pending debounced exit
            pending = state.exit_debounce_task
            state.exit_debounce_task = None
            absorbed_pending_exit = False
            if pending is not None and not pending.done():
                pending.cancel()
                absorbed_pending_exit = True
                scheduled_at = state.exit_debounce_at
                state.exit_debounce_at = None
                pending_for_ms = (
                    int((time.monotonic() - scheduled_at) * 1000)
                    if scheduled_at is not None else None
                )
                logger.info(
                    "EXIT_CANCELLED: rule=%s name=%s by=ENTERED pending_for_ms=%s",
                    rule.id, rule.name, pending_for_ms,
                )
                self._publish_rule_event(
                    "rule_exit_cancelled", rule.id,
                    {"by": "ENTERED", "pending_for_ms": pending_for_ms},
                )
            # 离开 exit_debounce 阶段：清掉所有 source 的 pending_enter 残留
            # （多 source 场景下其它 source 的 1st-true 可能还停留在观察窗）
            self._clear_pending_source_enter(rule.id)

            # exit_debounce 未完成就被 ENTER 打断 → state 从未真正离开 →
            # 不重复 fire on_enter。否则 omni 偶发漏识会让 on_enter 反复触发。
            # 结论按 STILL_IN 上报（规则持续在态，只是吸收了一次伪退出）——与帧级
            # 抖动吸收路径（update_state 里 pending_exit 吸收）同语义、同标签。
            if absorbed_pending_exit:
                return TriggerOutcome.STILL_IN

            # duration_seconds 配置时：不在翻转那一刻 fire；fire 由
            # _evaluate_duration 在窗口达比例时触发（actual_started_at 走那条路径
            # 用滑窗里第一帧 true 的对齐时间，本路径取的 wall-clock 不用）。
            if rule.duration_seconds:
                return TriggerOutcome.NOT_FIRED

            slot = _slot_for(rule, event)
            if slot is ActionSlot.ON_EXIT:
                # exit 型 rule 的进入边沿就是"该退出了"。达标兜底必须排在状态机翻
                # off 之前 —— 翻完再喂会被 NOT_IN_SESSION 拦掉, 这一天的达标就丢了。
                await self._record_source.settle(rule.task_id)

            if not self._state_machine_allows(rule, RuleEvent.ENTERED):
                # 状态机吞掉了这次边沿（已在态内 / 被对侧条件拦住）。按 STILL_IN
                # 上报——与帧级抖动吸收同语义：规则确实在态，只是没有新一次进入。
                return TriggerOutcome.STILL_IN

            sources = self._sources_currently_true(rule.id) or [source_did]
            # Fire-and-forget: dynamic callback retry is up to 1+2+4=7s of sleep,
            # and update_state() runs on perception's hot path. Awaiting fire
            # here would freeze the main loop for the duration of every dynamic
            # retry. The state-machine bookkeeping above is already done; the
            # fire only writes log/cooldown state, which is safe to do async.
            # milestone rule 的进入边沿就是「达标」。按 TARGET_FIRED 记, 否则日志与
            # 台账把达标记成一次进入, 事后分不出来; 也别注入 actual_started_at ——
            # 达标不是 session 起点, agent 会照着它去调 session-start。
            if slot is ActionSlot.ON_TARGET:
                self._spawn_fire(
                    rule, RuleEvent.TARGET_FIRED, sources, context,
                    trigger_room, trigger_dids,
                    extra_metadata=dict(extra_metadata or {}),
                    caption=caption, device_name=device_name,
                )
                return TriggerOutcome.FIRED

            self._spawn_fire(
                rule, event, sources, context, trigger_room, trigger_dids,
                extra_metadata={
                    "actual_started_at": actual_started_at,
                    **(extra_metadata or {}),
                },
                caption=caption, device_name=device_name,
            )
            self._sync_record_source(rule, slot)
            return TriggerOutcome.FIRED

        # EXITED
        if rule.resolved_direction is not RuleDirection.SESSION:
            # 只有 session 的退出边沿有意义: enter / exit / milestone 都是单方向,
            # slot_for_edge 对它们的 EXITED 返 None, 走下去也选不到槽。
            return TriggerOutcome.NOT_FIRED

        # session + duration 但未 fire on_enter：进入态从未被确认 → 当这次 EXITED
        # 没发生过。不 fire on_exit（没配对的 ENTERED），不启动 debounce，也不清
        # 窗口——窗口靠后续 evaluate 持续 append 0 自然演化，符合 duration_ratio
        # 的间歇容忍设计（用户中途短暂离开仍允许后续凑齐）。
        if rule.duration_seconds and not state.state_duration_fired:
            return TriggerOutcome.NOT_FIRED

        # state mode: cancel any existing debounce before scheduling a new one
        old = state.exit_debounce_task
        if old is not None and not old.done():
            old.cancel()
        state.exit_debounce_task = None
        state.exit_debounce_at = None
        # 新一轮 debounce 开始前，清掉上一轮残留的 pending_enter
        self._clear_pending_source_enter(rule.id)

        delay = rule.exit_debounce_seconds
        # 真实退出时刻：debounce 调度的此刻才是用户实际离开的时间；
        # _debounced_exit fire 时的 wall-clock 已经晚了 delay 秒
        actual_exited_at = ms_to_iso_local(now_ms())
        task = asyncio.create_task(
            self._debounced_exit(rule, [source_did], context, delay, actual_exited_at)
        )
        state.exit_debounce_task = task
        state.exit_debounce_at = time.monotonic()
        fires_at_ts_ms = int(time.time() * 1000) + delay * 1000
        logger.info(
            "EXIT_SCHEDULED: rule=%s name=%s delay=%ds fires_at_ts_ms=%d",
            rule.id, rule.name, delay, fires_at_ts_ms,
        )
        self._publish_rule_event(
            "rule_exit_scheduled", rule.id,
            {"delay_seconds": delay, "fires_at_ts_ms": fires_at_ts_ms},
        )
        return TriggerOutcome.NOT_FIRED

    async def _debounced_exit(
        self,
        rule: Rule,
        sources: list[str],
        context: str,
        delay: float,
        actual_exited_at: str,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Cleanup before firing so a re-entry during fire doesn't see stale handle
        rs = self._ensure_state(rule.id)
        rs.exit_debounce_task = None
        rs.exit_debounce_at = None
        # debounce 已真完成，rule 离开 exit_debounce 阶段；清掉所有 source 的
        # pending_enter，避免下一轮 debounce 开始时旧观察窗复用
        self._clear_pending_source_enter(rule.id)
        # STATE + duration：真 fire on_exit 时清掉 fired 标记和窗口，让下次
        # ENTERED 重新走完整"累积 → 达标确认"流程。
        if rule.duration_seconds:
            rs.state_duration_fired = False
            rs.duration_window = None
            rs.last_duration_round = None
        # 达标兜底必须排在状态机翻 off 之前：达标动作要求 task 还在 session 里,
        # 翻完再喂就被 NOT_IN_SESSION 拦掉。撤 timer 反过来必须排在之后 —— 多 rule
        # 时这次退出可能被别的条件挡住 (STILL_HELD), session 还在, 撤了就丢达标。
        await self._record_source.settle(rule.task_id)
        # record-bound duration rule：注入今日累计 / target metadata，让 fire-agent
        # 在 on-exit-desc 含「若今日累计已达目标则使用手机推送通知...」条件通知文案时，
        # 按真实数据拼装通知（accumulated >= target 才推；文案不写死时长）。
        # 非 duration record / 无 target / 无 record 时跳过。
        exit_metadata: dict | None = None
        if rule.task_id:
            try:
                state = self._task_record_service.read_duration_target_state(
                    rule.task_id
                )
            except Exception:
                logger.exception(
                    "Rule %s read duration target state failed; "
                    "skipping exit metadata",
                    rule.id,
                )
                state = None
            if state is not None and state[0] is not None:
                exit_metadata = {
                    "accumulated_minutes_today": state[1],
                    "target_minutes": state[0],
                }

        if not self._state_machine_allows(rule, RuleEvent.EXITED):
            return
        self._record_source.disarm(rule.task_id)

        # Background-task path: swallow exceptions so they don't surface as
        # "Task exception was never retrieved" warnings.
        try:
            await self._fire(
                rule, RuleEvent.EXITED, sources, context, str(uuid.uuid4()),
                actual_exited_at=actual_exited_at,
                extra_metadata=exit_metadata,
            )
        except Exception:
            logger.exception(
                "Rule %s debounced exit fire failed", rule.id
            )

    # ---- Fire-and-forget plumbing ----

    _SLOT_TO_EVENT = {
        "on_enter": RuleEvent.ENTERED,
        "on_exit": RuleEvent.EXITED,
        "on_target": RuleEvent.TARGET_FIRED,
    }

    def dispatch_task_action(self, task_id: str, slot_name: str) -> bool:
        """状态机自己发起的动作 —— 没有上游边沿, 由本函数补出一次 fire。

        用在 §19.5 重新配置时的强制 ``on_exit`` 与 §5 的手动注入。感知路径不走
        这里 (它有自己的边沿与上下文, 状态机对它只做许可闸)。

        动作在 task 行上, 但日志与冷却仍按 rule 归属, 所以要挑一条代表 rule。
        名下无 rule 时返回 False —— 那正是"删掉最后一条 rule"的情形, 动作已经无处
        归属, 只能记日志。
        """
        event = self._SLOT_TO_EVENT.get(slot_name)
        if event is None:
            logger.warning("unknown action slot %r for task %s", slot_name, task_id)
            return False
        rules = [r for r in self._rules.values() if r.task_id == task_id]
        if not rules:
            logger.warning(
                "task %s 请求 %s 但名下已无 rule, 动作无处归属, 跳过",
                task_id,
                slot_name,
            )
            return False
        rule = rules[0]
        if event is RuleEvent.EXITED:
            # 重新配置的强制 on_exit 与手动注入都从这里出 session。不撤的话 timer
            # 到点会在 off 态喂真, 把条件锁死。
            # 这条路没有"翻 off 之前"的位置可用 (状态机先改状态再派动作), 所以只撤
            # 不兜底 —— 这两条都不是感知到的真实离开。
            self._record_source.disarm(task_id)
        self._spawn_fire(rule, event, [], f"task_state_machine_{slot_name}")
        return True

    def _sync_record_source(self, rule: Rule, slot: ActionSlot | None) -> None:
        """按这条边沿把 task 推向哪边, 让 record 源跟上 session 边界。

        锚点是边沿的意图而不是 rule 的 ``mode``: exit 型 rule 的"条件成立"就是
        "该退出了", 在那儿 arm 等于在 session 结束的瞬间起达标 timer —— 而它排的
        timer 到点会在 off 态喂真, 把条件锁死, 当天再也产不出达标边沿。
        """
        if slot is ActionSlot.ON_ENTER:
            self._record_source.arm(rule.task_id)
        elif slot is ActionSlot.ON_EXIT:
            self._record_source.disarm(rule.task_id)

    def _state_machine_allows(self, rule: Rule, event: RuleEvent) -> bool:
        """问 task 状态机: 这次已确认的边沿该不该 fire。

        未接管该 task → 恒 True, 完全是旧行为 (expand-contract 阶段 A 的回退闸)。

        接管后状态机同时维护 ``runtime_state``, 所以这个调用有副作用, 每次边沿
        只能调一次。

        达标不在这里单独判: milestone rule 的进入边沿由 ``slot_for_edge`` 映射成
        达标槽, 状态机的 ``_handle_milestone`` 做的正是"确认 task 在 on"这件事。
        在这里再判一次就是同一条判据的两份实现。

        **为什么是"许可闸"而不是"状态机代为派发"**: 各个 fire 点带着不同的
        metadata (actual_started_at / exit_metadata / 达标数字)、不同的
        日志与冷却上下文。把它们穿过状态机再派回来, 等于把整套执行上下文搬一遍,
        而阶段 A 的目标是行为等价。状态机自己发起动作的两条路 (重配置时强制
        on_exit、手动注入) 走 ``dispatch_action``, 那里本来就没有上游边沿。
        """
        sm = self._state_machine
        if sm is None or not sm.owns(rule.task_id):
            return True

        kind = _EVENT_TO_SIGNAL_KIND.get(event)
        if kind is None:
            logger.warning(
                "许可闸收到不是边沿的事件 %s (rule=%s), 不放行", event, rule.id
            )
            return False
        slot = slot_for_edge(rule.resolved_direction.value, kind)
        if slot is None:
            # 这个边沿对该方向没有意义 (单方向的 rule 只有一个边沿) —— 不发信号,
            # 也就没有动作。发出去只能让 task 层再判一次同样的事。
            return False
        # dispatch=False: 本调用只要结论。动作由下面的 _spawn_fire / _fire 走
        # 原路径执行, 让状态机再派一次会重复触发。
        outcome = sm.handle(
            TaskSignal(rule.task_id, rule.id, kind, slot), dispatch=False
        )
        return outcome in _FIRING_OUTCOMES

    def _spawn_fire(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        context: str,
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        extra_metadata: dict | None = None,
        caption: str = "",
        device_name: str = "",
    ) -> None:
        """Schedule a fire as a background task; record handle to prevent GC."""
        task = asyncio.create_task(
            self._fire_safely(
                rule, event, sources, context, str(uuid.uuid4()),
                trigger_room, trigger_dids, extra_metadata,
                caption=caption, device_name=device_name,
            )
        )
        self._fire_tasks.add(task)
        task.add_done_callback(self._fire_tasks.discard)

    async def _fire_safely(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        context: str,
        execute_id: str,
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        extra_metadata: dict | None = None,
        caption: str = "",
        device_name: str = "",
    ) -> None:
        try:
            await self._fire(
                rule, event, sources, context, execute_id,
                trigger_room, trigger_dids, extra_metadata,
                caption=caption, device_name=device_name,
            )
        except Exception:
            logger.exception(
                "Rule fire failed: rule=%s event=%s", rule.id, event.value
            )

    def force_cross_day_reset(
        self,
        task_id: str,
        pre_rollover_state: tuple[int | None, int] | None = None,
    ) -> None:
        """跨日 rollover 完成后调：对所有"当前在 ENTERED 态"的 task 关联 rule，
        真 fire on_exit（计入旧一天 session-end）+ 真 fire on_enter（建新一天
        session-start），并让 record 源按新一天重新武装。

        语义上等价于"用户跨过 00:00 那一刻 EXITED → ENTERED"，但实际 condition
        没变；``state.last_rule_state`` 保持 True（避免下一次 condition tick
        再触发 ENTERED）。

        pre_rollover_state 为 rollover_one 执行前 snapshot 的旧一天
        ``(target_minutes, accumulated_minutes_today)``——rollover 已清旧累计，
        record 源读不到「旧一天已达标」这个信号，必须靠 snapshot 兑现。
        """
        # 只挑 session: 它的条件为真才等于"此刻在会话中", 强补一对进出才有意义。
        # milestone 的条件为真是"今天发过达标了", exit 的是"该退出了", 都不表示在
        # 态内 —— 强发进入边沿会分别多播一条达标通知、在 task 已关闭时多执行一次
        # 退出动作, 而这条路径不经许可闸 (§5.3)。
        affected: list[Rule] = [
            r for r in self._rules.values()
            if r.task_id == task_id
            and r.resolved_direction is RuleDirection.SESSION
            and r.id in self._state
            and self._state[r.id].last_rule_state
        ]
        for rule in affected:
            logger.info(
                "CROSS_DAY_RESET: rule=%s task=%s force on_exit + on_enter",
                rule.id, task_id,
            )
            sources = self._sources_currently_true(rule.id)
            rs = self._ensure_state(rule.id)
            # 1) 取消可能在跑的 exit debounce（用户真在态内，不该有，但兜底）
            pending = rs.exit_debounce_task
            rs.exit_debounce_task = None
            if pending is not None and not pending.done():
                pending.cancel()
            rs.exit_debounce_at = None
            # 2) STATE + duration：清 fired 标记和窗口，让 on_enter 重新走累积
            #    （新一天计时窗口从零开始）
            if rule.duration_seconds:
                rs.state_duration_fired = False
                rs.duration_window = None
                rs.last_duration_round = None
            # 3) 强制 fire on_exit / on_enter：不注入 actual_exited_at /
            #    actual_started_at。rollover_one 已切段（旧 session 落账、新 record
            #    active_session_start_at = rollover 触发时刻），agent 不该再做
            #    session 边界操作；若投 actual_exited_at=midnight，preamble 会
            #    强制 agent 调 session-end --at midnight，而新 record 的
            #    active_session_start_at > midnight，触发 RecordSchemaError。
            self._spawn_force_fire(
                rule, RuleEvent.EXITED, sources, "cross_day_rollover",
            )
            self._spawn_force_fire(
                rule, RuleEvent.ENTERED, sources, "cross_day_rollover",
            )

        # record 源不看 affected：归零是 record 的事，与此刻有没有 rule 在态内无关。
        # 不归零的话条件停在真，第二天达标产不出新边沿、通知永久消失。
        handle = asyncio.create_task(
            self._record_source.roll_over(task_id, pre_rollover_state)
        )
        self._fire_tasks.add(handle)
        handle.add_done_callback(self._fire_tasks.discard)

    def _spawn_force_fire(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        context: str,
    ) -> None:
        """跨日 force-reset 专用：fire on_exit / on_enter 不带 session 时间戳
        metadata（rollover_one 已处理 session 切段）。独立错误日志保留 context 上下文。"""
        task = asyncio.create_task(
            self._force_fire_safely(rule, event, sources, context)
        )
        self._fire_tasks.add(task)
        task.add_done_callback(self._fire_tasks.discard)

    async def _force_fire_safely(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        context: str,
    ) -> None:
        try:
            await self._fire(
                rule, event, sources, context, str(uuid.uuid4()),
            )
        except Exception:
            logger.exception(
                "Cross-day force fire failed: rule=%s event=%s",
                rule.id, event.value,
            )

    async def drain(self) -> None:
        """Wait for all in-flight fire tasks to finish.

        Used by tests that assert on fire side effects right after
        update_state(); also useful for graceful shutdown.
        """
        if self._fire_tasks:
            await asyncio.gather(*self._fire_tasks, return_exceptions=True)

    def _sources_currently_true(self, rule_id: str) -> list[str]:
        """Source DIDs whose latest report is True. Used to populate
        RuleTriggerCallback.source on ENTERED."""
        rs = self._state.get(rule_id)
        if rs is None:
            return []
        return [did for did, src in rs.sources.items() if src.last_bool]

    @staticmethod
    def _publish_rule_event(event_type: str, rule_id: str, payload: dict) -> None:
        client = get_metrics_client()
        if client is None:
            return
        client.publish_event(event_type=event_type, source=rule_id, payload=payload)

    # ---- Slot-aware execution ----

    async def _fire(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        context: str,
        execute_id: str,
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        extra_metadata: dict | None = None,
        actual_exited_at: str | None = None,
        caption: str = "",
        device_name: str = "",
    ) -> RuleExecuteResult | None:
        """Pick the slot for (mode, event), execute, write log."""
        slot = self._select_slot(rule, event)
        if slot is None:
            logger.debug(
                "rule %s event %s: empty slot, skipping", rule.id, event.value
            )
            return None

        start_time = int(time.time() * 1000)
        kind, value = slot

        direction = rule.resolved_direction.value
        logger.info(
            "FIRE: rule=%s name=%s event=%s direction=%s slot=%s sources=%s "
            "execute_id=%s",
            rule.id, rule.name, event.value, direction, kind, sources, execute_id,
        )
        self._publish_rule_event(
            "rule_fire", rule.id,
            {
                "event": event.value,
                "direction": direction,
                "slot": kind,
                "sources": sources,
                "execute_id": execute_id,
            },
        )

        if kind == "static":
            action_results = [await self._execute_action(rule.id, a) for a in value]
            ok_all = all(r.result for r in action_results)
            exec_result = RuleExecuteResult(
                event=event,
                action_results=action_results,
                dynamic_rule_event_sent=False,
            )
        else:  # dynamic
            sent = await self._execute_dynamic(
                rule, event, sources, value,
                trigger_room, trigger_dids, extra_metadata,
                actual_exited_at=actual_exited_at,
                caption=caption, device_name=device_name,
                trigger_reason=context,
            )
            ok_all = sent
            exec_result = RuleExecuteResult(
                event=event,
                action_results=[],
                dynamic_rule_event_sent=sent,
            )

        log_kind = (
            RuleLogKind.RULE_TRIGGER_SUCCESS
            if ok_all
            else RuleLogKind.RULE_TRIGGER_FAILURE
        )
        self._log_repo.create(
            RuleLog(
                id=execute_id,
                timestamp=start_time,
                kind=log_kind,
                rule_id=rule.id,
                rule_name=rule.name,
                rule_query=rule.condition.query,
                trigger_context=context,
                execute_result=exec_result,
            )
        )
        return exec_result

    def _select_slot(self, rule: Rule, event: RuleEvent) -> Slot:
        """Return ``("static", actions)`` / ``("dynamic", prompt_text)`` for the
        slot matching (direction, event), or ``None`` when the slot is empty.

        Dispatch kind is inferred from field presence: 单方向的 rule 看
        ``actions`` vs ``action_descriptions``; session 看 ``on_*_actions`` vs
        ``on_*_desc``。Validation enforces these as mutually exclusive.
        """
        task_slot = self._select_task_slot(rule, event)
        if task_slot is not _NO_TASK_ACTIONS:
            return task_slot

        # 达标槽在方向分支之前判: milestone 走下面的单方向分支就只认 ENTERED,
        # 达标永远选不到槽。
        if event == RuleEvent.TARGET_FIRED:
            if rule.on_target_desc:
                return ("dynamic", rule.on_target_desc)
            return None

        if rule.resolved_direction is not RuleDirection.SESSION:
            if event != RuleEvent.ENTERED:
                return None
            if rule.actions:
                return ("static", rule.actions)
            if not rule.action_descriptions:
                return None
            joined = "\n".join(
                f"{i + 1}. {d}" for i, d in enumerate(rule.action_descriptions)
            )
            return ("dynamic", joined)

        # session
        if event == RuleEvent.ENTERED:
            if rule.on_enter_actions:
                return ("static", rule.on_enter_actions)
            if rule.on_enter_desc:
                return ("dynamic", rule.on_enter_desc)
            return None
        if event == RuleEvent.EXITED:
            if rule.on_exit_actions:
                return ("static", rule.on_exit_actions)
            if rule.on_exit_desc:
                return ("dynamic", rule.on_exit_desc)
            return None
        return None

    # ---- 设备直控路径（V1 direct dispatch） ----

    def _select_task_slot(self, rule: Rule, event: RuleEvent) -> Slot:
        """从 task 的边界动作里选槽 (expand-contract 阶段 A: 读 task 优先)。

        返回 ``_NO_TASK_ACTIONS`` 表示该 task 没有动作快照 —— 调用方回退到 rule
        上的旧字段。返回 ``None`` 表示 task 接管了但这个方向是空槽, 不再回退:
        回退会让"用户故意留空的方向"重新捡起 rule 上的存量动作。
        """
        actions = self._task_actions.get(rule.task_id)
        if actions is None:
            return _NO_TASK_ACTIONS

        slot = _slot_for(rule, event)
        if slot is None:
            return None
        static_key, desc_key = f"{slot.value}_actions", f"{slot.value}_desc"

        raw_actions = actions.get(static_key) or []
        if raw_actions:
            return ("static", [RuleAction(**a) for a in raw_actions])
        desc = actions.get(desc_key)
        if desc:
            return ("dynamic", desc)
        return None

    def _in_cooldown(self, rule_id: str, action: RuleAction) -> bool:
        """非幂等 action 是否还在冷却窗内。幂等 action 恒 False（走读现值比对）。"""
        if action.idempotent or not action.cooldown_minutes:
            return False
        rs = self._state.get(rule_id)
        last_exec = (
            rs.action_cooldown.get((action.did, action.iid), 0)
            if rs is not None else 0
        )
        if time.time() - last_exec >= action.cooldown_minutes * 60:
            return False
        logger.info(
            "Rule %s action %s %s in cooldown, skipping",
            rule_id, action.did, action.iid,
        )
        return True

    def _mark_cooldown(self, rule_id: str, action: RuleAction) -> None:
        """执行成功后记冷却起点。未确认成功的执行不能记（否则会静默吞掉重试）。"""
        if action.idempotent or not action.cooldown_minutes:
            return
        self._ensure_state(rule_id).action_cooldown[
            (action.did, action.iid)
        ] = time.time()

    async def _execute_scene_action(
        self, rule_id: str, action: RuleAction
    ) -> RuleActionExecuteResult:
        """触发米家场景（``iid`` 为 SCENE_IID，``did`` 位置是 scene_id）。

        去重只靠冷却（原因见 ``SCENE_IID``）。台账由
        ``miot.service._trigger_scene`` 统一落，与 CLI 触发同一形状，只是
        ``source`` 标成 rule、``source_id`` 写 rule_id。
        """
        # service 校验只在 CRUD 走；runner 装载既有规则是直接从 repo 灌进来的，
        # 库里混进 idempotent=true 或 cooldown<1 的场景行都会让 _in_cooldown 直接
        # 放行 → 每次 fire 都真触发一次场景，零限频。这里再挡一道。
        if action.idempotent or (action.cooldown_minutes or 0) < 1:
            logger.error(
                "Rule %s scene action %s has no dedup guard "
                "(idempotent=%s, cooldown_minutes=%s), refusing to dispatch",
                rule_id, action.did, action.idempotent, action.cooldown_minutes,
            )
            return RuleActionExecuteResult(
                action=action,
                result=False,
                error=(
                    "scene action requires idempotent=false "
                    "and cooldown_minutes >= 1"
                ),
            )

        if self._in_cooldown(rule_id, action):
            return RuleActionExecuteResult(
                action=action, result=True, skipped=True
            )

        from miloco.miot.service import _trigger_scene

        try:
            success = await _trigger_scene(
                self._miot_proxy, action.did, source="rule", source_id=rule_id
            )
        except Exception as e:
            # 场景不存在 / 不在允许的家庭 / SDK 抛错都归到这里；失败详情进
            # rule_log.execute_result，不吞。
            logger.error(
                "Failed to trigger scene %s (rule %s): %s", action.did, rule_id, e
            )
            return RuleActionExecuteResult(
                action=action, result=False, error=f"exception: {e}"
            )

        if success:
            self._mark_cooldown(rule_id, action)
        return RuleActionExecuteResult(
            action=action,
            result=success,
            error=None if success else "scene_trigger_failed",
        )

    async def _execute_action(
        self, rule_id: str, action: RuleAction
    ) -> RuleActionExecuteResult:
        """Execute a single RuleAction (设备直控路径).

        Behavior is V1-compatible (per latest v3-system-overview.md §6.3):

        - ``iid == SCENE_IID`` → ``_execute_scene_action``
        - Parse ``iid`` ("prop.<siid>.<piid>" / "action.<siid>.<aiid>")
        - Idempotent path (only meaningful for prop.* + value not None):
          query current value, skip if already at target.
        - Cooldown path (idempotent=False with cooldown_minutes): skip if
          inside the time window since last successful exec.
        - Dispatch via miot_proxy.set_device_properties /
          call_device_action and report success.

        Cooldown state: ``self._state[rule_id].action_cooldown[(did, iid)]``.
        """
        # 场景没有 siid/aiid 可拆，必须在 iid 解析之前分流。
        if action.iid == SCENE_IID:
            return await self._execute_scene_action(rule_id, action)

        # 白名单式判定：三种形态之外一律报错。少了这道，`scene.1.2` 会掉进
        # 「不是 prop. 就当 action.」的兜底，拿 scene_id 当 did 发出去。
        parsed = parse_device_iid(action.iid)
        if parsed is None:
            logger.error("Invalid iid format '%s'", action.iid)
            return RuleActionExecuteResult(
                action=action, result=False, error=f"invalid_iid: {action.iid}"
            )
        is_prop, siid, p_a_id = parsed

        # Idempotent check: query current state, skip if already at target.
        if action.idempotent and is_prop and action.value is not None:
            try:
                results = await self._miot_proxy.get_device_properties(
                    [MIoTGetPropertyParam(did=action.did, siid=siid, piid=p_a_id)]
                )
                if results and results[0].get("code", -1) == 0:
                    if results[0].get("value") == action.value:
                        logger.info(
                            "Rule %s action %s %s already at target, skipping",
                            rule_id, action.did, action.iid,
                        )
                        return RuleActionExecuteResult(
                            action=action, result=True, skipped=True
                        )
            except Exception as e:
                logger.warning(
                    "Idempotent check failed: %s %s: %s",
                    action.did, action.iid, e,
                )

        # Cooldown check: non-idempotent actions inside cooldown window are skipped.
        if self._in_cooldown(rule_id, action):
            return RuleActionExecuteResult(
                action=action, result=True, skipped=True
            )

        # Execute. rule static 直控不经 MiotService.control_device,故这里显式落 action_ledger
        # ——复用同一个 _write_action_ledger helper(source=rule),避免两套组装逻辑漂移。
        import json as _json

        from miloco.miot.service import _write_action_ledger

        # 台账元组先归一好,成功/异常路径共用——SDK/网络抛异常时台账也要能看到
        # 规则当时试图设置什么值 / 什么参数(失败审计完整性)。
        _ltype = "set_property" if is_prop else "call_action"
        try:
            _lvalue = _json.dumps(
                action.value if is_prop else (action.params or []),
                ensure_ascii=False,
            )
        except Exception:
            _lvalue = None  # 参数不可序列化时不反噬规则执行

        try:
            if is_prop:
                params = [
                    MIoTSetPropertyParam(
                        did=action.did, siid=siid, piid=p_a_id, value=action.value
                    )
                ]
                results = await self._miot_proxy.set_device_properties(params)
                success, _lcode, _lmsg = _summarize_rule_result(results)
            else:
                param = MIoTActionParam(
                    did=action.did,
                    siid=siid,
                    aiid=p_a_id,
                    in_=action.params or [],
                )
                result = await self._miot_proxy.call_device_action(param)
                success, _lcode, _lmsg = _summarize_rule_result(result)
            # 有实际结果时与 control_device 同口径(负码即失败,镜像 #394,msg 是失败码
            # 释义);空/不可判定返回按失败处理(见 _summarize_rule_result:未确认的执行
            # 不能写 cooldown)。台账 result_msg 不再恒 NULL。
            err: str | None = None if success else (_lmsg or "miot_failed")

            await _write_action_ledger(
                self._miot_proxy,
                action_type=_ltype, did=action.did, iid=action.iid,
                value_json=_lvalue, result_code=_lcode, result_msg=_lmsg,
                success=success, error=err, source="rule", source_id=rule_id,
            )

            if success:
                self._mark_cooldown(rule_id, action)

            return RuleActionExecuteResult(
                action=action, result=success, error=err
            )

        except Exception as e:
            await _write_action_ledger(
                self._miot_proxy,
                action_type=_ltype,
                did=action.did, iid=action.iid, value_json=_lvalue,
                result_code=None, result_msg=None,
                success=False, error=str(e), source="rule", source_id=rule_id,
            )
            logger.error(
                "Failed to execute action %s %s: %s",
                action.did, action.iid, e,
            )
            return RuleActionExecuteResult(
                action=action, result=False, error=f"exception: {e}"
            )

    # ---- Agent 回调路径 ----

    # 3-retry exponential backoff: 1s, 2s, 4s between attempts (V3 §6.6.4)
    _AGENT_CALLBACK_MAX_RETRIES = 3
    _AGENT_CALLBACK_INITIAL_BACKOFF_SEC = 1.0

    async def _execute_dynamic(
        self,
        rule: Rule,
        event: RuleEvent,
        sources: list[str],
        prompt_text: str,
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        extra_metadata: dict | None = None,
        actual_exited_at: str | None = None,
        caption: str = "",
        device_name: str = "",
        trigger_reason: str = "",
    ) -> bool:
        """构造 V3 回调载荷，via OpenClaw plugin runtime 投递给 Agent。

        For ``lifecycle=temporary`` rules, ``terminate_when`` is appended to the
        prompt_text as an extra metadata line so the agent has visibility on
        the termination condition (v3-system-overview.md §6.5 metadata format).
        The **authoritative** termination path is the background
        ``TerminateEvaluator`` (see ``terminate_evaluator.py``); the agent
        **may** also self-delete via ``miloco-cli rule delete`` as a fast-path
        when it judges the condition met.

        ⚠️ Today the evaluator's ``_evaluate`` is a stub — temporary rules
        will not auto-clean until it lands. Use ``miloco-terminate-task`` skill or
        manual delete as bridge.

        Failure handling (V3 §6.6.4):
        - dispatch_event accepts on enqueue and returns True in the common
          case; this retry loop only covers enqueue rejection (queue-cap
          eviction), which is rare.
        - Transient webhook transport failures (connect / 5xx / HTTP timeout)
          are retried in dispatcher ``_send_batch`` (transport-level backoff),
          not here.
        - On enqueue rejection with retries exhausted, append a record to
          ``memory/_system/dynamic_failures.md`` and drop the callback (no
          catch-up on subsequent flips).
        """
        if actual_exited_at is not None:
            extra_metadata = {
                **(extra_metadata or {}),
                "actual_exited_at": actual_exited_at,
            }
        full_prompt = self._compose_prompt_text(rule, prompt_text, extra_metadata)
        callback = RuleTriggerCallback(
            rule_id=rule.id,
            rule_name=rule.name,
            event=event,
            triggered_at=ms_to_iso_local(now_ms()),
            source=sources,
            room_name=trigger_room,
            source_device_ids=trigger_dids or [],
            prompt_text=full_prompt,
            caption=caption,
            trigger_reason=trigger_reason,
            device_name=device_name,
            rule_query=rule.condition.query,
        )
        logger.debug(
            "Agent callback payload built: rule=%s event=%s sources=%s",
            rule.id, event.value, sources,
        )

        sent = await self._send_dynamic_with_retry(callback)
        if not sent:
            logger.error(
                "Agent callback exhausted retries for rule %s; "
                "recording to dynamic_failures.md",
                rule.id,
            )
            self._record_dynamic_failure(callback)
        return sent

    async def _send_dynamic_with_retry(
        self, callback: RuleTriggerCallback
    ) -> bool:
        """Enqueue the callback via dispatch_event, retrying enqueue rejection.

        dispatch_event returns True once the event is accepted into the queue
        (the common case), so this loop only retries the rare enqueue rejection
        (queue-cap eviction). Transient webhook transport retries live in
        dispatcher ``_send_batch``, not here. Returns True on acceptance, False
        after exhausting retries.
        Per V3 §6.6.4: missed callbacks are not replayed -- the next frame
        flip is treated as a new event.
        """
        delay = self._AGENT_CALLBACK_INITIAL_BACKOFF_SEC
        for attempt in range(self._AGENT_CALLBACK_MAX_RETRIES + 1):
            try:
                # sent = 入队被接纳。常态必成功;仅当 rule 事件被超长淘汰
                # (队列满且其为最不紧急)时返回 False，触发重试 / 兜底。
                sent = await dispatch_event(
                    "rule", [callback], build_rule_callbacks_text
                )
            except Exception as e:
                logger.warning(
                    "Agent callback attempt %d raised: %s", attempt + 1, e
                )
                sent = False

            if sent:
                if attempt > 0:
                    logger.info(
                        "Agent callback succeeded for rule %s after %d retries",
                        callback.rule_id,
                        attempt,
                    )
                return True

            if attempt < self._AGENT_CALLBACK_MAX_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
        return False

    @staticmethod
    def _record_dynamic_failure(callback: RuleTriggerCallback) -> None:
        """Append a record to ``<workspace>/memory/_system/dynamic_failures.md``.

        Path is plugin-internal; final location may shift to OpenClaw plugin
        storage once the runtime interface is finalized. Failure of this path
        itself is logged but never raises -- it is already a tail-fallback.
        """
        try:
            # Lazy import to keep runner import cheap and avoid pulling settings
            # into module-import-time circular dependencies.
            from miloco.config import get_settings

            workspace = get_settings().directories.workspace_dir
            path = workspace / "memory" / "_system" / "dynamic_failures.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            indented_prompt = callback.prompt_text.replace("\n", "\n    ")
            entry = (
                "\n---\n"
                f"- triggered_at: {callback.triggered_at}\n"
                f"- rule_id: {callback.rule_id}\n"
                f"- rule_name: {callback.rule_name}\n"
                f"- event: {callback.event.value}\n"
                f"- source: {callback.source}\n"
                f"- room_name: {callback.room_name}\n"
                f"- source_device_ids: {callback.source_device_ids}\n"
                f"- session: {callback.session}\n"
                f"- prompt_text: |\n"
                f"    {indented_prompt}\n"
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.error("Failed to record dynamic failure (rule %s): %s",
                         callback.rule_id, e)

    def _compose_prompt_text(
        self, rule: Rule, slot_text: str, extra_metadata: dict | None = None
    ) -> str:
        """三段拼装：意图 → 处理流程（仅 WITH_RECORD） → 额外信息。

        Record-bound 判定由 backend 实时查 task_record 决定，不依赖 desc 字符串里的
        marker。task_id / record_kind / terminate_when 与传入的 extra_metadata 字段
        合并写入"额外信息" JSON 块（单行 ensure_ascii=False），agent 端解析时只看
        JSON，不再扫末尾 k=v 行。
        """
        import json

        from miloco.rule.schema import RuleLifecycle

        record_kind = (
            self._task_record_service.detect_record_kind(rule.task_id)
            if rule.task_id
            else None
        )

        info: dict = {}
        if record_kind is not None and rule.task_id:
            info["task_id"] = rule.task_id
            info["record_kind"] = record_kind
        if rule.lifecycle == RuleLifecycle.TEMPORARY and rule.terminate_when:
            info["terminate_when"] = rule.terminate_when
        if extra_metadata:
            info.update(extra_metadata)

        info_json = json.dumps(info, ensure_ascii=False)

        parts = [f"**意图**：\n{slot_text}"]
        if record_kind is not None:
            parts.append(_FIRE_PREAMBLE_WITH_RECORD)
        parts.append(f"**额外信息**：\n{info_json}")

        return "\n\n---\n\n".join(parts)
