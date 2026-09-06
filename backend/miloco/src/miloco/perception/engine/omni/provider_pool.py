"""omni 多 provider 自动 failback + 恢复探测池。

设计概要：
- 主 provider (primary) 来自 ``model.omni``，备选来自 ``model.omni_fallbacks``
  （label 列表，按优先级排序，运行时从 ``model.omni_profiles`` 解析为完整配置）
- 主 provider 熔断器打开时自动依次尝试 fallback
- 所有 provider 都不可用时感知引擎暂停：熔断器保持切换发生时的开启状态
  （``OPEN_RECOVERABLE`` 或 ``OPEN_CONFIG``），前者 tick 退避探测仍在跑，
  后者只能靠本模块的恢复循环自愈
- 后台恢复循环定期探测 failed provider，primary 恢复后自动切回
- 去抖保护：短时间内连续切换受 ``_min_switch_interval`` 限制
- 每次 ``get_active()`` / failover 时动态从 settings 读取 provider 列表，
  确保 web 改 fallback 无需重启即可生效
- active provider 按 label 身份追踪而非位置下标：管理员在故障期间拖拽重排
  omni_fallbacks 不会导致静默错指到另一个 provider

线程模型：
- CB listener 回调可能来自任意线程（感知循环或 inference worker）
- 回调内通过 ``call_soon_threadsafe`` 投递到主 loop 处理
- 恢复循环跑在主 loop 上
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from miloco.config.settings import OmniModelSettings

if TYPE_CHECKING:
    from miloco.perception.engine.omni.circuit_breaker import HealthSnapshot

logger = logging.getLogger(__name__)

# 两次 failover 之间的最小间隔（秒），防止配置错误的多 provider 来回抖动
_MIN_SWITCH_INTERVAL_SEC = 30.0

# 恢复探测间隔（秒）：定期探测所有 failed provider 是否恢复
_RECOVERY_PROBE_INTERVAL_SEC = 30.0


def _provider_key(omni: OmniModelSettings) -> str:
    """生成 provider 的唯一标识（用于 failed 集合追踪）。

    必须带上 api_key 指纹：同 model + 同 base_url 但换 key 的档案
    （同一网关下的多把配额 key）是两个独立 provider，不带指纹会被
    折叠成一个 —— 第二把 key 永远选不中，failover 直接判耗尽。
    只取 sha256 前 8 位，避免明文 key 进日志 / 进 snapshot().failed_keys。
    """
    fp = hashlib.sha256((omni.api_key or "").encode()).hexdigest()[:8]
    return f"{omni.model}@{omni.base_url}#{fp}"


@dataclass
class PoolSnapshot:
    """Provider 池当前状态的只读快照，供 admin API 暴露。"""

    active_label: str
    active_model: str
    active_base_url: str
    active_is_primary: bool
    active_index: int  # 0 = primary, >=1 = fallback index
    fallback_count: int
    failed_keys: list[str]
    last_switch_at_ms: int | None
    recovery_loop_running: bool
    exhausted: bool  # 是否处于「所有 provider 都不可用」的暂停态


class OmniProviderPool:
    """多 provider 故障转移 + 自动恢复管理器。

    生命周期：
    - ``init_pool(loop)`` 创建并存储模块级实例
    - ``start()`` 启动后台恢复循环（``init_perception_module`` 中调一次）
    - ``stop()`` 停止恢复循环（仅在进程 shutdown，main.py lifespan 收尾时调一次；
      不在 runner.stop() 中调——池是进程级单例，不绑定单代 runner 生命周期）
    - ``get_pool()`` 获取模块级单例（None = 未初始化）

    Provider 配置动态解析：
    - ``get_active()`` 每次从 settings 读取最新 omni / omni_fallbacks / omni_profiles，
      确保 web 改 fallback 无需重启即可生效。
    - ``_resolve_providers_unlocked()`` 按 omni_fallbacks labels 从 omni_profiles 查找
      完整 OmniModelSettings；label 不存在于 profiles 中时自动跳过并告警。
    """

    def __init__(
        self,
        main_loop: asyncio.AbstractEventLoop,
        *,
        min_switch_interval_sec: float = _MIN_SWITCH_INTERVAL_SEC,
        recovery_probe_interval_sec: float = _RECOVERY_PROBE_INTERVAL_SEC,
    ) -> None:
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        self._loop = main_loop
        self._lock = threading.RLock()

        # 可注入的时间常量（测试可设为 0 避免真实等待）
        self._min_switch_interval_sec = min_switch_interval_sec
        self._recovery_probe_interval_sec = recovery_probe_interval_sec

        # 运行时状态：按 label 身份追踪 active provider（None = primary）
        # 使用 label 而非位置下标，因为 omni_fallbacks 列表可能在运行时被修改
        self._active_label: str | None = None
        self._failed_keys: set[str] = set()
        self._last_switch_monotonic: float = 0.0
        self._last_switch_wall_ms: int | None = None  # time.time() epoch ms，对外暴露
        # 耗尽标志：所有 provider 都不可用。与 _active_label=None（正常在主）区分开，
        # 因为 omni_client 的配置变更钩子会把「active 翻回主」误判成用户改配置而清熔断。
        self._exhausted: bool = False

        # 恢复循环控制
        self._recovery_task: asyncio.Task | None = None
        self._failover_event = asyncio.Event()

        # 注册熔断器 listener：CB 状态变为非 ok 时触发热 failover
        cb = get_omni_circuit_breaker()
        cb.register_listener(self._on_cb_change)

        primary, fallbacks = self._resolve_providers_unlocked()
        logger.info(
            "[provider-pool] 初始化完成: primary=%s, fallbacks=%d",
            _provider_key(primary),
            len(fallbacks),
        )

    # ── 公开接口 ──────────────────────────────────────────────────────────

    def get_active(self) -> OmniModelSettings:
        """返回当前活跃的 provider 配置（omni_client / probe 调此方法获取 config）。

        每次调用时从 settings 动态读取 provider 列表，确保 web 改 fallback
        无需重启即可在下一推理周期生效。

        按 label 身份追踪 active provider，而非位置下标——这样即使管理员在
        故障期间拖拽重排 omni_fallbacks，也不会静默错指到另一个 provider。
        """
        with self._lock:
            return self._get_active_unlocked()

    def snapshot(self) -> PoolSnapshot:
        """返回池当前状态快照（供 admin API）。"""
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        with self._lock:
            active = self._get_active_unlocked()
            _, fallbacks = self._resolve_providers_unlocked()
            # 从 label 反查 active_index（兼容 admin API 快照字段）
            active_index: int = 0
            if self._active_label is not None:
                for i, fb in enumerate(fallbacks):
                    if fb.label == self._active_label:
                        active_index = i + 1
                        break
            return PoolSnapshot(
                active_label=active.label,
                active_model=active.model,
                active_base_url=active.base_url,
                active_is_primary=(self._active_label is None),
                active_index=active_index,
                fallback_count=len(fallbacks),
                failed_keys=sorted(self._failed_keys),
                last_switch_at_ms=self._last_switch_wall_ms,
                recovery_loop_running=(
                    self._recovery_task is not None
                    and not self._recovery_task.done()
                ),
                # 熔断器已回正常 → 感知在跑，耗尽态是陈旧的，不要误导前端。
                exhausted=self._exhausted
                and get_omni_circuit_breaker().snapshot().state != "ok",
            )

    async def start(self) -> None:
        """启动后台恢复探测循环。幂等：已运行时 no-op。"""
        if self._recovery_task is not None and not self._recovery_task.done():
            logger.debug("[provider-pool] 恢复循环已在运行，跳过")
            return
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        logger.info("[provider-pool] 恢复循环已启动")

    async def stop(self) -> None:
        """停止后台恢复探测循环。幂等。"""
        task = self._recovery_task
        if task is None or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # cancel() 后的预期结果
        except Exception:
            logger.warning("[provider-pool] 恢复循环关闭时遇到未预期异常", exc_info=True)
        finally:
            if self._recovery_task is task:
                self._recovery_task = None
            logger.info("[provider-pool] 恢复循环已停止")

    # ── CB listener ───────────────────────────────────────────────────────

    def _on_cb_change(self, snap: HealthSnapshot) -> None:
        """熔断器状态变化回调（在锁外、任意线程调用）。

        非 ok 状态时通过 call_soon_threadsafe 通知主 loop 尝试 failover。
        """
        if snap.state == "ok":
            return
        try:
            self._loop.call_soon_threadsafe(self._failover_event.set)
        except RuntimeError:
            # loop 已关闭（shutdown 中），忽略
            pass

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _resolve_providers_unlocked(self) -> tuple[OmniModelSettings, list[OmniModelSettings]]:
        """从 settings 动态读取 primary + fallback 列表（调用方已持锁）。

        omni_fallbacks 存储 label 列表，运行时从 omni_profiles 解析为完整配置。
        label 不在 profiles 中的会自动跳过并 warning。
        """
        from miloco.config import get_settings

        m = get_settings().model
        primary = m.omni
        profiles_dict: dict[str, OmniModelSettings] = {
            p.label: p for p in m.omni_profiles
        }
        fallbacks: list[OmniModelSettings] = []
        for label in m.omni_fallbacks:
            cfg = profiles_dict.get(label)
            if cfg is not None:
                fallbacks.append(cfg)
            else:
                logger.warning(
                    "[provider-pool] fallback label '%s' 在 omni_profiles 中不存在，已跳过",
                    label,
                )
        return primary, fallbacks

    def _get_active_unlocked(self) -> OmniModelSettings:
        """获取 active provider（调用方已持锁）。

        按 label 身份追踪：_active_label 为 None → primary，
        否则在 fallbacks 中按 label 匹配。若 label 已不存在于列表中
        （被管理员删除/改名），则回退 primary。
        """
        primary, fallbacks = self._resolve_providers_unlocked()
        if self._active_label is None:
            return primary
        for fb in fallbacks:
            if fb.label == self._active_label:
                return fb
        logger.warning(
            "[provider-pool] active label '%s' 已不在 fallbacks，回退 primary",
            self._active_label,
        )
        self._active_label = None
        return primary

    async def _try_failover(self) -> bool:
        """尝试切换到下一个健康的备选 provider。

        前置条件：CB 当前非 CLOSED。
        返回 True 表示成功切换，False 表示无可用备选（池耗尽）。

        切换动作：
        1. 把当前 provider 加入 failed 集
        2. 从 fallbacks 中找第一个不在 failed 集中的 provider
        3. 切换到该 provider 并 reset CB
        """
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        with self._lock:
            # 去抖：限制切换频率
            now = time.monotonic()
            if now - self._last_switch_monotonic < self._min_switch_interval_sec:
                logger.debug(
                    "[provider-pool] 距上次切换不足 %.0fs，跳过 failover",
                    self._min_switch_interval_sec,
                )
                return False

            # 检查 CB 状态，若已恢复 CLOSED 则不需要 failover
            cb = get_omni_circuit_breaker()
            snap = cb.snapshot()
            if snap.state == "ok":
                return False

            # tick 探测在飞行中 → 本轮不切。切换尾部的 reset_on_config_change 只
            # 复位状态机，_transition_to_closed_locked 不清 _probe_in_flight，旧
            # provider 的探测结论落回来会把刚复位成 CLOSED 的熔断器重新推开，新
            # provider 被无辜短路（配置类错误还会把 tick 通道钉死）。与
            # _probe_failed_providers 同口径：OPEN_CONFIG（state == "error"）下
            # tick 不 arm 新探测，不让权，仍由池收尾。
            if cb.probe_in_flight() and snap.state != "error":
                logger.debug("[provider-pool] tick 探测在飞行中，本轮跳过 failover")
                return False

            # 动态读取 fallback 列表（支持热更新）
            _primary, fallbacks = self._resolve_providers_unlocked()

            # 没配置任何备选 → 池无事可做，把自愈完整交还给熔断器 tick 通道。
            # 必须在 _failed_keys.add 之前返回：一旦 primary 进了 failed 集，
            # _probe_failed_providers 就会起一条绕开退避的 30s 固定探测通道。
            if not fallbacks:
                return False

            # 标记当前 provider 为 failed
            current = self._get_active_unlocked()
            current_key = _provider_key(current)
            self._failed_keys.add(current_key)
            logger.info(
                "[provider-pool] 当前 provider %s 已标记 failed",
                current_key,
            )

            # 查找下一个健康备选
            selected_label: str | None = None
            for fb in fallbacks:
                if _provider_key(fb) not in self._failed_keys and fb.api_key:
                    selected_label = fb.label
                    break

            if selected_label is None:
                # 所有备选都不可用 → 池耗尽。
                # 关键：这里不清 _active_label。若清成 None，get_active() 会从
                # 当前备选翻回 primary，omni_client 的配置变更钩子看到三元组变化
                # 会误判成「用户改配置」而清掉熔断器，感知没暂停反而继续对着已知
                # 挂掉的主 provider 发 payload。改用 _exhausted 显式标记。
                logger.error(
                    "[provider-pool] 所有 provider 已耗尽（failed=%s），感知引擎暂停",
                    self._failed_keys,
                )
                self._exhausted = True
                # 只更新去抖用的 monotonic 起点：耗尽分支 active 没变，
                # 对外的 last_switch_at_ms 不该上报一次不存在的切换
                # （与 _probe_failed_providers 的 resume_in_place 分支同口径）。
                self._last_switch_monotonic = now
                return False

            # 切换到新 provider
            self._active_label = selected_label
            self._exhausted = False
            new_active = self._get_active_unlocked()
            logger.warning(
                "[provider-pool] failover: %s → %s (label=%s)",
                current_key,
                _provider_key(new_active),
                selected_label,
            )
            self._last_switch_monotonic = now
            self._last_switch_wall_ms = int(time.time() * 1000)

        # 锁外 reset CB（reset_on_config_change 是 async，不能在锁内 await）
        # 新 provider 从头开始，CLOSED 状态
        await cb.reset_on_config_change()
        return True

    async def _probe_failed_providers(self) -> None:
        """探测所有 failed provider 是否恢复。

        - primary 恢复 → 自动切回
        - fallback 恢复 → 从 failed 集中移除（后续可再被选中）
        """
        from miloco.perception.engine.omni import probe as _probe
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        with self._lock:
            if not self._failed_keys:
                return
            primary, fallbacks = self._resolve_providers_unlocked()
            # 档案被删 / 改名后旧 key 不会再出现在 primary+fallbacks 里，
            # 也就永远进不了 to_probe、永远摘不掉。这里顺手裁掉，避免
            # snapshot().failed_keys 和耗尽日志长期挂着不存在的 provider。
            live_keys = {_provider_key(primary)} | {
                _provider_key(fb) for fb in fallbacks
            }
            self._failed_keys &= live_keys
            # 运行期备选被清空时，主也要退出 failed 集：零备选下池不介入
            # 自愈，探测通道完全交还熔断器的指数退避（与 PR 描述的
            # "零配置兼容"口径一致，也与 _try_failover 的空备选早退对称）。
            if not fallbacks:
                self._failed_keys.discard(_provider_key(primary))
            if not self._failed_keys:
                return

            to_probe: list[tuple[str, OmniModelSettings, bool]] = []
            pk = _provider_key(primary)
            if pk in self._failed_keys:
                to_probe.append((pk, primary, True))
            for fb in fallbacks:
                fk = _provider_key(fb)
                if fk in self._failed_keys:
                    to_probe.append((fk, fb, False))

        if not to_probe:
            return

        # 熔断器的 tick 探测通道正在探测当前 active 时，池本轮跳过，避免两条
        # 探测通道对打（池的固定 30s 探测 vs tick 的指数退避探测）。
        if get_omni_circuit_breaker().probe_in_flight():
            return

        primary_recovered = False
        recovered_keys: set[str] = set()

        for key, cfg, is_primary in to_probe:
            if not cfg.api_key:
                # 无 key 的 provider 无法探测，跳过
                continue
            try:
                result = await _probe.probe_omni(cfg.model, cfg.base_url, cfg.api_key)
            except Exception as e:
                logger.debug(
                    "[provider-pool] 恢复探测 %s 异常: %s", key, e
                )
                continue

            if result.get("ok"):
                logger.info("[provider-pool] provider %s 恢复探测通过", key)
                recovered_keys.add(key)
                if is_primary:
                    primary_recovered = True

        cb = get_omni_circuit_breaker()
        # tick 探测在飞行中 → 本轮探测结论整轮不落账。摘 failed 集 / 解除耗尽 /
        # 切回 primary 三个动作要么全做要么全不做：只落一半会留下「池已在 primary、
        # primary 又不在 failed 集、熔断器仍 OPEN」的中间态 —— tick 拿的是 arm 那
        # 一刻解析定的旧 provider，它的失败结果会把熔断器推深，而此后
        # _try_failover 只从 fallbacks 里选，没有任何路径能把 active 带回 primary。
        # OPEN_CONFIG（state == "error"）下 tick 不探测，不让权，仍由池收尾。
        if cb.probe_in_flight() and cb.snapshot().state != "error":
            logger.debug("[provider-pool] tick 探测在飞行中，本轮探测结论不落账")
            return

        resume_in_place = False
        if recovered_keys:
            with self._lock:
                self._failed_keys -= recovered_keys
                # 池耗尽时若当前 active 自己探测通过，必须就地解除耗尽态并 reset 熔断器：
                # primary 未恢复 → 不走 _switch_back_to_primary；而 OPEN_CONFIG 下
                # tick 通道的 try_arm_probe() 恒为 False —— 不在这里收尾就没人收尾了。
                if (
                    self._exhausted
                    and _provider_key(self._get_active_unlocked())
                    not in self._failed_keys
                ):
                    self._exhausted = False
                    # 只更新去抖用的 monotonic 起点：这条路径 active 没变，
                    # 对外的 last_switch_at_ms 不该上报一次不存在的切换。
                    self._last_switch_monotonic = time.monotonic()
                    resume_in_place = True

        # primary 恢复 → 自动切回（内部已负责 reset 熔断器）
        if primary_recovered:
            await self._switch_back_to_primary()
        elif resume_in_place:
            logger.info(
                "[provider-pool] 当前 provider %s 自愈，解除池耗尽态并恢复感知",
                _provider_key(self.get_active()),
            )
            await cb.record_probe_result(True, None)

    async def _switch_back_to_primary(self) -> None:
        """自动切回 primary provider。"""
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        cb = get_omni_circuit_breaker()
        with self._lock:
            if self._active_label is None:
                # 池耗尽后 primary 恢复：active 已指向 primary，但 CB 仍 OPEN，
                # 必须 reset 让感知恢复，否则永久暂停。
                need_reset = cb.snapshot().state != "ok"
            else:
                old = self._get_active_unlocked()
                self._active_label = None
                logger.info(
                    "[provider-pool] 自动切回 primary: %s → %s",
                    _provider_key(old),
                    _provider_key(self._get_active_unlocked()),
                )
                self._last_switch_monotonic = time.monotonic()
                self._last_switch_wall_ms = int(time.time() * 1000)
                need_reset = True
            self._exhausted = False

        if need_reset:
            # 走熔断器自己的探测结果入口：写 _last_probe_at / _last_probe_result，
            # 由熔断器统一维护，admin 面板「上次探测时间/结果」才正确更新。
            # 「tick 探测在飞行中就不抢收尾权」的守卫由唯一调用方
            # _probe_failed_providers 在落账前统一持有，这里不再重复判断
            # —— 在这里判等于状态已改了才掉头，会留下半提交的中间态。
            await cb.record_probe_result(True, None)

    async def _recovery_loop(self) -> None:
        """后台恢复循环：监听 failover 事件 + 定期探测 failed provider。

        每轮无条件尝试 failover（_try_failover 内部已自带 CB-ok 短路 + 去抖门控，
        健康时是 no-op）。这样做是为了处理以下场景：备选 provider 因 CONFIG 错误
        导致 CB 进入 OPEN_CONFIG，自此事件不再重发；若上一次 failover 被去抖跳过，
        恢复循环再无机会重试，感知会「钉死」在坏 provider 上。每轮都调 _try_failover
        保证无论事件触达与否，去抖窗口过后下一超时自然推进。
        """
        logger.info("[provider-pool] 恢复循环启动")
        while True:
            try:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._failover_event.wait(),
                        timeout=self._recovery_probe_interval_sec,
                    )
                    self._failover_event.clear()

                # 每轮无条件尝试 failover：事件已触发或定时到达都可能推进
                await self._try_failover()
                await self._probe_failed_providers()
                # 让出当前时间片，避免 interval=0 时在同一 tick 内高频空转
                await asyncio.sleep(0)

            except asyncio.CancelledError:
                logger.info("[provider-pool] 恢复循环被取消")
                break
            except Exception:
                logger.error(
                    "[provider-pool] 恢复循环异常", exc_info=True
                )
                await asyncio.sleep(5.0)


# ── 模块级单例 ──────────────────────────────────────────────────────────────

_POOL: OmniProviderPool | None = None


def init_pool(
    loop: asyncio.AbstractEventLoop,
    *,
    min_switch_interval_sec: float = _MIN_SWITCH_INTERVAL_SEC,
    recovery_probe_interval_sec: float = _RECOVERY_PROBE_INTERVAL_SEC,
) -> OmniProviderPool:
    """初始化 provider pool（感知模块启动时调用一次）。幂等：重复调用返回已有实例。"""
    global _POOL
    if _POOL is None:
        _POOL = OmniProviderPool(
            loop,
            min_switch_interval_sec=min_switch_interval_sec,
            recovery_probe_interval_sec=recovery_probe_interval_sec,
        )
    return _POOL


def get_pool() -> OmniProviderPool | None:
    """获取 provider pool 单例。未初始化时返回 None。"""
    return _POOL


def reset_pool_for_tests() -> None:
    """测试专用：重置单例。生产代码禁调。"""
    global _POOL
    _POOL = None
