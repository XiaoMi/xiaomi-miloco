# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Local models management utility.
Provides functionality to manage local AI models including loading, unloading, and querying.
"""

import asyncio
import logging
from enum import Enum
from typing import List

from miloco_server.config import LOCAL_MODEL_CONFIG
from miloco_server.middleware.exceptions import ValidationException, LLMServiceException
from miloco_server.schema.model_schema import LLMModelInfo
from miloco_server.utils.http_request_forwarding import forward_get, forward_post
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

LOCAL_MODEL_ID_PREFIX = "local-"
LOCAL_MODEL_API_KEY = "no api key"


class ModelPurpose(str, Enum):
    """Model purpose enumeration"""
    PLANNING = "planning"
    VISION_UNDERSTANDING = "vision_understanding"

    def __missing__(self, value):
        logger.error("Invalid model purpose: %s", value)
        raise ValidationException(f"Invalid model purpose: {value}")


class LocalModelApi(str, Enum):
    """Local model API endpoints"""
    BASE_VERSION = "v1"
    MODELS_DESCRIPTION = "models"
    MODELS_LOAD = "models/load"
    MODELS_UNLOAD = "models/unload"
    CUDA_INFO = "cuda_info"


class LocalModels:
    """Local model management class."""

    def __init__(self):
        self._local_models: List[LLMModelInfo] = []
        try:
            asyncio.create_task(self._fetch_models_from_http_sync())
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Init local model list failed: %s", e)
            raise LLMServiceException("Init local model list failed") from e

    async def toggle_model(self, model_name: str, load: bool):
        """Load or unload specified model."""

        url = self._get_service_url(LocalModelApi.MODELS_LOAD if load else LocalModelApi.MODELS_UNLOAD)
        await self._forward_local_models_services(url, method_get=False, params={"model_name": model_name})
        await self._fetch_models_from_http_sync()

    async def local_cuda_info(self):
        """Get local CUDA info."""
        url = self._get_service_url(LocalModelApi.CUDA_INFO)
        json_resp = await self._forward_local_models_services(url, method_get=True)
        return json_resp

    async def get_local_models(self) -> List[LLMModelInfo]:
        """Get cached local model list."""
        try:
            await self._fetch_models_from_http_sync()
        except Exception:  # pylint: disable=broad-exception-caught
            self._local_models = []
        return self._local_models

    async def get_local_model_from_id(self, model_id: str) -> LLMModelInfo:
        """Get local model by ID."""
        await self.get_local_models()  # refresh local models

        for model in self._local_models:
            if model.id == model_id:
                return model
        return None

    def _get_service_url(self, api: LocalModelApi) -> str:
        """Get service URL"""
        host: str = LOCAL_MODEL_CONFIG["host"]
        port: str = LOCAL_MODEL_CONFIG["port"]
        return f"http://{host}:{port}/{api.value}"

    async def _fetch_models_from_http_sync(self):
        """Fetch model list from ai_engine via HTTP request synchronously."""
        models_url = self._get_service_url(LocalModelApi.MODELS_DESCRIPTION)

        json_resp = await self._forward_local_models_services(models_url, method_get=True)
        data = json_resp["data"]
        models = []
        if data:
            models = [
                LLMModelInfo(
                    id=f"{LOCAL_MODEL_ID_PREFIX}{idx}",
                    base_url=self._get_service_url(LocalModelApi.BASE_VERSION),
                    api_key=LOCAL_MODEL_API_KEY,
                    local=True,
                    model_name=model.get("id"),
                    loaded=model.get("loaded", False),
                    estimate_vram_usage=model.get("estimate_vram_usage", -1.0))
                for idx, model in enumerate(data)
            ]
        self._local_models = models

    async def _forward_local_models_services(self,
                                             target_url: str,
                                             method_get: bool = True,
                                             headers: Optional[Dict[str, str]] = None,
                                             params: Optional[Dict[str, Any]] = None,
                                             json_data: Optional[Dict[str, Any]] = None,
                                             timeout: float = 30.0)-> dict:
        """Forward GET request to local model service."""
        response = None
        if method_get:
            response = await forward_get(target_url, headers=headers, params=params, timeout=timeout)
        else:
            response = await forward_post(target_url, headers=headers, params=params,
                                          json_data=json_data, timeout=timeout)

        try:
            response.raise_for_status()
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Check if not response
            if not response:
                logger.error("No response received from %s", target_url)
                raise LLMServiceException("No response received from local model service")
            # Check if response content is empty
            if not response.content:
                logger.error("Empty response received from %s", target_url)
                raise LLMServiceException("Received empty response from local model service")
            # Check if response content is not JSON
            if not response.is_json():
                logger.error("Response content is not JSON from %s", target_url)
                raise LLMServiceException("Received non-JSON response from local model service")

            # Handle HTTP errors
            error_data = response.json()
            if error_data and error_data.get("code", None) and error_data.get("message", None):
                logger.error("Forward local model service failed: errCode[%s]: %s",
                            error_data["code"], error_data["message"])
                raise LLMServiceException(error_data["message"]) from e
            else:
                logger.error("Forward local model service failed: %s", e)
                raise LLMServiceException(f"Forward local model service failed: {str(e)}") from e
        
        return response.json()