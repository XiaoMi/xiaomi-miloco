# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""「已加载」语义与事件补救点。

去掉周期兜底之后，读取路径只在从未成功加载时自刷新；加载成功（含空结果）之后
靠事件与用户主动刷新补。这里钉住这几条：
- 加载过但为空 → 读取不再回源
- 从未加载 → 读取回源一次
- 换号 → 「已加载」作废，下次读取重新回源
- MQTT 重连 → 设备与相机都补刷（断线期间的事件没有别的补救点）
"""

import asyncio
import types

from miloco.miot.client import MiotProxy


def _getter(attr, refresh_name, loaded_attr):
    calls = []

    async def refresh():
        calls.append(refresh_name)

    proxy = types.SimpleNamespace(
        **{
            attr: {},
            loaded_attr: False,
            "is_operational": True,
            refresh_name: refresh,
        }
    )
    return proxy, calls


class TestLoadedSemantics:
    def _run(self, method, proxy):
        return asyncio.run(getattr(MiotProxy, method)(proxy))

    def test_empty_but_loaded_does_not_refetch(self):
        for attr, refresh_name, loaded_attr, method in [
            ("_device_info_dict", "refresh_devices", "_devices_loaded", "get_devices"),
            ("_scene_info_dict", "refresh_scenes", "_scenes_loaded", "get_all_scenes"),
        ]:
            proxy, calls = _getter(attr, refresh_name, loaded_attr)
            setattr(proxy, loaded_attr, True)  # 加载成功但结果为空
            self._run(method, proxy)
            self._run(method, proxy)
            assert calls == [], f"{method} 加载过之后不该回源"

    def test_never_loaded_refetches_once(self):
        proxy, calls = _getter("_device_info_dict", "refresh_devices", "_devices_loaded")
        self._run("get_devices", proxy)
        assert calls == ["refresh_devices"]

    def test_user_info_empty_but_loaded_does_not_refetch(self):
        calls = []

        async def refresh_user_info():
            calls.append("refresh_user_info")

        proxy = types.SimpleNamespace(
            _user_info=None,
            _user_loaded=True,
            is_operational=True,
            refresh_user_info=refresh_user_info,
        )
        asyncio.run(MiotProxy.get_user_info(proxy))
        assert calls == []

    def test_mark_info_stale_resets_flags_and_caches(self):
        proxy = types.SimpleNamespace(
            _camera_info_dict={"c": 1},
            _device_info_dict={"d": 1},
            _scene_info_dict={"s": 1},
            _user_info=object(),
            _cameras_loaded=True,
            _devices_loaded=True,
            _scenes_loaded=True,
            _user_loaded=True,
            _camera_retry_at=123.0,
        )
        MiotProxy.mark_info_stale(proxy)
        assert proxy._cameras_loaded is False
        assert proxy._devices_loaded is False
        assert proxy._scenes_loaded is False
        assert proxy._user_loaded is False
        assert proxy._camera_retry_at == 0.0
        # 旧账号的数据也不该留在缓存里
        assert proxy._camera_info_dict == {}
        assert proxy._device_info_dict == {}
        assert proxy._scene_info_dict == {}
        assert proxy._user_info is None


class TestMipsReconnect:
    def test_reconnect_refreshes_devices_and_cameras(self):
        calls = []

        async def refresh_devices():
            calls.append("devices")

        async def refresh_cameras():
            calls.append("cameras")

        proxy = types.SimpleNamespace(
            refresh_devices=refresh_devices,
            refresh_cameras=refresh_cameras,
            _mips_connect_listeners=[],
        )
        asyncio.run(MiotProxy._on_mips_connected(proxy))
        # 相机必须一起刷：断线期间新绑的相机不会再有推送。
        assert calls == ["devices", "cameras"]
