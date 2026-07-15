# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot_cloud.py.
"""

import asyncio
import logging
import time
import webbrowser
from typing import Dict, Optional

import pytest
from miot.cloud import MIoTHttpClient, MIoTOAuth2Client
from miot.storage import MIoTStorage
from miot.types import MIoTManualSceneInfo, MIoTOauthInfo, MIoTUserInfo
from pydantic_core import to_jsonable_python

_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_oauth_auth_async(
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
    miot_oauth = MIoTOAuth2Client(
        redirect_uri=test_oauth2_redirect_uri, cloud_server=test_cloud_server, uuid=uuid
    )

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
        auth_url: str = miot_oauth.gen_auth_url()
        assert isinstance(auth_url, str)
        _LOGGER.info("auth url: %s", auth_url)
        # get code
        webbrowser.open(auth_url)
        code: str = input("input code: ")
        assert code is not None
        # get access_token
        res_obj = await miot_oauth.get_access_token_async(code=code)
        assert res_obj is not None
        oauth_info = res_obj
        _LOGGER.info("get_access_token result: %s", res_obj)
        rc = await miot_storage.save_async(
            domain=test_domain_cloud_cache,
            name=test_name_oauth2_info,
            data=oauth_info.model_dump(exclude_none=True),
        )
        assert rc
        _LOGGER.info("save oauth info")
        rc = await miot_storage.save_async(
            test_domain_cloud_cache, test_name_uuid, uuid
        )
        assert rc
        _LOGGER.info("save uuid")

    access_token = oauth_info.access_token
    assert isinstance(access_token, str)
    _LOGGER.info("access_token: %s", access_token)
    refresh_token = oauth_info.refresh_token
    assert isinstance(refresh_token, str)
    _LOGGER.info("refresh_token: %s", refresh_token)

    await miot_oauth.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency(on=["test_oauth_auth_async"])
async def test_oauth_refresh_token_async(
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
    assert isinstance(oauth_info, dict)
    oauth_info = MIoTOauthInfo(**oauth_info)
    remaining_time = oauth_info.expires_ts - int(time.time())
    _LOGGER.info("token remaining valid time: %ss", remaining_time)
    # Refresh token
    miot_oauth = MIoTOAuth2Client(
        redirect_uri=test_oauth2_redirect_uri, cloud_server=test_cloud_server, uuid=uuid
    )
    refresh_token = oauth_info.refresh_token
    assert refresh_token
    update_info = await miot_oauth.refresh_access_token_async(
        refresh_token=refresh_token
    )
    assert update_info
    remaining_time = update_info.expires_ts - int(time.time())
    assert remaining_time > 0
    _LOGGER.info("refresh token, remaining valid time: %ss", remaining_time)
    # Save oauth2 info
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_oauth2_info,
        data=update_info.model_dump(exclude_none=True),
    )
    assert rc
    _LOGGER.info("refresh token success, %s", update_info)
    # NOTICE: sleep 1s
    await asyncio.sleep(1)

    await miot_oauth.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_user_info_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Get nickname
    user_info = await miot_http.get_user_info_async()
    assert isinstance(user_info, MIoTUserInfo)
    _LOGGER.info("your info: %s", user_info)

    await miot_http.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_homes_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_homes: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Get homeinfos
    homeinfos = await miot_http.get_homes_async()
    assert isinstance(homeinfos, dict)
    _LOGGER.info("your homeinfos: %s", homeinfos)
    # Storage homeinfos
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_homes,
        data=to_jsonable_python(homeinfos),
    )
    assert rc

    await miot_http.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_devices_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_devices: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Get devices
    devices = await miot_http.get_devices_async()
    assert isinstance(devices, dict)
    _LOGGER.info("your devices count: %s", len(devices))
    # _LOGGER.info("your devices: %s", devices)
    # Storage devices
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_devices,
        data=to_jsonable_python(devices),
    )
    assert rc

    await miot_http.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_devices_with_dids_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_devices: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Load devices
    local_devices = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_devices, type_=dict
    )
    assert isinstance(local_devices, dict)
    did_list = list(local_devices.keys())
    assert len(did_list) > 0
    # Get device with dids
    test_list = did_list[:6]
    devices_info = await miot_http.get_devices_with_dids_async(dids=test_list)
    assert isinstance(devices_info, dict)
    _LOGGER.info("test did list, %s, %s", len(test_list), test_list)
    _LOGGER.info("test result: %s, %s", len(devices_info), list(devices_info.keys()))
    _LOGGER.info("your devices: %s", devices_info)

    await miot_http.deinit_async()


# @pytest.mark.asyncio
# @pytest.mark.dependency()
# async def test_miot_cloud_get_prop_async(
#     test_cache_path: str,
#     test_cloud_server: str,
#     test_domain_cloud_cache: str,
#     test_name_oauth2_info: str,
#     test_name_devices: str
# ):
#     miot_storage = MIoTStorage(test_cache_path)
#     oauth_info = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
#     assert isinstance(oauth_info, dict) and "access_token" in oauth_info
#     miot_http = MIoTHttpClient(
#         cloud_server=test_cloud_server,
#         access_token=oauth_info["access_token"])

#     # Load devices
#     local_devices = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_devices, type_=dict)
#     assert isinstance(local_devices, dict)
#     did_list = list(local_devices.keys())
#     assert len(did_list) > 0
#     # Get prop
#     test_list = did_list[:6]
#     for did in test_list:
#         prop_value = await miot_http.get_prop_async(did=did, siid=2, piid=1)
#         device_name = local_devices[did]["name"]
#         _LOGGER.info("%s(%s), prop.2.1: %s", device_name, did, prop_value)

#     await miot_http.deinit_async()


# @pytest.mark.asyncio
# @pytest.mark.dependency()
# async def test_miot_cloud_get_props_async(
#     test_cache_path: str,
#     test_cloud_server: str,
#     test_domain_cloud_cache: str,
#     test_name_oauth2_info: str,
#     test_name_devices: str
# ):

#     miot_storage = MIoTStorage(test_cache_path)
#     oauth_info = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
#     assert isinstance(oauth_info, dict) and "access_token" in oauth_info
#     miot_http = MIoTHttpClient(
#         cloud_server=test_cloud_server,
#         access_token=oauth_info["access_token"])

#     # Load devices
#     local_devices = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_devices, type_=dict)
#     assert isinstance(local_devices, dict)
#     did_list = list(local_devices.keys())
#     assert len(did_list) > 0
#     # Get props
#     test_list = did_list[:6]
#     prop_values = await miot_http.get_props_async(params=[
#         {"did": did, "siid": 2, "piid": 1} for did in test_list])

#     _LOGGER.info("test did list, %s, %s", len(test_list), test_list)
#     _LOGGER.info("test result, %s, %s", len(prop_values), prop_values)

#     await miot_http.deinit_async()


# @pytest.mark.skip(reason="skip danger operation")
# @pytest.mark.asyncio
# @pytest.mark.dependency()
# async def test_miot_cloud_set_prop_async(
#     test_cache_path: str,
#     test_cloud_server: str,
#     test_domain_cloud_cache: str,
#     test_name_oauth2_info: str,
#     test_name_devices: str
# ):
#     """
#     WARNING: This test case will control the actual device and is not enabled
#     by default. You can uncomment @pytest.mark.skip to enable it.
#     """

#     miot_storage = MIoTStorage(test_cache_path)
#     oauth_info = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
#     assert isinstance(oauth_info, dict) and "access_token" in oauth_info
#     miot_http = MIoTHttpClient(
#         cloud_server=test_cloud_server,
#         access_token=oauth_info["access_token"])

#     # Load devices
#     local_devices = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_devices, type_=dict)
#     assert isinstance(local_devices, dict)
#     assert len(local_devices) > 0
#     # Set prop
#     # Find central hub gateway, control its indicator light switch
#     # You can replace it with the device you want to control.
#     test_did = ""
#     for did, dev in local_devices.items():
#         if dev["model"] == "xiaomi.gateway.hub1":
#             test_did = did
#             break
#     assert test_did != "", "no central hub gateway found"
#     result = await miot_http.set_prop_async(params=[{
#         "did": test_did, "siid": 3, "piid": 1, "value": False}])
#     _LOGGER.info("test did, %s, prop.3.1=False -> %s", test_did, result)
#     await asyncio.sleep(1)
#     result = await miot_http.set_prop_async(params=[{
#         "did": test_did, "siid": 3, "piid": 1, "value": True}])
#     _LOGGER.info("test did, %s, prop.3.1=True -> %s", test_did, result)

#     await miot_http.deinit_async()


# @pytest.mark.skip(reason="skip danger operation")
# @pytest.mark.asyncio
# @pytest.mark.dependency()
# async def test_miot_cloud_action_async(
#     test_cache_path: str,
#     test_cloud_server: str,
#     test_domain_cloud_cache: str,
#     test_name_oauth2_info: str,
#     test_name_devices: str
# ):
#     """
#     WARNING: This test case will control the actual device and is not enabled
#     by default. You can uncomment @pytest.mark.skip to enable it.
#     """

#     miot_storage = MIoTStorage(test_cache_path)
#     oauth_info = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict)
#     assert isinstance(oauth_info, dict) and "access_token" in oauth_info
#     miot_http = MIoTHttpClient(
#         cloud_server=test_cloud_server,
#         access_token=oauth_info["access_token"])

#     # Load devices
#     local_devices = await miot_storage.load_async(
#         domain=test_domain_cloud_cache, name=test_name_devices, type_=dict)
#     assert isinstance(local_devices, dict)
#     assert len(local_devices) > 0
#     # Action
#     # Find central hub gateway, trigger its virtual events
#     # You can replace it with the device you want to control.
#     test_did = ""
#     for did, dev in local_devices.items():
#         if dev["model"] == "xiaomi.gateway.hub1":
#             test_did = did
#             break
#     assert test_did != "", "no central hub gateway found"
#     result = await miot_http.action_async(
#         did=test_did, siid=4, aiid=1,
#         in_list=[{"piid": 1, "value": "hello world."}])
#     _LOGGER.info("test did, %s, action.4.1 -> %s", test_did, result)

#     await miot_http.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_get_manual_scene_list_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_manual_scene_list: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Get manual scene list
    manual_scene_list = await miot_http.get_manual_scenes_async()
    assert isinstance(manual_scene_list, Dict)
    _LOGGER.info("your manual scene list: %s", manual_scene_list)
    # Save manual scene list
    rc = await miot_storage.save_async(
        domain=test_domain_cloud_cache,
        name=test_name_manual_scene_list,
        data=to_jsonable_python(value=manual_scene_list),
    )
    assert rc
    _LOGGER.info("save manual scene list success")

    await miot_http.deinit_async()


@pytest.mark.skip(reason="skip danger operation")
@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_run_manual_scene_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
    test_name_manual_scene_list: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Load manual scene list
    manual_scene_list = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_manual_scene_list, type_=dict
    )
    assert isinstance(manual_scene_list, Dict)
    assert len(manual_scene_list) > 0
    _LOGGER.info("your manual scene list: %s", manual_scene_list)

    # Run all manual scene.
    for scene_id, scene_info in manual_scene_list.items():
        manual_scene = MIoTManualSceneInfo(**scene_info)
        result = await miot_http.run_manual_scene_async(scene_info=manual_scene)
        _LOGGER.info(
            "run manual scene %s(%s): %s", manual_scene.scene_name, scene_id, result
        )

    await miot_http.deinit_async()


@pytest.mark.asyncio
@pytest.mark.dependency()
async def test_app_notify_async(
    test_cache_path: str,
    test_cloud_server: str,
    test_domain_cloud_cache: str,
    test_name_oauth2_info: str,
):
    miot_storage = MIoTStorage(test_cache_path)
    oauth_info = await miot_storage.load_async(
        domain=test_domain_cloud_cache, name=test_name_oauth2_info, type_=dict
    )
    assert isinstance(oauth_info, dict) and "access_token" in oauth_info
    miot_http = MIoTHttpClient(
        cloud_server=test_cloud_server, access_token=oauth_info["access_token"]
    )

    # Get all notify and delete it.
    notifies = await miot_http.get_app_notifies_async()
    assert isinstance(notifies, Dict)
    _LOGGER.info("all notifies: %s", notifies)
    if notifies:
        result = await miot_http.delete_app_notifies_async(
            notify_ids=list(notifies.keys())
        )
        assert result
        _LOGGER.info("delete all notifies success, %s", result)
        notifies = await miot_http.get_app_notifies_async()
        assert notifies == {}

    # Create valid app notify
    notify_list = []
    notify_id_en = await miot_http.create_app_notify_async(text="Hello Xiaomi Home")
    assert isinstance(notify_id_en, str)
    notify_list.append(notify_id_en)
    _LOGGER.info("create app notify, notify_id_en: %s", notify_id_en)
    notify_id_zh = await miot_http.create_app_notify_async(text="中文测试")
    assert isinstance(notify_id_zh, str)
    notify_list.append(notify_id_zh)
    _LOGGER.info("create app notify, notify_id_zh: %s", notify_id_zh)
    notify_id_unicode = await miot_http.create_app_notify_async(
        text="😃 😢 🌟 🚗 🍎 ❤️ ✈️"
    )
    assert isinstance(notify_id_unicode, str)
    notify_list.append(notify_id_unicode)
    _LOGGER.info("create app notify, notify_id_unicode: %s", notify_id_unicode)

    # Create invalid app notify
    create_invalid_status = False
    try:
        await miot_http.create_app_notify_async(text="<sensitive word>")
        create_invalid_status = True
    except Exception as err:
        _LOGGER.info("create app notify, empty text, expect fail, %s", err)
    assert not create_invalid_status, (
        "create invalid app notify success, enter sensitive word"
    )

    # Compare notify list
    notifies = await miot_http.get_app_notifies_async()
    assert isinstance(notifies, Dict)
    assert set(notify_list) == set(notifies.keys())

    # Send notify
    for notify_id in notify_list:
        result = await miot_http.send_app_notify_async(notify_id=notify_id)
        assert result
        _LOGGER.info("send app notify, notify_id: %s, result: %s", notify_id, result)

    await miot_http.deinit_async()
