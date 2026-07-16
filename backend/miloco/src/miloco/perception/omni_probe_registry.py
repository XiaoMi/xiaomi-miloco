"""omni 熔断器 tick 驱动的探测 task 生命周期注册表。

独立成 module 是为了让 runner.stop 拿到清理入口时不需要 import processor —— 后者
经 client → manager → perception/__init__ 会兜回 runner,构成 CodeQL 报的
py/cyclic-import(实际靠 __init__ 的函数级懒加载不会 runtime 死锁,但静态分析看到
就报)。本模块无外部 miloco 依赖,任何模块 import 它都不进环。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# 强引用集合:防 asyncio 弱引用模型下 GC 提前回收 fire-and-forget probe task。
# done_callback 里自动 discard。processor.drive_omni_probe 创建 task 时 add + 挂
# discard 回调,runner.stop 时靠 cancel_inflight() 兜底清位。
_OMNI_PROBE_TASKS: set[asyncio.Task] = set()


def register(task: asyncio.Task) -> None:
    """processor 创建 probe task 后调:加入强引用集,挂 done 回调自动 discard。"""
    _OMNI_PROBE_TASKS.add(task)
    task.add_done_callback(_OMNI_PROBE_TASKS.discard)


async def cancel_inflight() -> None:
    """runner.stop 用:取消所有未完成的 omni probe task 并等待它们退出。

    若不清理,shutdown 时 loop 销毁前 probe 未跑到 finally 的 clear_probe_in_flight,
    同进程再启 runner (测试 / manager 重建) 时 _probe_in_flight 残留 True,
    try_arm_probe 永远返 False,自愈通道永久卡死,只能重启进程。
    """
    for task in list(_OMNI_PROBE_TASKS):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 预期路径:probe 被 cancel 后走 finally 里的 clear_probe_in_flight
                # 再抛出 CancelledError,这里只需吞掉 —— 不是错误状态。
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("[engine] 清理 omni probe task 时异常 | %s", e)
