# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot_client.py.
"""

import asyncio
import logging
import os
import time
import webbrowser
from typing import Dict, Optional

import aiofiles
import pytest
from miot.camera import MIoTCameraInstance
from miot.client import MIoTClient
from miot.storage import MIoTStorage
from miot.types import MIoTCameraVideoQuality, MIoTOauthInfo, MIoTUserInfo
from pydantic_core import to_jsonable_python

_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_oauth_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_uuid: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    local_uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    uuid = str(local_uuid or test_uuid)
    _LOGGER.info("uuid: %s", uuid)

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        cloud_server=test_cloud_server,
        oauth_info=None,
    )
    await miot_client.init_async()

    oauth_info: Optional[MIoTOauthInfo] = None
    load_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    if (
        isinstance(load_info, dict)
        and "access_token" in load_info
        and "expires_ts" in load_info
        and load_info["expires_ts"] > int(time.time())
    ):
        _LOGGER.info("load oauth info, %s", load_info)
        oauth_info = MIoTOauthInfo(**load_info)
    if oauth_info is None:
        # gen oauth url
        auth_url: str = await miot_client.gen_oauth_url_async()
        assert isinstance(auth_url, str)
        _LOGGER.info("auth url: %s", auth_url)
        # get code
        webbrowser.open(auth_url)
        code: str = input("input code: ")
        assert code is not None
        state: str = input("input state: ")
        assert state is not None
        # get access_token
        res_obj = await miot_client.get_access_token_async(code=code, state=state)
        assert res_obj is not None
        oauth_info = res_obj
        _LOGGER.info("get_access_token result: %s", res_obj)
        rc = await miot_storage.save_async(
            domain=test_domain_cloud_cache,
            name=test_name_oauth2_info,
            data=oauth_info.model_dump(),
        )
        assert rc
        _LOGGER.info("saved oauth info")
        rc = await miot_storage.save_async(
            test_domain_cloud_cache, test_name_uuid, uuid
        )
        assert rc
        _LOGGER.info("saved uuid")

    access_token = oauth_info.access_token
    assert isinstance(access_token, str)
    _LOGGER.info("access_token: %s", access_token)
    refresh_token = oauth_info.refresh_token
    assert isinstance(refresh_token, str)
    _LOGGER.info("refresh_token: %s", refresh_token)

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency(on=["test_miot_oauth_async"])
async def test_miot_oauth_refresh_token(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict)
    oauth_info = MIoTOauthInfo(**oauth_info)
    remaining_time = oauth_info.expires_ts - int(time.time())
    _LOGGER.info("token remaining valid time: %ss", remaining_time)
    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        cloud_server=test_cloud_server,
        oauth_info=oauth_info,
    )
    await miot_client.init_async()

    update_info = await miot_client.refresh_access_token_async(
        refresh_token=oauth_info.refresh_token
    )
    assert update_info
    remaining_time = update_info.expires_ts - int(time.time())
    assert remaining_time > 0
    _LOGGER.info("refresh token, remaining valid time: %ss", remaining_time)
    # Save oauth2 info
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_oauth2_info,
        data=update_info.model_dump(),
    )
    assert rc
    _LOGGER.info("refresh token success, %s", update_info)

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_user_info_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_user_info: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict)
    oauth_info = MIoTOauthInfo(**oauth_info)

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get User info.
    user_info = await miot_client.get_user_info_async()
    assert isinstance(user_info, MIoTUserInfo)
    _LOGGER.info("your account info: %s", user_info)
    # Save user info.
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_user_info,
        data=user_info.model_dump(),
    )
    assert rc

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_homes_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_homes: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict)

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get homes info.
    homes = await miot_client.get_homes_async(fetch_share_home=False)
    assert isinstance(homes, Dict)
    _LOGGER.info("your home info: %s", homes)
    # Save homes info.
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_homes,
        data=to_jsonable_python(homes),
    )
    assert rc
    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_devices_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_devices: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get devices info.
    devices = await miot_client.get_devices_async()
    assert isinstance(devices, Dict)
    _LOGGER.info("your devices: %s", devices)
    # Save devices info.
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_devices,
        data=to_jsonable_python(devices),
    )
    assert rc
    _LOGGER.info("save devices info success")

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_cameras_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get cameras.
    cameras = await miot_client.get_cameras_async()
    assert isinstance(cameras, Dict)
    _LOGGER.info("your cameras1: %s", cameras)
    # Test icon cache.
    cameras = await miot_client.get_cameras_async()
    assert isinstance(cameras, Dict)
    _LOGGER.info("your cameras2: %s", cameras)

    # Save cameras info.
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_cameras,
        data=to_jsonable_python(cameras),
    )
    assert rc
    _LOGGER.info("save cameras info success")

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_manual_scenes_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,  # type: ignore
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get manual scenes info.
    manual_scenes = await miot_client.get_manual_scenes_async()
    assert isinstance(manual_scenes, Dict)
    _LOGGER.info("your manual scenes: %s", manual_scenes)
    # Run test manual scene1.
    manual_result = await miot_client.run_manual_scene_async(
        scene_info=list(manual_scenes.values())[0]
    )
    _LOGGER.info("run manual scene1 result: %s", manual_result)
    # Run test manual scene2.
    manual_result = await miot_client.run_manual_scene_async(
        scene_info=list(manual_scenes.values())[1]
    )
    _LOGGER.info("run manual scene2 result: %s", manual_result)

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_app_notify_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,  # type: ignore
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Send app notify.
    notify_id = await miot_client.create_app_notify_async(
        text="this is a test app notify"
    )
    result = await miot_client.send_app_notify_async(notify_id=notify_id)
    assert isinstance(result, bool)
    _LOGGER.info("send app notify result: %s", result)
    result = await miot_client.delete_app_notifies_async(notify_ids=[notify_id])
    assert isinstance(result, bool)
    _LOGGER.info("delete app notify result: %s", result)

    result = await miot_client.send_app_notify_once_async("are you ok?")
    assert isinstance(result, bool)
    _LOGGER.info("send app notify once result: %s", result)

    await miot_client.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_miot_camera_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_oauth2_redirect_uri: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_uuid: str,
    test_name_cameras: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    uuid = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_uuid, type_=str
    )
    assert isinstance(uuid, str)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, Dict) and "access_token" in oauth_info

    miot_client: MIoTClient = MIoTClient(
        uuid=uuid,
        redirect_uri=test_oauth2_redirect_uri,
        oauth_info=oauth_info,
        cloud_server=test_cloud_server,
    )
    await miot_client.init_async()

    # Get cameras list.
    cameras = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_cameras, type_=dict
    )
    assert isinstance(cameras, Dict)

    image_path = os.path.join(test_cache_path, "camera_jpg")
    os.makedirs(image_path, exist_ok=True)

    for camera in cameras.values():
        camera_ins: MIoTCameraInstance = await miot_client.create_camera_instance_async(
            camera_info=camera
        )
        await camera_ins.start_async(qualities=MIoTCameraVideoQuality.LOW)

        for channel in range(camera["channel_count"]):

            async def on_jpeg_data(
                did: str, data: bytes, ts: int, channel: int
            ) -> None:
                _LOGGER.info("on_jpeg_data: %s, %d, %d", did, ts, len(data))
                async with aiofiles.open(
                    os.path.join(image_path, f"./camera_jpg_{did}_{channel}.jpg"),
                    mode="wb",
                ) as f:
                    await f.write(data)

            await camera_ins.register_decode_jpg_async(
                callback=on_jpeg_data, channel=channel
            )

            async def on_raw_stream(
                did: str, data: bytes, ts: int, seq: int, channel: int
            ) -> None:
                _LOGGER.info(
                    "on_raw_stream: % s-%d, % d, % d", did, channel, ts, len(data)
                )

            await camera_ins.register_raw_video_async(
                callback=on_raw_stream, channel=channel
            )

    while True:
        await asyncio.sleep(1)

    await camera_ins.stop_async()
    await camera_ins.unregister_decode_jpg_async()
    await camera_ins.unregister_raw_stream_async()

    await miot_client.deinit_async()
