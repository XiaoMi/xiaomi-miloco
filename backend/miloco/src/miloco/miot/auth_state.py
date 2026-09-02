# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""米家授权健康度。

存在的理由：`_oauth_info` 只能表达「有没有凭据」，表达不了「凭据还能不能用」。
过去刷新失败时的做法是把 `_oauth_info` 置 None，等于用「没有凭据」去表示
「凭据可能不好使」——而 `_oauth_info is None` 同时也是感知侧相机发现的闸门，
于是一次网络抖动就会让全部摄像头掉线。把健康度拆成独立的一份状态之后，
`_oauth_info` 回归它字面的含义（凭据在不在），感知不再被授权问题牵连。

判据只认「云端明确拒绝」：超时、连接失败、5xx、响应体不合法都算瞬时故障，
退避重试、不改状态；只有 401、以及 OAuth 响应体带 `error` 字段（如 96009
invalid refresh token）这类明确的凭据拒绝才置为 `DEGRADED`。任何一次刷新
成功、或用户重新授权成功，都会立刻回到 `OK`。
"""

from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel

#: 瞬时故障的退避序列（秒）。定时任务本身每 300s 转一圈，这里控制的是
#: 「同一轮里失败后多久再试」，上限刻意小于一圈，避免两套节奏互相错开。
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 20, 60)

#: 同一状态内重复失败的日志间隔（秒）。状态**迁移**那条 ERROR 不受限频——
#: 它每次故障只出现一次，正是排障要找的那条。受限的是之后每轮重试都会产生的
#: 同义 WARNING：降级后定时任务仍每 300s 试一次，不限频就是每小时 12 条、
#: 每天近 300 条无新信息的重复行。
FAILURE_LOG_INTERVAL_SECONDS: int = 1800


class MiotAuthState(str, Enum):
    """授权健康度。

    只有两档：能用 / 需要用户重新授权。刻意不做「即将失效」这类中间档——
    当前链路无法可信地区分「凭据真失效」与「网络抖动」之外的更细粒度，
    多分一档只会让提示失去分量。
    """

    OK = "ok"
    DEGRADED = "degraded"


class MiotAuthHealth(BaseModel):
    """授权健康度快照，落 KV（``AuthConfigKeys.MIOT_AUTH_STATE_KEY``）。

    进程重启后要立刻可用，否则重启到第一次刷新之间（最长 5 分钟）界面会误报
    「一切正常」。
    """

    state: MiotAuthState = MiotAuthState.OK
    #: 进入 DEGRADED 的时刻；OK 时为 None
    since_ts: int | None = None
    #: 最近一次失败的机器可读错误码（MIoTErrorCode.value）
    error_code: int | None = None
    #: 最近一次失败的可读信息，只用于日志与 doctor，不直接给住户看
    error_message: str | None = None
    #: 连续失败次数（含瞬时故障），用于退避与排障；刷新成功即归零
    consecutive_failures: int = 0
    #: 最近一次刷新成功的时刻
    last_success_ts: int | None = None
    #: 最近一次尝试刷新的时刻（无论成败），用于判断定时任务是否还活着
    last_attempt_ts: int | None = None
    #: 最近一次为「同状态内重复失败」打过日志的时刻，供限频判断。落库是有意的：
    #: 否则每次重启都会立刻再打一条，反而在崩溃自愈频繁时刷得更凶。
    last_failure_log_ts: int | None = None

    @property
    def is_degraded(self) -> bool:
        return self.state is MiotAuthState.DEGRADED

    def mark_success(self) -> "MiotAuthHealth":
        """刷新成功：无条件回到 OK。"""
        now = int(time.time())
        return MiotAuthHealth(
            state=MiotAuthState.OK,
            since_ts=None,
            error_code=None,
            error_message=None,
            consecutive_failures=0,
            last_success_ts=now,
            last_attempt_ts=now,
        )

    def mark_failure(
        self, *, permanent: bool, code: int | None, message: str
    ) -> tuple["MiotAuthHealth", bool]:
        """刷新失败。

        ``permanent=False``（瞬时故障）只累计失败次数，**不改变 state**——这正是
        「网络抖一下不该点亮告警」这条要求的落点。

        Returns:
            (新状态, 本次是否应当打日志)。状态迁移必打；同状态内的重复失败按
            ``FAILURE_LOG_INTERVAL_SECONDS`` 限频，避免每轮重试刷一条同义行。
        """
        now = int(time.time())
        degraded = permanent or self.is_degraded
        transitioned = degraded is not self.is_degraded
        due = (
            self.last_failure_log_ts is None
            or now - self.last_failure_log_ts >= FAILURE_LOG_INTERVAL_SECONDS
        )
        should_log = transitioned or due
        return (
            MiotAuthHealth(
                state=MiotAuthState.DEGRADED if degraded else MiotAuthState.OK,
                # 已经是 DEGRADED 时保留最初进入的时刻，便于展示「自 X 时起」
                since_ts=(self.since_ts or now) if degraded else None,
                error_code=code,
                error_message=message,
                consecutive_failures=self.consecutive_failures + 1,
                last_success_ts=self.last_success_ts,
                last_attempt_ts=now,
                last_failure_log_ts=now if should_log else self.last_failure_log_ts,
            ),
            should_log,
        )


def is_permanent_auth_error(code: int | None) -> bool:
    """错误码是否表示「凭据已被云端拒绝，重试无用」。

    只认 SDK 明确给出语义的两个码；其余（超时、连接失败、5xx、响应体不合法、
    JSON 解析失败）一律按可重试处理。fail-open 的方向是刻意的：宁可晚一点
    告警，也不要因为一次网络故障就告诉住户「授权失效了」。
    """
    # 延迟导入：miloco 侧不应在模块加载期依赖 SDK 的枚举
    from miot.error import MIoTErrorCode

    if code is None:
        return False
    return code in (
        MIoTErrorCode.CODE_OAUTH_UNAUTHORIZED.value,
        MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value,
    )
