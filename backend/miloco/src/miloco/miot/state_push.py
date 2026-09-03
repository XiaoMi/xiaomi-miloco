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

**推送和上线补拉这两条通道，往 `iot/device/*/prop` 写的都走这里。** 两道闸因此只有一份，
且都在**写入的那一刻**判 —— 补拉要打一趟云端，往返期间设备可能搬出当前家庭（同一代之内，
代号不变），只在往返前判一次是挡不住的。补拉那一侧自己也有几道早退，那是为了省掉没必要的
云端往返，不是闸；正确性只靠这里。

启动对齐那条通道不走这里，它有自己的一套判据（采集期就按当前家庭取设备、每台设备前比一次
代号）：`state_align._write_device` 整台写 `prop` 子树、用的是替换语义，而它被容器拒收后
退化的 `_write_props_per_leaf` 与写在线标志的 `_write_online_flags` 是逐叶子直写。
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

# 拉来的那一批（上线补拉）记这个。与推送分开：排障方向不同，一条去翻 MQTT 日志、
# 一条去看那次补拉。与对齐同一个标记 —— 对补拉来说它就是一次迟到的对齐
PULL_SOURCE = "iot_align"

# 形态类告警每多少条报一次。推送里遥测属性可以秒级刷屏，无限量报会把日志冲掉
_WARN_EVERY = 100


def _valid_segment(value: Any) -> bool:
    """这一段当不当得了路径段。带 '/' 的桥接子设备 did 会把路径劈成两段。"""
    try:
        validate_segment(value)
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
    if not _valid_segment(did):
        return False
    return store.set(f"iot/device/{did}/status/online", bool(online), source=source)


def write_prop(
    store: StateStore, did: str, iid: str, value: Any, *, source: str = SOURCE
) -> bool:
    """写一条属性到叶子。`iid` 形如 `"2.1"`。返回这一笔有没有进树。

    `dict` 显式拒绝：容器不会抛错，它会把这条路径从叶子翻成子树，订阅旧形态的消费方
    从此收不到这条路径的事件。靠 try/except 挡不住，因为根本不抛。

    `did` 与 `iid` 都是路径段，两个都要校验 —— 含 `/` 的话路径会多出一层、值落到别处。
    """
    if not _valid_segment(did) or not _valid_segment(iid):
        return False
    if isinstance(value, dict):
        return False
    try:
        return store.set(f"iot/device/{did}/prop/{iid}", value, source=source)
    except (TypeError, ValueError):
        return False


def present_prop_iids(store: StateStore, did: str) -> set[str]:
    """容器里这台设备已有哪些属性叶子。

    补拉两处用它：请求侧算缺口、写入侧防覆盖。两处同一份判据。
    """
    have = store.get(f"iot/device/{did}/prop", {})
    return set(have) if isinstance(have, dict) else set()


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

    async def write_pulled_props(self, did: str, values: dict[str, Any]) -> int:
        """把拉来的一批属性写进容器，过与推送同一套闸。返回写进去的条数。

        `values` 的键是 `"<siid>.<piid>"`。补拉走这里而不是自己写，是为了让「逐叶子
        往 `iot/device/*/prop` 写的都过同一套闸」成为结构保证 —— 两道闸都在写入的那
        一刻判，所以云端往返期间设备搬出当前家庭也挡得住。

        **缺口在这里重算，紧挨着写、中间没有 await。** 云端给的是缓存里的最后一次上报、
        推送给的是实时值，容器没有时间戳可仲裁 —— 往返期间被推送填上的那几条不覆盖。
        """
        if not self._scope_is_aligned():
            self._count("pull_not_aligned")
            return 0
        if not await self._in_current_home(did):
            self._count("pull_out_of_home")
            return 0
        present = present_prop_iids(self._store, did)
        written = 0
        for iid, value in values.items():
            if iid in present:
                self._count("pull_not_overwritten")
                continue
            if write_prop(self._store, did, iid, value, source=PULL_SOURCE):
                written += 1
                self._count("pull_written")
            else:
                self._count("pull_rejected")
        return written

    async def on_device_props(self, msg: Any) -> None:
        """属性变化推送。两道闸都要过。"""
        if not self._scope_is_aligned():
            self._count("prop_not_aligned")
            return
        if not await self._in_current_home(msg.did):
            self._count("prop_out_of_home")
            return
        for change in msg.changes:
            iid = f"{change.siid}.{change.piid}"
            if write_prop(self._store, msg.did, iid, change.value):
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
