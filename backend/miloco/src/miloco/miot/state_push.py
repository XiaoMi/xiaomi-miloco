# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""把 MIoT 的上下线推送和属性变化推送写进状态容器。

启动对齐只拉一次，之后容器要跟上真实世界只能靠推送。两条链路共用这一个模块，因为它们
共用同样的两道闸和同样的作用域判定，分开写会变成两份判据。

**两道闸对应两种失效**：

* 换作用域用 `scope_is_aligned()`（它比的是「已对齐的代号 == 当前代号」，代号一推进就
  自动失效）—— 授权新账号时旧 MIPS 连接还没拆，那段窗口里旧账号的推送会写进刚清空的树；
* 掉出作用域用设备集 —— 代号是全局的，挡不住同一代之内一台设备搬出当前家庭，那时删除
  调和已经把它的叶子删了，一条在途的推送会把它整个写回来，变成谁也删不掉的幽灵设备。

**上下线那条只过第二道闸**，理由见 `on_device_state`。切账号时启用集会先被清空，而
`filter_by_home` 对空启用集一律返回假，所以那个窗口是失败关闭的。

**属性推送多一道对齐门，上下线不需要**。对齐拿的是云端缓存里的最后一次上报，推送拿的是
实时值，两者都不带时间戳，谁覆盖谁只能靠顺序定死（先对齐再落推送）。而在线标志的容器值
本来就取自设备缓存，推送更新的正是那份缓存，两条路同源，早写晚写结果一样。

**值只能写叶子，不能写父路径**。容器的 `set` 恒为替换，写 `iot/device/<did>/prop` 会把
同级其他属性全删掉。这与对齐整台写一次的用法方向相反，别照抄那一处。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from miloco.state import StateStore
from miloco.state.path import validate_segment

logger = logging.getLogger(__name__)

# 容器里这一笔的来源标记。与对齐（iot_align）、调和（iot_reconcile）分开记，
# dump 里一眼能看出这条叶子是拉来的还是推来的
SOURCE = "iot_push"

# 形态类告警每多少条报一次。推送里遥测属性可以秒级刷屏，无限量报会把日志冲掉
_WARN_EVERY = 100


def _valid_did(did: Any) -> bool:
    """这个 did 当不当得了路径段。带 '/' 的桥接子设备会把路径劈成两段。"""
    try:
        validate_segment(did)
    except (TypeError, ValueError):
        return False
    return True


def write_online(
    store: StateStore, did: str, online: bool, *, source: str = SOURCE
) -> bool:
    """写在线标志。返回这一笔有没有进树。

    `source` 默认记成推送。云端重拉那条通道（`_sync_iot_online_flags`）要传自己的标记
    —— 两条通道的排障方向完全不同（一条去翻 MQTT 日志、一条去看那次刷新），混记会让
    dump 指错方向。
    """
    if not _valid_did(did):
        return False
    return store.set(f"iot/device/{did}/status/online", bool(online), source=source)


def write_prop(store: StateStore, did: str, siid: int, piid: int, value: Any) -> bool:
    """写一条属性到叶子。返回这一笔有没有进树。

    `dict` 显式拒绝：容器不会抛错，它会把这条路径从叶子翻成子树，订阅旧形态的消费方
    从此收不到这条路径的事件。靠 try/except 挡不住，因为根本不抛。
    """
    if not _valid_did(did):
        return False
    if isinstance(value, dict):
        return False
    try:
        return store.set(f"iot/device/{did}/prop/{siid}.{piid}", value, source=SOURCE)
    except (TypeError, ValueError):
        return False


class IotPushWriter:
    """推送写容器的两个入口。两道闸都在这里，调用方不自己判。"""

    def __init__(
        self,
        store: StateStore,
        miot_proxy: Any,
        *,
        scope_is_aligned: Callable[[], bool],
    ) -> None:
        self._store = store
        self._proxy = miot_proxy
        self._scope_is_aligned = scope_is_aligned
        self._counts: dict[str, int] = {}
        self._warn_countdown = 1

    def stats(self) -> dict[str, int]:
        """各类计数。丢弃是常态（切换窗口、家庭外），要能看出量级。"""
        return dict(self._counts)

    def _count(self, kind: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1

    def _should_warn(self) -> bool:
        self._warn_countdown -= 1
        if self._warn_countdown > 0:
            return False
        self._warn_countdown = _WARN_EVERY
        return True

    async def _in_current_home(self, did: str) -> bool:
        try:
            return did in await self._proxy.devices_in_current_home()
        except Exception as e:
            # 拿不到设备集就当不在：写错一台设备的值比少写一次更难查
            logger.warning("push: 取当前家庭设备失败 did=%s: %s", did, e)
            return False

    async def on_device_state(self, msg: Any) -> None:
        """云端上下线推送。只过第二道闸 —— 理由见模块 docstring。"""
        online = msg.event == "online"
        if not await self._in_current_home(msg.did):
            self._count("online_out_of_home")
            return
        if write_online(self._store, msg.did, online):
            self._count("online_written")
        else:
            self._count("online_rejected")

    async def on_device_props(self, msg: Any) -> None:
        """属性变化推送。两道闸都要过。"""
        if not self._scope_is_aligned():
            self._count("prop_not_aligned")
            return
        if not await self._in_current_home(msg.did):
            self._count("prop_out_of_home")
            return
        for change in msg.changes:
            if write_prop(self._store, msg.did, change.siid, change.piid, change.value):
                self._count("prop_written")
                continue
            self._count("prop_rejected")
            if self._should_warn():
                logger.warning(
                    "push: 属性值被拒 did=%s iid=prop.%s.%s type=%s",
                    msg.did,
                    change.siid,
                    change.piid,
                    type(change.value).__name__,
                )
