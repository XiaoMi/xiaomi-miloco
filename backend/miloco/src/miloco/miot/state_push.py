# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""把 MIoT 的上下线推送和属性变化推送写进状态容器。

启动对齐只拉一次，之后容器要跟上真实世界只能靠推送。两条链路共用这一个模块，因为它们
共用同样的作用域判定，分开写会变成两份判据。

**作用域的两道闸对应两种失效**：

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

**推送和拉取这两条通道，往 `iot/device/*/prop` 写的都走这里。** 作用域闸因此只有一份，
且在**写入的那一刻**判 —— 拉取要打一趟云端，往返期间设备可能搬出当前家庭或掉线，只在往返前
判一次是挡不住的。拉取那一侧自己也有早退，那是为了省掉没必要的云端往返；写入处仍需保留
在线检查，避免把离线设备的云端缓存值写进容器。

启动对齐那条通道不走这里，它有自己的一套判据（采集期就按当前家庭取设备、每台设备前比一次
代号）：`state_align._write_device` 整台写 `prop` 子树、用的是替换语义，而它被容器拒收后
退化的 `_write_props_per_leaf` 与写在线标志的 `_write_online_flags` 是逐叶子直写。

**事件推送（`on_device_event`）是第三条推送通道，只有推送这一半。** 它写
`iot/device/<did>/event/` 而不是 `/prop/`，因为事件参数没有可拉取的读接口（云端没有
「读最近一次事件」）—— 于是这条命名空间没有启动对齐、没有上线补拉，重启即为空。两道闸的
判据与属性推送逐条相同，多出来的只有一次 spec 查询：帧里带的是数字 piid，叶子的键是参数名。
查询按 urn 设闸：有一趟在飞时后来的帧不再出门（按在飞状态判 —— 一趟最长 90s，比失败静默期还长），没拿到可用表之后按时间静默一段；别看它是同步的一问一答就以为次数有上界。
**这次查询让「写入那一刻判」多了一个必须处理的后果**：它是这条车道上唯一一个能挂几十秒的
await，两道闸判在它之前的话，写入时判据已经过期 —— 所以这道车道上两道闸判两次。
"""

from __future__ import annotations

import logging
import time
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

# 取事件 spec 没拿到可用结果之后，隔多久才再问一次云端。事件帧里带的是数字 piid，
# 要落成参数名的叶子就得先有 spec；而取一趟表在 SDK 那侧是三次重试、每次里又有不止
# 一个 30s 超时的云端请求（parse_async 的 range(3) 与 _multi_lang 的取译文各一次），
# 一趟是**分钟级**的，且失败不进缓存 —— 不设静默期就等于按帧放大，事件密的设备能
# 一直打云端。静默期内的事件帧照常计数（event_arg_unknown），只是不再出门；到期
# 自动重试，所以一次抖动不会把这台设备锁到进程结束。
#
# 这个时长**只管「失败之后」**。「正在飞的那一趟」由另一个记号（`_event_spec_inflight`）
# 按状态挡，不按时间 —— 一趟取表比这个静默期长，拿时间戳兼表两件事的话，飞过静默期
# 之后后来的帧又会各自出门，正好是这条记号本来要拦的那个放大。
_EVENT_SPEC_QUIET_SECONDS = 60.0


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


def write_event(
    store: StateStore, did: str, iid: str, value: Any, *, source: str = SOURCE
) -> bool:
    """写一条事件参数到叶子。`iid` 形如 `"2.1020.operation-position"`。

    与 ``write_prop`` 同形，只有命名空间不同（``/event/``）—— 段校验与 ``dict``
    拒绝的理由逐条相同，见那边。`iid` 的三个点分段里最后一段是 spec 里的参数名。

    **事件没有一个对应的拉取通道**（云端没有「读最近一次事件」的接口），所以这条
    命名空间里没有对齐、没有上线补拉：进程重启后事件叶一律为空，直到下一次真实事件
    到达。引用事件叶的规则因此在重启后按 ``PATH_MISSING`` 保守拦截 —— 这是设计，
    不是缺陷，验收时别当成回归。

    语义是**「最近一次」而不是「每一次」**：叶子里只有最后落下的那个值。同一参数值
    连续出现（门锁连着两次室内开锁，``operation-position`` 恒为 2）时容器走同值分支、
    不产变更，于是引用它的规则不会被叫醒 —— 与属性车道同口径（同值重写同样不产事件），
    也是 ``IotPushWriter`` 与规则侧共用的那条「变更只当提醒，值一律现读」的必然结果。
    返回值说的只是「这一笔没被叶子上限 / 级联深度拒」，**不表示规则看见了它**。
    """
    if not _valid_segment(did) or not _valid_segment(iid):
        return False
    if isinstance(value, dict):
        return False
    try:
        return store.set(f"iot/device/{did}/event/{iid}", value, source=source)
    except (TypeError, ValueError):
        return False


def present_prop_iids(store: StateStore, did: str) -> set[str]:
    """容器里这台设备已有哪些属性叶子。上线补拉用它算缺口。"""
    have = store.get(f"iot/device/{did}/prop", {})
    return set(have) if isinstance(have, dict) else set()


class IotPushWriter:
    """推送和拉取写容器的入口。作用域与在线闸门在这里统一判。"""

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
        # 事件 spec 的两个出门闸，见 _event_arg_names：正在取表的 urn 集合（按状态，不按
        # 时间），以及取表失败的最近时刻（monotonic）
        self._event_spec_inflight: set[str] = set()
        self._event_spec_quiet_at: dict[str, float] = {}

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

    async def _device_or_none(self, did: str) -> Any | None:
        """取当前家庭里的这台设备；取不到设备集时按「不在」返回。

        拿不到设备集就当不在：写错一台设备的值比少写一次更难查。
        """
        try:
            return await self._current_home_device(did)
        except Exception as e:
            logger.warning("push: 取当前家庭设备失败 did=%s: %s", did, e)
            return None

    async def _in_current_home(self, did: str) -> bool:
        return await self._device_or_none(did) is not None

    async def _current_home_device(self, did: str) -> Any | None:
        return (await self._proxy.devices_in_current_home()).get(did)

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

    async def write_pulled_props(
        self, did: str, values: dict[str, Any], *, keep_reported_since: int = 0
    ) -> int:
        """把拉来的一批属性写进容器，过与推送同一套闸。返回写进去的条数。

        `values` 的键是 `"<siid>.<piid>"`。拉来的都走这里而不是自己写，是为了让「逐
        叶子往 `iot/device/*/prop` 写的都过同一套闸」成为结构保证 —— 作用域和在线状态都在
        写入的那一刻判，所以云端往返期间设备搬出当前家庭或掉线也挡得住。

        **不用旧值盖掉更新的值。** 云端给的是缓存里的最后一次上报、推送给的是实时值，
        谁新只能靠叶子上的 `last_reported` 判。`keep_reported_since` 是这次拉取开始
        的时刻：不早于它的叶子说明往返期间有更新的值到达，不覆盖。

        默认 0 = 已经在树上的一律不覆盖 —— 上线补拉走这条：它拉的本来就是缺的叶子，
        期间被推送填上的那几条一条都不该动。

        **判据在这里现读，紧挨着写、中间没有 await。**
        """
        if not self._scope_is_aligned():
            self._count("pull_not_aligned")
            return 0
        try:
            device = await self._current_home_device(did)
        except Exception as e:
            # 拿不到设备集时拒绝写入，避免把未确认归属或状态的值落入容器。
            logger.warning("push: 取当前家庭设备失败 did=%s: %s", did, e)
            device = None
        if device is None:
            self._count("pull_out_of_home")
            return 0
        if not getattr(device, "online", True):
            self._count("pull_offline")
            return 0
        written = 0
        for iid, value in values.items():
            entry = self._store.get_entry(f"iot/device/{did}/prop/{iid}")
            if entry is not None and entry.last_reported >= keep_reported_since:
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

    async def _event_arg_names(self, urn: Any, siid: int, eiid: int) -> dict[int, str]:
        """这台设备这个事件的 `{piid: 参数名}`。取不到 spec 时给空表。

        空表与「这个事件真没有参数」在调用侧长得一样，都落不了叶 —— 但计数分开
        （`event_arg_unknown`），所以量级看得见。

        取表没拿到可用结果就按 urn 静默一小段（`_EVENT_SPEC_QUIET_SECONDS`），不按帧
        重试：这条路径上每一帧都问一次，而一次失败是三次 30s 超时的云端请求。

        **两道闸，两种判据，不能合成一个时间戳。** 正在取表按**状态**挡（`_event_spec_inflight`），
        失败之后按**时间**挡：一趟取表是分钟级的，比失败静默期还长，共用一个时间戳的话飞过静默期
        之后后来的帧会各自再起一趟 —— 正好是这道闸本来要拦的那个放大。代价只是「正在取」
        这一小段里同一台设备的其他事件帧也落不了叶（它们本来就要等同一份表，等不到也是等）；
        表取回来即撤掉在飞记号，命中缓存那条路没有挂起点，正常路径一点不挡。
        """
        if not urn:
            # 没有 urn 就无从取表，也就解不出参数名。**不去问**：取表按 urn 建缓存键、
            # 一趟是分钟级的云端请求，空 urn 问不出东西，只是按帧放大。落下来的参数由
            # 调用侧照常计 `event_arg_unknown` —— 与「表里没有这个 piid」同一口径。
            return {}
        key = str(urn)
        if key in self._event_spec_inflight:
            # 有一趟正在飞：这一帧搭不上那趟车，也不再另起一趟。**不看时刻**，理由见上
            return {}
        quiet_at = self._event_spec_quiet_at.get(key)
        if quiet_at is not None and time.monotonic() - quiet_at < _EVENT_SPEC_QUIET_SECONDS:
            # 刚失败过：这一帧不出门。参数名解不出，调用侧照常记 event_arg_unknown
            # —— 静默期不动计数口径
            return {}
        self._event_spec_inflight.add(key)
        try:
            events = await self._proxy._fetch_device_events(key)
        except Exception as e:
            # 失败要按**失败那一刻**计时，不是按出门那一刻。
            # 常见那种失败不落到这里：客户端已经把取表失败吞成空表（联网失败、拿不到
            # instance、没有 OAuth 都 return {}），走的是下面那个空表分支，那一支在
            # 同一刻置静默。这里兜的是它返回之后才出错的形态（spec 结构不对之类）——
            # 少见不等于可以省，漏了这一步就是按帧放大。
            logger.warning("push: 取事件 spec 失败 urn=%s: %s", urn, e)
            self._event_spec_quiet_at[key] = time.monotonic()
            return {}
        finally:
            # 撤销放在 finally：中途被取消（CancelledError 不继承 Exception）时也得撤，
            # 否则这台设备在进程余下的寿命里再也取不到表 —— 那是比放大更坏的静默失效
            self._event_spec_inflight.discard(key)
        if not events:
            # 空表与取表失败在调用侧长得一样、都落不了叶，一并静默：设备真没有事件时
            # 也不必每帧再问一遍（那一次走的是客户端缓存，但没必要）
            self._event_spec_quiet_at[key] = time.monotonic()
        else:
            # 拿到可用表即撤掉静默记号。成功那条路走的是 SDK 的缓存，下一帧再问也不出门
            self._event_spec_quiet_at.pop(key, None)
        names: dict[int, str] = {}
        for entry in events.values():
            if entry.get("siid") != siid or entry.get("eiid") != eiid:
                continue
            piid, name = entry.get("piid"), entry.get("name")
            if isinstance(piid, int) and name:
                names[piid] = str(name)
        return names

    async def on_device_event(self, msg: Any) -> None:
        """事件推送。两道闸都要过，与属性推送同一条理由。

        **两道闸判两次**：取表之前先判一次（省下那趟云端往返），取表之后、写叶子之前
        再判一次 —— 取表能飞几十秒，判在取表之前那一次到写入时已经过期了。早退那次
        与写入前那次各记各的计数，同一帧只会被记一次。

        与属性推送的两处形制差异，都来自「帧里只有数字 piid、而叶子的键是参数名」：

        * 落叶子前要先按 spec 把 piid 翻成参数名 —— 因此这台设备得先能取到 spec；
        * 翻不出来的参数**逐条跳过并计数**，不整帧丢弃：一个事件里认得的参数照样
          该落地，一个不认得的参数不该连坐其余。
        """
        if not self._scope_is_aligned():
            self._count("event_not_aligned")
            return
        device = await self._device_or_none(msg.did)
        if device is None:
            self._count("event_out_of_home")
            return
        if not msg.args:
            # spec 允许无参事件。它的到达是真的，但没有可落的参数 —— 单独计数，
            # 免得「丢了多少帧」这个问题只能靠猜
            self._count("event_no_args")
            return
        names = await self._event_arg_names(
            getattr(device, "urn", None), msg.siid, msg.eiid
        )
        # 上面那两道闸是早退，真正的判据在这里 —— 取表可能飞上几分钟（冷表，或静默
        # 到期后的重试：SDK 那边一趟是三次重试、每次不止一个 30s 超时），那期间设备可能搬出当前家庭、
        # 也可能切作用域把树清空。不复查的话取表回来照样写：复活一条删除调和刚删掉
        # 的叶子（谁也删不掉的幽灵设备），或把上一个账号的事件值写进刚清空的树 ——
        # 正是模块 docstring 说这两道闸要挡的两件事。口径同拉取车道：判据在写入的
        # 那一刻现读；作用域放在最后，它与循环之间没有 await。
        if await self._device_or_none(msg.did) is None:
            self._count("event_out_of_home")
            return
        if not self._scope_is_aligned():
            self._count("event_not_aligned")
            return
        for arg in msg.args:
            name = names.get(arg.piid)
            if name is None:
                self._count("event_arg_unknown")
                if self._should_warn():
                    logger.warning(
                        "push: 事件参数名未知 did=%s iid=event.%s.%s piid=%s",
                        msg.did,
                        msg.siid,
                        msg.eiid,
                        arg.piid,
                    )
                continue
            if write_event(
                self._store, msg.did, f"{msg.siid}.{msg.eiid}.{name}", arg.value
            ):
                self._count("event_written")
                continue
            self._count("event_rejected")
            if self._should_warn():
                logger.warning(
                    "push: 事件值被拒 did=%s iid=event.%s.%s.%s type=%s",
                    msg.did,
                    msg.siid,
                    msg.eiid,
                    name,
                    type(arg.value).__name__,
                )
