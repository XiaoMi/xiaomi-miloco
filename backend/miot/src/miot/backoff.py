# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""云端接口的失败退避。

放在网络层（cloud.py 的出口）而不是各个业务模块，是因为上层普遍是
「缓存为空就再拉一次」的写法：一旦云端持续失败，那个判据会让每次调用
都打一次云端。业务模块既看不见失败次数、也没法自己收敛，所以退避只能在
它们绕不过的地方做——进程内唯一的发包口。

语义：同一接口连续失败后指数退避（1s 起、翻倍、封顶 60s），窗口内的调用
直接抛 ``CODE_HTTP_BACKOFF``、不发包；任一次成功立即清零。云端若在响应里
给了 ``wait_second``，以它为准（有上限）。
"""

from __future__ import annotations

import time

from .error import MIoTErrorCode, MIoTHttpError

#: 首次失败后的等待（秒），之后每失败一次翻倍。
BASE_SEC: float = 1.0
#: 退避上限（秒）。再长没有意义：上层本来就有周期任务会再来。
CAP_SEC: float = 300.0
#: 云端明确给出 wait_second 时最多等这么久，防一个异常值把接口冻死。
SERVER_WAIT_CAP_SEC: float = 300.0

#: 用户主动刷新（CLI / 接口的 refresh）可以打破退避，但强刷本身也要有兜底：
#: 同一接口每窗口最多强刷这么多次，超出的按普通退避处理——否则一个脚本或
#: 卡住的前端循环点刷新，就能把刚修掉的高频请求重新造出来。
FORCE_MAX_PER_WINDOW: int = 6
FORCE_WINDOW_SEC: float = 300.0


class EndpointBackoff:
    """单个接口的失败退避（指数 + 上限），成功后清零。"""

    def __init__(self, base_sec: float = BASE_SEC, cap_sec: float = CAP_SEC) -> None:
        self._base = base_sec
        self._cap = cap_sec
        self._failures = 0
        self._retry_at = 0.0
        self._force_times: list[float] = []

    @property
    def failures(self) -> int:
        return self._failures

    def remaining(self) -> float:
        """距可以再次请求还有多久（秒）；0 表示可以发。"""
        return max(0.0, self._retry_at - time.monotonic())

    def check(self) -> None:
        """在退避窗口内直接拒绝，不发包。"""
        delay = self.remaining()
        if delay > 0:
            raise MIoTHttpError(
                f"cloud request backed off, retry in {delay:.1f}s",
                MIoTErrorCode.CODE_HTTP_BACKOFF,
            )

    def begin(self) -> int:
        """发请求前取一个代次：成功回来时用它判断期间有没有并发失败。"""
        return self._failures

    def on_success(self, token: int | None = None) -> None:
        """请求成功：清零。

        ``token`` 是本请求发起时的失败代次。同接口可能有多条在飞请求，若期间
        已有别的请求失败（比如 429）建立了窗口，这次成功不该把它抹掉——那会让
        并发下的限流保护形同虚设。
        """
        if token is not None and self._failures != token:
            return
        self._failures = 0
        self._retry_at = 0.0

    def force(self) -> bool:
        """用户主动刷新：清掉退避窗口，让这一次请求真的发出去。

        每 ``FORCE_WINDOW_SEC`` 内最多生效 ``FORCE_MAX_PER_WINDOW`` 次；超出的
        返回 False 且**不清窗口**——强刷通道本身也要有兜底，否则「点刷新」就
        成了绕过保护的万能钥匙。本来就没有窗口时不占额度。
        """
        if self.remaining() <= 0:
            return True
        now = time.monotonic()
        self._force_times = [
            ts for ts in self._force_times if now - ts < FORCE_WINDOW_SEC
        ]
        if len(self._force_times) >= FORCE_MAX_PER_WINDOW:
            return False
        self._force_times.append(now)
        self._failures = 0
        self._retry_at = 0.0
        return True

    def on_failure(self, retry_after: float | None = None) -> float:
        """记一次失败并推进窗口，返回这次要等多久。"""
        self._failures += 1
        # 指数钳到 2**32：封顶后每 300s 才失败一次，但计数会一直涨，
        # 不钳的话几天后会抛 OverflowError 而不是正常退避。
        wait = min(self._base * (2 ** min(self._failures - 1, 32)), self._cap)
        if retry_after is not None and retry_after > 0:
            wait = min(max(wait, retry_after), SERVER_WAIT_CAP_SEC)
        self._retry_at = time.monotonic() + wait
        return wait
