"""感知侧每 cycle 取规则用的是「有效启用」那份。

``rule.enabled`` 只表示用户意图 (§19.9), 只按它过滤会让停用的 task 继续下发给
omni、继续推理、继续触发 —— 停用就成了空操作。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_realtime_perceive_takes_effectively_enabled_rules():
    """两个口径都能跑通, 断言取的是有效启用那个。"""
    from miloco.perception.client import PerceptionEngineProxy

    proxy = PerceptionEngineProxy()
    proxy.perception_engine = MagicMock()  # ready

    rule_service = MagicMock()
    rule_service.get_effectively_enabled_rules = AsyncMock(return_value=[])
    rule_service.get_all_rules = AsyncMock(return_value=[])
    manager = MagicMock()
    manager.rule_service = rule_service

    batch = MagicMock()
    batch.devices = {}
    batch.to_batched_snapshot.return_value = None  # 取完规则就提前退出

    with patch("miloco.manager.get_manager", return_value=manager):
        await proxy.realtime_perceive(batch)

    rule_service.get_effectively_enabled_rules.assert_awaited_once()
    rule_service.get_all_rules.assert_not_awaited()
