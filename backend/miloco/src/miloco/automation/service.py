from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from miloco.automation.schema import (
    MiotEventCatalog,
    MiotEventMapping,
    MiotEventMappingUpdate,
    MiotEventSource,
    MiotEventTrigger,
    MiotEventTriggerLog,
    MiotPropertyFilterCondition,
)
from miloco.config import get_settings
from miloco.database.kv_repo import KVRepo
from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.perception.schema import OnDemandPerceptionRequest
from miloco.perception.snapshot_writer import (
    check_disk_space,
    get_snapshot_root,
    save_clips,
)
from miloco.rule.schema import RuleTriggerType
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

_KV_MAPPINGS = "AUTOMATION_MIOT_EVENT_MAPPINGS"
_KV_LOGS = "AUTOMATION_MIOT_EVENT_LOGS"
_MAX_LOGS = 200


def _normalize_filter_condition(expected: Any) -> MiotPropertyFilterCondition:
    if isinstance(expected, MiotPropertyFilterCondition):
        return expected
    if isinstance(expected, dict):
        try:
            return MiotPropertyFilterCondition.model_validate(expected)
        except Exception:
            pass
    if expected == "*":
        return MiotPropertyFilterCondition(op="any", value="*")
    return MiotPropertyFilterCondition(op="eq", value=expected)


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _match_condition(actual: Any, expected: Any) -> bool:
    cond = _normalize_filter_condition(expected)
    if cond.op == "any":
        return actual is not None
    if cond.op == "eq":
        return str(actual) == str(cond.value)
    if cond.op == "ne":
        return actual is not None and str(actual) != str(cond.value)
    actual_num = _coerce_number(actual)
    expected_num = _coerce_number(cond.value)
    if actual_num is None or expected_num is None:
        return False
    if cond.op == "gt":
        return actual_num > expected_num
    if cond.op == "lt":
        return actual_num < expected_num
    if cond.op == "gte":
        return actual_num >= expected_num
    if cond.op == "lte":
        return actual_num <= expected_num
    return False


class AutomationService:
    def __init__(self, kv_repo: KVRepo):
        self._kv_repo = kv_repo
        self._cooldowns: dict[str, float] = {}

    def _load_mappings(self) -> list[MiotEventMapping]:
        raw = self._kv_repo.get(_KV_MAPPINGS, "[]") or "[]"
        try:
            return [MiotEventMapping.model_validate(item) for item in json.loads(raw)]
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load automation mappings: %s", e)
            return []

    def _save_mappings(self, mappings: list[MiotEventMapping]) -> None:
        self._kv_repo.set(
            _KV_MAPPINGS,
            json.dumps([m.model_dump(mode="json") for m in mappings], ensure_ascii=False),
        )

    def _load_logs(self) -> list[MiotEventTriggerLog]:
        raw = self._kv_repo.get(_KV_LOGS, "[]") or "[]"
        try:
            return [MiotEventTriggerLog.model_validate(item) for item in json.loads(raw)]
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load automation logs: %s", e)
            return []

    def _append_log(self, item: MiotEventTriggerLog) -> None:
        logs = self._load_logs()
        logs.insert(0, item)
        self._kv_repo.set(
            _KV_LOGS,
            json.dumps(
                [log.model_dump(mode="json") for log in logs[:_MAX_LOGS]],
                ensure_ascii=False,
            ),
        )

    async def list_catalog(self, miot_service) -> MiotEventCatalog:
        devices = await miot_service.get_miot_device_list()
        scenes = await miot_service.get_miot_scene_list()
        cameras = await miot_service.list_cameras_with_state()
        return MiotEventCatalog(
            devices=[
                MiotEventSource(
                    source_type="device",
                    source_id=d.did,
                    source_name=d.name,
                    home_id=d.home_id,
                    room_name=d.room_name,
                )
                for d in devices
            ],
            scenes=[
                MiotEventSource(
                    source_type="scene",
                    source_id=s.scene_id,
                    source_name=s.scene_name,
                )
                for s in scenes
            ],
            cameras=cameras,
        )

    def list_mappings(self) -> list[MiotEventMapping]:
        return self._load_mappings()

    def create_mapping(self, mapping: MiotEventMapping) -> MiotEventMapping:
        mappings = self._load_mappings()
        current = now_ms()
        mapping.id = mapping.id or str(uuid.uuid4())
        mapping.created_at = current
        mapping.updated_at = current
        mappings.insert(0, mapping)
        self._save_mappings(mappings)
        return mapping

    def update_mapping(self, mapping_id: str, update: MiotEventMappingUpdate) -> MiotEventMapping:
        mappings = self._load_mappings()
        for mapping in mappings:
            if mapping.id != mapping_id:
                continue
            fields = update.model_fields_set
            for field in fields:
                setattr(mapping, field, getattr(update, field))
            mapping.updated_at = now_ms()
            self._save_mappings(mappings)
            return mapping
        raise ResourceNotFoundException(f"Mapping '{mapping_id}' not found")

    def build_miot_event_rule_payload(
        self,
        task_id: str,
        name: str,
        source_ids: list[str],
        event_kinds: list[str],
        query: str,
        property_filters: dict[str, Any],
        action_descriptions: list[str] | None = None,
    ) -> dict:
        return {
            "name": name,
            "task_id": task_id,
            "trigger_type": "miot_event",
            "mode": "event",
            "lifecycle": "permanent",
            "enabled": True,
            "condition": {
                "perceive_device_ids": [],
                "query": query,
                "source_ids": source_ids,
                "event_kinds": event_kinds,
                "property_filters": property_filters,
                "mapping_ids": [],
                "use_global_mapping": True,
            },
            "actions": [],
            "action_descriptions": action_descriptions or ["米家事件触发规则命中"],
            "on_enter_actions": [],
            "on_enter_desc": None,
            "on_exit_actions": [],
            "on_exit_desc": None,
            "on_target_desc": None,
            "terminate_when": None,
            "exit_debounce_seconds": 60,
            "duration_seconds": None,
            "duration_ratio": None,
            "created_at": None,
            "updated_at": None,
        }

    def delete_mapping(self, mapping_id: str) -> None:
        mappings = [m for m in self._load_mappings() if m.id != mapping_id]
        self._save_mappings(mappings)

    def list_logs(self, limit: int = 50) -> list[MiotEventTriggerLog]:
        return self._load_logs()[:limit]

    def get_device_property_keys(self, did: str) -> list[dict]:
        """Return known property keys for a device from recent trigger logs,
        with recent observed values for each key."""
        logs = self._load_logs()
        counter: dict[str, int] = {}
        values: dict[str, list[str]] = {}
        for item in logs:
            if item.trigger.source_id != did:
                continue
            for k, v in item.trigger.changed_properties.items():
                counter[k] = counter.get(k, 0) + 1
                sv = str(v)
                if k not in values:
                    values[k] = []
                if sv not in values[k]:
                    values[k].append(sv)
        result = []
        for k, v in sorted(counter.items(), key=lambda x: -x[1]):
            entry = {"key": k, "count": v, "recent_values": values.get(k, [])[:5]}
            result.append(entry)
        return result


    def _match_rule(self, rule, trigger: MiotEventTrigger) -> bool:
        if getattr(rule, "trigger_type", RuleTriggerType.PERCEPTION) != RuleTriggerType.MIOT_EVENT:
            return False
        cond = rule.condition
        if cond.source_ids and trigger.source_id not in cond.source_ids:
            return False
        if cond.event_kinds and trigger.event_name not in cond.event_kinds:
            return False
        for key, expected in cond.property_filters.items():
            actual = trigger.changed_properties.get(key)
            if actual is None:
                actual = trigger.raw.get(key)
            if not _match_condition(actual, expected):
                return False
        return True

    def _match_property_filters(
        self,
        filters: dict[str, Any],
        trigger: MiotEventTrigger,
    ) -> bool:
        for key, expected in filters.items():
            actual = trigger.changed_properties.get(key)
            if actual is None:
                actual = trigger.raw.get(key)
            if not _match_condition(actual, expected):
                return False
        return True

    def _match_mapping(self, mapping: MiotEventMapping, trigger: MiotEventTrigger) -> bool:
        if not mapping.enabled:
            return False
        if mapping.source_type != trigger.source_type:
            return False
        if mapping.source_id != trigger.source_id:
            return False
        if mapping.event_kinds and trigger.event_name not in mapping.event_kinds:
            return False
        return self._match_property_filters(mapping.property_filters, trigger)

    def _collect_mappings_for_rules(
        self,
        trigger: MiotEventTrigger,
        candidate_rules: list[Any],
    ) -> list[MiotEventMapping]:
        all_mappings = self._load_mappings()
        source_mappings = [
            m
            for m in all_mappings
            if m.enabled
            and m.source_type == trigger.source_type
            and m.source_id == trigger.source_id
        ]
        source_index = {m.id: m for m in source_mappings}
        selected: dict[str, MiotEventMapping] = {}
        for rule in candidate_rules:
            if rule.condition.mapping_ids:
                for mapping_id in rule.condition.mapping_ids:
                    mapping = source_index.get(mapping_id)
                    if mapping is not None:
                        selected[mapping.id] = mapping
            elif rule.condition.use_global_mapping:
                for mapping in source_mappings:
                    selected[mapping.id] = mapping
        return list(selected.values())

    def _collect_direct_mappings(
        self,
        trigger: MiotEventTrigger,
    ) -> list[MiotEventMapping]:
        return [
            mapping
            for mapping in self._load_mappings()
            if self._match_mapping(mapping, trigger)
        ]

    def _cooldown_key(self, trigger: MiotEventTrigger, mapping: MiotEventMapping) -> str:
        return f"{trigger.source_type}:{trigger.source_id}:{mapping.id}"

    async def handle_trigger(
        self,
        *,
        trigger: MiotEventTrigger,
        perception_service,
        rule_service,
        miot_service,
        meaningful_events_dao,
        pipeline=None,
    ) -> MiotEventTriggerLog:
        all_rules = await rule_service.get_all_rules(enabled_only=True)
        candidate_rules = [rule for rule in all_rules if self._match_rule(rule, trigger)]
        mapping_by_id = {
            mapping.id: mapping
            for mapping in self._collect_direct_mappings(trigger)
        }
        for mapping in self._collect_mappings_for_rules(trigger, candidate_rules):
            mapping_by_id[mapping.id] = mapping
        mappings = list(mapping_by_id.values())
        log_item = MiotEventTriggerLog(
            id=str(uuid.uuid4()),
            trigger=trigger,
            mapping_ids=[m.id for m in mappings],
            candidate_rule_ids=[r.id for r in candidate_rules],
            created_at=now_ms(),
        )
        if not mappings:
            log_item.skipped_reason = "no_mapping"
            self._append_log(log_item)
            return log_item

        now = time.monotonic()
        active_mappings: list[MiotEventMapping] = []
        for mapping in mappings:
            cooldown_key = self._cooldown_key(trigger, mapping)
            if now < self._cooldowns.get(cooldown_key, 0.0):
                continue
            self._cooldowns[cooldown_key] = now + float(mapping.cooldown_seconds)
            active_mappings.append(mapping)
        if not active_mappings:
            log_item.skipped_reason = "cooldown"
            self._append_log(log_item)
            return log_item

        camera_ids: list[str] = []
        seen: set[str] = set()
        for mapping in active_mappings:
            for did in mapping.camera_dids:
                if did not in seen:
                    seen.add(did)
                    camera_ids.append(did)
        log_item.camera_dids = camera_ids
        if not camera_ids:
            log_item.skipped_reason = "no_camera"
            self._append_log(log_item)
            return log_item

        query_parts = [
            f"这是一次由米家事件触发的主动感知。事件源：{trigger.source_name or trigger.source_id}。",
            f"事件类型：{trigger.event_name or trigger.source_type}。",
        ]
        if trigger.changed_properties:
            query_parts.append(f"属性变化：{trigger.changed_properties}")
        if candidate_rules:
            query_parts.extend(
                [f"- {rule.name}: {rule.condition.query}" for rule in candidate_rules]
            )
        mapping_query = next((m.query_template for m in active_mappings if m.query_template), "")
        if mapping_query:
            query_parts.append(mapping_query)
        query_parts.append("请结合当前画面简明回答，并重点说明与触发事件相关的观察。")
        log_item.perception_started = True
        clips_by_device: dict[str, tuple[bytes, str]] = {}
        try:
            result = await perception_service.on_demand_perceive(
                OnDemandPerceptionRequest(
                    sources=camera_ids,
                    query="\n".join(query_parts),
                ),
                snapshot_sink=clips_by_device,
            )
        except Exception as e:  # noqa: BLE001
            log_item.error = str(e)
            self._append_log(log_item)
            return log_item

        answer = result.answer if result else ""
        # Save a snapshot frame from the on-demand collected batch for video replay
        snapshot_paths: list[str] = []
        try:
            import os
            from pathlib import Path as _Path
            
            import cv2
            import numpy as np

            miloco_home = os.environ.get("MILOCO_HOME", "/root/.openclaw/miloco")
            clips_dir = _Path(miloco_home) / "static" / "clips" / "automation"
            clips_dir.mkdir(parents=True, exist_ok=True)

            batch = pipeline._collector.collect_batch(camera_ids, drain=False) if pipeline else None
            if batch and not batch.empty:
                for device_id in camera_ids:
                    data = batch.devices.get(device_id)
                    if data and data.video:
                        frame = data.video[-1].frame
                        if isinstance(frame, np.ndarray) and frame.size > 0:
                            snap_path = clips_dir / f"{log_item.id}_{device_id}.jpg"
                            cv2.imwrite(str(snap_path), frame)
                            snapshot_paths.append(str(snap_path))
            if not snapshot_paths:
                logger.debug("snapshot: no frames available for %s", camera_ids)
        except Exception as e:
            logger.debug("snapshot save failed: %s", e)

        log_item.perception_answer = answer
        log_item.snapshot_paths = snapshot_paths

        clip_count = 0
        clip_kind = ""
        clip_device_ids: list[str] = []
        if clips_by_device:
            settings = get_settings()
            snapshot_root = get_snapshot_root()
            if check_disk_space(
                snapshot_root, settings.perception.snapshot_min_free_disk_mb
            ):
                clip_count = save_clips(log_item.id, clips_by_device)
                if clip_count > 0:
                    clip_kind = next(iter(clips_by_device.values()))[1]
                    clip_device_ids = [
                        device_id for device_id in camera_ids if device_id in clips_by_device
                    ]
            else:
                logger.error(
                    "automation clip disk low (< %d MB free), skip save for event %s",
                    settings.perception.snapshot_min_free_disk_mb,
                    log_item.id,
                )
        log_item.clip_kind = clip_kind
        log_item.clip_device_ids = clip_device_ids

        matched_rule_ids: list[str] = []
        context = (
            f"米家事件触发感知\n"
            f"来源: {trigger.source_name or trigger.source_id}\n"
            f"事件: {trigger.event_name}\n"
            f"属性: {trigger.changed_properties}\n"
            f"感知结果: {answer}"
        )
        for rule in candidate_rules:
            exec_result = await rule_service.trigger_rule(rule.id, context)
            if exec_result is not None:
                matched_rule_ids.append(rule.id)
        log_item.matched_rule_ids = matched_rule_ids

        if answer:
            text = (
                f"[米家事件触发]\n"
                f"来源：{trigger.source_name or trigger.source_id}\n"
                f"事件：{trigger.event_name}\n"
                f"结果：{answer}"
            )
            meaningful_events_dao.insert(
                event_id=log_item.id,
                timestamp=trigger.occurred_at or now_ms(),
                text=text,
                payload_json=json.dumps(
                    {
                        "trigger": trigger.model_dump(mode="json"),
                        "matched_rule_ids": matched_rule_ids,
                    },
                    ensure_ascii=False,
                ),
                has_rule_hit=bool(matched_rule_ids),
                has_suggestion=False,
                has_asr=False,
                device_ids=camera_ids,
                snapshot_count=clip_count,
                rule_names={rule.id: rule.name for rule in candidate_rules},
                home_id=trigger.home_id,
            )
            if clip_count > 0:
                meaningful_events_dao.update_snapshot_count(log_item.id, clip_count)
        self._append_log(log_item)
        if pipeline is not None and answer:
            pipeline._publish(
                "meaningful_event",
                {
                    "event_id": log_item.id,
                    "timestamp": trigger.occurred_at or now_ms(),
                    "text": text,
                    "has_rule_hit": bool(matched_rule_ids),
                    "has_suggestion": False,
                    "has_asr": False,
                    "snapshot_count": clip_count,
                    "device_ids": camera_ids,
                    "rule_names": {rule.id: rule.name for rule in candidate_rules},
                    "clip_kind": clip_kind or None,
                },
            )
        return log_item

    async def emit_scene_trigger(
        self,
        *,
        home_id: str,
        scene_id: str | None,
        event_name: str,
        raw: dict[str, Any],
        miot_service,
        perception_service,
        rule_service,
        meaningful_events_dao,
        pipeline=None,
    ) -> None:
        if not scene_id:
            return
        scenes = await miot_service.get_miot_scene_list()
        scene = next((item for item in scenes if item.scene_id == scene_id), None)
        trigger = MiotEventTrigger(
            source_type="scene",
            source_id=scene_id,
            source_name=scene.scene_name if scene else scene_id,
            home_id=home_id,
            event_name="scene",
            occurred_at=now_ms(),
            raw={"scene_event": event_name, **raw},
        )
        await self.handle_trigger(
            trigger=trigger,
            perception_service=perception_service,
            rule_service=rule_service,
            miot_service=miot_service,
            meaningful_events_dao=meaningful_events_dao,
            pipeline=pipeline,
        )
