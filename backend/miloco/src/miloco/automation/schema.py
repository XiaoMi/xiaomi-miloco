from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MiotPropertyFilterCondition(BaseModel):
    op: Literal["eq", "ne", "gt", "lt", "gte", "lte", "any"] = Field(default="eq")
    value: Any = Field(default="*")


class MiotEventTrigger(BaseModel):
    trigger_kind: str = Field(default="miot_event")
    source_type: Literal["device", "scene"] = Field(...)
    source_id: str = Field(...)
    source_name: str = Field(default="")
    home_id: str | None = Field(default=None)
    room_name: str | None = Field(default=None)
    event_name: str = Field(default="")
    changed_properties: dict[str, Any] = Field(default_factory=dict)
    occurred_at: int = Field(default=0)
    raw: dict[str, Any] = Field(default_factory=dict)


class MiotEventSource(BaseModel):
    source_type: Literal["device", "scene"] = Field(...)
    source_id: str = Field(...)
    source_name: str = Field(...)
    home_id: str | None = Field(default=None)
    room_name: str | None = Field(default=None)


class MiotEventMapping(BaseModel):
    id: str = Field(default="")
    source_type: Literal["device", "scene"] = Field(...)
    source_id: str = Field(...)
    source_name_snapshot: str = Field(default="")
    camera_dids: list[str] = Field(default_factory=list)
    enabled: bool = Field(default=True)
    query_template: str = Field(default="")
    event_kinds: list[str] = Field(default_factory=list)
    property_filters: dict[str, Any] = Field(default_factory=dict)
    cooldown_seconds: int = Field(default=30, ge=0)
    notes: str = Field(default="")
    snapshot_paths: list[str] = Field(default_factory=list)
    created_at: int | None = Field(default=None)
    updated_at: int | None = Field(default=None)


class MiotEventMappingUpdate(BaseModel):
    source_type: Literal["device", "scene"] | None = Field(default=None)
    source_id: str | None = Field(default=None)
    source_name_snapshot: str | None = Field(default=None)
    camera_dids: list[str] | None = Field(default=None)
    enabled: bool | None = Field(default=None)
    query_template: str | None = Field(default=None)
    event_kinds: list[str] | None = Field(default=None)
    property_filters: dict[str, Any] | None = Field(default=None)
    cooldown_seconds: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None)


class MiotEventTriggerLog(BaseModel):
    id: str
    trigger: MiotEventTrigger
    mapping_ids: list[str] = Field(default_factory=list)
    candidate_rule_ids: list[str] = Field(default_factory=list)
    camera_dids: list[str] = Field(default_factory=list)
    clip_device_ids: list[str] = Field(default_factory=list)
    clip_kind: str = Field(default="")
    perception_started: bool = Field(default=False)
    perception_answer: str = Field(default="")
    matched_rule_ids: list[str] = Field(default_factory=list)
    skipped_reason: str = Field(default="")
    error: str = Field(default="")
    snapshot_paths: list[str] = Field(default_factory=list)
    created_at: int


class MiotEventManualTriggerRequest(BaseModel):
    source_type: Literal["device", "scene"] = Field(...)
    source_id: str = Field(...)
    source_name: str = Field(default="")
    home_id: str | None = Field(default=None)
    room_name: str | None = Field(default=None)
    event_name: str = Field(default="")
    changed_properties: dict[str, Any] = Field(default_factory=dict)


class MiotEventCatalog(BaseModel):
    devices: list[MiotEventSource] = Field(default_factory=list)
    scenes: list[MiotEventSource] = Field(default_factory=list)
    cameras: list[dict[str, Any]] = Field(default_factory=list)

