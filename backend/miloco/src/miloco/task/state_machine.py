# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 运行态状态机。

rule 产边沿, task 消费。本模块只做三件事: 聚合名下 rule 的信号、维护
``runtime_state``、把边界动作派给动作执行组件。

设计见 docs/superpowers/specs/2026-07-21-task-runtime-state-and-multi-source-
rules-design.md §5 / §15 / §19.4~§19.6。三条硬约束:

1. **不做慢操作** —— 不 await 设备控制、agent 回调、DB 写入。动作一律经
   ``dispatch_action`` 交出去异步跑。这是队列不积压的根本保证, 容量只是兜底。
2. **动作失败不反噬状态** —— ``runtime_state`` 反映感知到的现实, 不反映动作成败。
3. **per-task 串行** —— 每个 task 一条消费链, 幂等判断与状态写入之间不会插进别的信号。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# per-task 信号队列深度。状态机只做内存判断 + 派发, 正常绝不会积压;
# 积压到这个数本身就是异常信号, 溢出必须可观测而不是静默丢。
SIGNAL_QUEUE_DEPTH = 8


class RuleDirection(str, Enum):
    ENTER = "enter"
    EXIT = "exit"
    SESSION = "session"
    MILESTONE = "milestone"


class TaskRuntimeState(str, Enum):
    OFF = "off"
    ON = "on"


class SignalKind(str, Enum):
    """聚合前的原始信号 —— rule 产的边沿, 尚未映射成进/出。"""

    ENTERED = "entered"
    EXITED = "exited"


class ActionSlot(str, Enum):
    ON_ENTER = "on_enter"
    ON_EXIT = "on_exit"
    ON_TARGET = "on_target"


class TransitionOutcome(str, Enum):
    """一次信号处理的结论, 供 §18 跟踪。正常类与异常类都在这里, 落库与否由跟踪层判。"""

    ENTERED = "entered"
    EXITED = "exited"
    EVENT_FIRED = "event_fired"
    MILESTONE_FIRED = "milestone_fired"
    ALREADY_IN_STATE = "already_in_state"
    BLOCKED_BY_EXIT_CONDITION = "blocked_by_exit_condition"
    STILL_HELD = "still_held"
    NOT_IN_SESSION = "not_in_session"
    SIGNAL_DROPPED = "signal_dropped"
    UNKNOWN_RULE = "unknown_rule"


@dataclass(frozen=True)
class TaskSignal:
    task_id: str
    rule_id: str
    kind: SignalKind
    # 派发时原样交回动作层的上下文。状态机不解释它。
    # compare=False: payload 常是 dict / 模型对象, 带进 __hash__ 会炸。
    payload: object | None = field(default=None, compare=False)


@dataclass
class TaskTopology:
    """一个 task 名下 rule 的方向分布 —— 形态由此推导, 不是存储字段 (§4.3)。"""

    task_id: str
    directions: dict[str, RuleDirection] = field(default_factory=dict)

    @property
    def enter_side_rule_ids(self) -> set[str]:
        """能把 task 推进 ``on`` 的 rule。"""
        return {
            rid
            for rid, d in self.directions.items()
            if d in (RuleDirection.ENTER, RuleDirection.SESSION)
        }

    @property
    def exit_side_rule_ids(self) -> set[str]:
        """能把 task 推回 ``off`` 的 rule。"""
        return {
            rid
            for rid, d in self.directions.items()
            if d in (RuleDirection.EXIT, RuleDirection.SESSION)
        }

    @property
    def is_session_type(self) -> bool:
        """有出路径才谈得上"模式开着" —— 否则是事件型, 恒 ``off``。"""
        return bool(
            {
                d
                for d in self.directions.values()
                if d in (RuleDirection.EXIT, RuleDirection.SESSION)
            }
        )


def aggregate_to_transition(
    signal: TaskSignal, topology: TaskTopology
) -> ActionSlot | None:
    """聚合点: rule 边沿 → task 进/出意图。本次是 OR 直通 (§15)。

    OR 不是一条聚合逻辑, 是透明层的默认行为 —— 单 rule 恒等映射, 多 rule 因状态机
    幂等消费自然坍缩成 OR。future 换 AND / guard 只动这一个函数。

    milestone 不进聚合 (§5.3), 由调用方在此之前分流。
    """
    direction = topology.directions.get(signal.rule_id)
    if direction is None:
        return None
    if direction is RuleDirection.ENTER and signal.kind is SignalKind.ENTERED:
        return ActionSlot.ON_ENTER
    if direction is RuleDirection.EXIT and signal.kind is SignalKind.ENTERED:
        # exit 型 rule 的"条件成立"就是"该退出了" —— 它自己的 entered 边沿是出信号
        return ActionSlot.ON_EXIT
    if direction is RuleDirection.SESSION:
        return (
            ActionSlot.ON_ENTER
            if signal.kind is SignalKind.ENTERED
            else ActionSlot.ON_EXIT
        )
    return None


class TaskStateMachine:
    """per-task 串行消费信号, 维护 runtime_state, 派发边界动作。

    四个注入点让本模块不依赖 rule / 动作层, 可独立测试:

    - ``is_condition_satisfied(rule_id) -> bool | None``: 该 rule 的条件现在是不是真。
      ``None`` = 未就绪 (未 seed / 设备离线 / 脉冲型无稳态)。
    - ``reset_edge_baseline(rule_id)``: 把该 rule 的边沿基线置为"已满足" (§5.2)。
    - ``dispatch_action(task_id, slot, payload)``: 交出去异步跑, **必须立即返回**。
      ``payload`` 是 submit 时带的上下文, 状态机原样转交。
    - ``track(outcome, signal)``: 跟踪一次结论, 必须是同步内存操作 (§18.3)。
    - ``on_forget(task_id)``: task 撤销登记时清跟踪数据。
    """

    def __init__(
        self,
        *,
        is_condition_satisfied: Callable[[str], bool | None],
        reset_edge_baseline: Callable[[str], None],
        dispatch_action: Callable[[str, ActionSlot, object | None], None],
        track: Callable[[TransitionOutcome, TaskSignal], None] | None = None,
        on_forget: Callable[[str], None] | None = None,
    ) -> None:
        self._is_condition_satisfied = is_condition_satisfied
        self._reset_edge_baseline = reset_edge_baseline
        self._dispatch_action = dispatch_action
        self._track = track or (lambda outcome, signal: None)
        self._on_forget = on_forget or (lambda task_id: None)

        # handle(dispatch=False) 期间关掉派发 —— 见 handle 的 docstring
        self._dispatching = True
        self._topologies: dict[str, TaskTopology] = {}
        self._states: dict[str, TaskRuntimeState] = {}
        self._queues: dict[str, deque[TaskSignal]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._consumers: dict[str, asyncio.Task] = {}
        # 重新配置期间到达的信号直接丢 —— 它们对应的是改动前的配置 (§19.5)
        self._reconfiguring: set[str] = set()

    # ── 查询 ──────────────────────────────────────────────────────

    def owns(self, task_id: str) -> bool:
        """该 task 的拓扑是否已登记。未登记 → 调用方走旧路径。"""
        return task_id in self._topologies

    def runtime_state(self, task_id: str) -> TaskRuntimeState:
        return self._states.get(task_id, TaskRuntimeState.OFF)

    # ── 拓扑维护 ──────────────────────────────────────────────────

    def register_task(self, task_id: str, directions: dict[str, RuleDirection]) -> None:
        """登记 / 重新登记一个 task。重启后从 ``off`` 起 (§7)。"""
        self._topologies[task_id] = TaskTopology(task_id, dict(directions))
        self._states.setdefault(task_id, TaskRuntimeState.OFF)
        self._queues.setdefault(task_id, deque(maxlen=SIGNAL_QUEUE_DEPTH))
        self._wakeups.setdefault(task_id, asyncio.Event())

    def unregister_task(self, task_id: str) -> None:
        # 跟踪层也要跟着清 —— 不清就是一条随 task 数单调增长的内存泄漏
        self._on_forget(task_id)
        self._stop_consumer(task_id)
        self._topologies.pop(task_id, None)
        self._states.pop(task_id, None)
        self._queues.pop(task_id, None)
        self._wakeups.pop(task_id, None)
        self._reconfiguring.discard(task_id)

    # ── 信号入口 ──────────────────────────────────────────────────

    def submit(self, signal: TaskSignal) -> None:
        """投递一个信号。**同步、不阻塞** —— 上游是 omni hot path。"""
        queue = self._queues.get(signal.task_id)
        if queue is None:
            self._track(TransitionOutcome.UNKNOWN_RULE, signal)
            return
        if signal.task_id in self._reconfiguring:
            self._track(TransitionOutcome.SIGNAL_DROPPED, signal)
            return
        if len(queue) == SIGNAL_QUEUE_DEPTH:
            # deque(maxlen) 满了会自己丢最旧。终态仍对 (最后一个信号代表最新现实),
            # 但被丢那条对应的边界动作也一起丢了 —— 所以归异常类、必须可观测。
            self._track(TransitionOutcome.SIGNAL_DROPPED, queue[0])
        queue.append(signal)
        self._wakeups[signal.task_id].set()

    def start(self, task_id: str) -> None:
        """起该 task 的消费链。重复调用是 no-op。"""
        if task_id in self._consumers and not self._consumers[task_id].done():
            return
        if task_id not in self._queues:
            return
        self._consumers[task_id] = asyncio.create_task(
            self._consume(task_id), name=f"task-sm-{task_id}"
        )

    def _stop_consumer(self, task_id: str) -> None:
        consumer = self._consumers.pop(task_id, None)
        if consumer is not None and not consumer.done():
            consumer.cancel()

    async def _consume(self, task_id: str) -> None:
        wakeup = self._wakeups[task_id]
        queue = self._queues[task_id]
        while True:
            await wakeup.wait()
            wakeup.clear()
            while queue:
                self.handle(queue.popleft())

    # ── 状态转换 ──────────────────────────────────────────────────

    def handle(self, signal: TaskSignal, *, dispatch: bool = True) -> TransitionOutcome:
        """处理一个信号。同步且不抛 —— 它跑在消费链上, 抛出去会把整条链带死。

        ``dispatch=False``: 只要结论、不派动作。给"上游已经拿着一次边沿、自己会
        fire"的调用方用 (rule runner 走的就是这条)。默认 True 是给消费链和状态机
        自己发起的路径用的。
        """
        self._dispatching = dispatch
        try:
            return self._handle(signal)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "task state machine failed on %s/%s: %s",
                signal.task_id,
                signal.rule_id,
                e,
            )
            return TransitionOutcome.UNKNOWN_RULE

    def _handle(self, signal: TaskSignal) -> TransitionOutcome:
        topology = self._topologies.get(signal.task_id)
        if topology is None:
            return self._done(TransitionOutcome.UNKNOWN_RULE, signal)

        direction = topology.directions.get(signal.rule_id)
        if direction is None:
            return self._done(TransitionOutcome.UNKNOWN_RULE, signal)

        if direction is RuleDirection.MILESTONE:
            return self._handle_milestone(signal)

        slot = aggregate_to_transition(signal, topology)
        if slot is ActionSlot.ON_ENTER:
            return self._handle_enter(signal, topology)
        if slot is ActionSlot.ON_EXIT:
            return self._handle_exit(signal, topology)
        return self._done(TransitionOutcome.ALREADY_IN_STATE, signal)

    def _handle_milestone(self, signal: TaskSignal) -> TransitionOutcome:
        """不改状态、不查稳态、不重置基线 —— 只确认 task 在 ``on`` 然后派动作 (§5.3)。"""
        if self.runtime_state(signal.task_id) is not TaskRuntimeState.ON:
            # milestone 的语义是"这个 task 进行期间发生了什么"; task 没在进行,
            # 里程碑无处附着。
            return self._done(TransitionOutcome.NOT_IN_SESSION, signal)
        self._maybe_dispatch(signal.task_id, ActionSlot.ON_TARGET, signal.payload)
        return self._done(TransitionOutcome.MILESTONE_FIRED, signal)

    def _handle_enter(
        self, signal: TaskSignal, topology: TaskTopology
    ) -> TransitionOutcome:
        if not topology.is_session_type:
            # 事件型: runtime_state 恒 off, 每次进信号都执行 on_enter, 不卡死。
            self._maybe_dispatch(signal.task_id, ActionSlot.ON_ENTER, signal.payload)
            return self._done(TransitionOutcome.EVENT_FIRED, signal)

        if self.runtime_state(signal.task_id) is TaskRuntimeState.ON:
            # 幂等: 多条路径同时进只执行一次边界动作。
            return self._done(TransitionOutcome.ALREADY_IN_STATE, signal)

        if self._other_exit_side_holds(signal, topology):
            # §5.1: 进入时退出条件已为真 → 拒绝进入。让错误表现从"开了永远不关"
            # 变成"从来不开" —— 后者用户当场能发现。
            return self._done(TransitionOutcome.BLOCKED_BY_EXIT_CONDITION, signal)

        self._states[signal.task_id] = TaskRuntimeState.ON
        self._maybe_dispatch(signal.task_id, ActionSlot.ON_ENTER, signal.payload)
        return self._done(TransitionOutcome.ENTERED, signal)

    def _handle_exit(
        self, signal: TaskSignal, topology: TaskTopology
    ) -> TransitionOutcome:
        if self.runtime_state(signal.task_id) is not TaskRuntimeState.ON:
            return self._done(TransitionOutcome.ALREADY_IN_STATE, signal)

        if self._other_exit_side_holds(signal, topology):
            # OR 的退出条件是「全部都不成立」。少了这一步就成了「任一条断开就整个
            # 退出」, 另一条还撑着也没用 —— 那不是 OR。
            return self._done(TransitionOutcome.STILL_HELD, signal)

        self._states[signal.task_id] = TaskRuntimeState.OFF
        self._maybe_dispatch(signal.task_id, ActionSlot.ON_EXIT, signal.payload)

        # §5.2 基线重置: 要求进入条件先变假、再变真, 才算新一次进入。
        # 不引入第三个状态、不引入冷却时长。
        for rule_id in topology.enter_side_rule_ids:
            self._reset_edge_baseline(rule_id)
        return self._done(TransitionOutcome.EXITED, signal)

    def _other_exit_side_holds(
        self, signal: TaskSignal, topology: TaskTopology
    ) -> bool:
        """除信号自己那条以外, 出口侧还有没有条件此刻为真。

        进入和退出用的是同一个查询, 只是结论相反: 进入时"有真"意味着出口已经
        开着、别进 (§5.1, 让错误从"开了永远不关"变成"从来不开"); 退出时"有真"
        意味着还有条件撑着、别出。

        排除信号自己那条 rule: 对称模式下进出是同一条 session rule, 不排除的话
        它会拿自己的条件挡住自己, 永远进不去也出不来。

        只有明确为 ``True`` 才拦。``None`` 是未就绪或脉冲型无稳态 —— 拿"不知道"
        当"成立"会把正常的进出全挡掉。
        """
        for rule_id in topology.exit_side_rule_ids - {signal.rule_id}:
            if self._is_condition_satisfied(rule_id) is True:
                return True
        return False

    def _maybe_dispatch(
        self, task_id: str, slot: ActionSlot, payload: object | None
    ) -> None:
        if self._dispatching:
            self._dispatch_action(task_id, slot, payload)

    def _done(
        self, outcome: TransitionOutcome, signal: TaskSignal
    ) -> TransitionOutcome:
        self._track(outcome, signal)
        return outcome

    # ── 重新配置 (§19.5) ──────────────────────────────────────────

    def reconfigure(self, task_id: str, directions: dict[str, RuleDirection]) -> None:
        """rule 增删改 / 单独启停 / task 重新 enable 统一走这条。

        运行态反映感知到的现实, 而配置变化不改变现实 —— 只在出口侧再没有条件
        撑着时才退回 ``off``, 否则原样保留。无条件清 ``off`` 会同时造成两个后果:
        那次退出的 on_exit 漏发, 且 runner 侧的边沿仍认为在态内、不会再发 enter
        信号, 两边就此分叉且不会自愈。
        """
        self._reconfiguring.add(task_id)
        try:
            queue = self._queues.get(task_id)
            if queue:
                # 期间到达的旧信号对应改动前的配置, 排队只会把过期信号灌进新配置。
                for stale in queue:
                    self._track(TransitionOutcome.SIGNAL_DROPPED, stale)
                queue.clear()

            was_on = self.runtime_state(task_id) is TaskRuntimeState.ON
            new_topology = TaskTopology(task_id, dict(directions))
            self._topologies[task_id] = new_topology
            self._queues.setdefault(task_id, deque(maxlen=SIGNAL_QUEUE_DEPTH))
            self._wakeups.setdefault(task_id, asyncio.Event())

            if was_on and not self._exit_side_still_holds(new_topology):
                self._dispatch_action(task_id, ActionSlot.ON_EXIT, None)
                self._states[task_id] = TaskRuntimeState.OFF
        finally:
            self._reconfiguring.discard(task_id)

    def suspend(self, task_id: str) -> None:
        """task 停用 —— 停止观察, 不是观察到退出。

        清运行态让 enable 回来时从 ``off`` 重建 (§7), 但**不派发 on_exit**: 现实里
        条件可能还成立, 停用只是我们不再看了。这与 ``reconfigure`` 是两种语义, 不能
        共用 —— 后者面对的是配置变了而现实没变。
        """
        if task_id not in self._topologies:
            return
        self._states[task_id] = TaskRuntimeState.OFF
        queue = self._queues.get(task_id)
        if queue:
            for stale in queue:
                self._track(TransitionOutcome.SIGNAL_DROPPED, stale)
            queue.clear()

    def _exit_side_still_holds(self, topology: TaskTopology) -> bool:
        """出口侧还有条件成立 → 该保持 ``on``。

        ``_is_condition_satisfied`` 是三值: ``True`` 成立, ``False`` 观测到不
        成立, ``None`` 还没喂过数据(新加进来的 rule 就是这种)。只认 ``True``:
        全是 ``None`` 时无从确认还撑着, 宁可多发一次退出动作也不要静默卡在 on;
        事件型的出口侧集合为空, 同样落到退出, 与改动前一致。
        """
        return any(
            self._is_condition_satisfied(rule_id) is True
            for rule_id in topology.exit_side_rule_ids
        )

    # ── 手动触发 (debug, §5) ──────────────────────────────────────

    def manual_inject(self, task_id: str, slot: ActionSlot) -> TransitionOutcome:
        """调试入口: 直接对 task 注入进/出信号。

        不走 rule 边沿, **也不重置基线** —— 它不代表"感知到条件不再满足"。
        """
        if task_id not in self._topologies:
            return TransitionOutcome.UNKNOWN_RULE
        if slot is ActionSlot.ON_ENTER:
            self._states[task_id] = TaskRuntimeState.ON
            self._dispatch_action(task_id, ActionSlot.ON_ENTER, None)
            return TransitionOutcome.ENTERED
        if slot is ActionSlot.ON_EXIT:
            self._states[task_id] = TaskRuntimeState.OFF
            self._dispatch_action(task_id, ActionSlot.ON_EXIT, None)
            return TransitionOutcome.EXITED
        self._dispatch_action(task_id, ActionSlot.ON_TARGET, None)
        return TransitionOutcome.MILESTONE_FIRED

    # ── 关停 ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        for task_id in list(self._consumers):
            self._stop_consumer(task_id)


def derive_directions(
    rules: Iterable[tuple[str, str]],
) -> dict[str, RuleDirection]:
    """``(rule_id, direction_str)`` → 拓扑字典。方向串不认识时跳过并记日志。"""
    out: dict[str, RuleDirection] = {}
    for rule_id, raw in rules:
        try:
            out[rule_id] = RuleDirection(raw)
        except ValueError:
            logger.warning(
                "unknown rule direction %r on rule %s, skipped", raw, rule_id
            )
    return out
