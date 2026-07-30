# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Device welcome action — shared by the bind and home-move paths.

A device "arrives" in a managed home either by being freshly bound
(``user/{uid}/g_op/bind``) or by being moved in from another home
(``device/{did}/g_op/hr_change`` into a whitelisted home). Both cases want
the same thing: greet the device via the agent. This service owns that
action so neither the bind listener nor the meta listener has to carry it.

``welcome(did)`` is intentionally stateless w.r.t. refresh/debounce — the
caller refreshes the device list first, then asks to welcome a did. The only
state kept here is a short dedup window so a single arrival that fires *both*
a bind and an hr_change push (each debounced in its own listener) is greeted
once, not twice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from miot.types import MIoTDeviceInfo

from miloco.dispatch import dispatch_event, join_text_blocks
from miloco.miot.mips_listeners import BIND_DEBOUNCE_SEC, META_DEBOUNCE_SEC

logger = logging.getLogger(__name__)


# A single arrival can surface as a bind push AND an hr_change push, debounced
# on two separate chains. Their welcomes both fire at (last event)+debounce, so
# the debounce cancels and what's left is the skew between the chains' last
# events — normally ~1-2s, but a flapping burst on one chain can stretch it to
# ~one debounce window. Take 2× the larger listener debounce to cover that with
# margin; kept tight so a genuine re-arrival (unbind+rebind / move-out+in)
# outside the window can still be welcomed. Derived from the listener constants
# (no manual sync; mips_listeners doesn't import this module, so no cycle).
WELCOME_DEDUP_SEC: float = 2 * max(BIND_DEBOUNCE_SEC, META_DEBOUNCE_SEC)


GetDevice = Callable[[str], MIoTDeviceInfo | None]
IsHomeAllowed = Callable[[str | None], bool]
LogDeviceDiff = Callable[[str, MIoTDeviceInfo | None, str], None]


class DeviceWelcomeService:
    """Greets a device that is present in a managed home.

    Wired with the proxy's device lookup, scope check and diff logger. Both
    the bind listener and the meta (home-move) listener call ``welcome(did)``
    after they have refreshed the device list.
    """

    def __init__(
        self,
        get_device: GetDevice,
        is_home_allowed: IsHomeAllowed,
        log_device_diff: LogDeviceDiff,
    ) -> None:
        self._get_device = get_device
        self._is_home_allowed = is_home_allowed
        self._log_device_diff = log_device_diff
        # did -> monotonic ts of last welcome, for the dedup window.
        self._recent: dict[str, float] = {}

    async def welcome(self, did: str) -> bool:
        """Greet ``did`` if it is present and in a managed home.

        Returns True only when a welcome message was actually sent. Absent
        device, out-of-scope home, recent duplicate, or a send failure all
        return False (and log the reason).
        """
        dev = self._get_device(did)
        if dev is None:
            logger.info("welcome skipped: did=%s not present", did)
            return False

        try:
            allowed = self._is_home_allowed(dev.home_id)
        except Exception as e:
            logger.error("welcome: is_home_allowed check failed did=%s: %s", did, e)
            return False
        if not allowed:
            logger.info(
                "welcome skipped: did=%s home_id=%s not in allowed scope",
                did,
                dev.home_id,
            )
            return False

        now = time.monotonic()
        last = self._recent.get(did)
        if last is not None and now - last < WELCOME_DEDUP_SEC:
            logger.info(
                "welcome skipped: did=%s greeted %.1fs ago (dedup)", did, now - last
            )
            return False

        self._log_device_diff("WELCOME", dev, did)
        msg_text = self._format_message(dev)
        try:
            # Routed through the message dispatcher as a "bind" event (its own
            # merge channel); drainer skips track_agent_run for bind, so the
            # dashboard doesn't count it. Returns whether anything was sent.
            sent = await dispatch_event("bind", [msg_text], join_text_blocks)
        except Exception as e:
            logger.error("welcome dispatch_event raised did=%s: %s", did, e)
            return False
        # Record only on a real send so a failed attempt can be retried.
        if sent:
            self._recent[did] = now
        logger.info(
            "welcome: did=%s name=%r → dispatch_event %s",
            did,
            dev.name,
            "OK" if sent else "FAILED",
        )
        return sent

    @staticmethod
    def _format_message(dev: MIoTDeviceInfo) -> str:
        """Build the structured welcome message sent to the agent.

        本文本进的是 ``agent:main:miloco`` 的 full 会话（dispatcher 把 bind 事件固定
        路由到那里），也就是与插件的 ``B_IDENTITY`` 同一轮上下文。该块已明令 agent
        「不要改口自称 Miloco」，故这里也不能要求它对用户自称 miloco——否则两条硬
        指令在同一轮里打架：要么播报出"miloco 发现…"顶掉宿主人设，要么模型服从
        B_IDENTITY 而让本模板的要求静默落空。改用不指名的第一人称，语义不变。
        """
        room = dev.room_name or "未知房间"
        name = dev.name or "未知设备"
        home = dev.home_name or "未知家庭"
        model = dev.model or "未知型号"
        did = dev.did
        return (
            f"[新设备接入] 检测到米家账户新增设备「{name}」。\n"
            f"设备信息：设备id「{did}」，型号「{model}」，位于「{home}」-「{room}」。\n"
            f"根据以上信息，用你自己的人设口吻生成一段给用户的欢迎播报（不要自称 miloco）："
            f"1. 告知你发现「{room}」新加入了「{name}」。"
            f"2. 按型号判断设备品类，把它视为你自身能力的延伸，说清接入它后你能多为用户主动做什么、带来哪些个性化的好处。\n"
            f"然后通过 miloco-notify skill 播报这段内容。"
        )
