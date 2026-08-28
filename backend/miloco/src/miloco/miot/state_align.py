# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""启动时把 iot 设备属性拉一遍写进状态容器。

推送只在值变化时才来，重启后容器是空的、不会自己长回来，所以要主动拉一次。这里只做
「启动跑一次」，上线/重连那条时机还没接。

**离线设备只写在线标志，不拉属性。** 云端给的是缓存里的最后一次上报，可能任意旧，写进去
会把 `last_reported` 刷成当前时刻，而响应不带时间戳、消费方看不出来。整台跳过也不行 ——
容器里没有这台设备，消费方就分不出「离线」和「没接入」。

**只拉当前启用家庭的设备。** 没启用的家庭在别处一律拒绝访问，容器是要喂 agent 的那份
数据源，不该自己开一条旁路。

**在线标志放在 `status/` 下而不是直接挂在设备那一层。** 设备的字段那一层全是子树，
`iot/device/<did>/*` 才会在少写 `**` 时报错；混一片叶子进去它就改为静默返回残缺结果。

读失败按返回码分级：码表认识的降到 debug（已知常态），不认识的留 warning。汇总行按
「释义 × 型号」分组计数，不靠样本还原分布 —— 占比高的时候要看的是分布，而样本上限恰好
会把它挡住。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from miot.types import MIoTGetPropertyParam

from miloco.miot.iid import try_parse_iid
from miloco.miot.result_codes import code_message, is_failure, is_known_code
from miloco.state import StateStore
from miloco.state.path import validate_segment

logger = logging.getLogger(__name__)

# 云端单次请求的属性条数上限，与 SDK 里 get_props 的批量口径一致
CHUNK_SIZE = 150

# 每类异常最多报几条样本。一台坏设备能把日志刷满，而定位只需要头几条
SAMPLE_LIMIT = 5

SOURCE = "iot_align"


@dataclass(slots=True, frozen=True)
class _DeviceMeta:
    online: bool
    model: str


class _Samples:
    """按类别限量收集样本，同时记全量计数。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.shown: dict[str, int] = {}

    def take(self, kind: str) -> bool:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.shown.get(kind, 0) >= SAMPLE_LIMIT:
            return False
        self.shown[kind] = self.shown.get(kind, 0) + 1
        return True


async def _collect_params(
    miot_proxy: Any, samples: _Samples
) -> tuple[list[MIoTGetPropertyParam], dict[str, _DeviceMeta]]:
    """拼出跨设备的请求清单，同时带回每台的在线状态和型号。

    离线设备进 meta（要写在线标志）但不进 params（不拉属性）。
    """
    devices = await miot_proxy.devices_in_current_home()
    params: list[MIoTGetPropertyParam] = []
    meta: dict[str, _DeviceMeta] = {}
    for did, device in devices.items():
        try:
            validate_segment(did)
        except (TypeError, ValueError) as e:
            # did 拼进路径，非法段（桥接子设备的 did 带 '/'）连在线标志都写不进去
            if samples.take("bad_did_requested"):
                logger.warning("align: skip did %r: %s", did, e)
            continue
        meta[did] = _DeviceMeta(
            online=bool(getattr(device, "online", True)),
            model=str(getattr(device, "model", "?")),
        )
        if not meta[did].online:
            continue
        try:
            iids = await miot_proxy.get_readable_prop_iids(did)
        except Exception as e:
            if samples.take("spec_failed"):
                logger.warning("align: spec unavailable did=%s: %s", did, e)
            continue
        for iid in iids:
            parsed = try_parse_iid(iid, "prop")
            if parsed is None:
                if samples.take("bad_iid"):
                    logger.warning("align: unparsable iid did=%s iid=%s", did, iid)
                continue
            siid, piid = parsed
            params.append(MIoTGetPropertyParam(did=did, siid=siid, piid=piid))
    return params, meta


def _log_unreadable(
    did: str, model: str, siid: Any, piid: Any, code: int, samples: _Samples
) -> None:
    """码表认识的降到 debug，不认识的留 warning。

    没人解释过的码才是要看的，跟已知常态混在一起就被埋掉了。
    """
    args = (
        "align: unreadable did=%s model=%s iid=prop.%s.%s code=%s (%s)",
        did,
        model,
        siid,
        piid,
        code,
        code_message(code),
    )
    if is_known_code(code):
        logger.debug(*args)
    elif samples.take("unknown_code"):
        logger.warning(*args)


async def _read_values(
    miot_proxy: Any,
    params: list[MIoTGetPropertyParam],
    meta: dict[str, _DeviceMeta],
    unreadable: dict[str, int],
    samples: _Samples,
) -> dict[str, dict[str, Any]]:
    """分批读取，按 did 归拢成 {did: {"<siid>.<piid>": value}}。

    只收本轮请求过的 did。云端会在同一批响应里顺带回没请求的行，那些行要么属于别的
    家庭，要么来自被跳过的设备（离线、拿不到 spec），都不该进容器。
    """
    requested = {param.did for param in params}
    by_device: dict[str, dict[str, Any]] = {}
    for start in range(0, len(params), CHUNK_SIZE):
        chunk = params[start : start + CHUNK_SIZE]
        try:
            rows = await miot_proxy.get_device_properties(chunk)
        except Exception as e:
            # 云端断了每批都抛同一个异常，批次编号对定位没有帮助
            if samples.take("chunk_failed"):
                logger.warning(
                    "align: chunk read failed offset=%s size=%s: %s",
                    start,
                    len(chunk),
                    e,
                )
            continue
        for row in rows:
            did = row.get("did")
            siid, piid = row.get("siid"), row.get("piid")
            if did is None or siid is None or piid is None:
                if samples.take("row_without_ids"):
                    logger.warning("align: row missing did/siid/piid: %s", row)
                continue
            if did not in requested:
                # 判据是「本轮请求过没有」而不是「在不在 meta 里」：离线设备和拿不到
                # spec 的设备都在 meta 里，却一条属性都没请求过。分两类记是因为排查
                # 方向不同 —— 家庭外的写进去等于绕过家庭过滤，本轮没请求的则是云端
                # 缓存值，写进去会把 last_reported 盖成当下、看不出它其实很旧
                kind = "out_of_home" if did not in meta else "not_requested"
                if samples.take(kind):
                    logger.warning(
                        "align: drop %s row did=%s iid=prop.%s.%s",
                        kind,
                        did,
                        siid,
                        piid,
                    )
                continue
            model = meta[did].model
            # 失败判定跟 result_codes 同一份：那边把 accept 一类的负码算成功，
            # 这里另立「非 0 即失败」会把带值的成功行丢掉，还占满未知码那条告警通道
            code = row.get("code")
            if is_failure(code):
                bucket = f"{code_message(code)}({code}) {model}"
                unreadable[bucket] = unreadable.get(bucket, 0) + 1
                _log_unreadable(did, model, siid, piid, code, samples)
                continue
            if "value" not in row:
                if samples.take("row_without_value"):
                    logger.warning(
                        "align: row is not a failure but carries no value: "
                        "did=%s iid=prop.%s.%s code=%s",
                        did,
                        siid,
                        piid,
                        code,
                    )
                continue
            value = row["value"]
            if isinstance(value, dict):
                # 容器会把 dict 展开成一层子树，这条属性就不再是叶子，按叶子读的人取到空。
                # 别的类型要么是合法叶子，要么写的时候被容器抛出来
                if samples.take("dict_value"):
                    logger.warning(
                        "align: dict value dropped did=%s iid=prop.%s.%s value=%.120r",
                        did,
                        siid,
                        piid,
                        value,
                    )
                continue
            by_device.setdefault(did, {})[f"{siid}.{piid}"] = value
    return by_device


async def _yield_to_dispatch() -> None:
    """让事件循环把排队的投递跑完。每写一台之后都要调。

    容器的待投递水位闸只报第一条告警，而那个标志只在 `start()` 复位、一个进程只
    复位一次。一口气写完再让出的话，待投递量会堆成「设备数 × 属性数」，启动就把这
    一次告警烧掉，日志还写着「订阅方卡住了」—— 而这时一个订阅方都没有。烧掉之后
    真有订阅方卡住时就再也不会有日志。顺带让收尾行的 `stats()` 读到投递后的数。
    """
    await asyncio.sleep(0)


async def _write_online_flags(
    store: StateStore, meta: dict[str, _DeviceMeta], samples: _Samples
) -> None:
    """每台设备都写在线标志，离线的也写。

    先写标志再写属性：一条属性都读不到的设备也要在容器里留下痕迹。

    两种拒收都要接：抛异常那种在这里到不了（did 进 meta 前已过段校验、值是 bool），
    留着是防御；真会发生的是撞上叶子上限那种，它不抛、只让 `set` 返回假，不看就成了
    静默丢失 —— 尤其在一条属性都读不到的那条早退路径上，收尾行是唯一的信号。
    """
    for did, info in meta.items():
        try:
            landed = store.set(
                f"iot/device/{did}/status/online", info.online, source=SOURCE
            )
        except (TypeError, ValueError) as e:
            if samples.take("online_flag_rejected"):
                logger.warning("align: online flag rejected did=%s: %s", did, e)
            continue
        if not landed and samples.take("online_flag_dropped"):
            logger.warning("align: online flag hit the leaf limit did=%s", did)
        await _yield_to_dispatch()


def _write_device(
    store: StateStore, did: str, props: dict[str, Any], samples: _Samples
) -> int:
    """整台写一次；被容器拒收就退成逐条写，只丢有问题的那几条。

    返回写进去的属性条数。整台写失败时不能连累整台 —— 容器的校验是「整笔不写」，
    一个畸形值会让这台设备一条都进不去。

    容器有两种拒收：校验失败抛异常，撞上叶子上限不抛、只让 `set` 返回假。后者不看
    返回值就会把一条都没进树的量算进返回值。
    """
    try:
        # 名单闸在上游（_read_values 按 meta 丢掉了名单外的行），这里是防御性重校：
        # 真放行一个含 '/' 的 did，路径会被劈成两段
        validate_segment(did)
    except (TypeError, ValueError) as e:
        # 与请求侧分开记：桥接子设备是常态且成批，共用额度会把云端异常这条挤掉
        if samples.take("bad_did_in_response"):
            logger.warning("align: skip did %r from response: %s", did, e)
        return 0

    path = f"iot/device/{did}/prop"
    try:
        if store.set(path, props, source=SOURCE):
            return len(props)
        if samples.take("leaf_limit"):
            logger.warning(
                "align: batch write hit the leaf limit did=%s; retrying per property",
                did,
            )
    except (TypeError, ValueError) as e:
        if samples.take("batch_rejected"):
            logger.warning(
                "align: batch write rejected did=%s (%s); retrying per property", did, e
            )

    written = 0
    for iid, value in props.items():
        try:
            # iid 拼进路径，含 '/' 就会多出一层、值落到别处；整台写那条是容器替我们校的
            validate_segment(iid)
        except (TypeError, ValueError) as e:
            if samples.take("iid_rejected"):
                logger.warning("align: iid rejected did=%s iid=%r: %s", did, iid, e)
            continue
        try:
            if store.set(f"{path}/{iid}", value, source=SOURCE):
                written += 1
        except (TypeError, ValueError) as e:
            if samples.take("value_rejected"):
                logger.warning(
                    "align: value rejected did=%s iid=prop.%s type=%s: %s",
                    did,
                    iid,
                    type(value).__name__,
                    e,
                )
    return written


async def align_iot_state(store: StateStore, miot_proxy: Any) -> bool:
    """拉一遍在线设备的可读属性写进容器。任何异常都只记日志，不往外抛。

    返回这一轮跑完了没有，**不回答读全了没有** —— 部分设备读失败仍算跑完，一台坏
    设备不该卡死等着这个判定的下游。下游拿到真才把当前作用域标成已对齐。
    """
    started = time.monotonic()
    samples = _Samples()
    unreadable: dict[str, int] = {}
    try:
        if not miot_proxy.has_enabled_home():
            # 空作用域没有「已对齐」可言，这条兜住把对齐排在建立启用集之前的顺序错误。
            # 判据是启用集空不空而不是家庭里有几台设备：空家庭的容器本来就该是空的，
            # 那已经对齐了，判成失败会让这一代的门一直关着、后来加的设备也进不来
            logger.warning("align: no home is enabled; nothing to align")
            return False
        params, meta = await _collect_params(miot_proxy, samples)
        await _write_online_flags(store, meta, samples)
        offline = sum(1 for info in meta.values() if not info.online)
        if not params:
            logger.warning(
                "align: no readable properties found; wrote online flags only "
                "(devices=%s offline=%s issues=%s)",
                len(meta),
                offline,
                samples.counts or "none",
            )
            return True
        by_device = await _read_values(miot_proxy, params, meta, unreadable, samples)
        per_device: dict[str, int] = {}
        for did, props in by_device.items():
            per_device[did] = _write_device(store, did, props, samples)
            await _yield_to_dispatch()
        written = sum(per_device.values())
        for did, props in by_device.items():
            logger.debug("align: did=%s values=%s", did, props)
        logger.info(
            "align done: devices=%s offline=%s written_devices=%s requested=%s "
            "written=%s elapsed=%.1fs issues=%s unreadable=%s store=%s",
            len(meta),
            offline,
            sum(1 for count in per_device.values() if count),
            len(params),
            written,
            time.monotonic() - started,
            samples.counts or "none",
            unreadable or "none",
            store.stats(),
        )
        return True
    except Exception as e:
        logger.error(
            "align failed after %.1fs, issues=%s unreadable=%s: %s",
            time.monotonic() - started,
            samples.counts or "none",
            unreadable or "none",
            e,
            exc_info=True,
        )
        return False
