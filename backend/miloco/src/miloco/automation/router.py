from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, Depends

from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.miot.schema import (
    MiotEventManualTriggerRequest,
    MiotEventMapping,
    MiotEventMappingUpdate,
    MiotEventTrigger,
)
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["Automation"])


def manager():
    return get_manager()


@router.get("/catalog", response_model=NormalResponse, summary="Automation source catalog")
async def get_catalog(current_user: str = Depends(verify_token)):
    manager = get_manager()
    data = await manager.automation_service.list_catalog(manager.miot_service)
    return NormalResponse(code=0, message="ok", data=data)


@router.get("/mappings", response_model=NormalResponse, summary="List MiOT event mappings")
async def list_mappings(current_user: str = Depends(verify_token)):
    return NormalResponse(
        code=0,
        message="ok",
        data=manager().automation_service.list_mappings(),
    )


@router.post("/mappings", response_model=NormalResponse, summary="Create MiOT event mapping")
async def create_mapping(mapping: MiotEventMapping, current_user: str = Depends(verify_token)):
    mgr = manager()
    data = mgr.automation_service.create_mapping(mapping)
    data = await mgr.automation_service.sync_mapping_rule(data, mgr.rule_service)
    await mgr.miot_service.sync_automation_property_subscriptions(
        mgr.automation_service.list_mappings()
    )
    return NormalResponse(code=0, message="created", data=data)


@router.patch("/mappings/{mapping_id}", response_model=NormalResponse, summary="Update MiOT event mapping")
async def update_mapping(
    mapping_id: str,
    update: MiotEventMappingUpdate,
    current_user: str = Depends(verify_token),
):
    mgr = manager()
    data = mgr.automation_service.update_mapping(mapping_id, update)
    data = await mgr.automation_service.sync_mapping_rule(data, mgr.rule_service)
    await mgr.miot_service.sync_automation_property_subscriptions(
        mgr.automation_service.list_mappings()
    )
    return NormalResponse(code=0, message="updated", data=data)


@router.delete("/mappings/{mapping_id}", response_model=NormalResponse, summary="Delete MiOT event mapping")
async def delete_mapping(mapping_id: str, current_user: str = Depends(verify_token)):
    mgr = manager()
    mapping = mgr.automation_service.get_mapping(mapping_id)
    await mgr.automation_service.delete_mapping_rule(mapping, mgr.rule_service)
    mgr.automation_service.delete_mapping(mapping_id)
    await mgr.miot_service.sync_automation_property_subscriptions(
        mgr.automation_service.list_mappings()
    )
    return NormalResponse(code=0, message="deleted", data=None)


@router.post("/test-trigger", response_model=NormalResponse, summary="Manual test trigger")
async def test_trigger(
    request: MiotEventManualTriggerRequest,
    current_user: str = Depends(verify_token),
):
    mgr = manager()
    log_item = await mgr.automation_service.handle_trigger(
        trigger=MiotEventTrigger(
            source_type=request.source_type,
            source_id=request.source_id,
            source_name=request.source_name,
            home_id=request.home_id,
            room_name=request.room_name,
            event_name=request.event_name or "device_prop",
            changed_properties=request.changed_properties,
            occurred_at=time.time_ns() // 1_000_000,
            raw=request.model_dump(mode="json"),
        ),
        perception_service=mgr.perception_service,
        rule_service=mgr.rule_service,
        miot_service=mgr.miot_service,
        meaningful_events_dao=mgr.meaningful_events_dao,
    )
    return NormalResponse(code=0, message="ok", data=log_item)


@router.get("/device-spec/{did}", response_model=NormalResponse, summary="Get device spec properties for automation filtering")
async def device_spec(did: str, current_user: str = Depends(verify_token)):
    """Return device spec with property names, value lists and value ranges.
    Uses the miot-spec parser (same data source as ha_xiaomi_home)."""
    # 路径注入/日志注入/ReDoS 防护：did 须为字母数字下划线连字符（米家设备 did 格式）
    if not re.match(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$", did):
        return NormalResponse(code=404, message="invalid device id", data=None)
    try:
        mgr = get_manager()
        data = await mgr.miot_service.get_automation_device_spec(did)
        if data is None:
            return NormalResponse(code=404, message="device not found", data=None)
        message = "ok" if data["properties"] or data["events"] else "no_spec_data"
        return NormalResponse(code=0, message=message, data=data)
    except Exception:
        logger.warning("device_spec failed", exc_info=True)
        return NormalResponse(code=500, message="device spec failed", data=None)


