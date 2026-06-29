from __future__ import annotations

import logging
import re
import time
from pathlib import Path as _Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from miloco.automation.schema import (
    MiotEventManualTriggerRequest,
    MiotEventMapping,
    MiotEventMappingUpdate,
    MiotEventTrigger,
)
from miloco.manager import get_manager
from miloco.middleware import verify_token, verify_token_query_fallback
from miloco.rule.schema import RuleTriggerType
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
    await mgr.miot_service.sync_automation_property_subscriptions()
    return NormalResponse(code=0, message="created", data=data)


@router.patch("/mappings/{mapping_id}", response_model=NormalResponse, summary="Update MiOT event mapping")
async def update_mapping(
    mapping_id: str,
    update: MiotEventMappingUpdate,
    current_user: str = Depends(verify_token),
):
    mgr = manager()
    data = mgr.automation_service.update_mapping(mapping_id, update)
    await mgr.miot_service.sync_automation_property_subscriptions()
    return NormalResponse(code=0, message="updated", data=data)


@router.delete("/mappings/{mapping_id}", response_model=NormalResponse, summary="Delete MiOT event mapping")
async def delete_mapping(mapping_id: str, current_user: str = Depends(verify_token)):
    mgr = manager()
    mgr.automation_service.delete_mapping(mapping_id)
    await mgr.miot_service.sync_automation_property_subscriptions()
    return NormalResponse(code=0, message="deleted", data=None)


@router.get("/snapshots/{filename}", summary="Serve automation snapshot image")
async def serve_snapshot(
    filename: str,
    request: Request,
    auth: None = Depends(verify_token_query_fallback),
):
    """Serve a saved automation snapshot JPEG."""
    _ = request, auth
    if not re.match(r"^[A-Za-z0-9_.\-]+\.jpg$", filename):
        return NormalResponse(code=400, message="invalid filename", data=None)
    import os
    home = os.environ.get("MILOCO_HOME", "/root/.openclaw/miloco")
    snap_path = _Path(home) / "static" / "clips" / "automation" / _Path(filename).name
    if not snap_path.exists():
        return NormalResponse(code=404, message="not found", data=None)
    return FileResponse(str(snap_path), media_type="image/jpeg")


@router.get("/devices/{did}/properties", response_model=NormalResponse, summary="Device property keys from recent logs")
async def list_device_properties(did: str, current_user: str = Depends(verify_token)):
    """Return known property keys for a device, sourced from recent trigger logs.
    Used by the frontend to suggest property filter keys when editing miot_event rules."""
    data = manager().automation_service.get_device_property_keys(did)
    return NormalResponse(code=0, message="ok", data=data)


@router.get("/logs", response_model=NormalResponse, summary="Recent MiOT event trigger logs")
async def list_logs(
    limit: int = Query(50, ge=1, le=200),
    current_user: str = Depends(verify_token),
):
    return NormalResponse(
        code=0,
        message="ok",
        data=manager().automation_service.list_logs(limit),
    )


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
            event_name=request.event_name or (
                "device_prop" if request.source_type == "device" else "scene"
            ),
            changed_properties=request.changed_properties,
            occurred_at=time.time_ns() // 1_000_000,
            raw=request.model_dump(mode="json"),
        ),
        perception_service=mgr.perception_service,
        rule_service=mgr.rule_service,
        miot_service=mgr.miot_service,
        meaningful_events_dao=mgr.meaningful_events_dao,
        pipeline=mgr.perception_service._pipeline,
    )
    return NormalResponse(code=0, message="ok", data=log_item)


@router.post("/rules", response_model=NormalResponse, summary="Create miot_event rule with property filters")
async def create_miot_event_rule(
    request: dict,
    current_user: str = Depends(verify_token),
):
    """Create a miot_event rule from the automation page.
    Accepts a simplified payload with property_filters baked in."""
    from miloco.manager import get_manager

    mgr = get_manager()
    service = mgr.automation_service
    from miloco.rule.schema import Rule

    payload = service.build_miot_event_rule_payload(
        task_id=request.get("task_id", ""),
        name=request.get("name", ""),
        source_ids=request.get("source_ids", []),
        event_kinds=request.get("event_kinds", ["device_prop"]),
        query=request.get("query", ""),
        property_filters=request.get("property_filters", {}),
        action_descriptions=request.get("action_descriptions"),
    )
    rule = Rule.model_validate(payload)
    rule_id = await mgr.rule_service.create_rule(rule)
    return NormalResponse(code=0, message="created", data={"rule_id": rule_id})


@router.get("/device-spec/{did}", response_model=NormalResponse, summary="Get device spec properties for automation filtering")
async def device_spec(did: str, current_user: str = Depends(verify_token)):
    """Return device spec with property names, value lists and value ranges.
    Uses the miot-spec parser (same data source as ha_xiaomi_home)."""
    try:
        mgr = get_manager()
        proxy = mgr.miot_service._miot_proxy
        devices = proxy._device_info_dict
        device = devices.get(did)
        if not device:
            return NormalResponse(code=404, message="device not found", data=None)

        urn = getattr(device, "urn", "") or ""
        model = device.model
        if not urn:
            return NormalResponse(code=0, message="no_spec",
                data={"model": model, "name": device.name, "properties": []})

        # Reuse Miloco's existing spec fetch path, which is already compatible
        # with vendor/custom services and value-list/value-range extraction.
        spec = await proxy._fetch_device_spec(urn=urn)
        if not spec:
            return NormalResponse(code=0, message="no_spec_data",
                data={"model": model, "name": device.name, "properties": []})

        props = []
        for iid, item in spec.items():
            if not iid.startswith("prop."):
                continue
            parts = iid.split(".")
            if len(parts) != 3:
                continue
            _, siid, piid = parts
            entry = {
                "siid": int(siid),
                "piid": int(piid),
                "key": iid,
                "name": item.get("description") or item.get("prop_description") or iid,
                "description": item.get("description") or "",
                "format": item.get("format") or "",
                "access": [
                    access
                    for enabled, access in (
                        (item.get("readable"), "read"),
                        (item.get("writeable"), "write"),
                    )
                    if enabled
                ],
                "unit": item.get("unit") or "",
            }

            value_list = item.get("value_list") or []
            if value_list:
                entry["value_list"] = [
                    {
                        "value": str(v.get("value", "")),
                        "description": v.get("description") or v.get("name") or str(v.get("value", "")),
                    }
                    for v in value_list
                ]
            elif entry["format"] == "bool":
                entry["value_list"] = [
                    {"value": "0", "description": "关"},
                    {"value": "1", "description": "开"},
                ]

            value_range = item.get("value_range")
            if value_range and len(value_range) == 3:
                entry["value_range"] = {
                    "min": value_range[0],
                    "max": value_range[1],
                    "step": value_range[2],
                }

            props.append(entry)

        props.sort(key=lambda item: (item["siid"], item["piid"]))

        return NormalResponse(code=0, message="ok", data={
            "model": model, "name": device.name, "properties": props
        })
    except Exception as e:
        safe_did = re.sub(r"[^\w.\-]", "_", did or "")
        logger.warning("device_spec failed for did=%s: %s", safe_did, e)
        return NormalResponse(code=500, message=str(e), data=None)


@router.get("/rules", response_model=NormalResponse, summary="List miot_event rules")
async def list_miot_event_rules(current_user: str = Depends(verify_token)):
    rules = await manager().rule_service.get_all_rules(enabled_only=False)
    data = [rule for rule in rules if rule.trigger_type == RuleTriggerType.MIOT_EVENT]
    return NormalResponse(code=0, message="ok", data=data)
