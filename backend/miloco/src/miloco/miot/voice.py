# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""可播报设备（音箱 / 带喇叭的屏显设备）的筛选与 TTS 下发。

场景联动「动态 TTS」用它把住户选的设备 did 变成一次真正的播报：

- ``filter_voice_devices``：与场景列表同口径，只保留**已启用家庭**内的设备；
- ``resolve_play_text``：按设备 spec 找 ``play-text`` 动作的 ``action.<siid>.<aiid>``。
  不同音箱的 siid/aiid 不一样（有的还叫 ``execute-text-directive``，那个是让小爱
  理解语义再自己执行，不是逐字念出），所以**绝不硬编码** iid，一律从 spec 解析；
- ``speak``：调用设备动作并落 action_ledger（与 ``MiotService.control_device`` 的
  call_action 同一形状，source 标 rule）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from miot.types import MIoTActionParam

from miloco.database.kv_repo import KVRepo
from miloco.miot.filter import filter_by_home
from miloco.miot.result_codes import summarize_results

logger = logging.getLogger(__name__)

# 逐字念出文案的动作名（按优先级）。``execute-text-directive`` 故意不在列表里：
# 它等同对音箱说"嘿小爱，xxx"，会让音箱自己理解并执行，而不是把文案念出来。
PLAY_TEXT_TYPES = ("play-text",)

# TTS 单条文案长度上限（"再说下去就太长了"）。模型偶尔会写一大段，截断到这句
# 不会打断语义，且能挡住异常长文案把播报拖成半分钟。
MAX_TTS_LEN = 200


def _urn_category(urn: str | None) -> str:
    """``urn:miot-spec-v2:device:<category>:<code>:...`` → ``<category>``。"""
    parts = (urn or "").split(":")
    if len(parts) >= 4 and parts[2] == "device":
        return parts[3]
    return ""


def is_voice_device(device: Any) -> bool:
    """粗筛：urn 品类为音箱 / 屏显 / 电视这类通常能播报的设备。

    只是候选集（清单接口不会为每台设备拉 spec），真正能不能播由 ``resolve_play_text``
    看到 spec 后决定；这里多收一点无妨，少收才会让住户在列表里找不到音箱。
    """
    category = _urn_category(getattr(device, "urn", None))
    return category in ("speaker", "screen", "tv", "projector")


def filter_voice_devices(kv_repo: KVRepo, devices: dict[str, Any]) -> dict[str, Any]:
    """按家庭白名单过滤可播报设备（空启用集 → {}，与 ``filter_by_home`` 同语义）。"""
    return filter_by_home(kv_repo, devices)


def find_voice_devices(devices: dict[str, Any]) -> dict[str, Any]:
    """Candidate voice devices → ``{did: device}``（不含家庭过滤）。"""
    return {did: dev for did, dev in (devices or {}).items() if is_voice_device(dev)}


def resolve_play_text(spec: dict[str, dict[str, Any]]) -> str | None:
    """从设备 spec 里找逐字播报动作，返回 ``action.<siid>.<aiid>``。

    spec 形如 ``{"action.5.1": {"type_name": "play-text", "in_params": [...]}}``。
    同名动作有多个时取 siid 最小的（稳定、可预测）。
    """
    hits: list[tuple[int, str]] = []
    for iid, entry in (spec or {}).items():
        if not iid.startswith("action.") or not isinstance(entry, dict):
            continue
        if entry.get("type_name") not in PLAY_TEXT_TYPES:
            continue
        try:
            siid = int(iid.split(".")[1])
        except (IndexError, ValueError):
            continue
        hits.append((siid, iid))
    if not hits:
        return None
    return min(hits)[1]


def clamp_tts(text: str) -> str:
    """归一播报文案：压平换行 + 截断到 ``MAX_TTS_LEN``。"""
    cleaned = " ".join((text or "").split())
    return cleaned[:MAX_TTS_LEN]


async def _fetch_spec(miot_proxy, device: Any) -> dict[str, dict[str, Any]]:
    """取设备 spec（带子设备别名），与 catalog / home_info 共用同一份缓存。"""
    fetch = getattr(miot_proxy, "_fetch_device_spec", None)
    if fetch is None:
        return {}
    from miloco.miot.client import build_sub_device_names

    try:
        names = build_sub_device_names(device)
    except Exception:  # noqa: BLE001
        names = {}
    return await fetch(getattr(device, "urn", "") or "", names or None)


async def speak(
    miot_proxy,
    did: str,
    text: str,
    *,
    source: str = "rule",
    source_id: str | None = None,
) -> tuple[bool, str | None]:
    """让音箱 ``did`` 逐字念出 ``text``。返回 ``(是否成功, 失败原因)``。

    失败一律不抛（调用方在 fire 路径上，播报失败不该打断自动化触发），原因写进
    rule_log 的 action_results.error 里供排查。
    """
    content = clamp_tts(text)
    if not content:
        return False, "empty_tts_text"
    from miloco.miot.service import _write_action_ledger

    try:
        devices = (await miot_proxy.get_voice_devices()) or {}
        device = devices.get(did)
        if device is None:
            return False, f"voice_device_not_found: {did}"
        spec = await _fetch_spec(miot_proxy, device)
        action_iid = resolve_play_text(spec)
        if action_iid is None:
            return False, f"play_text_action_not_found: {did}"
        _, siid, aiid = action_iid.split(".")
        param = MIoTActionParam(did=did, siid=int(siid), aiid=int(aiid), in_=[content])
        result = await miot_proxy.call_device_action(param)
        success, code, msg = summarize_results(result)
        err = None if success else (msg or "tts_failed")
        await _write_action_ledger(
            miot_proxy,
            action_type="call_action",
            did=did,
            iid=action_iid,
            value_json=json.dumps([content], ensure_ascii=False),
            result_code=code,
            result_msg=msg,
            success=success,
            error=err,
            source=source,
            source_id=source_id,
        )
        return success, err
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to speak on device %s: %s", did, e)
        try:
            await _write_action_ledger(
                miot_proxy,
                action_type="call_action",
                did=did,
                iid="scene_tts",
                value_json=json.dumps([content], ensure_ascii=False),
                result_code=None,
                result_msg=None,
                success=False,
                error=str(e),
                source=source,
                source_id=source_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return False, f"exception: {e}"
