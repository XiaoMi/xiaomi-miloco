# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Onboarding trigger — 全新安装时后端主动发起家庭信息初始化访谈。

参照 ``miloco.miot.welcome_service.DeviceWelcomeService`` 的模式：后端检测到
条件成立 → ``dispatch_event`` → agent 在聊天频道主动向用户开口。触发条件
（全部满足才发）：

- 米家已授权且已选定家庭（HOME_WHITE_LIST_KEY 非空）——访谈的空间/设备环节
  要跑 ``device catalog``，无家庭时问不出东西；
- person 表为空 **且** 家庭档案正式区为空（真·首次安装）——用户手工填过任何
  一条就保持安静，那种场景由 miloco-onboarding skill 的对话内提议兜底；
- 一次性 KV 标记（``OnboardingKeys.ONBOARDING_PROMPTED_KEY``）未置位。

一次性语义：标记只在 dispatch 确认 sent=True 后写入（存 ISO 时间戳），发送
失败不写 → 下次启动自然重试；置位后终身不再主动邀请（无重发定时器）。

调用点：① 启动时 manager 就绪后（lifespan 内 fire-and-forget）；② 授权 +
自动选家成功后（``MiotService.authorize_with_code`` 末尾）——首次安装授权完
即触发，无需重启。两处都汇入同一个幂等 ``maybe_trigger()``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from miloco.database.kv_repo import KVRepo, OnboardingKeys
from miloco.dispatch import dispatch_event, join_text_blocks

logger = logging.getLogger(__name__)


# 给 agent 的指令文本（口径参照 welcome_service._format_message 的祈使风格）。
_INSTRUCTION = (
    "[系统事件] 检测到 miloco 已完成米家授权，但家庭成员与家庭档案均为空——"
    "这应该是一次全新安装，用户还没做过家庭信息初始化。\n"
    "请使用 miloco-onboarding skill，在聊天频道主动向用户打招呼并发起家庭信息初始化访谈："
    "先用一两句话说明做这件事的好处（miloco 会更懂这家人，控制设备、提醒、建议都更贴合），"
    "征得用户同意后按该 skill 的访谈流程进行，每条消息聚焦一个环节、别一次抛出整张问卷。\n"
    "如果用户拒绝或说以后再说：礼貌回应即可，并告知之后随时可以对我说「初始化家庭」重新开始；"
    "此后不要再主动追问这件事。"
)


class OnboardingTriggerService:
    """全新安装检测 → 主动邀请 onboarding（终身一次）。

    依赖以可调用形式注入（同 DeviceWelcomeService 的风格），便于单测：
    - ``is_miot_ready``：米家已授权且已选定家庭
    - ``has_persons``：person 表非空
    - ``has_profile_entries``：家庭档案正式区非空
    KV 标记直接走 ``kv_repo``（get/set 语义简单，无需再包一层）。
    """

    def __init__(
        self,
        kv_repo: KVRepo,
        is_miot_ready: Callable[[], bool],
        has_persons: Callable[[], bool],
        has_profile_entries: Callable[[], bool],
    ) -> None:
        self._kv_repo = kv_repo
        self._is_miot_ready = is_miot_ready
        self._has_persons = has_persons
        self._has_profile_entries = has_profile_entries
        # 进程内一次性护栏：多个调用点（启动 + 授权回调）可能并发汇入，
        # lock 串行化「检查-发送-置位」，_fired 兜底 KV 写失败时同一进程内不重发。
        self._lock = asyncio.Lock()
        self._fired = False

    async def maybe_trigger(self) -> bool:
        """条件全满足时发一次主动邀请。仅真正发出（sent=True）返回 True。

        任何条件不满足、重复调用、发送失败都返回 False（并各自留日志）。
        条件判定回调抛异常按「不满足」处理——主动邀请是锦上添花，绝不能
        把启动 / 授权主流程带崩。
        """
        async with self._lock:
            if self._fired:
                logger.debug("onboarding trigger skipped: already fired this run")
                return False
            if self._kv_repo.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY):
                logger.debug("onboarding trigger skipped: prompted flag already set")
                return False
            try:
                if not self._is_miot_ready():
                    logger.info("onboarding trigger skipped: miot not authed / no home selected")
                    return False
                if self._has_persons():
                    logger.info("onboarding trigger skipped: person table not empty")
                    return False
                if self._has_profile_entries():
                    logger.info("onboarding trigger skipped: home profile not empty")
                    return False
            except Exception:  # noqa: BLE001
                logger.warning("onboarding trigger: 条件检查失败，跳过本次", exc_info=True)
                return False

            try:
                sent = await dispatch_event("onboarding", [_INSTRUCTION], join_text_blocks)
            except Exception:  # noqa: BLE001
                logger.warning("onboarding trigger: dispatch_event 异常", exc_info=True)
                return False

            # 只在真正送达后置位（含 KV 落盘），失败留给下次启动重试。
            if sent:
                self._fired = True
                self._kv_repo.set(
                    OnboardingKeys.ONBOARDING_PROMPTED_KEY,
                    datetime.now().isoformat(timespec="seconds"),
                )
            logger.info("onboarding trigger: dispatch_event %s", "OK" if sent else "FAILED")
            return sent
