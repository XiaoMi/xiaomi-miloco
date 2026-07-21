"""Hermes API 契约测试：固化我们依赖的 Hermes 行为。

为什么需要这个文件：miloco 插件 ``cron_setup.reconcile_cron_jobs`` 必须给
``create_job(deliver=...)`` 传一个 *真实存在的* Platform 名。如果将来 Hermes
改了 ``Platform`` enum（比如加了 ``ALL``）或者改了 ``DeliveryTarget.parse``
的回退行为（比如把 unknown 直接 raise 而不是回退到 LOCAL），插件可能会静默
退化——所有 cron job 推不到 IM。本文件把以下事实变成测试断言：

1. ``Platform("all")`` 不是合法 enum 成员。
2. ``DeliveryTarget.parse("all")`` 在 unknown 平台名上静默回退到 LOCAL（这就
   是 "deliver=all 会让输出落到本地 markdown 而不是 IM" 的根因）。
3. ``DeliveryTarget.parse("feishu")`` 能正确解析到 ``Platform.FEISHU``（证明
   state.json 里的 deliver.target 是真实 Platform 名时能 push 出去）。

跑这套测试不需要 Hermes runtime；只需要把 ``hermes-agent`` 仓库根加到
``sys.path``，从 ``gateway.config`` / ``gateway.delivery`` 直接 import。
conftest.py 已经做了这一步。
"""

from __future__ import annotations

import pytest

pytest.importorskip("gateway.config", reason="Hermes agent not installed; contract test requires real Hermes API")
pytest.importorskip("gateway.delivery", reason="Hermes agent not installed; contract test requires real Hermes API")

from gateway.config import Platform
from gateway.delivery import DeliveryTarget


def test_platform_enum_has_no_all_member():
    """'all' 不是合法 Platform 值（防 'all' 被加进 enum 后悄悄回退行为变化）。"""
    with pytest.raises(ValueError):
        Platform("all")


def test_delivery_target_parse_falls_back_to_local_on_unknown():
    """Unknown target string（'all' / 'origin' 无 origin session / typo）→ LOCAL。

    这是 PR #279 的根因：cron_setup 写死 ``deliver="all"`` 时
    ``DeliveryTarget.parse("all")`` 不抛错，而是悄悄变成 LOCAL——输出落本地
    markdown 而不是 IM。如果 Hermes 改了 parse 行为（比如改成 raise），这条
    测试会失败，提醒 reviewer 同步修 cron_setup。
    """
    parsed = DeliveryTarget.parse("all")
    assert parsed.platform == Platform.LOCAL


def test_delivery_target_parse_known_platform_resolves():
    """已知平台名 ('feishu') 必须解析到对应 Platform，不能落到 LOCAL。

    反向证明：state.json 里的 deliver.target 是真实平台名时，cron 输出真能
    push 到 IM。
    """
    parsed = DeliveryTarget.parse("feishu")
    assert parsed.platform == Platform.FEISHU
    assert parsed.platform != Platform.LOCAL
