# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""场景联动动态 TTS：可播报设备筛选 + play-text 解析 + speak 下发。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.miot.voice import (
    clamp_tts,
    filter_voice_devices,
    find_voice_devices,
    is_voice_device,
    resolve_play_text,
    speak,
)

HOME = "home-1"


def _dev(
    did="spk-1",
    urn="urn:miot-spec-v2:device:speaker:0000A015:xiaomi-lx06:1",
    home_id=HOME,
    name="客厅音箱",
    online=True,
):
    return SimpleNamespace(
        did=did,
        urn=urn,
        home_id=home_id,
        name=name,
        online=online,
        room_name="客厅",
        model="xiaomi.wifispeaker.lx06",
    )


def _kv(homes=(HOME,)):
    kv = MagicMock()
    kv.get = MagicMock(side_effect=lambda key, default=None: default)
    from miloco.database.kv_repo import ScopeConfigKeys

    kv.get = MagicMock(
        side_effect=lambda key, default=None: (
            f'["{homes[0]}"]' if key == ScopeConfigKeys.HOME_WHITE_LIST_KEY else default
        )
    )
    return kv


def test_is_voice_device_by_urn_category():
    assert is_voice_device(_dev())
    assert is_voice_device(_dev(urn="urn:miot-spec-v2:device:screen:0000A019:x:1"))
    assert not is_voice_device(
        _dev(urn="urn:miot-spec-v2:device:camera:0000A01C:xiaomi-cw500:1")
    )
    assert not is_voice_device(SimpleNamespace(urn=""))


def test_find_and_filter_voice_devices():
    devices = {
        "spk-1": _dev(),
        "cam-1": _dev(did="cam-1", urn="urn:miot-spec-v2:device:camera:0000A01C:x:1"),
    }
    assert set(find_voice_devices(devices)) == {"spk-1"}
    kv = _kv()
    # filter_voice_devices 只做家庭过滤（口径同 filter_by_home）；品类筛选由调用方
    # 先经 find_voice_devices 做（proxy.get_voice_devices 即如此），两层组合起来才只留音箱。
    assert set(filter_voice_devices(kv, find_voice_devices(devices))) == {"spk-1"}
    kv_other_home = _kv(homes=("home-2",))
    assert filter_voice_devices(kv_other_home, devices) == {}


def test_resolve_play_text_picks_lowest_siid():
    spec = {
        "prop.2.1": {"type_name": "on"},
        "action.6.1": {"type_name": "execute-text-directive"},
        "action.5.1": {"type_name": "play-text"},
        "action.5.2": {"type_name": "play-text"},
        "action.4.1": {"type_name": "play-radio"},
    }
    assert resolve_play_text(spec) == "action.5.1"


def test_resolve_play_text_none_when_absent():
    """只有 execute-text-directive（小爱指令）不算逐字播报。"""
    spec = {"action.6.1": {"type_name": "execute-text-directive"}}
    assert resolve_play_text(spec) is None


def test_clamp_tts_flattens_and_truncates():
    assert clamp_tts("  第一行\n第二行  ") == "第一行 第二行"
    assert len(clamp_tts("夸" * 500)) == 200


@pytest.mark.asyncio
async def test_speak_resolves_play_text_and_writes_ledger():
    proxy = MagicMock()
    proxy.get_voice_devices = AsyncMock(return_value={"spk-1": _dev()})
    proxy._fetch_device_spec = AsyncMock(
        return_value={"action.5.1": {"type_name": "play-text"}}
    )
    proxy.call_device_action = AsyncMock(return_value={"code": 0})
    with patch(
        "miloco.miot.service._write_action_ledger", new_callable=AsyncMock
    ) as ledger:
        ok, err = await speak(
            proxy, "spk-1", "今天这身很精神", source="rule", source_id="r1"
        )
    assert ok and err is None
    param = proxy.call_device_action.await_args.args[0]
    assert (param.did, param.siid, param.aiid, param.in_) == (
        "spk-1",
        5,
        1,
        ["今天这身很精神"],
    )
    ledger.assert_awaited_once()
    kwargs = ledger.await_args.kwargs
    assert kwargs["action_type"] == "call_action"
    assert kwargs["source"] == "rule" and kwargs["source_id"] == "r1"
    assert "今天这身很精神" in kwargs["value_json"]


@pytest.mark.asyncio
async def test_speak_unknown_device_fails_softly():
    proxy = MagicMock()
    proxy.get_voice_devices = AsyncMock(return_value={})
    ok, err = await speak(proxy, "spk-x", "你好")
    assert not ok
    assert err and "voice_device_not_found" in err


@pytest.mark.asyncio
async def test_speak_without_play_text_action():
    proxy = MagicMock()
    proxy.get_voice_devices = AsyncMock(return_value={"spk-1": _dev()})
    proxy._fetch_device_spec = AsyncMock(return_value={})
    ok, err = await speak(proxy, "spk-1", "你好")
    assert not ok
    assert err and "play_text_action_not_found" in err
