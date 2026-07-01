# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Rule service module
Business logic for rule CRUD and log queries (V3).

V3 validation matrix is enforced via :func:`_validate_rule_consistency` and
applied to every create / update / patch path. PATCH merges the incoming delta
into the persisted Rule before re-running the full matrix so partial updates
cannot leave the rule in an inconsistent state.

Reference: rule-design.md §6.1
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from miloco.database.rule_repo import RuleLogRepo, RuleRepo
from miloco.database.task_repo import TaskRepo

if TYPE_CHECKING:
    from miloco.task_record.service import TaskRecordService
from miloco.middleware.exceptions import (
    BusinessException,
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.miot.client import MiotProxy
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleLogKind,
    RuleMode,
    RuleUpdate,
)

logger = logging.getLogger(__name__)


# ---- Validation ------------------------------------------------------------


# 这些前缀都是"已发生事件通知"的断言性措辞，注入到感知模型 prompt 里时
# 模型会把 query 当成"系统已识别到的事实"而非"待判断条件"，导致连续误触发
# （现场抓到过 caption=无变化 仍触发 / reason 直接复读 query / 模型自承认未观察
# 到但仍触发等案例）。query 应改用进行时状态描述或可观测动作描述。
# 注:前端原本有同款软校验镜像(RuleDrawer),家庭面板 v3 起删除了"约定"UI,
# 当前只剩 backend 这一处校验,无前端镜像需同步。
_FORBIDDEN_QUERY_PREFIXES = (
    "检测到",
    "识别到",
    "感知到",
    "察觉到",
    "已检测",
    "已识别",
    "已发现",
    "已确认",
    "发现了",
)


def _validate_query_phrasing(query: str) -> None:
    q = query.strip()
    for prefix in _FORBIDDEN_QUERY_PREFIXES:
        if q.startswith(prefix):
            raise ValidationException(
                f"condition.query 不能以断言性词 {prefix!r} 开头，"
                "感知模型会把这种措辞当成已发生的事实通知。"
                "请改写为进行时状态或可观测动作描述，例如："
                "'用户正在做出喝水动作（举杯或瓶贴近嘴边并倾斜）'、"
                "'用户从站立或坐姿突然倒地，身体平躺或侧卧不动'。"
                f"当前 query: {query!r}"
            )


def _validate_rule_consistency(rule: Rule) -> None:
    """Apply V3 validation matrix to a fully-formed Rule.

    Raises ValidationException on any violation. See rule-design.md §6.1.
    """
    # ---- 1. condition.query 措辞 ----
    _validate_query_phrasing(rule.condition.query)

    # ---- 2. mode matrix（执行路径由 actions / action_descriptions 哪个非空决定）----
    if rule.mode == RuleMode.EVENT:
        # State-mode-only fields must be empty
        if (
            rule.on_enter_actions
            or rule.on_enter_desc
            or rule.on_exit_actions
            or rule.on_exit_desc
            or rule.on_target_desc
        ):
            raise ValidationException(
                "event mode must not set on_enter_* / on_exit_* / on_target_desc fields"
            )
        if rule.actions and rule.action_descriptions:
            raise ValidationException(
                "event mode: actions and action_descriptions are mutually exclusive"
            )
        if not rule.actions and not rule.action_descriptions:
            raise ValidationException(
                "event mode requires one of actions / action_descriptions"
            )
    else:  # state mode -- 每个方向独立按字段非空选择执行路径
        if rule.actions or rule.action_descriptions:
            raise ValidationException(
                "state mode must not set actions / action_descriptions "
                "(use on_enter_* / on_exit_* instead)"
            )
        enter_static = bool(rule.on_enter_actions)
        enter_dynamic = bool(rule.on_enter_desc)
        exit_static = bool(rule.on_exit_actions)
        exit_dynamic = bool(rule.on_exit_desc)
        if enter_static and enter_dynamic:
            raise ValidationException(
                "state on_enter cannot have both on_enter_actions and on_enter_desc"
            )
        if exit_static and exit_dynamic:
            raise ValidationException(
                "state on_exit cannot have both on_exit_actions and on_exit_desc"
            )
        if not (enter_static or enter_dynamic or exit_static or exit_dynamic):
            raise ValidationException(
                "state mode requires at least one of on_enter / on_exit to be configured"
            )

    # ---- 3. lifecycle ----
    if rule.lifecycle == RuleLifecycle.TEMPORARY and not rule.terminate_when:
        raise ValidationException(
            "lifecycle=temporary requires terminate_when"
        )

    # ---- 4. action idempotent / cooldown 配对 ----
    # idempotent=False 的 action 不会做"读现值后判跳过"，必须靠 cooldown_minutes
    # 限频；否则 runner._execute_action 的冷却分支会被 None 短路掉，每次 ENTERED
    # 都重发 → TTS / 通知风暴。
    for slot_name, slot_actions in (
        ("actions", rule.actions),
        ("on_enter_actions", rule.on_enter_actions),
        ("on_exit_actions", rule.on_exit_actions),
    ):
        for i, a in enumerate(slot_actions):
            if not a.idempotent and a.cooldown_minutes is None:
                raise ValidationException(
                    f"{slot_name}[{i}] (did={a.did}, iid={a.iid}): "
                    f"idempotent=false requires cooldown_minutes"
                )


# ---- Service factory -------------------------------------------------------


async def init_rule_service(miot_proxy: MiotProxy) -> RuleService:
    from miloco.config import get_settings
    from miloco.task_record.service import TaskRecordService

    rule_repo = RuleRepo()
    rule_log_repo = RuleLogRepo()
    sample_interval = get_settings().perception.collect.window_size
    task_record_service = TaskRecordService()
    rule_runner = RuleRunner(
        rules=rule_repo.get_all(enabled_only=False),
        miot_proxy=miot_proxy,
        rule_log_repo=rule_log_repo,
        sample_interval_seconds=sample_interval,
        task_record_service=task_record_service,
    )
    return RuleService(
        rule_repo,
        rule_log_repo,
        rule_runner,
        miot_proxy,
        task_record_service=task_record_service,
    )


class RuleService:
    """Rule service class"""

    def __init__(
        self,
        rule_repo: RuleRepo,
        rule_log_repo: RuleLogRepo,
        rule_runner: RuleRunner,
        miot_proxy: MiotProxy,
        task_repo: TaskRepo | None = None,
        task_record_service: "TaskRecordService | None" = None,
    ):
        self._repo = rule_repo
        self._log_repo = rule_log_repo
        self._runner = rule_runner
        self._miot_proxy = miot_proxy
        self._task_repo = task_repo or TaskRepo()
        if task_record_service is None:
            from miloco.task_record.service import TaskRecordService

            task_record_service = TaskRecordService()
        self._task_record_service = task_record_service

    def _validate_on_target_desc_compat(self, rule: Rule) -> None:
        """on_target_desc 非空 → task 必须有 duration record + target_minutes。

        报错按当前 record 状态分三种 case，每种附可执行的 CLI 修复命令。
        """
        if not rule.on_target_desc:
            return
        kind = self._task_record_service.detect_record_kind(rule.task_id)
        if kind is None:
            raise ValidationException(
                f"on_target_desc 要求 task {rule.task_id!r} 配 duration record + "
                f"target_minutes，但 task 当前无活跃 record。修复："
                f"miloco-cli task record init {rule.task_id} --kind duration "
                f'--content \'{{"target_minutes":N,'
                f'"recurring_pattern":{{"window":"day"}}}}\''
            )
        if kind != "duration":
            raise ValidationException(
                f"on_target_desc 要求 task {rule.task_id!r} 配 duration record，"
                f"当前 record kind={kind!r}（仅 duration 支持累计达标）。修复："
                f"先 miloco-cli task delete {rule.task_id}（连带删 record），"
                f"再 task create + task record init --kind duration"
            )
        state = self._task_record_service.read_duration_target_state(rule.task_id)
        target_minutes = state[0] if state is not None else None
        if target_minutes is None:
            raise ValidationException(
                f"on_target_desc 要求 task {rule.task_id!r} 的 duration record "
                f"设置 target_minutes（当前为空）。修复："
                f"miloco-cli task record update {rule.task_id} "
                f'--patch \'{{"target_minutes":N}}\''
            )

    async def _get_valid_perceive_device_ids(self) -> list[str]:
        """All valid perception device IDs (offline included)."""
        from miloco.manager import get_manager

        devices = await get_manager().perception_service.get_devices(online_only=False)
        return [device.did for device in devices]

    async def _validate_perceive_device_ids(self, dids: list[str]) -> None:
        valid_dids = await self._get_valid_perceive_device_ids()
        invalid = [d for d in dids if d not in valid_dids]
        if invalid:
            raise ValidationException(
                f"Invalid perception device IDs: {', '.join(invalid)}"
            )

    def _fill_default_duration_ratio(self, rule: Rule) -> None:
        """未显式指定时回填 settings.rule.default_duration_ratio。

        优先级：API/CLI 显式 > settings.rule.default_duration_ratio > 代码默认 0.6。
        """
        if rule.duration_ratio is None:
            from miloco.config import get_settings

            rule.duration_ratio = get_settings().rule.default_duration_ratio

    # ---- CRUD ----

    async def create_rule(self, rule: Rule) -> str:
        """Create a new rule with V3 validation matrix.

        方案 P 关键约束（spec §7.3）：rule.task_id 必须对应已存在的 task，
        否则返 404 ``task_not_found``。RuleRepo.create 内部一笔事务同时写
        rule + task_link(kind='rule')，崩在中间整笔回滚不留孤儿。
        """
        if self._repo.exists_by_name(rule.name):
            raise ConflictException(f"Rule name '{rule.name}' already exists")

        if not rule.task_id:
            raise ResourceNotFoundException(
                "task_not_found: rule.task_id is required"
            )
        if not self._task_repo.task_exists(rule.task_id):
            raise ResourceNotFoundException(
                f"task_not_found: rule.task_id={rule.task_id!r} not found"
            )

        self._fill_default_duration_ratio(rule)

        await self._validate_perceive_device_ids(rule.condition.perceive_device_ids)
        _validate_rule_consistency(rule)
        self._validate_on_target_desc_compat(rule)

        rule_id = self._repo.create(rule)
        if not rule_id:
            raise BusinessException("Failed to create rule")

        rule.id = rule_id
        self._runner.add_rule(rule)
        logger.info("Rule created: %s (task_link auto-written)", rule_id)
        return rule_id

    async def get_rule(self, rule_id: str) -> Rule:
        rule = self._repo.get_by_id(rule_id)
        if not rule:
            raise ResourceNotFoundException(f"Rule '{rule_id}' not found")
        return rule

    async def get_all_rules(self, enabled_only: bool = False) -> list[Rule]:
        return self._repo.get_all(enabled_only)

    def notify_record_rollover(
        self,
        task_id: str,
        pre_rollover_state: tuple[int | None, int] | None = None,
    ) -> None:
        """task_record rollover 完成后由 daily job 调入，触发 rule engine 跨日
        强制 on_exit + on_enter + 重 schedule on_target timer。pre_rollover_state
        为 rollover_one 执行前 snapshot 的 ``(target_minutes, accumulated_minutes_today)``，
        用于 rule engine 兜底 fire on_target（旧一天达标但 timer 还没到点的场景）。"""
        self._runner.force_cross_day_reset(task_id, pre_rollover_state)

    def get_enabled_rule_ids(self) -> list[str]:
        """同步返回 runner 内存里 enabled rule 的 ID list（不走 DB）。

        perception client 每 cycle 都要拿这份列表喂 update_state(False)
        给帧级抗抖做"持续 F"确认，是 hot path，不能 await DB。
        """
        return [r.id for r in self._runner.get_enabled_rules()]

    async def update_rule(self, rule: Rule) -> bool:
        """Full update of a rule (re-validates the V3 matrix; previously this
        path skipped consistency checks)."""
        if not rule.id:
            raise ValidationException("Rule ID is required")
        if not self._repo.exists(rule.id):
            raise ResourceNotFoundException(f"Rule '{rule.id}' not found")
        if self._repo.exists_by_name(rule.name, rule.id):
            raise ConflictException(f"Rule name '{rule.name}' already exists")

        self._fill_default_duration_ratio(rule)

        await self._validate_perceive_device_ids(rule.condition.perceive_device_ids)
        _validate_rule_consistency(rule)
        self._validate_on_target_desc_compat(rule)

        success = self._repo.update(rule)
        if success:
            self._runner.add_rule(rule)
        return success

    async def patch_rule(self, rule_id: str, update: RuleUpdate) -> bool:
        """Partial update — merge delta into persisted Rule, then run the full
        V3 matrix on the merged object so partial updates cannot leave the
        rule in an inconsistent state.

        合并语义用 ``update.model_fields_set`` 区分**显式置值**与**未提供**：
        - 字段不在 fields_set → 保留 existing 不动
        - 字段在 fields_set 且非 None → 用新值覆盖
        - 字段在 fields_set 且为 None → 清空（仅对 nullable 字段有意义；
          ``on_enter_desc`` / ``on_exit_desc`` / ``terminate_when`` 是这条
          路径的主要使用者，CLI 的 ``--clear`` 走的就是这里）

        这跟单纯 ``is not None`` 的差别在于：JSON ``null`` 跟"字段缺失"在
        pydantic v2 里都解析成 ``X = None``，只有 ``model_fields_set`` 能
        区分这两种意图。
        """
        existing = self._repo.get_by_id(rule_id)
        if not existing:
            raise ResourceNotFoundException(f"Rule '{rule_id}' not found")

        fields = update.model_fields_set

        if "name" in fields and update.name is not None:
            if self._repo.exists_by_name(update.name, rule_id):
                raise ConflictException(f"Rule name '{update.name}' already exists")
            existing.name = update.name

        if "task_id" in fields and update.task_id is not None:
            existing.task_id = update.task_id

        if "mode" in fields and update.mode is not None:
            existing.mode = update.mode

        if "lifecycle" in fields and update.lifecycle is not None:
            existing.lifecycle = update.lifecycle

        if "enabled" in fields and update.enabled is not None:
            existing.enabled = update.enabled

        if "condition" in fields:
            # condition 不允许显式置 null：Rule.condition 必填，整体清空没语义。
            if update.condition is None:
                raise ValidationException(
                    "condition cannot be cleared (rule must have a condition)"
                )
            # PATCH 语义：只合并 update.condition 里**显式置值**的字段，
            # 缺失字段保留 existing 的值。这样 `--condition "X"` 不带 `--source`
            # 时不会因为 RuleCondition 必填校验直接 422。
            cond_update = update.condition
            cond_fields = cond_update.model_fields_set
            if (
                "perceive_device_ids" in cond_fields
                and cond_update.perceive_device_ids is not None
            ):
                await self._validate_perceive_device_ids(
                    cond_update.perceive_device_ids
                )
                existing.condition.perceive_device_ids = (
                    cond_update.perceive_device_ids
                )
            if "query" in cond_fields and cond_update.query is not None:
                existing.condition.query = cond_update.query

        # list 字段：CLI 用 [] 表达"清空"；不传 → 不动。
        if "actions" in fields and update.actions is not None:
            existing.actions = update.actions

        if "action_descriptions" in fields and update.action_descriptions is not None:
            existing.action_descriptions = update.action_descriptions

        if "on_enter_actions" in fields and update.on_enter_actions is not None:
            existing.on_enter_actions = update.on_enter_actions

        if "on_exit_actions" in fields and update.on_exit_actions is not None:
            existing.on_exit_actions = update.on_exit_actions

        # nullable str 字段：CLI 用 null 表达"清空"，None 是合法新值。
        if "on_enter_desc" in fields:
            existing.on_enter_desc = update.on_enter_desc

        if "on_exit_desc" in fields:
            existing.on_exit_desc = update.on_exit_desc

        if "on_target_desc" in fields:
            existing.on_target_desc = update.on_target_desc

        if "terminate_when" in fields:
            existing.terminate_when = update.terminate_when

        if (
            "exit_debounce_seconds" in fields
            and update.exit_debounce_seconds is not None
        ):
            existing.exit_debounce_seconds = update.exit_debounce_seconds

        # duration_seconds: nullable，None = 清空滑窗
        if "duration_seconds" in fields:
            existing.duration_seconds = update.duration_seconds

        # duration_ratio: DB 读出始终为 concrete float；PATCH None = 不动
        if "duration_ratio" in fields and update.duration_ratio is not None:
            existing.duration_ratio = update.duration_ratio

        _validate_rule_consistency(existing)
        self._validate_on_target_desc_compat(existing)

        success = self._repo.update(existing)
        if success:
            self._runner.add_rule(existing)
        return success

    async def delete_rule(self, rule_id: str) -> bool:
        if not self._repo.exists(rule_id):
            raise ResourceNotFoundException(f"Rule '{rule_id}' not found")

        success = self._repo.delete(rule_id)
        if success:
            self._runner.remove_rule(rule_id)
            self._log_repo.delete_by_rule_id(rule_id)
            self._task_repo.delete_link_by_ref("rule", rule_id)
        return success

    # ---- Trigger ----

    async def trigger_rule(
        self,
        rule_id: str,
        context: str = "",
    ) -> RuleExecuteResult | None:
        """Manual debug trigger -- forwards to RuleRunner.trigger_rule which
        synthesizes a single ENTERED execution without touching frame-diff state.

        Production traffic from the perception engine should call
        :meth:`update_state` directly (per-source per-frame), not this entry.
        """
        return await self._runner.trigger_rule(rule_id, context)

    async def update_state(
        self,
        rule_id: str,
        source_did: str,
        current_bool: bool,
        context: str = "",
        trigger_room: str = "",
        trigger_dids: list[str] | None = None,
        caption: str = "",
        device_name: str = "",
        cycle_source_states: dict[str, bool] | None = None,
    ) -> None:
        """Per-frame, per-source state report from the perception engine.

        See :meth:`RuleRunner.update_state`.
        """
        await self._runner.update_state(
            rule_id, source_did, current_bool, context, trigger_room, trigger_dids,
            caption=caption, device_name=device_name,
            cycle_source_states=cycle_source_states,
        )

    # ---- Logs ----

    async def get_logs(
        self,
        limit: int = 10,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> tuple[list[RuleLog], int]:
        logs = self._log_repo.get_all(
            limit=limit, after_ts=after_ts, before_ts=before_ts, kind=kind
        )
        total = self._log_repo.count_all(
            after_ts=after_ts, before_ts=before_ts, kind=kind
        )
        return logs, total

    async def get_logs_by_rule_id(
        self,
        rule_id: str,
        limit: int = 10,
        after_ts: int | None = None,
        before_ts: int | None = None,
        kind: RuleLogKind | None = None,
    ) -> tuple[list[RuleLog], int]:
        logs = self._log_repo.get_by_rule_id(
            rule_id,
            limit=limit,
            after_ts=after_ts,
            before_ts=before_ts,
            kind=kind,
        )
        total = self._log_repo.count_by_rule_id(
            rule_id, after_ts=after_ts, before_ts=before_ts, kind=kind
        )
        return logs, total

    async def cleanup_logs(self, keep_days: int) -> int:
        return self._log_repo.delete_before_days(keep_days)
