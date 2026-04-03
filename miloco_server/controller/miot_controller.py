# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Xiaomi IoT controller
Handles Xiaomi IoT device login, authorization, and device management
"""
import asyncio
import logging
import os
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Optional
from fastapi import APIRouter, Depends, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect, WebSocketState

from miloco_server.middleware import (
    verify_token,
    verify_websocket_token
)
from miloco_server.middleware import MiotServiceException, ResourceNotFoundException
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.service.manager import get_manager

logger = logging.getLogger(name=__name__)

router = APIRouter(prefix="/miot", tags=["Xiaomi IoT"])

manager = get_manager()


@router.get("/xiaomi_home_callback", summary="Xiaomi Home authorization callback", response_class=HTMLResponse)
async def xiaomi_home_callback(code: str, state: str):
    """Xiaomi Home authorization callback handler"""
    logger.info(
        "Xiaomi Home authorization callback: code=%s, state=%s", code, state)

    # Read HTML template file
    template_path = os.path.join(os.path.dirname(
        __file__), "..", "templates", "miot_login_callback.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()
    except FileNotFoundError as exc:
        logger.error("HTML template file not found: %s", template_path)
        raise ResourceNotFoundException("HTML template file not found") from exc

    try:
        await manager.miot_service.process_xiaomi_home_callback(code, state)

        logger.info("Xiaomi Home authorization callback processed successfully")

        # Authorization successful
        title = "Authorization Successful"
        content = "Xiaomi Home authorization successful, you can close this page"
        button = "Close"
        success = True

    except MiotServiceException as e:
        logger.error(
            "Xiaomi Home authorization callback processing failed - MiOT service error: %s", e.message)

        # Authorization failed
        title = "Authorization Failed"
        content = f"Xiaomi Home authorization failed: {e.message}"
        button = "Close"
        success = False

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "Unknown error occurred during Xiaomi Home authorization callback processing: %s", str(e))

        # Unknown error
        title = "Authorization Failed"
        content = f"Unknown error occurred during Xiaomi Home authorization: {str(e)}"
        button = "Close"
        success = False

    # Fill HTML template
    web_page = template_content.replace("TITLE_PLACEHOLDER", title)
    web_page = web_page.replace("CONTENT_PLACEHOLDER", content)
    web_page = web_page.replace("BUTTON_PLACEHOLDER", button)
    web_page = web_page.replace(
        "STATUS_PLACEHOLDER", "true" if success else "false")

    return HTMLResponse(content=web_page)


@router.get("/login_status", summary="Check MiOT login status", response_model=NormalResponse)
async def get_miot_login_status(current_user: str = Depends(verify_token)):
    """Check MiOT login status"""
    logger.info("MiOT login status API called, user: %s", current_user)

    result = await manager.miot_service.get_miot_login_status()

    logger.info("MiOT login status: Login successful")
    return NormalResponse(
        code=0,
        message="Login status checked successfully",
        data=result
    )


@router.get(path="/user_info", summary="Get MiOT user information", response_model=NormalResponse)
async def get_miot_user_info(current_user: str = Depends(verify_token)):
    """Get MiOT user information"""
    logger.info("Get MiOT user info API called, user: %s", current_user)

    user_info = await manager.miot_service.get_miot_user_info()

    logger.info("Successfully retrieved Xiaomi Home user information")
    return NormalResponse(
        code=0,
        message="MiOT user information retrieved successfully",
        data=user_info
    )


@router.get(path="/camera_list", summary="Get MiOT camera list", response_model=NormalResponse)
async def get_miot_camera_list(current_user: str = Depends(verify_token)):
    """Get MiOT camera list"""
    logger.info("Get MiOT camera list API called, user: %s", current_user)

    camera_list = await manager.miot_service.get_miot_camera_list()

    logger.info(
        "Successfully retrieved Xiaomi Home camera list - Count: %s", len(camera_list))
    return NormalResponse(
        code=0,
        message="MiOT camera list retrieved successfully",
        data=camera_list
    )

@router.get(path="/device_list", summary="Get MiOT device list", response_model=NormalResponse)
async def get_miot_device_list(current_user: str = Depends(verify_token)):
    """Get MiOT device list"""
    logger.info("get miot device list, user: %s", current_user)
    device_list = await manager.miot_service.get_miot_device_list()
    logger.info("Successfully retrieved Xiaomi Home device list - Count: %s", len(device_list))
    return NormalResponse(
        code=0,
        message="MiOT device list retrieved successfully",
        data=device_list
    )

@router.get(path="/refresh_miot_all_info", summary="Refresh MiOT all information", response_model=NormalResponse)
async def refresh_miot_all_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT all information"""
    logger.info("Refresh MiOT all info API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_all_info()
    logger.info("MiOT information refresh completed: %s", result)
    return NormalResponse(
        code=0,
        message="MiOT information refresh completed",
        data=result
    )


@router.get(path="/refresh_miot_cameras", summary="Refresh MiOT camera information", response_model=NormalResponse)
async def refresh_miot_cameras(current_user: str = Depends(verify_token)):
    """Refresh MiOT camera information"""
    logger.info("Refresh MiOT cameras API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_cameras()

    logger.info("Successfully refreshed Xiaomi Home camera information")
    return NormalResponse(
        code=0,
        message="MiOT camera information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_scenes", summary="Refresh MiOT scene information", response_model=NormalResponse)
async def refresh_miot_scenes(current_user: str = Depends(verify_token)):
    """Refresh MiOT scene information"""
    logger.info("Refresh MiOT scenes API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_scenes()

    logger.info("Successfully refreshed Xiaomi Home scene information")
    return NormalResponse(
        code=0,
        message="MiOT scene information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_user_info", summary="Refresh MiOT user information", response_model=NormalResponse)
async def refresh_miot_user_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT user information"""
    logger.info("Refresh MiOT user info API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_user_info()

    logger.info("Successfully refreshed Xiaomi Home user information")
    return NormalResponse(
        code=0,
        message="MiOT user information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_devices", summary="Refresh MiOT device information", response_model=NormalResponse)
async def refresh_miot_devices(current_user: str = Depends(verify_token)):
    """Refresh MiOT device information"""
    logger.info("Refresh MiOT devices API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_devices()

    logger.info("Successfully refreshed Xiaomi Home device information")
    return NormalResponse(
        code=0,
        message="MiOT device information refreshed successfully",
        data=result
    )


@router.get(path="/miot_scene_actions", summary="Get MiOT scene actions list", response_model=NormalResponse)
async def get_miot_scene_actions(current_user: str = Depends(verify_token)):
    """Get MiOT scene actions list"""
    logger.info("Get MiOT actions API called, user: %s", current_user)
    actions = await manager.miot_service.get_miot_scene_actions()
    return NormalResponse(
        code=0,
        message="MiOT scene actions list retrieved successfully",
        data=actions
    )

@router.get(path="/send_notify", summary="Send notification", response_model=NormalResponse)
async def send_notify(notify: str, current_user: str = Depends(verify_token)):
    """Send notification"""
    logger.info("Send notify API called, notify: %s, user: %s", notify, current_user)
    await manager.miot_service.send_notify(notify)
    return NormalResponse(
        code=0,
        message="Notification sent successfully",
        data=None
    )


class MIoTVideoStreamManager:
    """MIoT Video WS Manager."""
    _CAMERA_CONNECT_COUNT_MAX: int = 4
    # key=camera_id.channel, value={user_name: {user_tag: Dict[id, websocket]}}
    _camera_connect_map: Dict[str, Dict[str, OrderedDict[str, WebSocket]]]
    _camera_connect_id: int

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_connect_id = 0
        logger.info("Init MIoT Video WS Manager")

    async def new_connection(
        self, websocket: WebSocket, user_name: str, token_hash: str,  camera_id: str, channel: int
    ) -> str:
        """New video stream connection."""
        camera_tag = f"{camera_id}.{channel}"
        if camera_tag not in self._camera_connect_map or not self._camera_connect_map[camera_tag]:
            self._camera_connect_map[camera_tag] = {}
            await manager.miot_service.start_video_stream(
                camera_id=camera_id, channel=channel, callback=self.__video_stream_callback)
            logger.info("Start video stream, %s.%d", camera_id, channel)
        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(self._camera_connect_id)
        self._camera_connect_id += 1
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        logger.info("New video stream connection, %s, %s, %s", camera_tag, user_tag, connection_id)
        if len(self._camera_connect_map[camera_tag][user_tag]) > self._CAMERA_CONNECT_COUNT_MAX:
            # pylint: disable=unused-variable
            logger.warning("Too many connections, %s.%d, %s, remove first connect",camera_id, channel, user_tag)
            _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(last=False)
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close()
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error("WebSocket close error: %s", err)
        return connection_id

    async def close_connection(
        self, user_name: str, token_hash: str, camera_id: str, channel: int, cid: str
    ):
        """Close video stream connection."""
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        if (
            camera_tag not in self._camera_connect_map
            or user_tag not in self._camera_connect_map[camera_tag]
            or cid not in self._camera_connect_map[camera_tag][user_tag]
        ):
            return
        logger.info("Close video stream connection, %s, %s, %s", camera_tag, user_tag, cid)

        try:
            ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error("WebSocket close error: %s", err)
        if len(self._camera_connect_map[camera_tag][user_tag]) == 0:
            self._camera_connect_map[camera_tag].pop(user_tag, None)
        if len(self._camera_connect_map[camera_tag]) == 0:
            await manager.miot_service.stop_video_stream(camera_id, channel)
            self._camera_connect_map.pop(camera_tag)
            logger.info("No connection, stop video stream, %s.%d", camera_id, channel)

    async def __video_stream_callback(
        self, did: str, data: bytes, ts: int, seq: int, channel: int
    ) -> None:
        """Video stream callback."""
        # pylint: disable=unused-argument
        camera_tag = f"{did}.{channel}"
        if camera_tag not in self._camera_connect_map:
            logger.error("No connection, %s.%d", did, channel)
            # Stop camera stream
            await manager.miot_service.stop_video_stream(did, channel)
            return
        for conn in self._camera_connect_map[camera_tag].values():
            for ws in conn.values():
                try:
                    await ws.send_bytes(data)
                except Exception as err:  # pylint: disable=broad-exception-caught
                    logger.error("WebSocket send error: %s", err)


miot_video_stream_manager = MIoTVideoStreamManager()


@router.websocket("/ws/video_stream")
async def video_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int,
    current_user: str = Depends(verify_websocket_token)
):
    """Video stream WebSocket."""
    logger.info(
        "WebSocket connection request, %s, %s.%d", current_user, camera_id, channel)
    start_time: datetime = datetime.now()
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: Optional[str] = None
    try:
        await websocket.accept()
        cid = await miot_video_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel)
        while True:
            try:
                message = await websocket.receive_text()
                logger.info("Received message from client, %s", message)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error("WebSocket error: %s", err)
                break
    except WebSocketDisconnect:
        logger.warning("Client disconnected, %s.%d", camera_id, channel)
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.error("WebSocket error, %s", err)
        await websocket.close(code=1011, reason=f"Server error: {str(err)}")
    finally:
        logger.info(
            "Websocket connect duration[%.2fs], %s.%d",
            (datetime.now() - start_time).total_seconds(), camera_id, channel)
        if cid:
            await miot_video_stream_manager.close_connection(
                user_name=current_user, token_hash=token_hash,camera_id=camera_id, channel=channel, cid=cid)


@router.websocket("/ws/mjpeg_stream")
async def mjpeg_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int = 0,
    current_user: str = Depends(verify_websocket_token)
):
    """MJPEG video stream WebSocket - sends decoded JPEG frames."""
    logger.info("MJPEG WebSocket connection, %s, %s.%d", current_user, camera_id, channel)
    await websocket.accept()

    proxy = manager.miot_service._miot_proxy
    camera_handler = proxy._camera_img_managers.get(camera_id)
    if not camera_handler:
        await websocket.close(code=1008, reason="Camera not found")
        return

    queue = asyncio.Queue(maxsize=5)

    async def jpg_callback(did, data, ts, ch):
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(data)

    reg_id = await camera_handler.miot_camera_instance.register_decode_jpg_async(
        jpg_callback, channel, multi_reg=True)

    try:
        while True:
            data = await queue.get()
            await websocket.send_bytes(data)
    except (WebSocketDisconnect, Exception) as err:
        logger.info("MJPEG WebSocket closed, %s.%d: %s", camera_id, channel, err)
    finally:
        await camera_handler.miot_camera_instance.unregister_decode_jpg_async(channel, reg_id)
