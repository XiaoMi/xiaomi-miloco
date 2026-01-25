# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

import logging

from miloco_server.utils.local_models import ModelPurpose
from miloco_server.utils.prompt_helper import VisionUnderstandToolPromptBuilder

from miloco_server.schema.miot_schema import CameraImgSeq

from miloco_server.schema.auth_schema import UserLanguage

from miloco_server.utils.llm_utils.base_llm_util import BaseLLMUtil

from miloco_server.utils.prompt_helper import PromptConfig, PromptType

logger = logging.getLogger(__name__)

class VisionUnderstander(BaseLLMUtil):
    """Actor for understanding and processing vision-related tasks."""

    def __init__(
        self,
        request_id: str,
        query: str,
        camera_img_seqs: list[CameraImgSeq],
        language: UserLanguage,
    ):
        """Initialize VisionUnderstander"""
        super().__init__(request_id=request_id, priority=PromptConfig.get_priority(PromptType.VISION_UNDERSTANDING), query=query, tools_meta=None)
        self._llm_proxy = self._manager.get_llm_proxy_by_purpose(ModelPurpose.VISION_UNDERSTANDING)
        self._camera_img_seqs = camera_img_seqs
        self._language = language
        self._init_conversation()
        logger.info("[%s] VisionUnderstander initialized, llm_proxy: %s", self._request_id, self._llm_proxy)


    def _init_conversation(self) -> None:
        self._chat_history = VisionUnderstandToolPromptBuilder.build_prompt(
            self._camera_img_seqs, self._query, self._language)

    async def run(self) -> str | None:
        """Run agent to process user query"""
        logger.info("[%s] Starting to vision understand", self._request_id)

        content, _, _ = await self._call_llm()
        return content

