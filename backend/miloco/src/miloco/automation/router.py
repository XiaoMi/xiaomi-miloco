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


def _build_spec_property_entry(iid: str, item: dict) -> dict | None:
    if not iid.startswith("prop."):
        return None
    parts = iid.split(".")
    if len(parts) != 3:
        return None
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
                "description": v.get("description")
                or v.get("name")
                or str(v.get("value", "")),
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
    return entry


async def _build_device_event_entries(proxy, urn: str) -> list[dict]:
    # urn 须为 urn:miot:... 格式（防注入，CodeQL ReDoS/路径注入）
    if not re.match(r"^urn:miot:[A-Za-z0-9:_\-\.]+$", urn):
        return []
    spec_device = await proxy.miot_client.spec_parser.parse_async(urn=urn)
    if spec_device and not any(service.events for service in spec_device.services):
        spec_device = await proxy.miot_client.spec_parser.parse_async(
            urn=urn,
            skip_cache=True,
        )
    if not spec_device:
        return []
    events: list[dict] = []
    for service in spec_device.services:
        for event in service.events:
            args = []
            for prop in event.arguments:
                item = {
                    "description": (
                        f"{service.description_trans} {prop.description_trans}"
                        if service.description_trans != prop.description_trans
                        else prop.description_trans
                    ),
                    "prop_description": prop.description,
                    "format": prop.format,
                    "readable": prop.readable,
                    "writeable": prop.writable,
                    "unit": prop.unit,
                }
                if prop.value_list:
                    item["value_list"] = [
                        {"name": v.name, "value": v.value} for v in prop.value_list
                    ]
                if prop.value_range:
                    item["value_range"] = [
                        prop.value_range.min_,
                        prop.value_range.max_,
                        prop.value_range.step,
                    ]
                entry = _build_spec_property_entry(
                    f"prop.{service.iid}.{prop.iid}",
                    item,
                )
                if entry is not None:
                    entry["key"] = f"arg.{service.iid}.{prop.iid}"
                    args.append(entry)
            events.append(
                {
                    "siid": service.iid,
                    "eiid": event.iid,
                    "key": f"event.{service.iid}.{event.iid}",
                    "name": (
                        f"{service.description_trans} {event.description_trans}"
                        if service.description_trans != event.description_trans
                        else event.description_trans
                    ),
                    "description": event.description_trans or event.description,
                    "arguments": args,
                }
            )
    events.sort(key=lambda item: (item["siid"], item["eiid"]))
    return events


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
    if not re.match(r"^[a-zA-Z0-9_-]+$", did):
        return NormalResponse(code=404, message="invalid device id", data=None)
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
                data={"model": model, "name": device.name, "properties": [], "events": []})
        # urn 须为 urn:miot:... 格式（防注入，CodeQL 路径注入/ReDoS）
        if not re.match(r"^urn:miot:[A-Za-z0-9:_\-\.]+$", urn):
            return NormalResponse(code=0, message="invalid urn",
                data={"model": model, "name": device.name, "properties": [], "events": []})

        # Reuse Miloco's existing spec fetch path, which is already compatible
        # with vendor/custom services and value-list/value-range extraction.
        spec = await proxy._fetch_device_spec(urn=urn)
        if not spec:
            return NormalResponse(code=0, message="no_spec_data",
                data={"model": model, "name": device.name, "properties": [], "events": []})

        props = []
        for iid, item in spec.items():
            entry = _build_spec_property_entry(iid, item)
            if entry is not None:
                props.append(entry)

        props.sort(key=lambda item: (item["siid"], item["piid"]))
        events = await _build_device_event_entries(proxy, urn)

        return NormalResponse(code=0, message="ok", data={
            "model": model, "name": device.name, "properties": props, "events": events
        })
    except Exception:
        logger.warning("device_spec failed", exc_info=True)
        return NormalResponse(code=500, message="device spec failed", data=None)


