# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""SceneTaskService — 场景联动任务编排层。

把「task（占位/启停/级联删除）+ state 模式 rule（enter/exit 挂动作）」
组合成一个 web 可直接管理的单元。rule 的创建/更新走 RuleService 的完整校验
（设备 did / 场景 id / 音响 did / query 措辞 / mode matrix），执行完全复用
RuleRunner 的命中、抗抖、冷却机制——不经过 agent。

一个方向（进入 / 退出）可以同时挂两类动作：
  - ``iid="scene"``     → 触发米家自动化场景（``did`` 位置是 scene_id）
  - ``iid="scene_tts"`` → 用指定音响播报 TTS（``did`` 位置是音响 did、``tts_text`` 静态
    兜底文案）；动态文案由 LLM 在命中帧上生成（``RuleCondition.tts_enabled`` +
    scene_notes 里的「## 动态播报要求」段），随命中经 MatchedRule.tts 交给 runner。

「进入确认时间」（enter_debounce_seconds）复用 Rule 的 duration_seconds +
duration_ratio=1.0：STATE 模式下它是 ENTERED 前置确认门槛——条件必须持续满足
整窗才 fire on_enter（见 rule/schema.py duration_seconds 注释与 runner
_evaluate_duration），恰好对应 web「进入确认时间」语义；0/None = 立即触发。
"""

from __future__ import annotations

import logging
import re
import uuid

from miloco.middleware.exceptions import ResourceNotFoundException, ValidationException
from miloco.rule.schema import (
    SCENE_IID,
    SCENE_TTS_IID,
    Rule,
    RuleAction,
    RuleCondition,
    RuleConditionUpdate,
    RuleLifecycle,
    RuleMode,
    RuleUpdate,
    SceneTtsSlot,
)
from miloco.scene_task.schema import (
    SceneTaskCreateRequest,
    SceneTaskUpdateRequest,
    SceneTaskView,
    SceneTtsView,
    SceneVoiceDevice,
    compose_tts_prompt,
    merge_scene_notes,
    normalize_tts_slot,
    parse_tts_prompts,
    split_scene_notes,
)
from miloco.task.schema import TaskCreateRequest, TaskUpdateRequest

logger = logging.getLogger(__name__)

_TASK_ID_UNSAFE = re.compile(r'[^a-z0-9_]+')


def resolve_tts_slot(
    slot: SceneTtsSlot | None,
    direction: str,
    previous: RuleAction | None,
    previous_prompt: str | None = None,
) -> SceneTtsSlot | None:
    """PATCH 提交的 TTS 配置 → 要落库的 slot。

    两条归一规则（与「播报要求 = 动态方向的文案要求」这个单一含义对齐）：

    - ``tts_content=True`` 但没写「播报要求」：沿用该方向原来的要求；原来没有 → 内置
      缺省要求（prompt 是**动态方向专属**的，只给固定方向配 prompt 会被这里丢掉）；
    - 没提交兜底文案（None）= 沿用旧动作上的静态文案（前端没传 tts_text 不意味着清空）。
    """
    slot = normalize_tts_slot(slot)
    if slot is None:
        return None
    if slot.tts_content:
        if not slot.tts_prompt:
            slot.tts_prompt = previous_prompt or prompt_default(direction)
    else:
        # 固定文案方向不需要「播报要求」；残留的 prompt 只会让 prompt / prompt_slot
        # 两条信息互相打架（模型也看不到它）。
        slot.tts_prompt = None
    if slot.tts_text is None and previous is not None:
        slot.tts_text = previous.tts_text
    return slot


def prompt_default(direction: str) -> str:
    """缺省写作要求（规则行里没写要求时模型按这句生成）。"""
    return (
        '用一句自然、亲切的话结合本轮画面'
        + ('描述或夸赞' if direction == 'enter' else '描述，与画面里的人或场景道别')
    )

class SceneTaskService:
    """场景联动任务服务。

    依赖注入 rule_service / task_service（与 Manager 单例同构），miot_proxy
    仅用于展示场景名 / 音响名与判断动态 TTS 是否可用（取不到时降级，不阻断列表）。
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
    def _tts_action(
        device_id: str, slot: SceneTtsSlot | None, cooldown_minutes: int
    ) -> RuleAction:
        """构造 TTS 动作（did 借位放音响 did，文案不进 RuleAction 的 params）。"""
        return RuleAction(
            did=device_id,
            iid=SCENE_TTS_IID,
            idempotent=False,
            cooldown_minutes=cooldown_minutes,
            tts_text=(slot.tts_text if slot is not None else None) or None,
        )

    @staticmethod
    def _direction_actions(
        scene_id: str | None,
        tts_device_id: str | None,
        tts_slot: SceneTtsSlot | None,
        cooldown_minutes: int,
    ) -> list[RuleAction]:
        """一个方向的完整动作列表：自动化场景 + TTS 可同时存在。"""
        actions: list[RuleAction] = []
        if scene_id:
            actions.append(
                SceneTaskService._scene_action(scene_id, cooldown_minutes)
            )
        if tts_device_id:
            actions.append(
                SceneTaskService._tts_action(tts_device_id, tts_slot, cooldown_minutes)
            )
        return actions

    @staticmethod
    def _is_scene_rule(rule: Rule) -> bool:
        """rule 的 enter / exit 任一方向挂了场景或 TTS 动作即视为场景联动规则。"""
        return any(
            a.iid in (SCENE_IID, SCENE_TTS_IID)
            for a in (*rule.on_enter_actions, *rule.on_exit_actions)
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

    async def _voice_name_map(self) -> dict[str, str]:
        """音响 did → 显示名（只含已启用家庭）；取不到时返回空 dict。"""
        if self._miot_proxy is None:
            return {}
        try:
            from miloco.miot.filter import filter_by_home

            devices = (await self._miot_proxy.get_voice_devices()) or {}
            kv = getattr(self._miot_proxy, '_kv_repo', None)
            if kv is not None:
                devices = filter_by_home(kv, devices)
            return {
                did: (getattr(d, 'name', None) or did)
                for did, d in devices.items()
            }
        except Exception as e:  # noqa: BLE001
            logger.warning('voice name map unavailable: %s', e)
            return {}

    @staticmethod
    def _dynamic_tts_ready() -> bool:
        """动态 TTS 当前能不能拿到文案：感知引擎已就绪 **且** 开着纯场景触发（rule_only）。

        纯场景触发模式的 omni 输出才有 matched_rules（含 tts），感知全量模式下动态文案
        拿不到；配置本身仍合法——真正播报时会退回静态兜底文案，VIEW 里据此提示住户。
        运行中改这个开关要重建引擎，故这里按当前 settings 做提示即可。
        """
        try:
            from miloco.config import get_settings
            from miloco.manager import get_manager

            engine_cfg = get_settings().perception.engine
            if not bool(engine_cfg.get('rule_only', False)):
                return False
            # 引擎没起（缺 key / 模型未下完 / 已停止）时感知不会跑，动态文案自然拿不到。
            return bool(get_manager().perception_service.engine_status().engine.ready)
        except Exception:  # noqa: BLE001
            return False

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

    @staticmethod
    def _direction_view(
        actions: list[RuleAction],
        names: dict[str, str],
        tts_enabled: bool,
        tts_prompt: str | None,
    ) -> tuple[str | None, SceneTtsView | None]:
        """一个方向的动作 → (scene_id, TTS 视图)。"""
        scene = next((a for a in actions if a.iid == SCENE_IID), None)
        tts = next((a for a in actions if a.iid == SCENE_TTS_IID), None)
        if tts is None:
            return (scene.did if scene is not None else None), None
        # tts_content：规则开了动态 TTS 且这个方向有「播报要求」行 → 该方向是动态文案。
        # 住户可能进入用动态、退出用固定文案（反之亦然），按方向各自判定。
        dynamic = bool(tts_enabled and tts_prompt)
        return (
            scene.did if scene is not None else None,
            SceneTtsView(
                device_id=tts.did,
                device_name=names.get(tts.did),
                tts_content=dynamic,
                tts_prompt=tts_prompt,
                tts_text=tts.tts_text,
            ),
        )

    def _to_view(
        self, rule: Rule, names: dict[str, str], voice_names: dict[str, str]
    ) -> SceneTaskView:
        tv = self._task_service.get_full_view(rule.task_id)
        enter_scene, enter_tts = self._direction_view(
            rule.on_enter_actions,
            voice_names,
            rule.condition.tts_enabled,
            self._enter_tts_prompt(rule),
        )
        exit_scene, exit_tts = self._direction_view(
            rule.on_exit_actions,
            voice_names,
            rule.condition.tts_enabled,
            self._exit_tts_prompt(rule),
        )
        entry_action = next(
            (a for a in (*rule.on_enter_actions, *rule.on_exit_actions)), None
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
            scene_notes=split_scene_notes(rule.condition.scene_notes),
            perceive_device_ids=rule.condition.perceive_device_ids,
            enter_scene_id=enter_scene,
            enter_scene_name=names.get(enter_scene) if enter_scene else None,
            exit_scene_id=exit_scene,
            exit_scene_name=names.get(exit_scene) if exit_scene else None,
            enter_tts=enter_tts,
            exit_tts=exit_tts,
            cooldown_minutes=(
                entry_action.cooldown_minutes if entry_action is not None else None
            ),
            enter_debounce_seconds=rule.duration_seconds or 0,
            exit_debounce_seconds=rule.exit_debounce_seconds,
            max_dwell_seconds=rule.max_dwell_seconds,
            dynamic_tts_ready=(
                self._dynamic_tts_ready()
                if (enter_tts and enter_tts.tts_content)
                or (exit_tts and exit_tts.tts_content)
                else True
            ),
        )

    @staticmethod
    def _tts_prompts(rule: Rule) -> tuple[str | None, str | None]:
        """从 scene_notes 的「## 动态播报要求」段拆出 (进入要求, 退出要求)。"""
        seconds = rule.condition.scene_notes or ''
        idx = seconds.find('\n## 动态播报要求')
        return parse_tts_prompts(seconds[idx:] if idx >= 0 else seconds)

    @classmethod
    def _enter_tts_prompt(cls, rule: Rule) -> str | None:
        return cls._tts_prompts(rule)[0]

    @classmethod
    def _exit_tts_prompt(cls, rule: Rule) -> str | None:
        return cls._tts_prompts(rule)[1]

    @staticmethod
    def _condition_tts_fields(
        enter_tts: SceneTtsSlot | None,
        exit_tts: SceneTtsSlot | None,
        user_notes: str | None,
    ) -> tuple[bool, str | None]:
        """(tts_enabled, 最终 scene_notes)：住户原文 + 合成的「## 动态播报要求」段。"""
        tts_enabled = any(
            slot is not None and slot.tts_content for slot in (enter_tts, exit_tts)
        )
        section = (
            compose_tts_prompt(
                enter_tts if enter_tts and enter_tts.tts_content else None,
                exit_tts if exit_tts and exit_tts.tts_content else None,
            )
            if tts_enabled
            else None
        )
        return tts_enabled, merge_scene_notes(user_notes, section)

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
        voice_names = await self._voice_name_map()
        return [self._to_view(r, names, voice_names) for r in by_task.values()]

    async def get(self, task_id: str) -> SceneTaskView:
        rule = await self._find_scene_rule(task_id)
        return self._to_view(
            rule, await self._scene_name_map(), await self._voice_name_map()
        )

    async def list_voice_devices(self, *, refresh: bool = True) -> list[SceneVoiceDevice]:
        """可选音响列表（与场景列表同口径：已启用家庭内）。

        ``refresh=True`` 时先从米家云刷新设备表——web 进入场景联动设置页即调用，避免新
        买的音箱要等下一轮设备刷新才出现；刷新失败回落缓存，不抛错（列表宁旧不空）。
        """
        if self._miot_proxy is None:
            return []
        from miloco.miot.filter import filter_by_home

        devices = None
        if refresh:
            devices = await self._miot_proxy.refresh_voice_devices()
        if devices is None:
            devices = await self._miot_proxy.get_voice_devices()
        kv = getattr(self._miot_proxy, '_kv_repo', None)
        if kv is not None:
            devices = filter_by_home(kv, devices or {})
        return [
            SceneVoiceDevice(
                did=d.did,
                name=getattr(d, 'name', None) or d.did,
                room_name=getattr(d, 'room_name', None),
                online=bool(getattr(d, 'online', False)),
            )
            for d in sorted(
                (devices or {}).values(),
                key=lambda x: (getattr(x, 'name', '') or '', x.did),
            )
        ]

    async def create(self, req: SceneTaskCreateRequest) -> SceneTaskView:
        """创建场景联动任务：task 占位 + state rule（完整校验）。

        rule 创建失败（设备 did 非法 / 场景 id 不存在 / 音响 did 非法 / query 措辞被拒等）
        时补偿删除刚建的 task 占位，避免留下孤儿任务。
        """
        enter_tts = normalize_tts_slot(req.enter_tts)
        exit_tts = normalize_tts_slot(req.exit_tts)
        tts_enabled, scene_notes = self._condition_tts_fields(
            enter_tts, exit_tts, req.scene_notes
        )

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
                # 空白串归一到 None（"配了但没内容" = 未注入，避免渲染空分节）。
                scene_notes=scene_notes,
                tts_enabled=tts_enabled,
            ),
            on_enter_actions=self._direction_actions(
                req.enter_scene_id,
                req.enter_tts_device_id,
                enter_tts,
                req.cooldown_minutes,
            ),
            on_exit_actions=self._direction_actions(
                req.exit_scene_id,
                req.exit_tts_device_id,
                exit_tts,
                req.cooldown_minutes,
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

        enter_scene_a = next(
            (a for a in rule.on_enter_actions if a.iid == SCENE_IID), None
        )
        exit_scene_a = next(
            (a for a in rule.on_exit_actions if a.iid == SCENE_IID), None
        )
        enter_tts_a = next(
            (a for a in rule.on_enter_actions if a.iid == SCENE_TTS_IID), None
        )
        exit_tts_a = next(
            (a for a in rule.on_exit_actions if a.iid == SCENE_TTS_IID), None
        )
        first_action = next(
            (a for a in (*rule.on_enter_actions, *rule.on_exit_actions)), None
        )
        cooldown = first_action.cooldown_minutes if first_action is not None else None
        new_cooldown = (
            req.cooldown_minutes
            if 'cooldown_minutes' in fields and req.cooldown_minutes is not None
            else cooldown
        )

        # ---- 进入方向：场景 id / 音响 did / TTS 配置三者任一变化 → 重建动作列表 ----
        enter_touched = bool(
            {'enter_scene_id', 'enter_tts_device_id', 'enter_tts', 'cooldown_minutes'}
            & fields
        )
        exit_touched = bool(
            {'exit_scene_id', 'exit_tts_device_id', 'exit_tts', 'cooldown_minutes'}
            & fields
        )
        enter_scene_id = (
            req.enter_scene_id if 'enter_scene_id' in fields
            else (enter_scene_a.did if enter_scene_a is not None else None)
        )
        exit_scene_id = (
            req.exit_scene_id if 'exit_scene_id' in fields
            else (exit_scene_a.did if exit_scene_a is not None else None)
        )
        enter_dev_id = (
            req.enter_tts_device_id if 'enter_tts_device_id' in fields
            else (enter_tts_a.did if enter_tts_a is not None else None)
        )
        exit_dev_id = (
            req.exit_tts_device_id if 'exit_tts_device_id' in fields
            else (exit_tts_a.did if exit_tts_a is not None else None)
        )
        # 只改文案（不换音响）时沿用原动作；换了音响 / 提交了新配置就整体替换。
        old_enter_prompt, old_exit_prompt = self._tts_prompts(rule)
        if 'enter_tts' in fields:
            enter_tts = resolve_tts_slot(
                req.enter_tts, 'enter', enter_tts_a, old_enter_prompt
            )
        elif enter_tts_a is not None:
            enter_tts = SceneTtsSlot(
                tts_content=bool(self._enter_tts_prompt(rule)),
                tts_prompt=self._enter_tts_prompt(rule),
                tts_text=enter_tts_a.tts_text,
            )
        else:
            enter_tts = None
        if 'exit_tts' in fields:
            exit_tts = resolve_tts_slot(
                req.exit_tts, 'exit', exit_tts_a, old_exit_prompt
            )
        elif exit_tts_a is not None:
            exit_tts = SceneTtsSlot(
                tts_content=bool(self._exit_tts_prompt(rule)),
                tts_prompt=self._exit_tts_prompt(rule),
                tts_text=exit_tts_a.tts_text,
            )
        else:
            exit_tts = None

        # ---- condition：scene_notes / tts_enabled 由 TTS 配置与住户原文重新合成 ----
        # 规则级只有一个 tts_enabled + 一段 scene_notes，而进入 / 退出方向的「播报要求」
        # 两行都写在同一段里：只改一侧时必须保留另一侧原有的要求，否则会把它的动态 TTS
        # 悄悄降级成「没要求」。
        user_notes = (
            req.scene_notes if 'scene_notes' in fields
            else split_scene_notes(rule.condition.scene_notes)
        )

        # 只改一侧时，另一侧的动态文案照旧生效，不能因为这次 PATCH 没带它就被清空；
        # 有方向被改成固定文案 / 被移除时，要把它的「要求」行从 scene_notes 里拿掉，
        # 否则模型会继续为它生成文案（规则级开关会一直留着）。
        dynamic_touched = bool(
            {'enter_tts', 'exit_tts', 'enter_tts_device_id', 'exit_tts_device_id'}
            & fields
        )
        if dynamic_touched:
            # 没提交新要求的动态方向沿用旧要求；固定文案方向本就没有要求（清掉）。
            enter_prompt = (
                (
                    old_enter_prompt
                    if 'enter_tts' not in fields and old_enter_prompt
                    else enter_tts.tts_prompt
                )
                if enter_tts is not None and enter_tts.tts_content
                else None
            )
            exit_prompt = (
                (
                    old_exit_prompt
                    if 'exit_tts' not in fields and old_exit_prompt
                    else exit_tts.tts_prompt
                )
                if exit_tts is not None and exit_tts.tts_content
                else None
            )
            tts_enabled, new_notes = self._condition_tts_fields(
                SceneTtsSlot(tts_content=True, tts_prompt=enter_prompt)
                if enter_prompt else None,
                SceneTtsSlot(tts_content=True, tts_prompt=exit_prompt)
                if exit_prompt else None,
                user_notes,
            )
        else:
            # 没动 TTS 配置：tts_enabled 不动，scene_notes 只在住户改了它时重写（保留
            # 原有「## 动态播报要求」段）。
            tts_enabled = rule.condition.tts_enabled
            new_notes = merge_scene_notes(
                user_notes,
                compose_tts_prompt(
                    SceneTtsSlot(tts_content=True, tts_prompt=old_enter_prompt)
                    if tts_enabled and old_enter_prompt else None,
                    SceneTtsSlot(tts_content=True, tts_prompt=old_exit_prompt)
                    if tts_enabled and old_exit_prompt else None,
                )
                if tts_enabled
                else None,
            )

        enter_actions = self._direction_actions(
            enter_scene_id, enter_dev_id, enter_tts, new_cooldown or 1
        )
        exit_actions = self._direction_actions(
            exit_scene_id, exit_dev_id, exit_tts, new_cooldown or 1
        )
        if not enter_actions and not exit_actions:
            raise ValidationException(
                '进入 / 退出至少配置一个动作（触发自动化场景或播报 TTS）'
            )

        update = RuleUpdate()
        if 'description' in fields and req.description is not None:
            update.name = req.description
        cond = RuleConditionUpdate()
        if 'query' in fields and req.query is not None:
            cond.query = req.query
        if 'perceive_device_ids' in fields and req.perceive_device_ids is not None:
            cond.perceive_device_ids = req.perceive_device_ids
        if 'scene_notes' in fields or dynamic_touched:
            cond.scene_notes = new_notes
        if dynamic_touched:
            cond.tts_enabled = tts_enabled
        if cond.model_fields_set:
            update.condition = cond
        if enter_touched:
            update.on_enter_actions = enter_actions
        if exit_touched:
            update.on_exit_actions = exit_actions
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
        """调试用：手动触发一次 ENTER 动作（走 RuleRunner.trigger_rule）。

        动态 TTS 没有「本轮画面」，模型不会产出文案——真正播报时用该动作的静态兜底
        文案；没配兜底文案则这次播报判 skipped（自动化场景动作照常执行）。
        """
        rule = await self._find_scene_rule(task_id)
        return await self._rule_service.trigger_rule(
            rule.id, context='web_manual_trigger'
        )


__all__ = ['SceneTaskService']
