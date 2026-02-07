# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant proxy module for handling Home Assistant related operations."""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic_core import to_jsonable_python
from miot.ha_api import HAAutomationInfo, HAHttpClient

from miloco_server.dao.kv_dao import AuthConfigKeys, KVDao, DeviceInfoKeys
from miloco_server.schema.miot_schema import HAConfig
from miloco_server.utils.ha_ws_client import HAWebSocketClient


logger = logging.getLogger(__name__)

class HAProxy:
    """Home Assistant proxy class responsible for handling Home Assistant related operations."""
    def __init__(self, kv_dao: KVDao):
        self._kv_dao = kv_dao
        self._ha_rest_api: Optional[HAHttpClient] = None
        self._ha_ws_client: HAWebSocketClient = HAWebSocketClient()
        self._automations: dict[str, HAAutomationInfo] = {}
        self.init_ha_info_dict()

    @property
    def ha_client(self) -> Optional[HAHttpClient]:
        return self._ha_rest_api

    @property
    def ha_ws_client(self) -> HAWebSocketClient:
        return self._ha_ws_client

    def init_ha_info_dict(self):
        """Initialize HA related information dictionary"""
        miot_ha_base_url = self._kv_dao.get(AuthConfigKeys.MIOT_HA_BASE_URL_KEY)
        miot_ha_token = self._kv_dao.get(AuthConfigKeys.MIOT_HA_TOKEN_KEY)
        if miot_ha_base_url and miot_ha_token:
            self._ha_rest_api = HAHttpClient(miot_ha_base_url, miot_ha_token)
            # 配置 WebSocket 客户端
            self._ha_ws_client.configure(miot_ha_base_url, miot_ha_token)
        else:
            self._ha_rest_api = None

        automations_str = self._kv_dao.get(DeviceInfoKeys.HA_AUTOMATIONS_KEY)
        if automations_str:
            self._automations: dict[str, HAAutomationInfo] = {
                automation_id: HAAutomationInfo.model_validate(automation_info)
                for automation_id, automation_info in json.loads(automations_str).items()}
        else:
            self._automations = {}

    async def start_ws_client(self):
        """启动 WebSocket 客户端（如果已配置）"""
        if self._ha_ws_client.is_configured:
            await self._ha_ws_client.start()
            logger.info("HA WebSocket 客户端启动完成")

    async def stop_ws_client(self):
        """停止 WebSocket 客户端"""
        await self._ha_ws_client.stop()
        logger.info("HA WebSocket 客户端已停止")

    def get_ws_status(self) -> Dict[str, Any]:
        """获取 WebSocket 连接状态"""
        return self._ha_ws_client.get_status()

    async def set_ha_config(self, miot_ha_base_url: str, miot_ha_token: str):
        """Set Home Assistant configuration"""
        if not await HAHttpClient.validate_async(miot_ha_base_url, miot_ha_token):
            raise ValueError("Miot ha rest api is not valid, please check the base url and token")

        self._kv_dao.set(AuthConfigKeys.MIOT_HA_BASE_URL_KEY, miot_ha_base_url)
        self._kv_dao.set(AuthConfigKeys.MIOT_HA_TOKEN_KEY, miot_ha_token)
        self._ha_rest_api = HAHttpClient(miot_ha_base_url, miot_ha_token)
        
        # 配置并重启 WebSocket 客户端
        self._ha_ws_client.configure(miot_ha_base_url, miot_ha_token)
        await self._ha_ws_client.reconnect()
        
        await self.refresh_ha_automations()

    def get_ha_config(self) -> HAConfig | None:
        """Get Home Assistant configuration"""
        base_url = self._kv_dao.get(AuthConfigKeys.MIOT_HA_BASE_URL_KEY)
        token = self._kv_dao.get(AuthConfigKeys.MIOT_HA_TOKEN_KEY)
        if base_url and token:
            return HAConfig(
                base_url=base_url,
                token=token
            )
        else:
            return None

    async def _fetch_and_save_automations(self) -> dict[str, HAAutomationInfo] | None:
        """Fetch automation information from Home Assistant and save to cache and KV storage"""
        if not self._ha_rest_api:
            logger.warning("Miot ha rest api is not initialized")
            return None
        try:
            automations = await self._ha_rest_api.get_automations_async()
            self._automations = automations
            self._kv_dao.set(DeviceInfoKeys.HA_AUTOMATIONS_KEY, json.dumps(to_jsonable_python(automations)))
            return automations
        except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
            logger.warning("Failed to fetch automations: %s", e)
            return None


    async def get_automations(self) -> dict[str, HAAutomationInfo] | None:
        """Get automation information, return from cache first, fetch from HA if cache is empty"""
        if self._automations:
            return self._automations
        return await self._fetch_and_save_automations()

    async def refresh_ha_automations(self) -> bool:
        """Force refresh Home Assistant automation information"""
        automations = await self._fetch_and_save_automations()
        if automations:
            logger.info("Successfully refreshed HA automations: %s", automations)
            return True
        else:
            logger.warning("Failed to refresh HA automations")
            return False

    async def trigger_automation(self, automation_id: str):
        """Trigger specified automation"""
        if self._ha_rest_api:
            try:
                return await self._ha_rest_api.trigger_automation_async(automation_id)
            except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
                logger.warning("Failed to trigger automation %s: %s", automation_id, e)
                return None
        else:
            logger.warning("Miot ha rest api is not initialized")
            return None

    # --- WebSocket API 方法 ---

    async def get_ha_devices(self) -> List[Dict[str, Any]]:
        """
        通过 WebSocket 获取 HA 设备列表
        
        Returns:
            设备列表
        """
        if not self._ha_ws_client.is_connected:
            raise ConnectionError("HA WebSocket 未连接")
        
        try:
            devices = await self._ha_ws_client.get_devices()
            return devices if devices else []
        except Exception as e:
            logger.error("获取 HA 设备列表失败: %s", e)
            raise

    async def get_ha_areas(self) -> List[Dict[str, Any]]:
        """
        通过 WebSocket 获取 HA 区域列表
        
        Returns:
            区域列表
        """
        if not self._ha_ws_client.is_connected:
            raise ConnectionError("HA WebSocket 未连接")
        
        try:
            areas = await self._ha_ws_client.get_areas()
            return areas if areas else []
        except Exception as e:
            logger.error("获取 HA 区域列表失败: %s", e)
            raise

    async def get_ha_device_entities(self, device_id: str) -> Dict[str, Any]:
        """
        通过 WebSocket 获取指定设备的实体列表
        
        Args:
            device_id: 设备ID
            
        Returns:
            设备实体信息
        """
        if not self._ha_ws_client.is_connected:
            raise ConnectionError("HA WebSocket 未连接")
        
        try:
            result = await self._ha_ws_client.get_device_entities(device_id)
            return result if result else {}
        except Exception as e:
            logger.error("获取 HA 设备实体失败: %s", e)
            raise

    async def get_ha_states(self) -> List[Dict[str, Any]]:
        """
        通过 WebSocket 获取所有实体状态
        
        Returns:
            实体状态列表
        """
        if not self._ha_ws_client.is_connected:
            raise ConnectionError("HA WebSocket 未连接")
        
        try:
            states = await self._ha_ws_client.get_states()
            return states if states else []
        except Exception as e:
            logger.error("获取 HA 实体状态失败: %s", e)
            raise

    async def get_ha_entity_registry(self) -> List[Dict[str, Any]]:
        """
        通过 WebSocket 获取实体注册表
        
        Returns:
            实体注册表列表
        """
        if not self._ha_ws_client.is_connected:
            raise ConnectionError("HA WebSocket 未连接")
        
        try:
            entities = await self._ha_ws_client.get_entity_registry()
            return entities if entities else []
        except Exception as e:
            logger.error("获取 HA 实体注册表失败: %s", e)
            raise
