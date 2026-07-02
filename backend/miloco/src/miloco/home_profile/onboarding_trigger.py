# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Onboarding trigger — 全新安装时后端主动发起家庭信息初始化访谈。

参照 ``miloco.miot.welcome_service.DeviceWelcomeService`` 的模式：后端检测到
条件成立 → ``dispatch_event`` → agent 主动向用户开口。onboarding 事件带
resolveTarget=owner-channel + deliver=true（见 dispatcher._DELIVERY）：整个
turn 直接跑在**车主 IM 会话**里、回复用户直接可见——交互式访谈的唯一合理
形态（只把开场白 push 过去而 turn 留在后台会话会割裂上下文：用户的 IM 回复
落在 channel 会话，访谈状态却在别处）。触发条件（全部满足才发）：

- 米家已授权且已选定家庭（HOME_WHITE_LIST_KEY 非空）——访谈的空间/设备环节
  要跑 ``device catalog``，无家庭时问不出东西；
- person 表为空 **且** 家庭档案正式区为空（真·首次安装）——用户手工填过任何
  一条就保持安静，那种场景由 miloco-onboarding skill 的对话内提议兜底；
- 一次性 KV 标记（``OnboardingKeys.ONBOARDING_PROMPTED_KEY``）未置位。

一次性语义：标记只在**真送达**（delivered future = True）后写入（存 ISO 时间
戳），未送达不写 → 下次启动自然重试；置位后终身不再主动邀请（无重发定时器）。
特别地，车主从未私聊过 bot 时插件解析不到 IM 会话，返回结构化 no-channel
（非传输失败、不烧重试）→ delivered=False → 标记不置位——"全新安装但还没绑
channel"这个边界因此自然收敛为"车主绑定 channel 后的下一次启动送达"。

调用点：① 启动时 manager 就绪后（lifespan 内 fire-and-forget）；② 授权 +
自动选家成功后（``MiotService.authorize_with_code`` 末尾）——首次安装授权完
即触发，无需重启。两处都汇入同一个幂等 ``maybe_trigger()``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from miloco.config import get_settings
from miloco.database.kv_repo import KVRepo, OnboardingKeys
from miloco.dispatch import AgentDispatcher, dispatch_event, join_text_blocks
from miloco.utils.agent_client import _HTTP_BUFFER_S

logger = logging.getLogger(__name__)


# 守护超时的安全余量：worst case 之外再留的固定缓冲（事件循环调度、日志等杂项开销）。
_GUARD_SAFETY_MARGIN_S = 30.0


def _delivery_guard_timeout_s() -> float:
    """送达 future 的守护超时（秒）——按 dispatcher 真实参数在调用时推导。

    **不变量：守护超时必须 > dispatcher 从取批到 resolve future 的最坏耗时**，
    否则一次"慢但最终成功"的送达会被误读为未送达 → KV 标记不置位 → 下次启动
    重发 → 双邀请（正是 status=timeout 记 delivered=True 想防住的场景）。冷启动
    webhook 慢、turn 拖长恰是本机制的主战场，不能栽在守护先到期上。

    最坏耗时 = 每次尝试的 HTTP 上限 (turn_wait + _HTTP_BUFFER_S) × 尝试次数
    (_TRANSPORT_RETRIES + 1) + 各次重试间的指数退避之和；直接引用 dispatcher /
    agent_client 的真实常量与 settings（而非镜像数字），后续任何一方调整都不会
    悄悄把守护挤到 worst case 之下。守护超时 = 送达结果未知（turn 可能仍在途）
    → 不置位 KV（下次启动重试），仅靠 _fired 防本进程内重发。
    """
    wait_s = get_settings().dispatcher.turn_wait_timeout_ms / 1000
    retries = AgentDispatcher._TRANSPORT_RETRIES
    attempts = retries + 1
    backoff_s = sum(AgentDispatcher._TRANSPORT_BACKOFF_S * (2**a) for a in range(retries))
    return attempts * (wait_s + _HTTP_BUFFER_S) + backoff_s + _GUARD_SAFETY_MARGIN_S

# 给 agent 的指令文本（口径参照 welcome_service._format_message 的祈使风格）。
# 本事件经 owner-channel 投递：turn 就跑在用户的 IM 会话里、回复用户直接可见，
# 所以指令是"直接开口"，而不是"去找渠道通知用户"。
_INSTRUCTION = (
    "[系统事件] 检测到 miloco 已完成米家授权，但家庭成员与家庭档案均为空——"
    "这应该是一次全新安装，用户还没做过家庭信息初始化。\n"
    "你现在就在用户的聊天会话里，这条回复用户会直接看到。请使用 miloco-onboarding skill "
    "立即向用户开口发起邀请：打招呼、用一两句话说明登记家庭信息的好处（miloco 会更懂这家人，"
    "控制设备、提醒、建议都更贴合），征得同意后按该 skill 的访谈流程开始（开场即可提示"
    "可以语音回复），每条消息聚焦一个环节、别一次抛出整张问卷。\n"
    "如果用户拒绝或说以后再说：礼貌回应即可，并告知之后随时可以说「初始化家庭」重新开始；"
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

            # dispatch_event 的返回值只是「入队被接纳」——drainer 随后才真正发
            # turn，传输耗尽时会静默丢批。终身一次性标记必须以**真送达**为准，
            # 故传入投递结果 future 并等待 dispatcher resolve（它保证每条丢弃/
            # 送达路径都会 resolve，不悬空）。
            delivered_fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            try:
                accepted = await dispatch_event(
                    "onboarding", [_INSTRUCTION], join_text_blocks, delivered=delivered_fut
                )
            except Exception:  # noqa: BLE001
                logger.warning("onboarding trigger: dispatch_event 异常", exc_info=True)
                return False
            if not accepted:
                logger.info("onboarding trigger: 事件未被接纳（调度器未就绪/队列淘汰）")
                return False

            guard_s = _delivery_guard_timeout_s()
            try:
                sent = await asyncio.wait_for(delivered_fut, timeout=guard_s)
            except asyncio.TimeoutError:
                # 守护超时按推导保证 > dispatcher 最坏 resolve 耗时，走到这里说明
                # future 悬空（理论不该发生）或事件循环异常拥塞：送达结果未知
                # （平台 turn 可能仍在途）→ 不置位 KV → 下次启动重试；置 _fired
                # 防本进程内重发，避免"在途邀请 + 重发"打两次招呼。
                self._fired = True
                logger.warning(
                    "onboarding trigger: 等待送达结果超时(%.0fs)，KV 标记不置位（下次启动重试）",
                    guard_s,
                )
                return False

            # 只在真正送达后置位（含 KV 落盘），失败留给下次启动重试。
            if sent:
                self._fired = True
                flag_ok = self._kv_repo.set(
                    OnboardingKeys.ONBOARDING_PROMPTED_KEY,
                    datetime.now().isoformat(timespec="seconds"),
                )
                if not flag_ok:
                    # KVRepo.set 吞 sqlite 错误返 False：标记没落盘，下次启动会再邀请
                    # 一次。留 WARN 便于排查"重复邀请"的根因。
                    logger.warning(
                        "onboarding trigger: KV 标记写入失败，重启后可能重复邀请一次"
                    )
            logger.info("onboarding trigger: dispatch %s", "DELIVERED" if sent else "FAILED")
            return sent
