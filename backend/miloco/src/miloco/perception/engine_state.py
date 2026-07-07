"""感知引擎「用户开关」的持久化状态。

用户在 Web 上「让它休息 / 唤醒」= 暂停 / 恢复感知。该意图必须落盘，
否则后台一旦重启就无条件把引擎拉起、继续烧 token。

约定与 miot/filter.py 一致：free function 首参 kv_repo、值用 json 序列化。
key 缺省视为「开启」——老部署与新装维持既有行为，只有用户主动暂停才写 false。
"""

import json
import logging

from miloco.database.kv_repo import KVRepo, SystemConfigKeys

logger = logging.getLogger(__name__)


def is_perception_enabled(kv_repo: KVRepo) -> bool:
    raw = kv_repo.get(SystemConfigKeys.PERCEPTION_ENABLED_KEY)
    if raw is None:
        return True  # 从未设置 → 默认开启（保持既有行为）
    try:
        return bool(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "KV %s 值非法(%r)，按默认开启处理",
            SystemConfigKeys.PERCEPTION_ENABLED_KEY,
            raw,
        )
        return True


def set_perception_enabled(kv_repo: KVRepo, enabled: bool) -> bool:
    """落盘用户「感知开关」意图，返回是否写入成功。

    KVRepo.set 内部吞掉 sqlite 异常、失败仅返回 False。这里透传该结果并记 error：
    「暂停/唤醒」若没真正落盘，重启后门控会读到旧值 → 本模块要根治的静默复位在边缘
    路径复现，故调用方（用户 endpoint）应据此 fail loud，而非假装写成功。
    """
    ok = kv_repo.set(SystemConfigKeys.PERCEPTION_ENABLED_KEY, json.dumps(enabled))
    if not ok:
        logger.error(
            "持久化感知开关意图失败 (enabled=%s)，重启后可能复位", enabled
        )
    return ok
