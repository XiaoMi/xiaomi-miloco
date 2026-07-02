from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from miloco.database.kv_repo import KVRepo
from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.miot.schema import (
    MiotEventCatalog,
    MiotEventMapping,
    MiotEventMappingUpdate,
    MiotEventSource,
    MiotEventTrigger,
    MiotEventTriggerLog,
    MiotPropertyFilterCondition,
)
from miloco.perception.types import CaptionEntry, Suggestion
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleLifecycle,
    RuleMode,
    RuleTriggerType,
)
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

_KV_MAPPINGS = "AUTOMATION_MIOT_EVENT_MAPPINGS"

_DEFAULT_MAPPING_QUERY = "画面中是否出现需要提醒用户的人、异常动作或重要变化"
_DEFAULT_MAPPING_ACTION = (
    "请通过已绑定的通知通道提醒用户，说明米家设备触发来源、画面描述、判断结果和建议。"
)


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


def _normalize_bool_like(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and value in (0, 1):
        return str(int(value))
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "on", "yes"}:
            return "1"
        if text in {"0", "false", "off", "no"}:
            return "0"
    return None


def _values_equal(actual: Any, expected: Any) -> bool:
    actual_bool = _normalize_bool_like(actual)
    expected_bool = _normalize_bool_like(expected)
    if actual_bool is not None and expected_bool is not None:
        return actual_bool == expected_bool
    return str(actual) == str(expected)


def _match_condition(actual: Any, expected: Any) -> bool:
    cond = _normalize_filter_condition(expected)
    if cond.op == "any":
        return actual is not None
    if cond.op == "eq":
        return actual is not None and _values_equal(actual, cond.value)
    if cond.op == "ne":
        return actual is not None and not _values_equal(actual, cond.value)
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


def _rule_to_prompt_dict(
    rule,
    *,
    camera_dids: list[str] | None = None,
    trigger_type: str = "perception",
) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "trigger_type": trigger_type,
        "condition": {
            "query": rule.condition.query,
            "perceive_device_ids": camera_dids or [],
        },
    }


def _mapping_rule_name(mapping: MiotEventMapping) -> str:
    name = mapping.source_name_snapshot or mapping.source_id
    suffix = mapping.id[:8] if mapping.id else "new"
    return f"[感知触发] {name} ({suffix})"


def _mapping_rule_query(mapping: MiotEventMapping) -> str:
    query = mapping.query_template.strip()
    if not query:
        return _DEFAULT_MAPPING_QUERY
    return query


def _mapping_to_rule(mapping: MiotEventMapping) -> Rule:
    event_kinds = mapping.event_kinds or ["device_prop"]
    return Rule(
        id=mapping.rule_id,
        name=_mapping_rule_name(mapping),
        task_id="",
        trigger_type=RuleTriggerType.MIOT_EVENT,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=mapping.enabled,
        condition=RuleCondition(
            source_ids=[mapping.source_id],
            event_kinds=event_kinds,
            property_filters=mapping.property_filters,
            mapping_ids=[mapping.id],
            use_global_mapping=False,
            query=_mapping_rule_query(mapping),
        ),
        actions=[],
        action_descriptions=[_DEFAULT_MAPPING_ACTION],
    )


def _build_trigger_context(
    trigger: MiotEventTrigger,
    mappings: list[MiotEventMapping] | None = None,
    spec_meta: dict | None = None,
) -> str:
    names = (spec_meta or {}).get("names") or {}
    values = (spec_meta or {}).get("values") or {}

    def _translate_id(name: str) -> str:
        return names.get(name) or name

    def _translate_changed(props: dict) -> str:
        items: list[str] = []
        for key, val in props.items():
            display_name = names.get(key) or key
            value_map = values.get(key)
            if value_map and val is not None:
                display_val = value_map.get(str(val)) or str(val)
            else:
                display_val = str(val)
            items.append(f"{display_name}={display_val}")
        return "；".join(items)

    label = "触发参数" if trigger.event_name.startswith("event.") else "属性变化"
    parts = [
        "# 米家触发上下文",
        "本次摄像头感知由米家设备事件或属性变化触发。",
        f"触发来源：{trigger.source_name or trigger.source_id}",
        f"触发类型：{_translate_id(trigger.event_name or trigger.source_type)}",
    ]
    if trigger.room_name:
        parts.append(f"触发设备所在房间：{trigger.room_name}")
    if trigger.changed_properties:
        parts.append(f"{label}：{_translate_changed(trigger.changed_properties)}")
    prompts = [_mapping_rule_query(mapping) for mapping in (mappings or [])]
    if prompts:
        parts.append("本轮用户配置的感知提示：")
        for prompt in dict.fromkeys(prompts):
            parts.append(f"- {prompt}")
        parts.append(
            "如果本轮画面中存在符合上述感知提示、或按常规实时感知标准值得提醒用户的事项，"
            "必须同时在 suggestions 中输出：检测到的事件、事件优先级和建议；"
            "不要只写 caption。确实没有可提醒事项时 suggestions 才能为空。"
        )
    parts.append("注意：以上只是触发摄像头查看的原因，不是当前画面事实；规则是否成立必须以本轮视频画面为准。")
    return "\n".join(parts)


def _fallback_suggestion_from_caption(
    trigger: MiotEventTrigger,
    mappings: list[MiotEventMapping],
    captions: list[CaptionEntry],
) -> Suggestion | None:
    """MiOT 触发是显式配置的主动查看；模型只给 caption 时补齐事件提醒格式。"""
    if not captions:
        return None
    caption = captions[0]
    prompts = [
        _mapping_rule_query(mapping)
        for mapping in mappings
        if _mapping_rule_query(mapping)
    ]
    prompt_text = "；".join(dict.fromkeys(prompts))
    event = caption.description.strip().rstrip("。.")
    action_parts = ["请查看回放"]
    if trigger.source_name or trigger.source_id:
        action_parts.append(f"结合触发来源“{trigger.source_name or trigger.source_id}”")
    if prompt_text:
        action_parts.append(f"按感知提示“{prompt_text}”")
    action_parts.append("确认是否需要处理")
    return Suggestion(
        event=event,
        action="，".join(action_parts),
        urgency="low",
        room_name=getattr(caption, "room_name", ""),
        source_device_ids=list(getattr(caption, "source_device_ids", []) or []),
        device_name=getattr(caption, "device_name", ""),
        time_window=getattr(caption, "time_window", ""),
    )


def _format_suggestion_answer(item: dict[str, Any]) -> str:
    lines: list[str] = []
    event = str(item.get("event") or "").strip().rstrip("。.")
    urgency = str(item.get("urgency") or "").strip()
    action = str(item.get("action") or "").strip().rstrip("。.")
    if event:
        lines.append(f"检测到：{event}")
    if urgency:
        lines.append(f"事件优先级：{urgency}")
    if action:
        lines.append(f"建议：{action}")
    return "\n".join(lines)


class AutomationService:
    def __init__(self, kv_repo: KVRepo):
        self._kv_repo = kv_repo
        self._cooldowns: dict[str, float] = {}

    def _load_mappings(self) -> list[MiotEventMapping]:
        raw = self._kv_repo.get(_KV_MAPPINGS, "[]") or "[]"
        try:
            loaded = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load automation mappings: %s", e)
            return []
        mappings: list[MiotEventMapping] = []
        for item in loaded:
            try:
                mappings.append(MiotEventMapping.model_validate(item))
            except Exception as e:  # noqa: BLE001
                logger.warning("Skip invalid automation mapping: %s", e)
        return mappings

    def _save_mappings(self, mappings: list[MiotEventMapping]) -> None:
        self._kv_repo.set(
            _KV_MAPPINGS,
            json.dumps([m.model_dump(mode="json") for m in mappings], ensure_ascii=False),
        )

    def _save_mapping(self, updated: MiotEventMapping) -> None:
        mappings = self._load_mappings()
        for idx, mapping in enumerate(mappings):
            if mapping.id == updated.id:
                mappings[idx] = updated
                self._save_mappings(mappings)
                return
        mappings.insert(0, updated)
        self._save_mappings(mappings)

    async def list_catalog(self, miot_service) -> MiotEventCatalog:
        devices = await miot_service.get_miot_device_list()
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
                value = getattr(update, field)
                if value is not None:
                    setattr(mapping, field, value)
            mapping.updated_at = now_ms()
            self._save_mappings(mappings)
            return mapping
        raise ResourceNotFoundException(f"Mapping '{mapping_id}' not found")

    def delete_mapping(self, mapping_id: str) -> None:
        mappings = [m for m in self._load_mappings() if m.id != mapping_id]
        self._save_mappings(mappings)

    def get_mapping(self, mapping_id: str) -> MiotEventMapping:
        for mapping in self._load_mappings():
            if mapping.id == mapping_id:
                return mapping
        raise ResourceNotFoundException(f"Mapping '{mapping_id}' not found")

    async def sync_mapping_rule(self, mapping: MiotEventMapping, rule_service) -> MiotEventMapping:
        """Keep the UI mapping backed by a real miot_event rule.

        The mapping remains the only user-facing configuration object, while
        the linked Rule gives it normal rule lifecycle, state and notifications.
        """
        if not mapping.id:
            mapping.id = str(uuid.uuid4())
        rule = _mapping_to_rule(mapping)
        if mapping.rule_id:
            try:
                await rule_service.update_rule(rule)
            except ResourceNotFoundException:
                mapping.rule_id = ""
                rule.id = ""
            else:
                self._save_mapping(mapping)
                return mapping
        rule_id = await rule_service.create_rule(rule)
        mapping.rule_id = rule_id
        mapping.updated_at = now_ms()
        self._save_mapping(mapping)
        return mapping

    async def delete_mapping_rule(self, mapping: MiotEventMapping, rule_service) -> None:
        if not mapping.rule_id:
            return
        try:
            await rule_service.delete_rule(mapping.rule_id)
        except ResourceNotFoundException:
            return

    async def ensure_mapping_rules(self, rule_service) -> None:
        for mapping in self._load_mappings():
            if mapping.rule_id:
                continue
            try:
                await self.sync_mapping_rule(mapping, rule_service)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to sync automation mapping rule: mapping_id=%s",
                    mapping.id,
                    exc_info=True,
                )

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
        all_mappings: list[MiotEventMapping],
    ) -> list[MiotEventMapping]:
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
        all_mappings: list[MiotEventMapping],
    ) -> list[MiotEventMapping]:
        return [
            mapping
            for mapping in all_mappings
            if self._match_mapping(mapping, trigger)
        ]

    def _cooldown_key(self, trigger: MiotEventTrigger, mapping: MiotEventMapping) -> str:
        return f"{trigger.source_type}:{trigger.source_id}:{mapping.id}"

    async def _build_spec_meta(
        self, trigger: MiotEventTrigger, miot_service
    ) -> dict | None:
        """构建 spec_meta，把 event_name/changed_properties 的裸 ID 翻译成人类可读语义。

        返回 {"names": {key: readable}, "values": {key: {value: desc}}}。
        失败返回 None，调用方回退到裸 ID（不破坏功能）。
        """
        try:
            proxy = miot_service._miot_proxy
            device = proxy._device_info_dict.get(trigger.source_id)
            if not device:
                return None
            urn = getattr(device, "urn", "") or ""
            if not urn:
                return None
            names: dict[str, str] = {}
            values: dict[str, dict[str, str]] = {}
            # properties（changed_properties 的 prop.x.y）
            spec = await proxy._fetch_device_spec(urn=urn)
            if spec:
                for iid, item in spec.items():
                    if not iid.startswith("prop."):
                        continue
                    names[iid] = (
                        item.get("description")
                        or item.get("prop_description")
                        or iid
                    )
                    vl = item.get("value_list") or []
                    if vl:
                        values[iid] = {
                            str(v.get("value", "")): (
                                v.get("description")
                                or v.get("name")
                                or str(v.get("value", ""))
                            )
                            for v in vl
                        }
                    elif item.get("format") == "bool":
                        values[iid] = {"0": "关", "1": "开"}
            # events（event_name 的 event.x.y + arg.x.y）
            spec_device = await proxy.miot_client.spec_parser.parse_async(urn=urn)
            if spec_device:
                for service in spec_device.services:
                    for event in service.events:
                        event_key = f"event.{service.iid}.{event.iid}"
                        event_name = (
                            f"{service.description_trans} {event.description_trans}"
                            if service.description_trans != event.description_trans
                            else event.description_trans
                        )
                        names[event_key] = event_name or event.description or event_key
                        for prop in event.arguments:
                            arg_key = f"arg.{service.iid}.{prop.iid}"
                            arg_name = (
                                f"{service.description_trans} {prop.description_trans}"
                                if service.description_trans != prop.description_trans
                                else prop.description_trans
                            )
                            names[arg_key] = arg_name or prop.description or arg_key
                            if prop.value_list:
                                values[arg_key] = {
                                    str(v.value): (v.description or v.name or str(v.value))
                                    for v in prop.value_list
                                }
            return {"names": names, "values": values}
        except Exception:
            logger.debug(
                "build_spec_meta failed for did=%s", trigger.source_id, exc_info=True
            )
            return None

    async def handle_trigger(
        self,
        *,
        trigger: MiotEventTrigger,
        perception_service,
        rule_service,
        miot_service,
        meaningful_events_dao,
    ) -> MiotEventTriggerLog:
        await self.ensure_mapping_rules(rule_service)
        all_rules = await rule_service.get_all_rules(enabled_only=True)
        candidate_rules = [rule for rule in all_rules if self._match_rule(rule, trigger)]
        all_mappings = self._load_mappings()
        mapping_by_id = {
            mapping.id: mapping
            for mapping in self._collect_direct_mappings(trigger, all_mappings)
        }
        for mapping in self._collect_mappings_for_rules(
            trigger,
            candidate_rules,
            all_mappings,
        ):
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
            return log_item

        active_mapping_ids = {mapping.id for mapping in active_mappings}
        candidate_rules = [
            rule
            for rule in candidate_rules
            if not rule.condition.mapping_ids
            or rule.condition.use_global_mapping
            or any(mapping_id in active_mapping_ids for mapping_id in rule.condition.mapping_ids)
        ]
        log_item.candidate_rule_ids = [rule.id for rule in candidate_rules]

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
            return log_item

        perception_rules = [
            rule
            for rule in all_rules
            if getattr(
                getattr(rule, "trigger_type", RuleTriggerType.PERCEPTION),
                "value",
                getattr(rule, "trigger_type", RuleTriggerType.PERCEPTION),
            )
            == RuleTriggerType.PERCEPTION.value
        ]
        prompt_rules = [
            rule.model_dump(mode="json") if hasattr(rule, "model_dump") else _rule_to_prompt_dict(rule)
            for rule in perception_rules
        ]
        prompt_rules.extend(
            _rule_to_prompt_dict(rule, camera_dids=camera_ids)
            for rule in candidate_rules
        )
        real_rule_ids = {rule.id for rule in candidate_rules}
        log_item.perception_started = True
        spec_meta = await self._build_spec_meta(trigger, miot_service)
        try:
            (
                result,
                early_sent_contents,
                early_sent_rule_ids,
                early_sent_sugg_ids,
                artifacts,
            ) = await perception_service.external_trigger_perceive(
                camera_ids,
                prompt_rules,
                extra_context_by_did={
                    did: _build_trigger_context(trigger, active_mappings, spec_meta)
                    for did in camera_ids
                },
            )
        except Exception as e:  # noqa: BLE001
            log_item.error = str(e)
            return log_item

        if result is not None and not result.suggestions:
            fallback_suggestion = _fallback_suggestion_from_caption(
                trigger,
                active_mappings,
                result.caption,
            )
            if fallback_suggestion is not None:
                result.suggestions.append(fallback_suggestion)

        captions = [entry.description for entry in (result.caption if result else [])]
        suggestions = [
            suggestion.model_dump(mode="json")
            for suggestion in (result.suggestions if result else [])
        ]
        structured_matched_rules = [
            matched.model_dump(mode="json")
            for matched in (result.matched_rules if result else [])
        ]
        matched_rule_ids = [matched["rule_id"] for matched in structured_matched_rules]
        answer_parts: list[str] = []
        if captions:
            answer_parts.append("画面观察：" + "；".join(captions))
        if structured_matched_rules:
            answer_parts.append(
                "命中判断："
                + "；".join(
                    f"{item.get('rule_name') or item['rule_id']}：{item.get('reason', '')}"
                    for item in structured_matched_rules
                )
            )
        if suggestions:
            answer_parts.append(
                "\n\n".join(
                    block for item in suggestions
                    if (block := _format_suggestion_answer(item))
                )
            )
        answer = "\n".join(part for part in answer_parts if part)
        log_item.perception_answer = answer
        log_item.captions = captions
        log_item.suggestions = suggestions
        log_item.structured_matched_rules = structured_matched_rules

        log_item.matched_rule_ids = matched_rule_ids

        persist_result = await perception_service.handle_structured_perception_result(
            result=result,
            early_sent_contents=early_sent_contents,
            early_sent_rule_ids=early_sent_rule_ids,
            early_sent_sugg_ids=early_sent_sugg_ids,
            device_ids=camera_ids,
            artifacts=artifacts,
            event_id=log_item.id,
            timestamp_ms=trigger.occurred_at or now_ms(),
            text_prefix=(
                "[米家设备触发]\n"
                f"来源：{trigger.source_name or trigger.source_id}\n"
                f"事件：{(spec_meta or {}).get('names', {}).get(trigger.event_name) or trigger.event_name}"
            ),
            payload_extra={
                "trigger_source": "miot",
                "trigger": trigger.model_dump(mode="json"),
                "automation_log_id": log_item.id,
            },
            home_id=trigger.home_id,
            pulse_reset_rule_ids=real_rule_ids,
            force_persist=bool(answer),
        )
        if persist_result is not None:
            log_item.clip_kind = persist_result.clip_kind or ""
            log_item.clip_device_ids = [
                device_id for device_id in camera_ids if device_id in artifacts.clips
            ] if persist_result.snapshot_count > 0 else []
        if answer and not matched_rule_ids:
            try:
                event_label = (spec_meta or {}).get("names", {}).get(trigger.event_name) or trigger.event_name
                await miot_service.send_notify(
                    f"[米家设备触发]\n来源：{trigger.source_name or trigger.source_id}\n事件：{event_label}\n{answer}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("automation notify failed: %s", e)
        return log_item

