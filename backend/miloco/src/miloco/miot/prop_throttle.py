# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Property-change throttle — 决定一条属性变化推送是否值得落进 device_prop_history。

**为什么必须有**：mips 推送是逐属性的，设备的遥测属性会以秒级刷屏。实测
(2026-07-29，72 台设备)一台空调的功率属性 `12/3` 每 1–2 秒推一次，120 秒内
522 条推送里它一台占 95 条；全量落库一天几十万行，30 天保留期直接把库撑爆。
同时设备开关机会**整包重发全部属性**，其中含大量同值条目（实测关→开一次推了
约 25 条，含重复值）。

**取舍**：属性历史要回答的是「空调几点开的」「传感器什么时候检测到人」这类
**离散状态**问题，不是能耗曲线。所以：

* 同值重发 → 一律丢弃（整包重发去重，与类型无关）。
* 非数值属性（bool / str / null）→ 变化即落库，无条件。开关、模式串、占用
  状态都在这一类，永不因节流丢失。
* 数值属性 → 分「离散」与「遥测」两类，只对遥测做「最小间隔 + 幅度」双阈值
  去抖，两者满足其一即落库。分类优先看 miot-spec（`classify` 回调），
  判据见 `MiotProxy._classify_prop`；spec 未命中（缓存冷、非标属性）时退回
  **频率启发式**：窗口内变化次数少的当离散，多的当遥测。

两个反直觉但被实测数据逼出来的设计：

1. **「可写」比「有单位」更能说明问题。** 空调设定温度 `prop.2.4` 带
   `unit=celsius`，看着像遥测，其实是用户意图；而同一台设备上真正刷屏的是只读
   的环境湿度 `prop.4.9`。所以 writeable 优先于 unit 判定。
2. **幅度要按满量程算，不能按「相对上次值」。** 摄像头「人数」1→2 是 100% 的
   相对变化却是噪声，空调功率 1140→1150 只有 0.9% 同样是噪声；换成满量程口径
   二者都被正确压掉，而开机瞬间 0→1140（满量程 38%）照常落库。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_Key = tuple[str, int, int]

# classify 回调的返回值。None = 无法判定（spec 缓存冷 / 非标属性），退回启发式。
DISCRETE = "discrete"
TELEMETRY = "telemetry"

# classify(did, siid, piid) -> (kind, span) | None
# span = 该属性 value-range 的量程跨度（max-min），用于幅度判定；未知给 None。
PropClassifier = Callable[[str, int, int], Optional[tuple[str, Optional[float]]]]


def same_value(a: Any, b: Any) -> bool:
    """类型敏感的相等判断。

    Python 里 ``True == 1``、``False == 0``，直接用 ``==`` 会把 bool 开关与
    int 枚举的互变当成同值丢掉。bool 与非 bool 一律视为不同值。
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _KeyState:
    __slots__ = ("last_value", "last_kept_ms", "seen", "has_value")

    def __init__(self) -> None:
        self.last_value: Any = None
        self.has_value: bool = False
        self.last_kept_ms: int = 0
        # 近期「观察到变化」的时间戳（含被丢弃的），用于频率判定。
        self.seen: deque[int] = deque()


class PropChangeThrottle:
    """Decide which property changes are worth persisting.

    线程/协程无关：调用点是单一的 mips 推送回调（同步、在主 loop 上），无需加锁。
    """

    def __init__(
        self,
        *,
        window_sec: int = 600,
        burst: int = 5,
        min_interval_sec: int = 900,
        rel_delta: float = 0.3,
        max_keys: int = 20000,
        classify: PropClassifier | None = None,
    ) -> None:
        self._classify = classify
        self._window_ms = max(1, window_sec) * 1000
        self._burst = max(2, burst)
        self._min_interval_ms = max(0, min_interval_sec) * 1000
        self._rel_delta = max(0.0, rel_delta)
        self._max_keys = max(1000, max_keys)
        self._state: dict[_Key, _KeyState] = {}
        self.kept = 0
        self.dropped_same_value = 0
        self.dropped_throttled = 0

    # ------------------------------------------------------------------ api

    def allow(self, did: str, siid: int, piid: int, value: Any, ts_ms: int) -> bool:
        """True if this change should be persisted."""
        key = (did, siid, piid)
        st = self._state.get(key)
        if st is None:
            st = self._state[key] = _KeyState()

        if st.has_value and same_value(value, st.last_value):
            # 整包重发 / 设备重复上报：不是一次真实变化。
            self.dropped_same_value += 1
            return False

        st.seen.append(ts_ms)
        # 淘汰放在 seen 记录**之后**:排序键取 seen[-1],刚新建的 key 若在记录前
        # 参与排序会退化到 last_kept_ms=0 而被立刻淘汰——正好淘汰掉最新的那个。
        self._evict_if_needed()
        cutoff = ts_ms - self._window_ms
        while st.seen and st.seen[0] < cutoff:
            st.seen.popleft()

        kind, span = self._kind(key)
        if self._should_keep(st, value, ts_ms, kind, span):
            st.last_value = value
            st.has_value = True
            st.last_kept_ms = ts_ms
            self.kept += 1
            return True

        # 被节流丢弃时**不**更新 last_value：下次比较仍以最后落库的值为基准，
        # 幅度阈值才是相对「历史上有记录的那个值」而非相对某个从未落库的中间值，
        # 否则每次 0.9% 的漂移累积起来永远迈不过阈值，长期漂移会被完全吞掉。
        self.dropped_throttled += 1
        return False

    def stats(self) -> dict[str, int]:
        return {
            "kept": self.kept,
            "dropped_same_value": self.dropped_same_value,
            "dropped_throttled": self.dropped_throttled,
            "keys": len(self._state),
        }

    # -------------------------------------------------------------- internals

    def _kind(self, key: _Key) -> tuple[str | None, float | None]:
        """spec 判定的属性种类与量程跨度；无 classify / 判不出时返回 (None, None)。

        classify 由调用方注入（读 miot-spec 缓存），缓存冷时会返回 None，
        此处不缓存判定结果——spec 后续被填充时下一条推送就能用上正确分类。
        """
        if self._classify is None:
            return (None, None)
        try:
            got = self._classify(*key)
        except Exception:
            logger.debug("prop classify failed key=%s", key, exc_info=True)
            return (None, None)
        return got if got else (None, None)

    def _should_keep(
        self,
        st: _KeyState,
        value: Any,
        ts_ms: int,
        kind: str | None,
        span: float | None,
    ) -> bool:
        if not st.has_value:
            return True  # 首见即基线，必须落库
        if not _is_numeric(value) or not _is_numeric(st.last_value):
            # 离散属性（开关 / 模式串 / 占用 / null）：变化即历史，无条件落库。
            return True
        if kind == DISCRETE:
            # spec 说了这是枚举 / 开关 / 用户可写的设定值，哪怕值是数字也一律
            # 落库——模式 1→2、设定温度 26→27 都是用户可感知的状态变更。
            return True
        if kind != TELEMETRY and len(st.seen) < self._burst:
            # spec 判不出且变化节奏低：按用户操作对待。spec 已明确是遥测时不走
            # 这条——冷启动的头几条同样不该放行。
            return True
        # 遥测：最小间隔与幅度阈值满足其一即落库。
        if ts_ms - st.last_kept_ms >= self._min_interval_ms:
            return True
        delta = abs(value - st.last_value)
        if span:
            # 有量程时按**满量程比例**判定。相对上次值的比例对小整数量毫无意义
            # ——摄像头「人数」1→2 是 100%，功率 1140→1150 只有 0.9%，但前者
            # 才是噪声。满量程口径下两者都被正确压掉，而 0→1140 这种开机跃变
            # （满量程的 38%）照样放行。
            return delta >= self._rel_delta * span
        base = abs(st.last_value)
        return delta >= self._rel_delta * base if base else delta > 0

    def _evict_if_needed(self) -> None:
        """Bound memory: 属性 key 数上限，超了丢最久没动过的那批。

        did × 属性数在真实家庭里是几千量级，正常远达不到上限；这里只防设备
        频繁增删或异常 did 导致的无界增长。
        """
        if len(self._state) <= self._max_keys:
            return
        victims = sorted(
            self._state.items(),
            key=lambda kv: kv[1].seen[-1] if kv[1].seen else kv[1].last_kept_ms,
        )[: len(self._state) // 10]
        for key, _ in victims:
            self._state.pop(key, None)
        logger.debug("prop throttle evicted %d cold keys", len(victims))
