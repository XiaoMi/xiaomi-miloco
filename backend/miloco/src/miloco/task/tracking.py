# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 状态机的判定跟踪（§18.3 的内存部分）。

回答两个不同的问题，所以是两份数据而不是一份：

- **「我的规则现在为什么不触发」** → per-rule 最后一条判定快照。规则完全不动时
  每个周期原因相同，看最后一个就够。
- **「偶发不触发是哪一层吃掉的」** → per-rule 按结论计数。快照答不了这个，它被
  高频覆盖。

写入路径的硬约束（§18.3）：**只做字典赋值与整数自增**。不做 IO、不序列化、不加
锁、不 await —— 它挂在判定路径上，这里的延迟就是规则的延迟。

本次只做内存部分。异常落库、``not_ready`` 持续超时告警、``rule inspect`` CLI
都还没做（§18.7 列的四项里的后两项）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 结论分类。异常类在 §18.4 里要落库 + 告警，本次只做分类本身。
_ABNORMAL = frozenset({"signal_dropped", "unknown_rule"})

# 正常但「没触发」的结论 —— 用户问「怎么没反应」时最需要看到的就是这些
_SUPPRESSED = frozenset(
    {
        "already_in_state",
        "already_off",
        "blocked_by_exit_condition",
        "not_in_session",
        "still_held",
    }
)


@dataclass
class Decision:
    """最后一条判定快照。ts 由调用方传入 —— 本模块不取时间, 便于测试。"""

    outcome: str
    rule_id: str
    ts_ms: int

    @property
    def is_abnormal(self) -> bool:
        return self.outcome in _ABNORMAL

    @property
    def is_suppressed(self) -> bool:
        return self.outcome in _SUPPRESSED


@dataclass
class TaskTrack:
    last: Decision | None = None
    counts: dict[str, int] = field(default_factory=dict)


class DecisionTracker:
    """per-task 跟踪。

    上限靠三件钉死 (§18.3): 快照每 task 只留最后一条、计数的键是固定枚举、
    task 撤销登记时整条清掉。没有历史序列, 所以内存与运行时长无关。
    """

    def __init__(self) -> None:
        self._tracks: dict[str, TaskTrack] = {}

    def record(self, task_id: str, rule_id: str, outcome: str, ts_ms: int) -> None:
        track = self._tracks.get(task_id)
        if track is None:
            track = TaskTrack()
            self._tracks[task_id] = track
        track.last = Decision(outcome=outcome, rule_id=rule_id, ts_ms=ts_ms)
        track.counts[outcome] = track.counts.get(outcome, 0) + 1

    def forget(self, task_id: str) -> None:
        """task 撤销登记时调 —— 不清就是一条随 task 数单调增长的内存泄漏。"""
        self._tracks.pop(task_id, None)

    def last_decision(self, task_id: str) -> Decision | None:
        track = self._tracks.get(task_id)
        return track.last if track is not None else None

    def counts(self, task_id: str) -> dict[str, int]:
        track = self._tracks.get(task_id)
        return dict(track.counts) if track is not None else {}

    def summary(self, task_id: str) -> dict[str, object] | None:
        """给 TaskFullView 用的摘要。没有任何判定过则为 None。"""
        last = self.last_decision(task_id)
        if last is None:
            return None
        return {
            "outcome": last.outcome,
            "rule_id": last.rule_id,
            "ts_ms": last.ts_ms,
            "suppressed": last.is_suppressed,
            "abnormal": last.is_abnormal,
        }
