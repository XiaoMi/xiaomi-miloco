# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""感知通路共用的「规则下发范围」与「每摄像头感知须知」。

这两件事与用哪个模型无关,是 miloco 的产品语义:
- 规则绑到哪些感知设备(``condition.perceive_device_ids``,空 = 广播);
- 每台摄像头用户自定义的「感知须知」prompt(面板可填,改动下一窗即生效)。

放在这里而不是各引擎各写一份 —— 一旦漂移,两条通路对同一条规则的下发范围就会
不一致,或者用户在面板上写的机位说明只对其中一条通路生效(而界面上看不出来)。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def physical_did(did: str) -> str:
    """合成通道 did → 物理 did:``cam1:ch0`` → ``cam1``;``cam1`` → ``cam1``。

    感知按合成通道 did(``did:ch{n}``)运作,而 rule 可绑到整台相机的物理 did;
    匹配时两种粒度都要能命中。
    """
    return did.rsplit(":ch", 1)[0] if ":ch" in did else did


def rules_for_device(rules: list[dict], did: str) -> list[dict]:
    """筛出该设备本轮该判的规则。

    ``condition.perceive_device_ids`` 命中该 device 才下发;空列表 = 向全部感知
    设备广播。rule 绑物理 did(整台相机)时,命中该相机的任一通道。
    """
    return [
        r for r in rules
        if not r.get("condition", {}).get("perceive_device_ids")
        or did in r["condition"]["perceive_device_ids"]
        or physical_did(did) in r["condition"]["perceive_device_ids"]
    ]


def camera_prompt_map() -> dict[str, str]:
    """实时读全部相机自定义「感知须知」prompt(did→文本)。

    读 KV(进程内缓存)失败时返回空 dict —— **fail-open**:瞬时故障只是少一段
    机位指导,不该阻断感知(与语音白名单的 fail-closed 相反,因本项只增益、不涉隐私)。
    """
    from miloco.manager import get_manager
    from miloco.miot.filter import camera_prompts

    try:
        return camera_prompts(get_manager().kv_repo)
    except Exception as e:  # noqa: BLE001
        logger.warning("camera prompt map lookup failed, injecting none: %s", e)
        return {}
