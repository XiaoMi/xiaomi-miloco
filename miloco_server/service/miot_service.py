# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MiOT service module
"""

import logging
from typing import List, Optional

from miot.types import MIoTUserInfo, MIoTCameraInfo, MIoTDeviceInfo, MIoTManualSceneInfo

from miloco_server.proxy.miot_proxy import MiotProxy
from miloco_server.schema.trigger_schema import Action
from miloco_server.schema.miot_schema import CameraChannel, CameraImgSeq, CameraInfo, DeviceInfo, SceneInfo
from miloco_server.middleware.exceptions import (
    MiotOAuthException,
    MiotServiceException,
    ValidationException,
    BusinessException,
    ResourceNotFoundException
)
from miloco_server.utils.default_action import DefaultPresetActionManager
from miloco_server.mcp.mcp_client_manager import MCPClientManager

logger = logging.getLogger(__name__)


class MiotService:
    """MiOT service class"""

    def __init__(self, miot_proxy: MiotProxy, mcp_client_manager: MCPClientManager,
                 default_preset_action_manager: Optional[DefaultPresetActionManager] = None):
        self._miot_proxy = miot_proxy
        self._mcp_client_manager = mcp_client_manager
        self._default_preset_action_manager = default_preset_action_manager

    @property
    def miot_client(self):
        """Get the MIoTClient instance."""
        return self._miot_proxy.miot_client

    async def process_xiaomi_home_callback(self, code: str, state: str):
        """
        Process Xiaomi MiOT authorization code
        """
        try:
            logger.info(
                "process_xiaomi_home_callback code: %s, status: %s", code, state)

            await self._miot_proxy.get_miot_auth_info(code=code,
                                                              state=state)
            await self._mcp_client_manager.init_miot_mcp_clients()

        except Exception as e:
            logger.error("Failed to process Xiaomi MiOT authorization code: %s", e)
            raise MiotServiceException(f"Failed to process Xiaomi MiOT authorization code: {str(e)}") from e


    async def refresh_miot_all_info(self) -> dict:
        """
        Refresh MiOT all information
        
        Returns:
            dict: Dictionary containing result of each refresh operation
        """
        try:
            return await self._miot_proxy.refresh_miot_info()
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to refresh MiOT all information: %s", e)
            raise MiotServiceException(f"Failed to refresh MiOT all information: {str(e)}") from e

    async def refresh_miot_cameras(self):
        """
        Refresh MiOT camera information
        """
        try:
            result = await self._miot_proxy.refresh_cameras()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT cameras")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT cameras: %s", e)
            raise MiotServiceException(f"Failed to refresh MiOT cameras: {str(e)}") from e

    async def refresh_miot_scenes(self):
        """
        Refresh MiOT scene information
        """
        try:
            result = await self._miot_proxy.refresh_scenes()
            # None means call failed; an empty dict just means no scenes available and should not be treated as an error
            if result is None:
                raise MiotServiceException("Failed to refresh MiOT scenes")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT scenes: %s", e)
            raise MiotServiceException(f"Failed to refresh MiOT scenes: {str(e)}") from e

    async def refresh_miot_user_info(self):
        """
        Refresh MiOT user information
        """
        try:
            result = await self._miot_proxy.refresh_user_info()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT user info")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT user info: %s", e)
            raise MiotServiceException(f"Failed to refresh MiOT user info: {str(e)}") from e

    async def refresh_miot_devices(self):
        """
        Refresh MiOT device information
        """
        try:
            result = await self._miot_proxy.refresh_devices()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT devices")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT devices: %s", e)
            raise MiotServiceException(f"Failed to refresh MiOT devices: {str(e)}") from e

    async def get_miot_login_status(self) -> dict:
        """
        Get MiOT login status

        Returns:
            dict: Dictionary containing status and login_url (if needed)

        Raises:
            MiotOAuthException: When user is not logged in or login status check fails
        """
        try:
            is_token_valid = await self._miot_proxy.check_token_valid()
            if not is_token_valid:
                login_url = await self._miot_proxy.get_miot_login_url()
                return {"is_logged_in": False, "login_url": login_url}
            return {"is_logged_in": True}

        except Exception as e:
            logger.error("Failed to check MiOT login status: %s", e)
            raise MiotOAuthException(f"Failed to check MiOT login status: {str(e)}") from e

    async def get_miot_user_info(self) -> MIoTUserInfo:
        """
        Get MiOT user information

        Returns:
            dict: User information dictionary

        Raises:
            ResourceNotFoundException: When unable to get user information
            ExternalServiceException: When external service call fails
        """
        try:
            user_info = await self._miot_proxy.get_user_info()

            if not user_info:
                raise ResourceNotFoundException("No logged in user information found")

            return user_info
        except Exception as e:
            logger.error("Failed to get MiOT user info: %s", e)
            raise MiotServiceException(f"Failed to get MiOT user info: {str(e)}") from e

    async def get_miot_camera_list(self, include_rtsp: bool = True) -> List[CameraInfo]:
        """
        Get MiOT camera list (including RTSP cameras)

        Args:
            include_rtsp: Whether to include RTSP cameras, default True

        Returns:
            List[CameraInfo]: Camera information list

        Raises:
            MiotServiceException: When getting camera list fails
        """
        try:
            camera_list = []
            
            # 获取米家摄像头
            camera_dict: dict[
                str,
                MIoTCameraInfo] | None = await self._miot_proxy.get_cameras()
            if camera_dict:
                for camera_info in camera_dict.values():
                    camera = CameraInfo.model_validate(camera_info.model_dump())
                    camera.camera_type = "miot"  # 标记为米家摄像头
                    camera_list.append(camera)

            # 获取RTSP摄像头
            if include_rtsp:
                try:
                    from miloco_server.service.manager import get_manager
                    rtsp_cameras = get_manager().rtsp_camera_service.get_all_cameras(enabled_only=True)
                    for rtsp_camera in rtsp_cameras:
                        # 将RTSP摄像头转换为CameraInfo格式
                        camera = CameraInfo(
                            did=rtsp_camera.id,
                            name=rtsp_camera.name,
                            model="rtsp.camera.custom",
                            online=rtsp_camera.online,
                            channel_count=rtsp_camera.channel_count,
                            camera_type="rtsp",
                            home_name=rtsp_camera.location,
                            room_name=rtsp_camera.location,
                        )
                        camera_list.append(camera)
                    logger.info("Added %d RTSP cameras to camera list", len(rtsp_cameras))
                except Exception as e:
                    logger.warning("Failed to get RTSP cameras: %s", e)

            if not camera_list:
                logger.warning("No cameras found (MiOT or RTSP)")
                return []

            return camera_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT camera list: %s", e)
            raise MiotServiceException(f"Failed to get MiOT camera list: {str(e)}") from e

    async def get_miot_device_list(self) -> List[DeviceInfo]:
        try:
            device_dict: dict[
                str, MIoTDeviceInfo] = await self._miot_proxy.get_devices()
            if not device_dict:
                raise MiotServiceException("Failed to get MiOT device list")
            device_list = [
                DeviceInfo.model_validate(device_info.model_dump())
                for device_info in device_dict.values()
            ]
            return device_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT device list: %s", e)
            raise MiotServiceException(f"Failed to get MiOT device list: {str(e)}") from e

    async def get_miot_cameras_img(
            self, camera_dids: list[str], vision_use_img_count: int) -> list[CameraImgSeq]:
        """
        获取摄像头图像（支持米家摄像头和RTSP摄像头）
        
        Args:
            camera_dids: 摄像头ID列表
            vision_use_img_count: 每个摄像头获取的图像数量
            
        Returns:
            摄像头图像序列列表
        """
        logger.info(
            "get_miot_cameras_img, camera_dids: %s", ", ".join(camera_dids))
        try:
            camera_img_seqs = []
            
            # 获取米家摄像头信息
            miot_camera_info: dict[str, MIoTCameraInfo] = await self._miot_proxy.get_cameras() or {}
            
            # 获取RTSP摄像头信息
            rtsp_camera_dict = {}
            try:
                from miloco_server.service.manager import get_manager
                rtsp_cameras = get_manager().rtsp_camera_service.get_all_cameras(enabled_only=True)
                rtsp_camera_dict = {camera.id: camera for camera in rtsp_cameras}
            except Exception as e:
                logger.warning("Failed to get RTSP cameras for image retrieval: %s", e)
            
            # 处理每个请求的摄像头
            for camera_did in camera_dids:
                # 检查是否是米家摄像头
                if camera_did in miot_camera_info:
                    camera_info = miot_camera_info[camera_did]
                    for channel in range(camera_info.channel_count or 1):
                        camera_img_seq = self._miot_proxy.get_recent_camera_img(
                            camera_did, channel, vision_use_img_count)
                        if camera_img_seq:
                            camera_img_seqs.append(camera_img_seq)
                        else:
                            logger.warning(
                                "get_miot_cameras_img: MiOT camera image failed, did: %s, channel: %s",
                                camera_did, channel
                            )
                # 检查是否是RTSP摄像头
                elif camera_did in rtsp_camera_dict:
                    rtsp_camera = rtsp_camera_dict[camera_did]
                    try:
                        from miloco_server.service.manager import get_manager
                        rtsp_service = get_manager().rtsp_camera_service
                        # 获取RTSP摄像头的图像（优先使用子码流channel=1，如果没有则使用主码流channel=0）
                        for channel in range(rtsp_camera.channel_count):
                            camera_img_seq = rtsp_service.get_recent_camera_img(
                                camera_did, channel, vision_use_img_count)
                            if camera_img_seq:
                                camera_img_seqs.append(camera_img_seq)
                                logger.info(
                                    "get_miot_cameras_img: Got RTSP camera images, did: %s, channel: %s, count: %d",
                                    camera_did, channel, len(camera_img_seq.img_list) if camera_img_seq.img_list else 0
                                )
                            else:
                                logger.warning(
                                    "get_miot_cameras_img: RTSP camera image failed, did: %s, channel: %s",
                                    camera_did, channel
                                )
                    except Exception as e:
                        logger.error("Failed to get RTSP camera images for %s: %s", camera_did, e)
                else:
                    logger.warning(
                        "get_miot_cameras_img: Camera not found in any source, did: %s", camera_did
                    )
            
            return camera_img_seqs
        except Exception as e:
            logger.error("Failed to get camera images: %s", e)
            raise MiotServiceException(f"Failed to get camera images: {str(e)}") from e

    async def get_miot_scene_list(self) -> List[SceneInfo]:
        """
        Get all MiOT scenes

        Returns:
            dict: Scene information dictionary

        Raises:
            MiotServiceException: When getting scenes fails
        """
        try:
            scenes: dict[
                str,
                MIoTManualSceneInfo] | None = await self._miot_proxy.get_all_scenes(
                )

            if scenes is None:
                raise MiotServiceException("Failed to get MiOT scene list")

            scene_info_list = [
                SceneInfo(scene_id=scene_info.scene_id,
                          scene_name=scene_info.scene_name)
                for scene_info in scenes.values()
            ]

            return scene_info_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT scene list: %s", e)
            raise MiotServiceException(f"Failed to get MiOT scene list: {str(e)}") from e

    async def send_notify(self, notify: str) -> None:
        """Send notification"""
        try:
            notify_id = await self._miot_proxy.get_miot_app_notify_id(notify)
            if not notify_id:
                raise ValidationException("MiOT app notification content is inappropriate, please re-enter")
            result = await self._miot_proxy.send_app_notify(notify_id)
            if not result:
                raise BusinessException("Failed to send notification")
        except Exception as e:
            logger.error("Failed to send notification: %s", str(e))
            raise BusinessException(f"Failed to send notification: {str(e)}") from e

    async def start_video_stream(self, camera_id: str, channel: int, callback):
        """
        Start video stream (business layer method)

        Args:
            camera_id: Camera device ID
            channel: Channel number
            callback: Video data callback function

        Raises:
            MiotServiceException: When startup fails
        """
        try:
            logger.info("Starting video stream: camera_id=%s, channel=%s", camera_id, channel)
            if callback:
                await self._miot_proxy.start_camera_raw_stream(
                    camera_id, channel, callback)
            else:
                logger.info("No callback function, only recording startup request: camera_id=%s", camera_id)
        except Exception as e:
            logger.error("Failed to start video stream: %s", e)
            raise MiotServiceException(f"Failed to start video stream: {str(e)}") from e

    async def stop_video_stream(self, camera_id: str, channel: int):
        """
        Stop video stream (business layer method)

        Args:
            camera_id: Camera device ID

        Raises:
            MiotServiceException: When stopping fails
        """
        try:
            logger.info("Stopping video stream: camera_id=%s", camera_id)
            await self._miot_proxy.stop_camera_raw_stream(camera_id, channel)
            logger.info("Video stream stopped successfully: camera_id=%s", camera_id)
        except Exception as e:
            logger.error("Failed to stop video stream: %s", e)
            raise MiotServiceException(f"Failed to stop video stream: {str(e)}") from e

    async def get_miot_scene_actions(self) -> List[Action]:
        """
        Get MiOT scene action list

        Returns:
            dict: MiOT scene action dictionary

        Raises:
            MiotServiceException: When getting scene actions fails
        """
        try:
            if not self._default_preset_action_manager:
                logger.error("DefaultPresetActionManager not initialized")
                raise MiotServiceException("DefaultPresetActionManager not initialized")

            actions = await self._default_preset_action_manager.get_miot_scene_actions()

            return list(actions.values())
        except Exception as e:
            logger.error("Failed to get MiOT scene action list: %s", e)
            raise MiotServiceException(f"Failed to get MiOT scene action list: {str(e)}") from e
