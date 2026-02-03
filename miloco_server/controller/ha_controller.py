# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Home Assistant controller
Handles Home Assistant configuration, automation, and action execution
Uses unified exception handling framework
"""
import logging
from fastapi import APIRouter, Depends

from miot.ha_api import HAAutomationInfo

from miloco_server.middleware import verify_token
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.schema.miot_schema import HAConfig
from miloco_server.service.manager import get_manager

logger = logging.getLogger(name=__name__)

router = APIRouter(prefix="/ha", tags=["Home Assistant"])

manager = get_manager()


@router.post(path="/set_config", summary="Set Home Assistant configuration", response_model=NormalResponse)
async def set_ha_config(ha_config: HAConfig, current_user: str = Depends(verify_token)):
    """Set Home Assistant configuration"""
    logger.info("Set HA config API called, user: %s, base_url: %s", current_user, ha_config.base_url)

    await manager.ha_service.set_ha_config(ha_config)

    logger.info("Home Assistant configuration set successfully")
    return NormalResponse(
        code=0,
        message="Home Assistant configuration set successfully",
        data=None
    )


@router.get(path="/get_config", summary="Get Home Assistant configuration", response_model=NormalResponse)
async def get_ha_config(current_user: str = Depends(verify_token)):
    """Get Home Assistant configuration"""
    logger.info("Get HA config API called, user: %s", current_user)

    ha_config = await manager.ha_service.get_ha_config()
    logger.info("Home Assistant configuration returned %s", ha_config)

    if ha_config:
        message="Home Assistant configuration retrieved successfully"
    else:
        message="Home Assistant configuration not set"

    return NormalResponse(
        code=0,
        message=message,
        data=ha_config
    )


@router.get(path="/automations", summary="Get Home Assistant automation list", response_model=NormalResponse)
async def get_ha_automations(current_user: str = Depends(verify_token)):
    """Get Home Assistant automation list"""
    logger.info("Get HA automations API called, user: %s", current_user)

    automations: list[HAAutomationInfo] = await manager.ha_service.get_ha_automations()

    logger.info(
        "Successfully retrieved Home Assistant automation list - Count: %s", len(automations))
    return NormalResponse(
        code=0,
        message="Home Assistant automation list retrieved successfully",
        data=automations
    )


@router.get(path="/automation_actions",
           summary="Get Home Assistant automation actions list", response_model=NormalResponse)
async def get_ha_automation_actions(current_user: str = Depends(verify_token)):
    """Get Home Assistant automation actions list"""
    logger.info("Get HA automation actions API called, user: %s", current_user)

    actions = await manager.ha_service.get_ha_automation_actions()
    return NormalResponse(
        code=0,
        message="Home Assistant automation actions list retrieved successfully",
        data=actions
    )


@router.get(path="/refresh_ha_automations",
           summary="Refresh Home Assistant automation information", response_model=NormalResponse)
async def refresh_ha_automations(current_user: str = Depends(verify_token)):
    """Refresh Home Assistant automation information"""
    logger.info("Refresh HA automations API called, user: %s", current_user)

    await manager.ha_service.refresh_ha_automations()

    logger.info("Successfully refreshed Home Assistant automation information")
    return NormalResponse(
        code=0,
        message="Home Assistant automation information refreshed successfully",
        data=None
    )


# --- WebSocket API 接口 ---

@router.get(path="/ws_status", summary="获取 HA WebSocket 连接状态", response_model=NormalResponse)
async def get_ha_ws_status(current_user: str = Depends(verify_token)):
    """获取 Home Assistant WebSocket 连接状态"""
    logger.info("Get HA WebSocket status API called, user: %s", current_user)
    
    status = manager.ha_service.get_ws_status()
    
    return NormalResponse(
        code=0,
        message="获取 HA WebSocket 状态成功",
        data=status
    )


@router.get(path="/devices", summary="获取 HA 设备列表", response_model=NormalResponse)
async def get_ha_devices(current_user: str = Depends(verify_token)):
    """获取 Home Assistant 设备列表（通过 WebSocket）"""
    logger.info("Get HA devices API called, user: %s", current_user)
    
    devices = await manager.ha_service.get_ha_devices()
    
    logger.info("成功获取 HA 设备列表，数量: %d", len(devices))
    return NormalResponse(
        code=0,
        message="获取 HA 设备列表成功",
        data=devices
    )


@router.get(path="/areas", summary="获取 HA 区域列表", response_model=NormalResponse)
async def get_ha_areas(current_user: str = Depends(verify_token)):
    """获取 Home Assistant 区域列表（通过 WebSocket）"""
    logger.info("Get HA areas API called, user: %s", current_user)
    
    areas = await manager.ha_service.get_ha_areas()
    
    logger.info("成功获取 HA 区域列表，数量: %d", len(areas))
    return NormalResponse(
        code=0,
        message="获取 HA 区域列表成功",
        data=areas
    )


@router.get(path="/device/{device_id}/entities", summary="获取 HA 设备实体列表", response_model=NormalResponse)
async def get_ha_device_entities(device_id: str, current_user: str = Depends(verify_token)):
    """获取 Home Assistant 指定设备的实体列表（通过 WebSocket）"""
    logger.info("Get HA device entities API called, user: %s, device_id: %s", current_user, device_id)
    
    entities = await manager.ha_service.get_ha_device_entities(device_id)
    
    logger.info("成功获取设备实体，device_id: %s", device_id)
    return NormalResponse(
        code=0,
        message="获取设备实体成功",
        data=entities
    )


@router.get(path="/states", summary="获取 HA 所有实体状态", response_model=NormalResponse)
async def get_ha_states(current_user: str = Depends(verify_token)):
    """获取 Home Assistant 所有实体状态（通过 WebSocket）"""
    logger.info("Get HA states API called, user: %s", current_user)
    
    states = await manager.ha_service.get_ha_states()
    
    logger.info("成功获取 HA 实体状态，数量: %d", len(states))
    return NormalResponse(
        code=0,
        message="获取 HA 实体状态成功",
        data=states
    )


@router.get(path="/entity_registry", summary="获取 HA 实体注册表", response_model=NormalResponse)
async def get_ha_entity_registry(current_user: str = Depends(verify_token)):
    """获取 Home Assistant 实体注册表（通过 WebSocket）"""
    logger.info("Get HA entity registry API called, user: %s", current_user)
    
    entities = await manager.ha_service.get_ha_entity_registry()
    
    logger.info("成功获取 HA 实体注册表，数量: %d", len(entities))
    return NormalResponse(
        code=0,
        message="获取 HA 实体注册表成功",
        data=entities
    )

