"""omni 全局熔断器。

状态机与阈值见 spec §3、§10。全进程单例;所有 omni HTTP 出口共用。
线程模型:asyncio 单线程 + Lock 保护;不跨线程使用(inference 线程有自己的 loop,
但 omni 调用回到主 loop 通过 run_coroutine_threadsafe,所以本模块只从主 loop 调)。
"""

from __future__ import annotations

import asyncio
import logging
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

_emit_logger = logging.getLogger(__name__)


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


# 「立即重试」冷却期:两次 probe 之间至少间隔这么久,防 UI 反复点 / 脚本 curl 打爆
# provider。放在这里而非 router.py 是为了让 snapshot 直接把它推给前端(单一来源),
# retry 端点与前端按钮冷却都从 snapshot.retry_cooldown_sec 读,不再手动两端同步。
RETRY_COOLDOWN_SEC = 5.0


@dataclass(frozen=True)
class HealthSnapshot:
    state: str  # ok | warn | error(前端 Severity)
    code: str | None
    message: str
    since_ms: int  # 当前非 CLOSED 状态起点(CLOSED 时=0)
    consecutive_failures: int
    next_probe_at_ms: int | None
    # 到下次 tick 探测的剩余秒数(monotonic 差算,不依赖两端时钟一致)。前端直接倒计时
    # 该值,避免 next_probe_at_ms(服务端 unix ms)与客户端 Date.now() 时钟偏差导致
    # 倒计时不准(家用 NAS/容器场景常见)。CLOSED / OPEN_CONFIG / HALF_OPEN 时为 None。
    next_probe_in_seconds: float | None
    last_probe_at_ms: int | None
    last_probe_result: str | None  # "ok" | "fail" | None
    # 前端「立即重试」按钮的本地冷却时长(秒),与后端 retry 端点冷却期同源,前端读此值
    # 不再自己 hardcode,避免两处手动同步。
    retry_cooldown_sec: float


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
        self._probe_in_flight: bool = False
        self._on_state_change: list[Callable[[HealthSnapshot], None]] = []

    def register_listener(self, cb: Callable[[HealthSnapshot], None]) -> None:
        """注册状态变化回调。cb(HealthSnapshot) 在锁外调用。"""
        self._on_state_change.append(cb)

    # ---- 主接口 --------------------------------------------------------------

    async def before_call(self) -> None:
        """只有 CLOSED 放行;其他态(OPEN_* / HALF_OPEN)全部短路。

        HALF_OPEN 也短路是为了让 tick 自动探测独占探测请求:probe.probe_omni 直接调
        httpx、绕开本方法,期间感知 tick 里 omni 调用继续被短路,避免"探测中却又真发
        带视频 base64"的窗口漏发。转 CLOSED 后感知才恢复。
        """
        async with self._lock:
            if self._state != CircuitState.CLOSED:
                code = self._current_code or "unreachable"
                raise CircuitOpenError(f"skipped:cooling:{code}", self._current_message)

    async def record_success(self) -> None:
        """只允许 HALF_OPEN → CLOSED;OPEN_* 忽略,CLOSED 稳态不 emit。

        多相机 gather 并发下(pipeline._run_device 每 device 一个 Task,run_omni_fused
        并行),cam1 的 record_failure 已把断路打开,cam2 之前拿到 before_call 通过
        的 in-flight 请求随后 200 回来 —— 若这里无差别转 CLOSED,cam1 打开的
        OPEN_RECOVERABLE / OPEN_CONFIG 就被抹掉。改用 HALF_OPEN 门控:唯一让熔断
        关闭的路径是 tick / retry 主动探测转 HALF_OPEN 后的 record_probe_result 或
        本方法(此时新调用是刻意放的探测请求,200 视为真恢复)。运行时并发 200 保持
        no-op,由 tick 的独立 probe 通道决定何时闭合。
        """
        changed = False
        async with self._lock:
            self._append_sample(True)
            self._consecutive_failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._transition_to_closed_locked()
                changed = True
        if changed:
            self._emit()

    async def record_failure(self, err: ClassifiedError) -> None:
        """运行时错误上报(omni_client / omni fused 出口调)。CONFIG 不再一击进
        OPEN_CONFIG —— 运行时 400 通常是 corrupted image/video 之类瞬时错(见
        error_classifier 里的注释),一帧坏画面就永久停感知比 PR 前"log 后继续"倒退;
        401/403/404 也可能是 provider 侧临时抖动。改成 CONFIG 也走 _should_open_locked
        的连续/窗口阈值,真正稳定复现的配置问题一样会打开熔断,只是需要多几次证据。

        探测语境(record_probe_result)保持"探到就信"—— 探测是主动、独占的一次调用,
        探到 401 就是 key 错,没有必要再等窗口。
        """
        emit = False
        async with self._lock:
            self._append_sample(False)
            self._consecutive_failures += 1

            if err.category == ErrorCategory.CONFIG:
                if (
                    self._should_open_locked()
                    and self._state != CircuitState.OPEN_CONFIG
                ):
                    self._transition_to_open_config_locked(err)
                    emit = True
                elif self._state == CircuitState.OPEN_CONFIG:
                    # 已在 OPEN_CONFIG,只刷最新错误码/文案让前端横条显示最新原因。
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
            self._probe_in_flight = False
        self._emit()

    def probe_due(self) -> bool:
        """外部 tick 查询:是否到 HALF_OPEN 时刻(不改状态)。"""
        if self._state != CircuitState.OPEN_RECOVERABLE:
            return False
        return (
            self._next_probe_at_monotonic is not None
            and time.monotonic() >= self._next_probe_at_monotonic
        )

    def try_arm_probe(self) -> bool:
        """tick 驱动占位:三条件齐(OPEN_RECOVERABLE + probe_due + 未 in-flight)时置
        in-flight 位并返回 True。调用方拿到 True 后 spawn probe task,task 里必须走
        mark_half_open → probe_omni → record_probe_result(record_probe_result 会清位)。

        asyncio 单线程 + 本方法无 await,判断和置位不会被切换,并发调用天然只有一个
        能拿到 True。
        """
        if self._state != CircuitState.OPEN_RECOVERABLE:
            return False
        if self._probe_in_flight:
            return False
        if not self.probe_due():
            return False
        self._probe_in_flight = True
        return True

    def clear_probe_in_flight(self) -> None:
        """强制清 in-flight 位。record_probe_result 之外的兜底:probe task 被 cancel
        (asyncio.CancelledError 不进 except Exception)时保证下次 tick 还能再 arm。
        不改状态,只清位。"""
        self._probe_in_flight = False

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
        next_in_s: float | None = None
        if (
            self._next_probe_at_monotonic is not None
            and self._state == CircuitState.OPEN_RECOVERABLE
        ):
            delta_s = max(0.0, self._next_probe_at_monotonic - mono_now)
            next_ms = now_ms + int(delta_s * 1000)
            next_in_s = round(delta_s, 1)
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
            next_probe_in_seconds=next_in_s,
            last_probe_at_ms=last_ms,
            last_probe_result=self._last_probe_result,
            retry_cooldown_sec=RETRY_COOLDOWN_SEC,
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
                # 之前 pass 吞掉了所有异常 —— 包括跨 loop put_nowait 踩 Queue 状态
                # 抛出的 RuntimeError,状态永远丢失。改成 warning + exc_info,监控/日志
                # 能看到"招牌横条不弹"的根因,即使问题在下游 listener 里。
                _emit_logger.warning("omni CB listener raised", exc_info=True)


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
