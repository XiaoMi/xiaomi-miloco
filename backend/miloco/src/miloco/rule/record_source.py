# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""record 源：把「累计量达标」产成条件项的 bool。

设计见 docs/superpowers/specs/2026-07-21-task-runtime-state-and-multi-source-rules
-design.md §6.4。两条要点：

1. **timer 是本层的实现细节。** ``accumulated`` 是读取时现算的连续量，没有天然的
   更新时刻，所以本层自己排 timer 产事件 —— 与 omni 跑一次模型、iot 订阅 broker
   是同一个位置的事，不是状态机的钩子。
2. **「本周期通知过没有」不在本层存。** 它由条件项自身的边沿承载：跨日归零把条件
   翻假，第二天达标就是一次新的假→真。本层只负责把 bool 喂准。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)

RECORD_SOURCE_TYPE = "record"

# 喂值时占的 source 位。record 源没有摄像头，用固定串占位；它不是设备 did，
# 不会出现在感知的 device_rule_map 里，所以不会被「未命中喂 False」那条路推退。
RECORD_SOURCE_DID = "record"

# 本次只支持累计时长的达标判断（spec §6.4）。别的 kind / 比较符等有用例再加。
SUPPORTED_KIND = "duration"
SUPPORTED_OP = ">="

# milestone rule 在旧 ``condition.perceive_device_ids`` 列上填这个 did。达标不看
# 摄像头，但那一列是必填的旧字段：填真实 did 或留空，都会让只认这一列的旧代码把
# "累计达标"当成一句视觉 query 塞进摄像头 prompt。填一个不存在的 did，建
# device_rule_map 时无摄像头认领，这条 rule 不参与任何视觉判定。
MILESTONE_SENTINEL_DID = "__milestone_no_camera__"


@dataclass(frozen=True)
class RecordRef:
    """一条 record 条件项引用的 record。

    没有 ``value`` 字段是有意的：阈值每次去 record 上读当前值。条件项里存一份
    副本的话，用户改完 ``target_minutes`` 两份就分叉，rule 还按旧阈值触发。
    """

    rule_id: str
    task_id: str


def record_ref_of(rule) -> RecordRef | None:
    """rule 的条件项是不是 record 源，是就返回它引用的 record。

    kind / op 不认识时记日志返 None，不抛：建 rule 时已经校验过，跑到这里还不认识
    说明是库里的存量脏数据，让这条不触发就行，别把整条感知链带崩。
    """
    dnf = getattr(rule, "condition_dnf", None)
    if dnf is None or not dnf.any_of:
        return None
    for conjunction in dnf.any_of:
        for item in conjunction:
            if item.source_type != RECORD_SOURCE_TYPE:
                continue
            spec = item.spec or {}
            task_id = spec.get("task_id")
            kind, op = spec.get("kind"), spec.get("op")
            if kind != SUPPORTED_KIND or op != SUPPORTED_OP:
                logger.warning(
                    "rule %s 的 record 条件项不支持 (kind=%s op=%s), 不触发",
                    rule.id, kind, op,
                )
                return None
            if not task_id:
                logger.warning("rule %s 的 record 条件项没写 task_id, 不触发", rule.id)
                return None
            return RecordRef(rule_id=rule.id, task_id=task_id)
    return None


def _reached_metadata(target: int, accumulated: int) -> dict:
    """达标那笔账 —— 达标文案要用真实数字, 不能只说"达标了"。"""
    return {
        "target_minutes": target,
        "accumulated_at_fire": accumulated,
        "actual_target_at": ms_to_iso_local(now_ms()),
    }


class RecordSource:
    """record 条件项的求值方。

    ``feed`` 把算出的 bool 交给条件层（``RuleRunner.update_state``）。本层不认识
    确认层与 task 层 —— 达标之后走哪个槽由方向映射决定，不是这里的事。
    """

    def __init__(
        self,
        record_service,
        feed: Callable[[str, bool, dict | None], Awaitable[None]],
        refs_of_task: Callable[[str], Iterable[RecordRef]],
    ) -> None:
        self._record_service = record_service
        self._feed = feed
        self._refs_of_task = refs_of_task
        self._timers: dict[str, asyncio.Task] = {}
        # 后台 arm 协程。强引用住, 否则 GC 可能在 await 中途回收掉。
        self._arming: set[asyncio.Task] = set()
        # 每个 task 现在是第几轮武装。出 session / 跨日归零时加一, 让上一轮起的
        # 后台协程全部作废 —— 撤 timer 撤不掉它们: arm 把读账放后台, 中间落出的
        # timer 活过 disarm, 到点在 off 态喂真把条件锁死, 当天后面真的达标就产不
        # 出边沿了。
        self._arm_round: dict[str, int] = {}

    # ── 生命周期入口 ────────────────────────────────────────────

    def arm(self, task_id: str) -> None:
        """task 进 session：按当前累计排 timer。

        DB 读放后台，因为调用方在感知热路径上。就地读会把每个判定周期都拖进 IO。
        """
        this_round = self._arm_round.setdefault(task_id, 0)
        for ref in self._refs_of_task(task_id):
            handle = asyncio.create_task(self._arm_one(ref, this_round))
            self._arming.add(handle)
            handle.add_done_callback(self._arming.discard)

    async def settle(self, task_id: str) -> None:
        """task 即将出 session：兜底算一次。

        **必须在状态机翻 off 之前 await 完。** 达标动作要求 task 还在 session 里
        （§5.3 的 ``NOT_IN_SESSION``），翻完再喂就被拦掉了。

        存在的理由是时间跳变：timer 按挂起前的时钟排，睡过头醒来时可能早该达标。

        不撤 timer —— 撤在 ``disarm``。这次退出可能被别的条件挡住（多 rule 的
        ``STILL_HELD``），session 还在，撤了这一天的达标就丢了。
        """
        for ref in self._refs_of_task(task_id):
            state = self._read(ref.task_id)
            if state is None:
                continue
            target, accumulated = state
            if target is not None and accumulated >= target:
                await self._feed_safely(
                    ref.rule_id, True, _reached_metadata(target, accumulated)
                )

    def cancel_rule(self, rule_id: str) -> None:
        """rule 被删 / 被改：撤掉它的 timer。

        按 rule_id 而不是 task_id，因为调用点上这条 rule 可能已经不在 runner 的
        rule 表里了，按 task 遍历找不到它。
        """
        self._cancel(rule_id)

    def forget_task(self, task_id: str) -> None:
        """task 被删：清掉按 task_id 存的那份轮次计数。

        留着不会算错 (轮次只比相等), 但它是一条随删除次数单调增长的残留。
        """
        self._arm_round.pop(task_id, None)

    def _abandon_round(self, task_id: str) -> None:
        """让这个 task 上一轮武装起的后台协程全部作废。"""
        self._arm_round[task_id] = self._arm_round.get(task_id, 0) + 1

    def _round_is_over(self, ref: RecordRef, this_round: int) -> bool:
        return self._arm_round.get(ref.task_id, 0) != this_round

    def disarm(self, task_id: str) -> None:
        """task 出 session / 被停用：撤掉未到点的 timer。

        timer 是 asyncio task，不受 rule 停用的入口早返影响 —— 不撤的话到点仍会
        喂一次 True。
        """
        self._abandon_round(task_id)
        for ref in self._refs_of_task(task_id):
            self._cancel(ref.rule_id)

    async def reset(self, task_id: str) -> None:
        """跨日归零：把条件翻假。

        这一步是第二天能重新通知的全部机制。少了它条件停在真，第二天达标不产生
        新边沿，通知永久消失。
        """
        self._abandon_round(task_id)
        for ref in self._refs_of_task(task_id):
            self._cancel(ref.rule_id)
            await self._feed_safely(ref.rule_id, False)

    async def roll_over(
        self, task_id: str, pre_rollover_state: tuple[int | None, int] | None
    ) -> None:
        """跨日：兑现旧一天 → 归零 → 按新一天重排。

        ``pre_rollover_state`` 是 rollover 执行前的旧一天 ``(target, accumulated)``。
        rollover 已经清了累计，读当前值看不出旧一天达标过，只能靠这份快照。

        兑现不需要「今天通知过没有」的标记：条件此刻为真就说明已经通知过了，喂真
        不产生边沿。这正是把防重复交给边沿之后省掉的那个标记。
        """
        if pre_rollover_state is not None:
            target, accumulated = pre_rollover_state
            if target is not None and accumulated >= target:
                metadata = _reached_metadata(target, accumulated)
                for ref in self._refs_of_task(task_id):
                    await self._feed_safely(ref.rule_id, True, metadata)
        await self.reset(task_id)
        self.arm(task_id)

    # ── 内部 ────────────────────────────────────────────────────

    async def _arm_one(self, ref: RecordRef, this_round: int) -> None:
        if self._round_is_over(ref, this_round):
            # 起这个协程的那次 arm 已经被 disarm / 归零作废了。
            return
        self._cancel(ref.rule_id)
        state = self._read(ref.task_id)
        if state is None:
            return
        target, accumulated = state
        if target is None:
            # 没设目标 = 没有达标这件事。条件保持原值, 不硬喂假 —— 那会把已经发出
            # 去的达标在下次配上目标时重发一遍。
            return
        if accumulated >= target:
            await self._feed_safely(
                ref.rule_id, True, _reached_metadata(target, accumulated)
            )
            return
        remaining_seconds = (target - accumulated) * 60
        handle = asyncio.create_task(
            self._feed_after(ref, remaining_seconds, target, this_round)
        )
        self._timers[ref.rule_id] = handle
        logger.info(
            "RECORD_TARGET_SCHEDULED: rule=%s task=%s remaining_s=%d "
            "(accumulated_min=%s target_min=%s)",
            ref.rule_id, ref.task_id, remaining_seconds, accumulated, target,
        )

    async def _feed_after(
        self, ref: RecordRef, delay: float, target_at_arm: int, this_round: int
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timers.pop(ref.rule_id, None)
        if self._round_is_over(ref, this_round):
            # 摘出 _timers 之后 disarm 就撤不到它了, 轮次是这一步唯一的闸。
            return
        # 到点重算, 不信 arm 时那笔账: 中间可能停过表 (session 断过) 或改过目标。
        state = self._read(ref.task_id)
        if state is None:
            return
        target, accumulated = state
        if target is None:
            return
        if accumulated < target:
            # 睡的这段时间里累计没跟上 (session 中断过) 或目标被调高了。重排一次,
            # 别把这一天的达标丢掉。
            logger.info(
                "RECORD_TARGET_REARM: rule=%s accumulated_min=%s target_min=%s "
                "(arm 时目标 %s)",
                ref.rule_id, accumulated, target, target_at_arm,
            )
            await self._arm_one(ref, this_round)
            return
        await self._feed_safely(
            ref.rule_id, True, _reached_metadata(target, accumulated)
        )

    def _read(self, task_id: str) -> tuple[int | None, int] | None:
        """读 ``(target_minutes, accumulated_minutes_today)``。

        阈值取 record 上的当前值，不取条件项里的副本 —— 见 ``RecordRef``。
        """
        try:
            return self._record_service.read_duration_target_state(task_id)
        except Exception:
            logger.exception("record 源读 task %s 的累计失败, 本次不判达标", task_id)
            return None

    async def _feed_safely(
        self, rule_id: str, value: bool, metadata: dict | None = None
    ) -> None:
        try:
            await self._feed(rule_id, value, metadata)
        except Exception:
            logger.exception("record 源喂 rule %s = %s 失败", rule_id, value)

    def _cancel(self, rule_id: str) -> None:
        handle = self._timers.pop(rule_id, None)
        if handle is not None and not handle.done():
            handle.cancel()
