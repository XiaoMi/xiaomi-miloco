"""omni 全局熔断器。

状态机与阈值见 spec §3、§10。全进程单例;所有 omni HTTP 出口共用。
线程模型:asyncio 单线程 + Lock 保护;不跨线程使用(inference 线程有自己的 loop,
但 omni 调用回到主 loop 通过 run_coroutine_threadsafe,所以本模块只从主 loop 调)。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN_RECOVERABLE = "open_recoverable"
    OPEN_CONFIG = "open_config"
    HALF_OPEN = "half_open"


_STATE_TO_UI: dict[CircuitState, str] = {
    CircuitState.CLOSED: "ok",
    CircuitState.OPEN_RECOVERABLE: "warn",
    CircuitState.HALF_OPEN: "warn",
    CircuitState.OPEN_CONFIG: "error",
}


class CircuitOpenError(Exception):
    """熔断期间调用被短路时抛出。上层(omni_client)捕获后包成 OmniError。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HealthSnapshot:
    state: str  # ok | warn | error(前端 Severity)
    code: str | None
    message: str
    since_ms: int  # 当前非 CLOSED 状态起点(CLOSED 时=0)
    consecutive_failures: int
    next_probe_at_ms: int | None
    last_probe_at_ms: int | None
    last_probe_result: str | None  # "ok" | "fail" | None


class OmniCircuitBreaker:
    """全局单例。所有方法可从主 event loop 安全并发调用。"""

    def __init__(
        self,
        *,
        consecutive_threshold: int = 3,
        window_seconds: float = 60.0,
        window_min_samples: int = 5,
        window_error_rate: float = 0.5,
        backoff_start: float = 1.0,
        backoff_multiplier: float = 2.0,
        backoff_caps: dict[str, float] | None = None,
        jitter_ratio: float = 0.2,
    ):
        self._consecutive_threshold = consecutive_threshold
        self._window_seconds = window_seconds
        self._window_min_samples = window_min_samples
        self._window_error_rate = window_error_rate
        self._backoff_start = backoff_start
        self._backoff_multiplier = backoff_multiplier
        self._backoff_caps = backoff_caps or {"rate_limited": 60.0, "_default": 600.0}
        self._jitter_ratio = jitter_ratio

        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._samples: deque[tuple[float, bool]] = deque()  # (timestamp, success)
        self._next_probe_at_monotonic: float | None = None
        self._current_backoff: float = 0.0
        self._current_code: str | None = None
        self._current_message: str = ""
        self._state_since: float = time.monotonic()
        self._last_probe_at: float | None = None
        self._last_probe_result: str | None = None
        self._on_state_change: list[Callable[[HealthSnapshot], None]] = []

    def register_listener(self, cb: Callable[[HealthSnapshot], None]) -> None:
        """注册状态变化回调。cb(HealthSnapshot) 在锁外调用。"""
        self._on_state_change.append(cb)

    # ---- 主接口 --------------------------------------------------------------

    async def before_call(self) -> None:
        """CLOSED / HALF_OPEN 直接放行;OPEN_* 抛 CircuitOpenError。"""
        async with self._lock:
            if self._state in (CircuitState.OPEN_RECOVERABLE, CircuitState.OPEN_CONFIG):
                code = self._current_code or "unreachable"
                raise CircuitOpenError(f"skipped:cooling:{code}", self._current_message)

    async def record_success(self) -> None:
        async with self._lock:
            self._append_sample(True)
            self._consecutive_failures = 0
            if self._state != CircuitState.CLOSED:
                self._transition_to_closed_locked()
        self._emit()

    async def record_failure(self, err: ClassifiedError) -> None:
        emit = False
        async with self._lock:
            self._append_sample(False)
            self._consecutive_failures += 1

            if err.category == ErrorCategory.CONFIG:
                if self._state != CircuitState.OPEN_CONFIG:
                    self._transition_to_open_config_locked(err)
                    emit = True
                else:
                    self._current_code, self._current_message = err.code, err.message
                    emit = True
            else:
                if (
                    self._should_open_locked()
                    and self._state != CircuitState.OPEN_RECOVERABLE
                ):
                    self._transition_to_open_recoverable_locked(err)
                    emit = True
        if emit:
            self._emit()

    async def record_probe_result(self, ok: bool, err: ClassifiedError | None) -> None:
        async with self._lock:
            self._last_probe_at = time.monotonic()
            self._last_probe_result = "ok" if ok else "fail"
            if ok:
                self._transition_to_closed_locked()
            else:
                assert err is not None
                if err.category == ErrorCategory.CONFIG:
                    self._transition_to_open_config_locked(err)
                else:
                    self._grow_backoff_locked(err)
                    self._state = CircuitState.OPEN_RECOVERABLE
                    self._current_code, self._current_message = err.code, err.message
        self._emit()

    def probe_due(self) -> bool:
        """外部 tick 查询:是否到 HALF_OPEN 时刻(不改状态)。"""
        if self._state != CircuitState.OPEN_RECOVERABLE:
            return False
        return (
            self._next_probe_at_monotonic is not None
            and time.monotonic() >= self._next_probe_at_monotonic
        )

    async def mark_half_open(self) -> None:
        """外部驱动:进入 HALF_OPEN(发起 probe 前调)。"""
        async with self._lock:
            if self._state == CircuitState.OPEN_RECOVERABLE:
                self._state = CircuitState.HALF_OPEN
        self._emit()

    async def retry_now(self) -> None:
        """用户点「立即重试」;OPEN_RECOVERABLE / OPEN_CONFIG → HALF_OPEN。"""
        async with self._lock:
            if self._state in (CircuitState.OPEN_RECOVERABLE, CircuitState.OPEN_CONFIG):
                self._state = CircuitState.HALF_OPEN
                self._next_probe_at_monotonic = time.monotonic()
        self._emit()

    async def reset_on_config_change(self) -> None:
        async with self._lock:
            self._transition_to_closed_locked()
        self._emit()

    def snapshot(self) -> HealthSnapshot:
        now_ms = int(time.time() * 1000)
        mono_now = time.monotonic()
        next_ms: int | None = None
        if (
            self._next_probe_at_monotonic is not None
            and self._state == CircuitState.OPEN_RECOVERABLE
        ):
            next_ms = now_ms + int((self._next_probe_at_monotonic - mono_now) * 1000)
        last_ms = None
        if self._last_probe_at is not None:
            last_ms = now_ms - int((mono_now - self._last_probe_at) * 1000)
        since_ms = 0
        if self._state != CircuitState.CLOSED:
            since_ms = now_ms - int((mono_now - self._state_since) * 1000)
        return HealthSnapshot(
            state=_STATE_TO_UI[self._state],
            code=self._current_code,
            message=self._current_message,
            since_ms=since_ms,
            consecutive_failures=self._consecutive_failures,
            next_probe_at_ms=next_ms,
            last_probe_at_ms=last_ms,
            last_probe_result=self._last_probe_result,
        )

    def state_for_test(self) -> CircuitState:
        return self._state

    # ---- private (锁内调用) ---------------------------------------------------

    def _append_sample(self, success: bool) -> None:
        now = time.monotonic()
        self._samples.append((now, success))
        cutoff = now - self._window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _should_open_locked(self) -> bool:
        if self._consecutive_failures >= self._consecutive_threshold:
            return True
        if len(self._samples) >= self._window_min_samples:
            fails = sum(1 for _, ok in self._samples if not ok)
            if fails / len(self._samples) > self._window_error_rate:
                return True
        return False

    def _cap_for(self, code: str | None) -> float:
        return self._backoff_caps.get(code or "", self._backoff_caps["_default"])

    def _grow_backoff_locked(self, err: ClassifiedError) -> None:
        cap = self._cap_for(err.code)
        if self._current_backoff <= 0:
            base = self._backoff_start
        else:
            base = min(self._current_backoff * self._backoff_multiplier, cap)
        if err.retry_after_seconds is not None:
            base = max(base, min(err.retry_after_seconds, cap))
        jitter = (
            1 + random.uniform(-self._jitter_ratio, self._jitter_ratio)
            if self._jitter_ratio
            else 1.0
        )
        self._current_backoff = base
        self._next_probe_at_monotonic = time.monotonic() + base * jitter

    def _transition_to_open_recoverable_locked(self, err: ClassifiedError) -> None:
        self._state = CircuitState.OPEN_RECOVERABLE
        self._state_since = time.monotonic()
        self._current_code, self._current_message = err.code, err.message
        self._current_backoff = 0.0
        self._grow_backoff_locked(err)

    def _transition_to_open_config_locked(self, err: ClassifiedError) -> None:
        self._state = CircuitState.OPEN_CONFIG
        self._state_since = time.monotonic()
        self._current_code, self._current_message = err.code, err.message
        self._next_probe_at_monotonic = None
        self._current_backoff = 0.0

    def _transition_to_closed_locked(self) -> None:
        self._state = CircuitState.CLOSED
        self._state_since = time.monotonic()
        self._current_code, self._current_message = None, ""
        self._consecutive_failures = 0
        self._samples.clear()
        self._next_probe_at_monotonic = None
        self._current_backoff = 0.0

    def _emit(self) -> None:
        snap = self.snapshot()
        for cb in list(self._on_state_change):
            try:
                cb(snap)
            except Exception:  # noqa: BLE001
                pass


_INSTANCE: OmniCircuitBreaker | None = None


def get_omni_circuit_breaker() -> OmniCircuitBreaker:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = OmniCircuitBreaker()
    return _INSTANCE


def reset_omni_circuit_breaker_for_tests() -> None:
    """测试专用:重置单例。生产代码禁调。"""
    global _INSTANCE
    _INSTANCE = None
