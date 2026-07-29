# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Device property poller — 属性历史的兜底采样源(**主链路是 mips 推送**)。

推送覆盖不到的三种情况由本轮询补上:

1. 订阅被 broker 拒(0x87)的设备——正常情况下不会发生(实测 2026-07-29:72 台
   设备的 ``properties_changed`` 子树全部 0x00 放行),但重连窗口存在 ACL 抖动;
2. mips 断连窗口内发生的变化——推送不补发,轮询下一周期能发现;
3. 设备**根本不推送**的属性——推送是设备侧行为,不是所有属性都上报。

实现:每周期一次 `/app/v2/miotspec/prop/get`(单请求上限 150 条,整屋 70+ 设备 ×
主开关 1 条 = 1 次 HTTP 调用),diff 内存中的上次值,变化才落 device_prop_history。

Watchlist 免配置自筛选:对全部设备读 ``prop.2.1``(miot-spec 惯例的主开关 /
occupancy-status 槽位);没有该属性的设备云端返回无 value 条目,当次跳过,零成本。
额外属性用 ``miot.prop_history_poll_extra``(形如 ``did:prop.S.P``)配置。

轮询语义与推送不同:只能保证「变化被发现的时间 ≤ 实际变化时间 + 周期」,行里
ts 是发现时刻。周期默认 300s——推送转正后轮询只是安全网,不必再按秒级采样;
推送路径本身是**事件时刻**,精度不受这个周期影响。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from miot.types import MIoTGetPropertyParam

from miloco.config import get_settings

if TYPE_CHECKING:
    from miloco.miot.client import MiotProxy

logger = logging.getLogger(__name__)

# 单次 /miotspec/prop/get 的条目上限(云端限制 150,留余量)。
_BATCH_LIMIT = 140


class DevicePropPoller:
    """Poll a self-selecting prop watchlist and persist value changes."""

    def __init__(self, proxy: "MiotProxy"):
        self._proxy = proxy
        # (did, siid, piid) -> last seen value。进程内基线;首见 key 先对 DB
        # 最新行 diff,避免每次重启都写一遍全量基线行。
        self._last: dict[tuple[str, int, int], Any] = {}

    async def run(self) -> None:
        """Poll loop. Cancelled by MiotProxy.deinit()."""
        interval = max(10, get_settings().miot.prop_history_poll_interval_sec)
        logger.info("device-prop poller started (interval=%ds)", interval)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 单周期失败(云端限频/网络抖动)只记 log,下周期自愈。
                logger.warning("device-prop poll cycle failed: %s", e)
            await asyncio.sleep(interval)

    def _watch_params(self) -> list[MIoTGetPropertyParam]:
        params = [
            MIoTGetPropertyParam(did=did, siid=2, piid=1)
            for did, dev in self._proxy._device_info_dict.items()
            if "/" not in did and dev.online
        ]
        seen = {(p.did, p.siid, p.piid) for p in params}
        for entry in get_settings().miot.prop_history_poll_extra:
            try:
                did, iid = entry.split(":", 1)
                s, p = iid.removeprefix("prop.").split(".")
                key = (did, int(s), int(p))
            except ValueError:
                logger.warning("prop_history_poll_extra 条目非法,已跳过: %r", entry)
                continue
            if key not in seen:
                seen.add(key)
                params.append(
                    MIoTGetPropertyParam(did=key[0], siid=key[1], piid=key[2])
                )
        return params

    async def _poll_once(self) -> None:
        if not get_settings().miot.prop_history_enabled:
            return
        client = self._proxy._miot_client
        if client is None or not self._proxy.is_authenticated:
            return
        params = self._watch_params()
        if not params:
            return
        results: list[dict] = []
        for i in range(0, len(params), _BATCH_LIMIT):
            results.extend(
                await client.http_client.get_props_async(params[i : i + _BATCH_LIMIT])
            )

        from miloco.manager import get_manager
        from miloco.utils.time_utils import now_ms

        dao = get_manager().device_prop_history_dao
        ts = now_ms()
        changed_by_did: dict[str, list[tuple[int, int, Any]]] = {}
        for r in results:
            if not isinstance(r, dict) or "value" not in r:
                continue  # 设备无此属性 / 离线 / 读失败:自筛选跳过
            try:
                key = (str(r["did"]), int(r["siid"]), int(r["piid"]))
            except (KeyError, TypeError, ValueError):
                continue
            value = r["value"]
            if key in self._last:
                if self._last[key] != value:
                    changed_by_did.setdefault(key[0], []).append(
                        (key[1], key[2], value)
                    )
                self._last[key] = value
                continue
            # 首见:对 DB 最新行 diff——重启后值未变则不写基线行,
            # 值变了(停机窗口内发生的变化)补一行,历史时间线保持连续。
            self._last[key] = value
            latest = dao.query(key[0], siid=key[1], piid=key[2], limit=1)
            if not latest or latest[0]["value"] != value:
                changed_by_did.setdefault(key[0], []).append((key[1], key[2], value))
        for did, changes in changed_by_did.items():
            dao.insert_changes(did, changes, ts)
        if changed_by_did:
            logger.info(
                "device-prop poll: %d change(s) across %d device(s) persisted",
                sum(len(c) for c in changed_by_did.values()),
                len(changed_by_did),
            )
