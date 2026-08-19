# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""启动时把 iot 设备属性拉一遍写进状态容器。

推送只在值变化时才来，重启后容器是空的、不会自己长回来，所以要主动拉一次。这里只做
「启动跑一次」，上线/重连那条时机还没接。

**离线设备只写在线标志，不拉属性。** 云端给的是缓存里的最后一次上报，可能任意旧，写进去
会把 `last_reported` 刷成当前时刻，而响应不带时间戳、消费方看不出来。整台跳过也不行 ——
容器里没有这台设备，消费方就分不出「离线」和「没接入」。

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

from miloco.miot.result_codes import code_message, is_known_code
from miloco.state import StateStore

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


def _parse_prop_iid(iid: str) -> tuple[int, int] | None:
    parts = iid.split(".")
    if len(parts) != 3 or parts[0] != "prop":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


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
    devices = await miot_proxy.get_devices()
    params: list[MIoTGetPropertyParam] = []
    meta: dict[str, _DeviceMeta] = {}
    for did, device in devices.items():
        if "/" in did:
            # 路径段不许含 '/'（桥接子设备的 did 长这样），连在线标志都写不进去
            if samples.take("did_with_slash"):
                logger.warning("align: skip did with '/': %s", did)
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
            parsed = _parse_prop_iid(iid)
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
    """分批读取，按 did 归拢成 {did: {"<siid>.<piid>": value}}。"""
    by_device: dict[str, dict[str, Any]] = {}
    for start in range(0, len(params), CHUNK_SIZE):
        chunk = params[start : start + CHUNK_SIZE]
        try:
            rows = await miot_proxy.get_device_properties(chunk)
        except Exception as e:
            samples.counts["chunk_failed"] = samples.counts.get("chunk_failed", 0) + 1
            logger.warning(
                "align: chunk read failed offset=%s size=%s: %s", start, len(chunk), e
            )
            continue
        for row in rows:
            did = row.get("did")
            siid, piid = row.get("siid"), row.get("piid")
            if did is None or siid is None or piid is None:
                if samples.take("row_without_ids"):
                    logger.warning("align: row missing did/siid/piid: %s", row)
                continue
            model = meta[did].model if did in meta else "?"
            code = row.get("code", 0)
            if code != 0:
                bucket = f"{code_message(code)}({code}) {model}"
                unreadable[bucket] = unreadable.get(bucket, 0) + 1
                _log_unreadable(did, model, siid, piid, code, samples)
                continue
            if "value" not in row:
                if samples.take("row_without_value"):
                    logger.warning(
                        "align: row has code 0 but no value: did=%s iid=prop.%s.%s",
                        did,
                        siid,
                        piid,
                    )
                continue
            value = row["value"]
            if not isinstance(value, (str, int, float, bool, type(None))):
                # §3.3 那条一直没验证的前提：属性值是否都是标量。非标量会让这条路径从叶子
                # 变子树，或者直接被容器拒收，所以必须逐条报出来
                if samples.take("non_scalar"):
                    logger.warning(
                        "align: non-scalar value did=%s iid=prop.%s.%s type=%s value=%.120r",
                        did,
                        siid,
                        piid,
                        type(value).__name__,
                        value,
                    )
            by_device.setdefault(did, {})[f"{siid}.{piid}"] = value
    return by_device


def _write_online_flags(store: StateStore, meta: dict[str, _DeviceMeta]) -> None:
    """每台设备都写在线标志，离线的也写。

    先写标志再写属性：一条属性都读不到的设备也要在容器里留下痕迹。
    """
    for did, info in meta.items():
        try:
            store.set(f"iot/{did}/online", info.online, source=SOURCE)
        except (TypeError, ValueError) as e:
            logger.warning("align: online flag rejected did=%s: %s", did, e)


def _write_device(
    store: StateStore, did: str, props: dict[str, Any], samples: _Samples
) -> int:
    """整台写一次；被容器拒收就退成逐条写，只丢有问题的那几条。

    返回写进去的属性条数。整台写失败时不能连累整台 —— 容器的校验是「整笔不写」，
    一个畸形值会让这台设备一条都进不去。
    """
    path = f"iot/{did}/prop"
    try:
        store.set(path, props, source=SOURCE)
        return len(props)
    except (TypeError, ValueError) as e:
        logger.warning(
            "align: batch write rejected did=%s (%s); retrying per property", did, e
        )

    written = 0
    for iid, value in props.items():
        try:
            store.set(f"{path}/{iid}", value, source=SOURCE)
            written += 1
        except (TypeError, ValueError) as e:
            if samples.take("write_rejected"):
                logger.warning(
                    "align: value rejected did=%s iid=prop.%s type=%s: %s",
                    did,
                    iid,
                    type(value).__name__,
                    e,
                )
    return written


async def align_iot_state(store: StateStore, miot_proxy: Any) -> None:
    """拉一遍在线设备的可读属性写进容器。任何异常都只记日志，不往外抛。"""
    started = time.monotonic()
    samples = _Samples()
    unreadable: dict[str, int] = {}
    try:
        params, meta = await _collect_params(miot_proxy, samples)
        _write_online_flags(store, meta)
        offline = sum(1 for info in meta.values() if not info.online)
        if not params:
            logger.warning(
                "align: no readable properties found; nothing written (offline=%s)",
                offline,
            )
            return
        by_device = await _read_values(miot_proxy, params, meta, unreadable, samples)
        written = sum(
            _write_device(store, did, props, samples)
            for did, props in by_device.items()
        )
        for did, props in by_device.items():
            logger.debug("align: did=%s values=%s", did, props)
        # 让 loop 转一圈把排队的投递跑掉，否则 stats() 里的 pending 是投递前的瞬时值，
        # 读起来像「订阅方卡住了」
        await asyncio.sleep(0)
        logger.info(
            "align done: devices=%s offline=%s requested=%s written=%s elapsed=%.1fs "
            "issues=%s unreadable=%s store=%s",
            len(by_device),
            offline,
            len(params),
            written,
            time.monotonic() - started,
            samples.counts or "none",
            unreadable or "none",
            store.stats(),
        )
    except Exception as e:
        logger.error(
            "align failed after %.1fs, issues=%s unreadable=%s: %s",
            time.monotonic() - started,
            samples.counts or "none",
            unreadable or "none",
            e,
            exc_info=True,
        )
