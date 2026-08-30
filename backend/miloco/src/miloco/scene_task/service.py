# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""SceneTaskService — 场景联动任务编排层。

把「task（占位/启停/级联删除）+ state 模式 rule（enter/exit 挂 scene 动作）」
组合成一个 web 可直接管理的单元。rule 的创建/更新走 RuleService 的完整校验
（设备 did / 场景 id / query 措辞 / mode matrix），执行完全复用 RuleRunner 的
命中、抗抖、冷却机制——不经过 agent。

「进入确认时间」（enter_debounce_seconds）复用 Rule 的 duration_seconds +
duration_ratio=1.0：STATE 模式下它是 ENTERED 前置确认门槛——条件必须持续满足
整窗才 fire on_enter（见 rule/schema.py duration_seconds 注释与 runner
_evaluate_duration），恰好对应 web「进入确认时间」语义；0/None = 立即触发。
"""

from __future__ import annotations

import logging
import re
import uuid

from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.rule.schema import (
    SCENE_IID,
    Rule,
    RuleAction,
    RuleCondition,
    RuleConditionUpdate,
    RuleLifecycle,
    RuleMode,
    RuleUpdate,
)
from miloco.scene_task.schema import (
    SceneTaskCreateRequest,
    SceneTaskUpdateRequest,
    SceneTaskView,
)
from miloco.task.schema import TaskCreateRequest, TaskUpdateRequest

logger = logging.getLogger(__name__)

_TASK_ID_UNSAFE = re.compile(r'[^a-z0-9_]+')


class SceneTaskService:
    """场景联动任务服务。

    依赖注入 rule_service / task_service（与 Manager 单例同构），miot_proxy
    仅用于展示场景名（取不到时回退 scene_id，不阻断列表）。
    """

    def __init__(self, rule_service, task_service, miot_proxy=None):
        self._rule_service = rule_service
        self._task_service = task_service
        self._miot_proxy = miot_proxy

    # ---- helpers ----

    @staticmethod
    def _scene_action(scene_id: str, cooldown_minutes: int) -> RuleAction:
        """构造 scene 触发动作（did 借位放 scene_id，idempotent=false 必须配冷却）。"""
        return RuleAction(
            did=scene_id,
            iid=SCENE_IID,
            idempotent=False,
            cooldown_minutes=cooldown_minutes,
        )

    @staticmethod
    def _is_scene_rule(rule: Rule) -> bool:
        """rule 的 enter / exit 任一方向挂了 scene 动作即视为场景联动规则。"""
        return any(a.iid == SCENE_IID for a in rule.on_enter_actions) or any(
            a.iid == SCENE_IID for a in rule.on_exit_actions
        )

    @staticmethod
    def _gen_task_id(description: str) -> str:
        """由任务名生成稳定可读的 snake_case task_id（≤32 字符）。"""
        slug = _TASK_ID_UNSAFE.sub('_', description.lower()).strip('_')[:20]
        if not slug:
            slug = 'scene'
        return f'{slug}_{uuid.uuid4().hex[:6]}'

    async def _scene_name_map(self) -> dict[str, str]:
        """scene_id → scene_name（只含已启用家庭）；取不到时返回空 dict，不阻断。"""
        if self._miot_proxy is None:
            return {}
        try:
            from miloco.miot.filter import filter_by_home

            scenes = (await self._miot_proxy.get_all_scenes()) or {}
            kv = getattr(self._miot_proxy, '_kv_repo', None)
            if kv is not None:
                scenes = filter_by_home(kv, scenes)
            return {
                sid: (getattr(sc, 'scene_name', None) or sid)
                for sid, sc in scenes.items()
            }
        except Exception as e:  # noqa: BLE001
            logger.warning('scene name map unavailable: %s', e)
            return {}

    async def _find_scene_rule(self, task_id: str) -> Rule:
        """按 task_id 找场景联动规则；不存在抛 404。

        RuleService.get_all_rules 是 async（每次现查 DB），必须 await——
        漏 await 会把 coroutine 当可迭代对象，list 推导式直接炸
        TypeError: 'coroutine' object is not iterable。
        """
        rules = await self._rule_service.get_all_rules(enabled_only=False)
        for rule in rules:
            if rule.task_id == task_id and self._is_scene_rule(rule):
                return rule
        raise ResourceNotFoundException(
            f'scene_task_not_found: task_id={task_id!r} 不是场景联动任务或不存在'
        )

    def _to_view(self, rule: Rule, names: dict[str, str]) -> SceneTaskView:
        tv = self._task_service.get_full_view(rule.task_id)
        enter = next((a for a in rule.on_enter_actions if a.iid == SCENE_IID), None)
        exit_a = next((a for a in rule.on_exit_actions if a.iid == SCENE_IID), None)
        cooldown = (
            enter.cooldown_minutes if enter is not None
            else exit_a.cooldown_minutes if exit_a is not None else None
        )
        return SceneTaskView(
            task_id=rule.task_id,
            description=tv.description if tv is not None else rule.name,
            status=tv.status if tv is not None else 'active',
            paused_at=tv.paused_at if tv is not None else None,
            created_at=tv.created_at if tv is not None else (rule.created_at or ''),
            updated_at=rule.updated_at,
            rule_id=rule.id,
            enabled=rule.enabled,
            query=rule.condition.query,
            perceive_device_ids=rule.condition.perceive_device_ids,
            enter_scene_id=enter.did if enter is not None else None,
            enter_scene_name=names.get(enter.did) if enter is not None else None,
            exit_scene_id=exit_a.did if exit_a is not None else None,
            exit_scene_name=names.get(exit_a.did) if exit_a is not None else None,
            cooldown_minutes=cooldown,
            enter_debounce_seconds=rule.duration_seconds or 0,
            exit_debounce_seconds=rule.exit_debounce_seconds,
            max_dwell_seconds=rule.max_dwell_seconds,
        )

    # ---- CRUD ----

    async def list(self) -> list[SceneTaskView]:
        """所有场景联动任务（含已停用）；每 task 只出主规则一条。"""
        rules = [
            r for r in await self._rule_service.get_all_rules(enabled_only=False)
            if self._is_scene_rule(r)
        ]
        by_task: dict[str, Rule] = {}
        for r in sorted(rules, key=lambda x: x.created_at or ''):
            by_task.setdefault(r.task_id, r)
        names = await self._scene_name_map()
        return [self._to_view(r, names) for r in by_task.values()]

    async def get(self, task_id: str) -> SceneTaskView:
        rule = await self._find_scene_rule(task_id)
        return self._to_view(rule, await self._scene_name_map())

    async def create(self, req: SceneTaskCreateRequest) -> SceneTaskView:
        """创建场景联动任务：task 占位 + state rule（完整校验）。

        rule 创建失败（设备 did 非法 / 场景 id 不存在 / query 措辞被拒等）时
        补偿删除刚建的 task 占位，避免留下孤儿任务。
        """
        task_id = self._gen_task_id(req.description)
        self._task_service.create_task(
            TaskCreateRequest(task_id=task_id, description=req.description)
        )
        rule = Rule(
            name=req.description,
            task_id=task_id,
            mode=RuleMode.STATE,
            lifecycle=RuleLifecycle.PERMANENT,
            enabled=req.enabled,
            condition=RuleCondition(
                perceive_device_ids=req.perceive_device_ids,
                query=req.query,
            ),
            on_enter_actions=(
                [self._scene_action(req.enter_scene_id, req.cooldown_minutes)]
                if req.enter_scene_id else []
            ),
            on_exit_actions=(
                [self._scene_action(req.exit_scene_id, req.cooldown_minutes)]
                if req.exit_scene_id else []
            ),
            exit_debounce_seconds=req.exit_debounce_seconds,
            max_dwell_seconds=req.max_dwell_seconds,
            # 进入确认时间：复用 STATE 模式 duration 前置确认门槛（滑窗满且全 True
            # 才 fire on_enter）；0 = 立即触发（duration_seconds=None，现状）。
            duration_seconds=req.enter_debounce_seconds or None,
            duration_ratio=1.0 if req.enter_debounce_seconds else None,
        )
        try:
            await self._rule_service.create_rule(rule)
        except Exception:
            try:
                self._task_service.delete_task(task_id, reason='abandoned')
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    'compensating delete of task %s failed: %s', task_id, e
                )
            raise
        return await self.get(task_id)

    async def update(
        self, task_id: str, req: SceneTaskUpdateRequest
    ) -> SceneTaskView:
        """部分更新：合并进现有 rule 后走 RuleService.patch_rule 全量校验。"""
        rule = await self._find_scene_rule(task_id)
        fields = req.model_fields_set

        if 'description' in fields and req.description is not None:
            self._task_service.update_description(
                task_id, TaskUpdateRequest(description=req.description)
            )

        enter = next((a for a in rule.on_enter_actions if a.iid == SCENE_IID), None)
        exit_a = next((a for a in rule.on_exit_actions if a.iid == SCENE_IID), None)
        cooldown = (
            enter.cooldown_minutes if enter is not None
            else exit_a.cooldown_minutes if exit_a is not None else None
        )
        # 新方向 id / 冷却：字段在 fields 里才变，否则沿用当前值
        new_enter_id = (
            req.enter_scene_id if 'enter_scene_id' in fields
            else (enter.did if enter is not None else None)
        )
        new_exit_id = (
            req.exit_scene_id if 'exit_scene_id' in fields
            else (exit_a.did if exit_a is not None else None)
        )
        new_cooldown = (
            req.cooldown_minutes
            if 'cooldown_minutes' in fields and req.cooldown_minutes is not None
            else cooldown
        )

        update = RuleUpdate()
        if 'description' in fields and req.description is not None:
            update.name = req.description
        cond = RuleConditionUpdate()
        if 'query' in fields and req.query is not None:
            cond.query = req.query
        if 'perceive_device_ids' in fields and req.perceive_device_ids is not None:
            cond.perceive_device_ids = req.perceive_device_ids
        if cond.model_fields_set:
            update.condition = cond
        if 'enter_scene_id' in fields or 'cooldown_minutes' in fields:
            update.on_enter_actions = (
                [self._scene_action(new_enter_id, new_cooldown)]
                if new_enter_id else []
            )
        if 'exit_scene_id' in fields or 'cooldown_minutes' in fields:
            update.on_exit_actions = (
                [self._scene_action(new_exit_id, new_cooldown)]
                if new_exit_id else []
            )
        if 'exit_debounce_seconds' in fields and req.exit_debounce_seconds is not None:
            update.exit_debounce_seconds = req.exit_debounce_seconds
        if 'max_dwell_seconds' in fields:  # None = 清除到期自动退出
            update.max_dwell_seconds = req.max_dwell_seconds
        if 'enter_debounce_seconds' in fields:
            # 0/None = 立即触发；>0 = 持续确认 N 秒（duration_ratio 恒 1.0 =
            # 严格连续满足，防止单次误识别）。清空时 duration_ratio 置 None 会被
            # rule service 当「不动」跳过，但 duration_seconds 已清、残留 ratio
            # 无效果（_evaluate_duration 由 duration_seconds 门控）。
            new_enter_confirm = req.enter_debounce_seconds or None
            update.duration_seconds = new_enter_confirm
            update.duration_ratio = 1.0 if new_enter_confirm else None
        if 'enabled' in fields and req.enabled is not None:
            update.enabled = req.enabled

        if update.model_fields_set:
            await self._rule_service.patch_rule(rule.id, update)
        return await self.get(task_id)

    # ---- 启停 / 删除 / 调试 ----

    def enable(self, task_id: str):
        return self._task_service.enable_task(task_id)

    def disable(self, task_id: str):
        return self._task_service.disable_task(task_id)

    def delete(self, task_id: str):
        """删任务（FK CASCADE 连带清 rule；住户手动删记 abandoned）。"""
        return self._task_service.delete_task(task_id, reason='abandoned')

    async def trigger(self, task_id: str):
        """调试用：手动触发一次 ENTER 场景（走 RuleRunner.trigger_rule）。"""
        rule = await self._find_scene_rule(task_id)
        return await self._rule_service.trigger_rule(
            rule.id, context='web_manual_trigger'
        )