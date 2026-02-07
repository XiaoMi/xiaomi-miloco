# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Vision chat tool module for image understanding and processing."""

import asyncio
import logging
from typing import Optional

from pydantic.dataclasses import dataclass
from miloco_server.schema.miot_schema import CameraImgSeq
from thespian.actors import Actor, ActorAddress, ActorExitRequest

from miloco_server import actor_system
from miloco_server.config import CHAT_CONFIG
from miloco_server.schema.chat_schema import Dialog, InstructionPayload, Template
from miloco_server.utils.llm_utils.device_chooser import DeviceChooser
from miloco_server.utils.llm_utils.vision_understander import VisionUnderstander
logger = logging.getLogger(__name__)


@dataclass
class VisionUnderstandStart:
    pass


class VisionChatTool(Actor):
    """Actor for handling vision chat and image understanding tasks."""

    def __init__(
        self,
        request_id: str,
        query: str,
        out_actor_address: ActorAddress,
        location_info: Optional[str],
        user_choosed_camera_dids: list[str],
        camera_images: Optional[list[CameraImgSeq]],
    ):
        """Initialize ReAct agent Actor"""
        super().__init__()
        from miloco_server.service.manager import get_manager # pylint: disable=import-outside-toplevel
        self._manager = get_manager()

        self._request_id = request_id
        self._query = query
        self._location_info = location_info
        self._user_choosed_camera_dids = user_choosed_camera_dids
        self._out_actor_address = out_actor_address
        self._future = None
        self._language = self._manager.auth_service.get_user_language().language
        self._vision_use_img_count = CHAT_CONFIG["vision_use_img_count"]
        self._camera_images: Optional[list[CameraImgSeq]] = camera_images
        logger.info("[%s] VisionChatTool initialized", self._request_id)

    def receiveMessage(self, msg, sender):
        """
        Actor message receiving method, handles received messages
        """
        try:
            if isinstance(msg, VisionUnderstandStart):
                self._future = asyncio.Future()
                self.send(sender, self._future)
                asyncio.create_task(self._handle_vision_understand_start())
            elif isinstance(msg, ActorExitRequest):
                logger.info(
                    "[%s] ChatAgent ActorExitRequest received", self._request_id
                )
            else:
                logger.warning(
                    "[%s] Unsupported message: %s", self._request_id, msg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                "[%s] Error in receiveMessage method: %s", self._request_id, e)

    async def _handle_vision_understand_start(self) -> None:
        """Run agent to process user query"""

        logger.info(
            "[%s] Starting to process user query: %s", self._request_id, self._query
        )

        try:
            if self._camera_images:
                camera_img_seqs = self._camera_images
            else:
                device_chooser = DeviceChooser(
                    request_id=self._request_id,
                    location=self._location_info,
                    choose_camera_device_ids=self._user_choosed_camera_dids)
                camera_list, all_cameras = await device_chooser.run()

                if len(camera_list) == 0:
                    camera_list = all_cameras

                camera_dids = [camera.did for camera in camera_list]
                camera_img_seqs = await self._manager.miot_service.get_miot_cameras_img(
                    camera_dids, self._vision_use_img_count)

                logger.info("[%s] Got %d camera image sequences, camera_infos: %s, img_counts: %s",
                        self._request_id,
                        len(camera_img_seqs),
                        [camera_img_seq.camera_info for camera_img_seq in camera_img_seqs],
                        [len(camera_img_seq.img_list) for camera_img_seq in camera_img_seqs])

                camera_img_seqs = [
                    camera_img_seq for camera_img_seq in camera_img_seqs
                    if camera_img_seq.camera_info.online and len(camera_img_seq.img_list) > 0
                ]

            if len(camera_img_seqs) == 0:
                self._future.set_result({"error": "No camera images found, please check cameras are working"})
                return

            camera_img_path_seqs = await asyncio.gather(*[
                camera_img_seq.store_to_path()
                for camera_img_seq in camera_img_seqs
            ])

            self._send_instruction(
                Template.CameraImages(
                    image_path_seq_list=camera_img_path_seqs))
            vision_understander = VisionUnderstander(
                request_id=self._request_id,
                query=self._query,
                camera_img_seqs=camera_img_seqs,
                language=self._language)

            content = await vision_understander.run()
            if content is not None:
                self._future.set_result({"content": content})
                return
            else:
                self._future.set_result({"error": "Failed to understand vision"})
                return

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                "[%s] Error occurred during agent execution: %s", self._request_id, str(e),
                exc_info=True)
            self._send_instruction(
                Dialog.Exception(message=f"Failed to understand vision: {str(e)}"))
            self._future.set_result({"error": f"Failed to understand vision: {str(e)}"})
            return


    def _send_instruction(self, instruction_payload: InstructionPayload):
        """Send instruction to transceiver actor (如果有的话)
        
        当通过音箱调用时，out_actor_address 可能为 None，
        此时跳过发送指令（因为没有前端界面需要接收）。
        """
        if self._out_actor_address is not None:
            actor_system.tell(self._out_actor_address, instruction_payload)
        else:
            logger.debug("[%s] 跳过发送指令，out_actor_address 为 None", self._request_id)

