# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot_client.py.
"""
import asyncio
import base64
import json
import logging
import os
import time
from typing import Dict, List
import aiofiles
import pytest

from miot.camera import MIoTCamera, MIoTCameraInstance
from miot.storage import MIoTStorage
from miot.types import MIoTCameraInfo, MIoTCameraStatus, MIoTCameraVideoQuality

# pylint: disable=import-outside-toplevel, unused-argument, missing-function-docstring
_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_camera_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    """Single camera test."""
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )

    version: str = await miot_camera.get_camera_version_async()
    _LOGGER.info("libmiot_camera.so version: %s", version)

    cameras = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0, "no cameras found"
    # create camera instance.
    camera_info = MIoTCameraInfo(** list(cameras.values())[0])
    _LOGGER.info("camera_info: %s", camera_info)
    camera_ins: MIoTCameraInstance = await miot_camera.create_camera_async(camera_info=camera_info)
    # start camera.
    await camera_ins.start_async(qualities=MIoTCameraVideoQuality.HIGH, enable_reconnect=True)

    async def on_status_changed_async(did: str, status: MIoTCameraStatus):
        _LOGGER.info("on_status_changed: %s, %s", did, status)
    await camera_ins.register_status_changed_async(callback=on_status_changed_async)

    async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int):
        _LOGGER.info("on_raw_video: %s, %d, %d, %d, %d", did, channel, len(data), ts, seq)
    await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=0)

    async def on_raw_audio_async(did: str, data: bytes, ts: int, seq: int, channel: int):
        _LOGGER.info("on_raw_audio: %s, %d, %d, %d, %d", did, channel, len(data), ts, seq)
    await camera_ins.register_raw_audio_async(callback=on_raw_audio_async, channel=0)

    image_path = os.path.join(test_cache_path, "camera_jpg")
    os.makedirs(image_path, exist_ok=True)

    async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
        _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
        async with aiofiles.open(os.path.join(image_path, f"./camera_jpg_{did}_{channel}.jpg"), mode="wb") as f:
            await f.write(data)
    await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=0)

    while True:
        await asyncio.sleep(60)
        _LOGGER.info("get camera status: %s", await camera_ins.get_status_async())

    await camera_ins.stop_async()
    await miot_camera.destroy_camera_async(did=camera_info.did)
    await miot_camera.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_camera_with_destroy_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    """Single camera test."""
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    image_path = os.path.join(test_cache_path, "camera_jpg")
    os.makedirs(image_path, exist_ok=True)

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )
    cameras = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0, "no cameras found"

    for _ in range(3):
        camera_ins_list: List[MIoTCameraInstance] = []
        tasks = []

        # create camera instance.
        for info in list(cameras.values()):
            camera_info = MIoTCameraInfo(** info)
            _LOGGER.info("camera_info: %s", camera_info)

            camera_ins: MIoTCameraInstance = await miot_camera.create_camera_async(camera_info=camera_info)
            camera_ins_list.append(camera_ins)

            async def on_status_changed_async(did: str, status: MIoTCameraStatus):
                _LOGGER.info("on_status_changed: %s, %s", did, status)
            await camera_ins.register_status_changed_async(callback=on_status_changed_async)

            async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
                _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
                async with aiofiles.open(os.path.join(image_path, f"./camera_jpg_{did}_{channel}.jpg"), mode="wb") as f:
                    await f.write(data)
            await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=0)

            tasks.append(camera_ins.start_async(qualities=MIoTCameraVideoQuality.LOW, enable_reconnect=True))
        # start cameras.
        await asyncio.gather(*tasks)

        await asyncio.sleep(6)

        # stop cameras.
        tasks = []
        for camera_ins in camera_ins_list:
            tasks.append(miot_camera.destroy_camera_async(did=camera_ins.camera_info.did))
        await asyncio.gather(*tasks)

    await miot_camera.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_camera_get_image_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    """Single camera test."""
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )

    cameras = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0, "no cameras found"
    # create camera instance.
    camera_info = MIoTCameraInfo(** list(cameras.values())[0])
    _LOGGER.info("camera_info: %s", camera_info)
    camera_ins: MIoTCameraInstance = await miot_camera.create_camera_async(camera_info=camera_info)
    # start camera.
    await camera_ins.start_async(qualities=MIoTCameraVideoQuality.LOW)

    async def on_status_changed_async(did: str, status: MIoTCameraStatus):
        _LOGGER.info("on_status_changed: %s, %s", did, status)
    await camera_ins.register_status_changed_async(callback=on_status_changed_async)

    # async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int):
    #     _LOGGER.info(
    #         "on_raw_video: %s, %d, %d, %d, %d", did, channel, len(data), ts, seq)
    # await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=0)

    async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
        _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
    await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=0)

    while True:
        await asyncio.sleep(3)
        _LOGGER.info("camera status: %s", await camera_ins.get_status_async())
        # get image
        # image = await camera_ins.get_image_async()

    await camera_ins.stop_async()
    await miot_camera.destroy_camera_async(did=camera_info["did"])
    await miot_camera.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_camera_with_detect_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    """Single camera test."""
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )

    cameras = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0, "no cameras found"
    # create camera instance.
    camera_info = MIoTCameraInfo(** list(cameras.values())[0])
    _LOGGER.info("camera_info: %s", camera_info)
    camera_ins: MIoTCameraInstance = await miot_camera.create_camera_async(camera_info=camera_info)
    # start camera.
    await camera_ins.start_async(qualities=MIoTCameraVideoQuality.LOW)

    async def on_status_changed_async(did: str, status: MIoTCameraStatus):
        _LOGGER.info("on_status_changed: %s, %s", did, status)
    await camera_ins.register_status_changed_async(callback=on_status_changed_async)

    async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int):
        _LOGGER.info("on_raw_video: %s, %d, %d, %d, %d", did, channel, len(data), ts, seq)
    await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=0)
    # await camera_ins.unregister_raw_video_async(channel=1)

    async def on_raw_audio_async(did: str, data: bytes, ts: int, seq: int, channel: int):
        _LOGGER.info("on_raw_audio: %s, %d, %d, %d, %d", did, channel, len(data), ts, seq)
    await camera_ins.register_raw_audio_async(callback=on_raw_audio_async, channel=0)

    # yolo = YoloClassify(model_name="yolo11n-pose.pt")  #

    async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
        _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
        async with aiofiles.open(f"./camera_jpg_{did}_{channel}.jpg", mode="wb") as f:
            await f.write(data)

        # result = await yolo.classify_async(image=data)
        # boxes = result.boxes  # 边界框对象
        # # 类别ID、置信度值
        # class_ids = boxes.cls.tolist()       # 类别索引列表
        # confidences = boxes.conf.tolist()    # 置信度列表
        # # 映射类别名称
        # class_names = [result.names[int(cls)] for cls in class_ids]
        # # 输出结果
        # reg_result = {}
        # for name, conf in zip(class_names, confidences):
        #     if name in reg_result and conf < reg_result[name]:
        #         continue
        #     reg_result[name] = conf
        #     # _LOGGER.info(f"检测到：{name}，置信度：{conf:.2f}")
        # _LOGGER.info("YOLO classify result: %s", reg_result)

    await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=0)
    # await camera_ins.unregister_decode_jpg_async(channel=1)

    while True:
        await asyncio.sleep(3)
        # await camera_ins.unregister_raw_video_async(channel=0)
        # await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=1)
        # await asyncio.sleep(3)
        # await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=0)
        # await camera_ins.unregister_raw_video_async(channel=1)

        _LOGGER.info("camera status: %s", await camera_ins.get_status_async())

    await camera_ins.stop_async()
    await miot_camera.destroy_camera_async(did=camera_info["did"])
    await miot_camera.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_camera_save_jpg_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )

    cameras = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0, "no cameras found"
    # create camera instance.
    camera_info = MIoTCameraInfo(** list(cameras.values())[3])
    _LOGGER.info("camera_info: %s", camera_info)
    camera_ins: MIoTCameraInstance = await miot_camera.create_camera_async(
        camera_info=camera_info, frame_interval=1000
    )
    # start camera.
    await camera_ins.start_async(qualities=MIoTCameraVideoQuality.HIGH, pin_code="0000")

    async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
        _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
        img_name = f"./image/jpg_{did}_{channel}_{int(time.time()*1000)}.jpg"
        async with aiofiles.open(img_name, mode="wb") as f:
            await f.write(data)
        _LOGGER.info("saved jpg to %s", img_name)

    await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=0)

    os.makedirs("./image", exist_ok=True)

    while True:
        await asyncio.to_thread(input)

    await camera_ins.stop_async()
    await miot_camera.destroy_camera_async(did=camera_info["did"])
    await miot_camera.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_multi_camera_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_uuid, type_=str)
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_camera = MIoTCamera(
        cloud_server=test_cloud_server,
        access_token=oauth_info["access_token"],
        loop=asyncio.get_event_loop()
    )

    cameras = await miot_storage.load_async(domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict)
    assert isinstance(cameras, Dict) and len(cameras) > 0

    cameras_ins: Dict[str, MIoTCameraInstance] = {}
    for did, camera_info in cameras.items():
        camera_ins = await miot_camera.create_camera_async(camera_info=camera_info)
        cameras_ins[did] = camera_ins

        for channel in range(camera_info["channel_count"]):
            async def on_status_changed_async(did: str, status: MIoTCameraStatus):
                _LOGGER.info("on_status_changed: %s, %s", did, status)
            await camera_ins.register_status_changed_async(callback=on_status_changed_async)

            async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int):
                _LOGGER.info("on_decode_jpg: %s, %d, %d, %d", did, channel, ts, len(data))
                async with aiofiles.open(f"./camera_jpg_{did}_{channel}.jpg", mode="wb") as f:
                    await f.write(data)
            await camera_ins.register_decode_jpg_async(callback=on_decode_jpg_async, channel=channel)
            # await camera_ins.unregister_decode_jpg_async(channel=1)

            async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int):
                _LOGGER.info("on_raw_video: %s, %d, %d, %d, %d", did, channel, ts, seq, len(data))
            await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=channel)
            # await camera_ins.unregister_raw_video_async(channel=1)

        await camera_ins.start_async(qualities=MIoTCameraVideoQuality.LOW)

    while True:
        await asyncio.sleep(3)
        # await camera_ins.unregister_raw_video_async(channel=0)
        # await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=1)
        # await asyncio.sleep(3)
        # await camera_ins.register_raw_video_async(callback=on_raw_video_async, channel=0)
        # await camera_ins.unregister_raw_video_async(channel=1)

    for camera_ins in cameras_ins.values():
        await camera_ins.stop_async()
        await miot_camera.destroy_camera_async(did=camera_ins.camera_info.did)
        await miot_camera.deinit_async()
