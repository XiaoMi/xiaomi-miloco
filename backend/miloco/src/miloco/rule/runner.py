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
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

if TYPE_CHECKING:
    from miloco.task_record.service import TaskRecordService

from miot.types import MIoTActionParam, MIoTGetPropertyParam, MIoTSetPropertyParam

from miloco.database.rule_repo import RuleLogRepo
from miloco.dispatch import dispatch_event
from miloco.miot.client import MiotProxy
from miloco.node_monitor import NodeName, get_monitor
from miloco.observability.metrics_client import get_metrics_client
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleEvent,
    RuleExecuteResult,
    RuleLog,
    RuleLogKind,
    RuleMode,
    RuleTriggerCallback,
)
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)

_EMPTY_RESULT_MSG = "MIoT 返回为空/不可判定,无法确认执行结果"


class TriggerOutcome(str, Enum):
    """一次 ``update_state`` 判定的「触发结论」——供住户日志展示「本周期是否真触发」。

    反映的是**状态机是否派发了一次触发**，不含 agent / 设备下游执行成败。
    """

    FIRED = "FIRED"          # 本周期到达 fire 决策点（ENTER 边沿 / 计时达标）
    STILL_IN = "STILL_IN"    # 已在态内、条件持续满足，不重复触发（含吸收伪退出/抖动）
    COUNTING = "COUNTING"    # duration 规则累积中，窗口未满 / 比例未达
    NOT_FIRED = "NOT_FIRED"  # 抗抖观察 / 空 slot / 其它未触发

    @property
    def label(self) -> str:
        return {
            TriggerOutcome.FIRED: "已触发",
            TriggerOutcome.STILL_IN: "未触发（持续中）",
            TriggerOutcome.COUNTING: "未触发（计时中）",
            TriggerOutcome.NOT_FIRED: "未触发",
        }[self]

    @property
    def _priority(self) -> int:
        return {
            TriggerOutcome.FIRED: 3,
            TriggerOutcome.COUNTING: 2,
            TriggerOutcome.STILL_IN: 1,
            TriggerOutcome.NOT_FIRED: 0,
        }[self]


def aggregate_outcomes(outcomes: Iterable[TriggerOutcome]) -> TriggerOutcome | None:
    """同一 rule 本周期多摄像头的结论聚合：取「最强」信号
    （FIRED > COUNTING > STILL_IN > NOT_FIRED）。空输入返回 None。"""
    best: TriggerOutcome | None = None
    for o in outcomes:
        if best is None or o._priority > best._priority:
            best = o
    return best


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
    """
    if not callbacks:
        return None

    from miloco.perception.event_text_builder import HEADER_MATCHED_RULE

    def _fmt_source(c: RuleTriggerCallback) -> str:
        did_tag = f"(did={','.join(c.source)})" if c.source else ""
        if c.room_name and c.device_name:
            return f"{c.room_name}的{c.device_name}{did_tag}"
        if c.room_name:
            return f"{c.room_name}{did_tag}" if did_tag else c.room_name
        if c.device_name:
            return f"{c.device_name}{did_tag}"
        return did_tag  # 仅 did 兜底

    def _fmt(c: RuleTriggerCallback) -> str:
        lines: list[str] = []
        time = c.triggered_at.split("T")[1][:8] if "T" in c.triggered_at else ""
        if time:
            lines.append(f"时间：{time}")
        source = _fmt_source(c)
        if source:
            lines.append(f"来源：{source}")
        if c.caption:
            lines.append(f"画面描述：{c.caption.rstrip('。.')}")
        condition = c.rule_query or c.rule_name
        if condition:
            lines.append(f"触发条件：{condition}")
        if c.trigger_reason:
            lines.append(f"触发原因：{c.trigger_reason.rstrip('。.')}")
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
    target_timer: "asyncio.Task | None" = None
    target_fired: bool = False
    action_cooldown: dict[tuple[str, str], float] = field(default_factory=dict)
    # 每 source 最近一次 update_state(True) 的触发结论（供日志展示）。随本 state 整条 pop 清理。
    last_outcome_by_source: dict[str, TriggerOutcome] = field(default_factory=dict)


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

        logger.info("RuleRunner init, rules: %d", len(self._rules))

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
    def _target_timers(self) -> dict[str, asyncio.Task]:
        return {
            rid: st.target_timer
            for rid, st in self._state.items()
            if st.target_timer is not None
        }

    @property
    def _target_fired(self) -> set[str]:
        return {rid for rid, st in self._state.items() if st.target_fired}

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

        When replacing an existing rule whose ``mode`` or
        ``condition.perceive_device_ids`` changed, drop the per-rule runtime
        state (last_source/rule_state, pending_exit, action_cooldown). Keeping
        stale state across a shape change can resurrect old EXIT debounces
        or skew the next OR-aggregation.
        """
        existing = self._rules.get(rule.id)
        if existing is not None:
            mode_changed = existing.mode != rule.mode
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
                mode_changed
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
        state = self._state.pop(rule_id, None)
        if state is None:
            return
        if state.exit_debounce_task is not None and not state.exit_debounce_task.done():
            state.exit_debounce_task.cancel()
        if state.target_timer is not None and not state.target_timer.done():
            state.target_timer.cancel()

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

    def get_enabled_rules(self) -> list[Rule]:
        return [r for r in self._rules.values() if r.enabled]

    def get_source_outcome(
        self, rule_id: str, source_did: str
    ) -> TriggerOutcome | None:
        """最近一次 ``update_state(True)`` 对 (rule, source) 记下的触发结论。
        未记录（本 cycle 未命中该源 / 规则已重置）返回 None。"""
        state = self._state.get(rule_id)
        if state is None:
            return None
        return state.last_outcome_by_source.get(source_did)

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
            if not rule.enabled:
                return TriggerOutcome.NOT_FIRED

            src = self._ensure_source(rule_id, source_did)
            prev = src.last_bool

            # 触发结论记账：仅对 True（命中）调用写入 last_outcome_by_source，供住户日志
            # 展示「本周期是否真触发」。out() 统一分流：duration 规则的结论完全由
            # _evaluate_duration 决定（frame 路径对 duration 不 fire、只做状态维护），
            # 非 duration 规则用 frame/diff 路径结论。
            dur_outcome: TriggerOutcome | None = None

            def record(outcome: TriggerOutcome) -> TriggerOutcome:
                if current_bool:
                    self._state[rule_id].last_outcome_by_source[source_did] = outcome
                return outcome

            def out(frame_outcome: TriggerOutcome) -> TriggerOutcome:
                return record(dur_outcome if rule.duration_seconds else frame_outcome)

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
                dur_outcome = self._evaluate_duration(
                    rule, effective_state, source_did, context, caption, device_name
                )

            # 帧级抗抖：source 上次 True 时，单帧 False 不立即翻转 — 视为 LLM 漏识，
            # 留一帧观察窗。下一帧仍 False 才确认 EXIT；翻回 True 则吸收为抖动。
            if prev:
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
            elif self._state[rule_id].exit_debounce_task is not None:
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
            )
            h.add_output(1)
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
        if not rule.enabled:
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

    def _evaluate_duration(
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
        - STATE mode 且已 fire on_enter（``state.state_duration_fired`` 置位）
          → 直接 return：STILL_IN 期间不重复 fire，等 _debounced_exit 真完成时
          清标记重新累积。EVENT mode 不用本拦截，fire 后清窗口走"周期 fire"
          by-design。
        """
        state = self._ensure_state(rule.id)
        if rule.mode == RuleMode.STATE and state.state_duration_fired:
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
                rule.mode.value,
                actual_started_at,
                sum(win),
                maxlen,
                rule.duration_ratio,
            )
            if rule.mode == RuleMode.EVENT:
                # EVENT：清窗口 → 下次 update_state 重新累积（by-design 周期 fire）
                state.duration_window = None
                state.last_duration_round = None
            else:
                # STATE：标记 fired 拦截 STILL_IN 重复 fire；窗口留着无害
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
            if rule.mode == RuleMode.STATE:
                self._schedule_target_timer_if_needed(rule, sources, context)
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
    ) -> TriggerOutcome:
        """Translate a diff event into an action-layer fire (with state-mode
        debounce on EXITED).

        Returns the触发结论 for the ENTERED path (``FIRED`` only when it actually
        spawns a fire). EXITED / suppressed paths return ``NOT_FIRED`` — for
        duration rules the caller ignores this and uses ``_evaluate_duration``."""
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

            sources = self._sources_currently_true(rule.id) or [source_did]
            # Fire-and-forget: dynamic callback retry is up to 1+2+4=7s of sleep,
            # and update_state() runs on perception's hot path. Awaiting fire
            # here would freeze the main loop for the duration of every dynamic
            # retry. The state-machine bookkeeping above is already done; the
            # fire only writes log/cooldown state, which is safe to do async.
            self._spawn_fire(
                rule, event, sources, context, trigger_room, trigger_dids,
                extra_metadata={"actual_started_at": actual_started_at},
                caption=caption, device_name=device_name,
            )
            self._schedule_target_timer_if_needed(rule, sources, context)
            return TriggerOutcome.FIRED

        # EXITED
        if rule.mode == RuleMode.EVENT:
            return TriggerOutcome.NOT_FIRED  # event mode does not handle exits

        # STATE + duration 但未 fire on_enter：进入态从未被确认 → 当这次 EXITED
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
        # on_target timer：cancel 未触发的 timer（保留 ``rs.target_fired``，
        # 同一天不重复 fire；清 fired 由跨日 force-reset / config reset 路径做）。
        # 兜底：cancel 前若 accumulated 已 ≥ target，先 fire TARGET——EXIT 60s
        # debounce 窗口内若累计跨过 target，cancel 否则会让达标信号丢失。
        self._fire_target_if_reached(rule, sources, "exit_debounce_target_check")
        self._cancel_target_timer(rule.id)
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

    # ---- on_target_desc 累计达标 timer（duration record 路径） ----

    def _fire_target_if_reached(
        self,
        rule: Rule,
        sources: list[str],
        context: str,
        state: tuple[int | None, int] | None = None,
    ) -> bool:
        """accumulated 已 ≥ target → 立即 fire TARGET 并返回 True；否则返回 False。
        ENTERED schedule 与 EXIT cancel 路径共用此入口：保证「达标必通知」语义
        不依赖 timer 时序——EXIT debounce 抢先于 timer 触发时，cancel 之前也要兑现。
        state 由调用方预读时直接传入，避免重复 SQL 查询。"""
        if not rule.on_target_desc:
            return False
        rs = self._ensure_state(rule.id)
        if rs.target_fired:
            return False
        if state is None:
            state = self._task_record_service.read_duration_target_state(
                rule.task_id
            )
        if state is None:
            return False
        target_minutes, accumulated_min = state
        if target_minutes is None:
            return False
        if accumulated_min < target_minutes:
            return False
        logger.info(
            "TARGET_IMMEDIATE: rule=%s task=%s accumulated_min=%s target_min=%s",
            rule.id, rule.task_id, accumulated_min, target_minutes,
        )
        rs.target_fired = True
        actual_target_at = ms_to_iso_local(now_ms())
        self._spawn_fire(
            rule, RuleEvent.TARGET_FIRED, list(sources), context,
            extra_metadata={
                "target_minutes": target_minutes,
                "actual_target_at": actual_target_at,
                "accumulated_at_fire": accumulated_min,
            },
        )
        return True

    def _schedule_target_timer_if_needed(
        self, rule: Rule, sources: list[str], context: str
    ) -> None:
        """ENTERED 真 fire 后调用：读 record.accumulated/target，起 timer 或立即 fire。"""
        rs = self._ensure_state(rule.id)
        # 取消可能残留的 timer（add_rule 重建场景 / 并发保护）
        old = rs.target_timer
        if old is not None and not old.done():
            old.cancel()
        rs.target_timer = None
        if not rule.on_target_desc:
            return
        if rs.target_fired:
            return
        state = self._task_record_service.read_duration_target_state(rule.task_id)
        if state is None:
            return
        if self._fire_target_if_reached(rule, sources, context, state=state):
            return
        target_minutes, accumulated_min = state
        if target_minutes is None:
            return
        remaining_seconds = max(
            0, target_minutes * 60 - accumulated_min * 60
        )
        task = asyncio.create_task(
            self._await_and_fire_target(
                rule, list(sources), context, remaining_seconds, target_minutes,
            )
        )
        rs.target_timer = task
        fires_at_ts_ms = int(time.time() * 1000) + remaining_seconds * 1000
        logger.info(
            "TARGET_SCHEDULED: rule=%s task=%s remaining_s=%d fires_at_ts_ms=%d "
            "(accumulated_min=%s target_min=%s)",
            rule.id, rule.task_id, remaining_seconds, fires_at_ts_ms,
            accumulated_min, target_minutes,
        )
        self._publish_rule_event(
            "rule_target_scheduled", rule.id,
            {
                "remaining_seconds": remaining_seconds,
                "fires_at_ts_ms": fires_at_ts_ms,
                "accumulated_minutes": accumulated_min,
                "target_minutes": target_minutes,
            },
        )

    async def _await_and_fire_target(
        self, rule: Rule, sources: list[str], context: str, delay: float,
        target_minutes: int,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        rs = self._ensure_state(rule.id)
        rs.target_timer = None
        # 守卫 1：condition 仍真满足才 fire（抖动 / EXITED 已发生则 drop）
        if not rs.last_rule_state:
            logger.info(
                "TARGET_DROPPED: rule=%s state-false-at-fire", rule.id,
            )
            return
        # 守卫 2：本 session 已 fire 过（理论上不会，防御性）
        if rs.target_fired:
            return
        rs.target_fired = True
        actual_target_at = ms_to_iso_local(now_ms())
        # fire 时刻 read 最新 accumulated（含 in-flight session）作为真实值；
        # 异常 / record 不可读时降级为 target_minutes（恒等式：达标必 ≥ target）。
        state = self._task_record_service.read_duration_target_state(rule.task_id)
        accumulated_at_fire = (
            state[1] if state is not None else target_minutes
        )
        try:
            await self._fire(
                rule, RuleEvent.TARGET_FIRED, sources, context, str(uuid.uuid4()),
                extra_metadata={
                    "target_minutes": target_minutes,
                    "actual_target_at": actual_target_at,
                    "accumulated_at_fire": accumulated_at_fire,
                },
            )
        except Exception:
            logger.exception(
                "Rule %s on_target fire failed", rule.id
            )

    def _cancel_target_timer(self, rule_id: str) -> None:
        """只 cancel 未触发的 timer，不动 target_fired 标记。

        `target_fired` 是 record-session 维度（每天 record rollover 清零），
        不是 rule-session 维度——同一天 EXITED 后再 ENTERED 不该重复 fire。
        清 fired 由跨日 force-reset / config reset / rule delete 路径显式做。
        """
        rs = self._state.get(rule_id)
        if rs is None:
            return
        t = rs.target_timer
        rs.target_timer = None
        if t is not None and not t.done():
            t.cancel()

    def force_cross_day_reset(
        self,
        task_id: str,
        pre_rollover_state: tuple[int | None, int] | None = None,
    ) -> None:
        """跨日 rollover 完成后调：对所有"当前在 ENTERED 态"的 task 关联 rule，
        真 fire on_exit（计入旧一天 session-end）+ 真 fire on_enter（建新一天
        session-start）+ 重新按 accumulated=0 schedule on_target timer。

        语义上等价于"用户跨过 00:00 那一刻 EXITED → ENTERED"，但实际 condition
        没变；``state.last_rule_state`` 保持 True（避免下一次 condition tick
        再触发 ENTERED）。

        pre_rollover_state 为 rollover_one 执行前 snapshot 的旧一天
        ``(target_minutes, accumulated_minutes_today)``。若旧累计已 ≥ target
        且本 session 未 fire 过，先 fire 兑现累计达标承诺——rollover 已清旧累计，
        read_duration_target_state 读不到这个信号，必须用 snapshot 判断。
        """
        affected: list[Rule] = [
            r for r in self._rules.values()
            if r.task_id == task_id
            and r.id in self._state
            and self._state[r.id].last_rule_state
        ]
        if not affected:
            return
        for rule in affected:
            logger.info(
                "CROSS_DAY_RESET: rule=%s task=%s force on_exit + on_enter",
                rule.id, task_id,
            )
            sources = self._sources_currently_true(rule.id)
            # 0) 跨日前若旧一天累计已达标且本 session 未 fire，先 fire on_target
            #    （清 ``rs.target_fired`` 前调，让 helper 内部守卫正常工作）
            if pre_rollover_state is not None:
                self._fire_target_if_reached(
                    rule, sources, "cross_day_pre_rollover_check",
                    state=pre_rollover_state,
                )
            # 1) 取消未触发的 target timer + 清 fired 标记
            #    （跨日新一天 record 重新计 accumulated，必须让 fired 状态归零，
            #    否则新一天 ENTERED 走 _schedule_target_timer_if_needed 早返不 fire）
            self._cancel_target_timer(rule.id)
            rs = self._ensure_state(rule.id)
            rs.target_fired = False
            # 2) 取消可能在跑的 exit debounce（用户真在态内，不该有，但兜底）
            pending = rs.exit_debounce_task
            rs.exit_debounce_task = None
            if pending is not None and not pending.done():
                pending.cancel()
            rs.exit_debounce_at = None
            # 3) STATE + duration：清 fired 标记和窗口，让 on_enter 重新走累积
            #    （新一天计时窗口从零开始）
            if rule.duration_seconds:
                rs.state_duration_fired = False
                rs.duration_window = None
                rs.last_duration_round = None
            # 4) 强制 fire on_exit / on_enter：不注入 actual_exited_at /
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
            # 5) 重新 schedule on_target timer（accumulated 已被 rollover 清零）
            self._schedule_target_timer_if_needed(
                rule, sources, "cross_day_rollover",
            )

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

        logger.info(
            "FIRE: rule=%s name=%s event=%s mode=%s slot=%s sources=%s execute_id=%s",
            rule.id, rule.name, event.value, rule.mode.value, kind, sources, execute_id,
        )
        self._publish_rule_event(
            "rule_fire", rule.id,
            {
                "event": event.value,
                "mode": rule.mode.value,
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
        slot matching (mode, event), or ``None`` when the slot is empty.

        Dispatch kind is inferred from field presence: event mode looks at
        ``actions`` vs ``action_descriptions``; state mode looks at
        ``on_*_actions`` vs ``on_*_desc`` per direction. Validation enforces
        these as mutually exclusive.
        """
        if rule.mode == RuleMode.EVENT:
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

        # state mode
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
        if event == RuleEvent.TARGET_FIRED:
            if rule.on_target_desc:
                return ("dynamic", rule.on_target_desc)
            return None
        return None

    # ---- 设备直控路径（V1 direct dispatch） ----

    async def _execute_action(
        self, rule_id: str, action: RuleAction
    ) -> RuleActionExecuteResult:
        """Execute a single RuleAction (设备直控路径).

        Behavior is V1-compatible (per latest v3-system-overview.md §6.3):

        - Parse ``iid`` ("prop.<siid>.<piid>" / "action.<siid>.<aiid>")
        - Idempotent path (only meaningful for prop.* + value not None):
          query current value, skip if already at target.
        - Cooldown path (idempotent=False with cooldown_minutes): skip if
          inside the time window since last successful exec.
        - Dispatch via miot_proxy.set_device_properties /
          call_device_action and report success.

        Cooldown state: ``self._state[rule_id].action_cooldown[(did, iid)]``.
        """
        parts = action.iid.split(".")
        try:
            siid, p_a_id = int(parts[1]), int(parts[2])
        except (IndexError, ValueError) as e:
            logger.error("Invalid iid format '%s': %s", action.iid, e)
            return RuleActionExecuteResult(
                action=action, result=False, error=f"invalid_iid: {action.iid}"
            )

        is_prop = action.iid.startswith("prop.")

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
        if not action.idempotent and action.cooldown_minutes:
            rs = self._state.get(rule_id)
            last_exec = (
                rs.action_cooldown.get((action.did, action.iid), 0)
                if rs is not None else 0
            )
            if time.time() - last_exec < action.cooldown_minutes * 60:
                logger.info(
                    "Rule %s action %s %s in cooldown, skipping",
                    rule_id, action.did, action.iid,
                )
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

            if success and not action.idempotent and action.cooldown_minutes:
                self._ensure_state(rule_id).action_cooldown[
                    (action.did, action.iid)
                ] = time.time()

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
