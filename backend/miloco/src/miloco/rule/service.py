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
from typing import TYPE_CHECKING, Any

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
from miloco.miot.filter import allowed_home_ids, filter_by_home
from miloco.rule.record_source import (
    MILESTONE_SENTINEL_DID,
    RECORD_SOURCE_TYPE,
    SUPPORTED_KIND,
    SUPPORTED_OP,
)
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    SCENE_IID,
    Rule,
    RuleDirection,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleLogKind,
    RuleMode,
    RuleUpdate,
    TriggerOutcome,
    parse_device_iid,
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


def _validate_lifecycle(rule: Rule) -> None:
    if rule.lifecycle == RuleLifecycle.TEMPORARY and not rule.terminate_when:
        raise ValidationException("lifecycle=temporary requires terminate_when")


def _validate_milestone_rule(rule: Rule) -> None:
    """direction=milestone 的形态 (spec §9)。

    单独一支是因为下面的矩阵按 ``mode`` 分，而 mode 表达不了 milestone —— milestone
    rule 的 mode 只是个占位值。
    """
    if (
        rule.on_enter_actions
        or rule.on_enter_desc
        or rule.on_exit_actions
        or rule.on_exit_desc
    ):
        raise ValidationException(
            "direction=milestone 只有达标一个方向，不能配 on_enter_* / on_exit_*"
        )
    if rule.actions or rule.action_descriptions:
        raise ValidationException(
            "direction=milestone 的动作走 task 的 on_target 槽，"
            "不配 actions / action_descriptions"
        )

    # 那一列由服务端定, 不信客户端传的: 填成真设备 did 会让 milestone rule 被感知
    # 认领, "累计达标"当成一句视觉 query 进摄像头 prompt。
    rule.condition.perceive_device_ids = [MILESTONE_SENTINEL_DID]

    dnf = rule.condition_dnf
    items = [item for conj in (dnf.any_of if dnf else []) for item in conj]
    if len(items) != 1 or items[0].source_type != RECORD_SOURCE_TYPE:
        raise ValidationException(
            "direction=milestone 本次只接受恰好一条 record 条件项，"
            f"当前 {len(items)} 条"
        )
    spec = items[0].spec or {}
    if not spec.get("task_id"):
        raise ValidationException("record 条件项必须写 task_id（引用哪个 task 的累计）")
    if spec.get("kind") != SUPPORTED_KIND:
        raise ValidationException(
            f"record 条件项本次只支持 kind={SUPPORTED_KIND!r}，"
            f"当前 {spec.get('kind')!r}"
        )
    if spec.get("op") != SUPPORTED_OP:
        raise ValidationException(
            f"record 条件项本次只支持 op={SUPPORTED_OP!r}，当前 {spec.get('op')!r}"
        )


def _task_rule_set_error(directions: list[RuleDirection]) -> str | None:
    """task 名下 rule 集合的合法性 (spec §9)。合法返 None, 非法返错误文案。

    校验对象是集合不是单条 rule —— 这两条约束都只有把同一 task 的 rule 放到一起
    看才成立。
    """
    if not directions:
        # 装配是分步的, task 可以暂时一条 rule 都没有。
        return None

    state_changing = [d for d in directions if d is not RuleDirection.MILESTONE]
    if RuleDirection.SESSION in state_changing and len(state_changing) > 1:
        return (
            "direction=session 的规则必须独占该 task: 与 enter / exit / 另一条 "
            "session 混挂会让 task 永久卡住 (spec §19.7)。"
            "milestone 不改状态, 不在此列。"
        )

    entry_directions = (RuleDirection.ENTER, RuleDirection.SESSION)
    if not any(d in entry_directions for d in directions):
        return (
            "task 名下没有进路径: 只有 exit / milestone 的 task 永远进不了 on, "
            "名下的动作一条都不会执行。至少要有一条 direction=enter 或 session "
            "的规则。"
        )
    return None


def _validate_rule_consistency(rule: Rule) -> None:
    """Apply V3 validation matrix to a fully-formed Rule.

    Raises ValidationException on any violation. See rule-design.md §6.1.
    """
    # ---- 1. condition.query 措辞 ----
    _validate_query_phrasing(rule.condition.query)

    if rule.resolved_direction is RuleDirection.MILESTONE:
        # 动作槽已校验为空, 所以下面第 4 节的 action 闸门对它是空转, 不用再走。
        _validate_milestone_rule(rule)
        _validate_lifecycle(rule)
        return

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
    _validate_lifecycle(rule)

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
            # 与 runner._execute_action 同口径:三种形态之外一律拒。少了这道,
            # `scene.1.2` / `prop.2` 这类写法能建成功,运行期每次 fire 只在
            # rule_log 里留一条 invalid_iid,规则永远是哑的。
            if a.iid != SCENE_IID and parse_device_iid(a.iid) is None:
                raise ValidationException(
                    f"{slot_name}[{i}] (did={a.did}, iid={a.iid}): iid must be "
                    f"'{SCENE_IID}', 'prop.<siid>.<piid>' or "
                    f"'action.<siid>.<aiid>'"
                )
            # 幂等分支会跳过判定直接下发,等于没有去重(原因见 SCENE_IID)。
            if a.iid == SCENE_IID and a.idempotent:
                raise ValidationException(
                    f"{slot_name}[{i}] (did={a.did}, iid={a.iid}): "
                    f"iid={SCENE_IID} requires idempotent=false"
                )
            # 冷却是场景唯一的去重手段,而 runner 把 0 当「无冷却」——填 0 会让
            # 每次 fire 都真触发一次场景,正是执行侧闸门要防的那种。
            if a.iid == SCENE_IID and (a.cooldown_minutes or 0) < 1:
                raise ValidationException(
                    f"{slot_name}[{i}] (did={a.did}, iid={a.iid}): "
                    f"iid={SCENE_IID} requires cooldown_minutes >= 1"
                )
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
    attach_task_state_machine(rule_runner, rule_repo)

    return RuleService(
        rule_repo,
        rule_log_repo,
        rule_runner,
        miot_proxy,
        task_record_service=task_record_service,
    )


def _rule_action_slots(rule: Rule) -> dict[str, Any]:
    """rule 的动作字段 → 它**管辖**的那些 task 槽。与 v2→v3 迁移同一套口径。

    只返回管辖的槽。不管辖的不出现在返回里, 调用方才不会把 ``task set-actions``
    写进去的槽覆盖成空 —— 一条 enter 型 rule 没有理由清掉 task 的达标动作。

    单方向的 rule (enter / exit) 只有一个边沿, 动作就填在 ``actions`` /
    ``action_descriptions`` 上, 不区分进出; 方向决定它落 on_enter 还是 on_exit。
    多条 agent 回调描述合成一条时的编号规则必须与 runner 的选槽逻辑逐字一致。
    """
    direction = rule.resolved_direction
    if direction is RuleDirection.SESSION:
        return {
            "on_enter_actions": [
                a.model_dump(mode="json") for a in rule.on_enter_actions
            ],
            "on_enter_desc": rule.on_enter_desc,
            "on_exit_actions": [
                a.model_dump(mode="json") for a in rule.on_exit_actions
            ],
            "on_exit_desc": rule.on_exit_desc,
            "on_target_desc": rule.on_target_desc,
        }
    if direction is RuleDirection.MILESTONE:
        # 达标动作在 task 列上, milestone rule 自己的动作字段恒空 —— 透传只会把
        # task 上那份清掉。
        return {}
    prefix = "on_exit" if direction is RuleDirection.EXIT else "on_enter"
    joined = (
        "\n".join(f"{i + 1}. {d}" for i, d in enumerate(rule.action_descriptions))
        or None
    )
    return {
        f"{prefix}_actions": [a.model_dump(mode="json") for a in rule.actions],
        f"{prefix}_desc": joined,
    }


def attach_task_state_machine(rule_runner: RuleRunner, rule_repo: RuleRepo) -> None:
    """建 task 状态机并把每个 task 的拓扑与边界动作登记进去。

    只登记**有边界动作**的 task —— 那是 expand-contract 阶段 A 的接管判据
    (§10.3「读 task 优先、缺失回退 rule」)。没动作的 task 走旧路径, 行为与接管前
    逐字相同, 所以这一步对未迁移的库、以及内存里直接构造 rule 的场景是无操作。

    重启一律从 ``off`` 起 (§7): 拓扑登记不恢复任何运行态。
    """
    from miloco.database.task_repo import TaskRepo
    from miloco.task.state_machine import TaskStateMachine, derive_directions
    from miloco.task.tracking import DecisionTracker
    from miloco.utils.time_utils import now_ms

    task_repo = TaskRepo()
    tracker = DecisionTracker()
    state_machine = TaskStateMachine(
        is_condition_satisfied=rule_runner.is_condition_satisfied,
        reset_edge_baseline=rule_runner.reset_edge_baseline,
        # 只有状态机自己发起动作时才会走到这里 (重新配置时强制 on_exit、手动
        # 注入)。边沿驱动的那条路由 runner 传 dispatch=False, 动作走它自己的
        # fire 路径。
        dispatch_action=lambda task_id, slot, payload: rule_runner.dispatch_task_action(
            task_id, slot.value
        ),
        track=lambda outcome, signal: tracker.record(
            signal.task_id, signal.rule_id, outcome.value, now_ms()
        ),
        on_forget=tracker.forget,
    )
    rule_runner.attach_state_machine(state_machine)
    rule_runner.attach_tracker(tracker)

    rules_by_task: dict[str, list] = {}
    for rule in rule_runner.get_all_rules():
        rules_by_task.setdefault(rule.task_id, []).append(rule)

    # 派生量 seed: 重启后按 DB 里的 task.status 重算「有效启用」(§19.9)
    for row in task_repo.list_all():
        rule_runner.set_task_paused(row["task_id"], row["status"] != "active")

    owned = 0
    for task_id, rules in rules_by_task.items():
        actions = task_repo.get_boundary_actions(task_id)
        rule_runner.set_task_actions(task_id, actions)
        if not rule_runner.task_owns_actions(task_id):
            continue
        state_machine.register_task(
            task_id,
            derive_directions((r.id, r.resolved_direction.value) for r in rules),
        )
        owned += 1
    logger.info(
        "task state machine attached: %d/%d task(s) owned",
        owned,
        len(rules_by_task),
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

    def _target_record_task_id(self, rule: Rule) -> str | None:
        """这条 rule 的达标看哪个 task 的 record —— 看不出达标就返 None。

        milestone rule 读条件项里引用的 task（spec §9 允许引用别的 task）；存量的
        rule 侧 ``on_target_desc`` 看自己所属的 task。
        """
        if rule.resolved_direction is RuleDirection.MILESTONE:
            dnf = rule.condition_dnf
            items = [item for conj in (dnf.any_of if dnf else []) for item in conj]
            return (items[0].spec or {}).get("task_id") if items else None
        return rule.task_id if rule.on_target_desc else None

    def _validate_task_rule_set(self, rule: Rule, previous: Rule | None = None) -> None:
        """这次变更有没有让 task 的 rule 集合变非法 (spec §9)。

        已经非法的放行, 只拦这次引入的: 存量迁移和删 rule 都可能留下非法的 task,
        一律拦住会把「改回合法」和「先停用它」这两条自救路一起堵死。丢失 rule 的
        那一侧 (删掉、改挂到别的 task) 同理不拦, 由重新配置路径兜住 (§19.5)。

        停用的 rule 照样计入: ``enabled`` 是用户意图 (§19.9), 停用一条进方向的
        规则是正常操作, 不是配置非法。
        """
        others = [
            r.resolved_direction
            for r in self._repo.list_by_task(rule.task_id)
            if r.id != rule.id
        ]
        before = list(others)
        if previous is not None and previous.task_id == rule.task_id:
            before.append(previous.resolved_direction)

        error = _task_rule_set_error([*others, rule.resolved_direction])
        if error and _task_rule_set_error(before) is None:
            raise ValidationException(error)

    def _require_target_action(self, rule: Rule) -> None:
        """达标规则要求 task 已配达标动作 (spec §9)。

        没配的话边沿照样被消耗、动作槽是空的 —— 用户看到规则建成功、然后什么都
        不发生, 且从规则本身看不出缺什么。
        """
        if rule.resolved_direction is not RuleDirection.MILESTONE:
            return
        actions = self._task_repo.get_full_view(rule.task_id) or {}
        slots = actions.get("actions") or {}
        if slots.get("on_target_actions") or slots.get("on_target_desc"):
            return
        if rule.on_target_desc:
            return
        raise ValidationException(
            f"达标规则要求 task {rule.task_id!r} 先配达标动作。修复："
            f'miloco-cli task set-actions {rule.task_id} --on-target-desc "..."'
        )

    def _validate_target_record(self, rule: Rule) -> None:
        """达标要求被引用的 task 有 duration record + target_minutes。

        没有阈值的达标通知永远不会触发，建成 active 等于留一个用户发现不了的失效项。
        报错按当前 record 状态分三种 case，每种附可执行的 CLI 修复命令。
        """
        task_id = self._target_record_task_id(rule)
        if not task_id:
            return
        self._require_target_action(rule)
        kind = self._task_record_service.detect_record_kind(task_id)
        if kind is None:
            raise ValidationException(
                f"累计达标要求 task {task_id!r} 配 duration record + "
                f"target_minutes，但 task 当前无活跃 record。修复："
                f"miloco-cli task record init {task_id} --kind duration "
                f'--content \'{{"target_minutes":N,'
                f'"recurring_pattern":{{"window":"day"}}}}\''
            )
        if kind != "duration":
            raise ValidationException(
                f"累计达标要求 task {task_id!r} 配 duration record，"
                f"当前 record kind={kind!r}（仅 duration 支持累计达标）。修复："
                f"先 miloco-cli task delete {task_id}（连带删 record），"
                f"再 task create + task record init --kind duration"
            )
        state = self._task_record_service.read_duration_target_state(task_id)
        target_minutes = state[0] if state is not None else None
        if target_minutes is None:
            raise ValidationException(
                f"累计达标要求 task {task_id!r} 的 duration record "
                f"设置 target_minutes（当前为空）。修复："
                f"miloco-cli task record update {task_id} "
                f'--patch \'{{"target_minutes":N}}\''
            )

    async def _get_valid_perceive_device_ids(self) -> list[str]:
        """All valid perception device IDs (offline included).

        多通道相机的感知 did 是合成 did（``cam1:ch0`` / ``cam1:ch1``）；rule 可以按
        整台相机的物理 did（``cam1``）绑定，也可以精确到某条通道。两种粒度都收进合法集。
        """
        from miloco.manager import get_manager

        devices = await get_manager().perception_service.get_devices(online_only=False)
        valid = [device.did for device in devices]
        physical = {d.rsplit(":ch", 1)[0] for d in valid if ":ch" in d}
        return valid + sorted(physical - set(valid))

    async def _validate_perceive_devices_of(self, rule: Rule) -> None:
        """milestone rule 的那一列是占位, 不是真设备 —— 按真设备校验会直接拒掉。"""
        if rule.resolved_direction is RuleDirection.MILESTONE:
            return
        await self._validate_perceive_device_ids(rule.condition.perceive_device_ids)

    async def _validate_perceive_device_ids(self, dids: list[str]) -> None:
        valid_dids = await self._get_valid_perceive_device_ids()
        invalid = [d for d in dids if d not in valid_dids]
        if invalid:
            raise ValidationException(
                f"Invalid perception device IDs: {', '.join(invalid)}"
            )

    async def _validate_scene_ids(self, rule: Rule) -> None:
        """场景动作的 did 必须是真实存在的 scene_id。

        抄错一位的话规则能建成功、运行期每次 fire 都失败,用户和 agent 都拿不到
        反馈——和 _validate_perceive_device_ids 挡 source did 是同一个理由。
        """
        wanted = {
            a.did
            for slot in (rule.actions, rule.on_enter_actions, rule.on_exit_actions)
            for a in slot
            if a.iid == SCENE_IID
        }
        if not wanted:
            return
        all_scenes = (await self._miot_proxy.get_all_scenes()) or {}
        # 场景表拿不到(缓存空 + 刷新失败)时别谎报「你的 id 无效」——两种失败
        # 的修法完全不同。
        if not all_scenes:
            raise ValidationException(
                "Scene list unavailable (MIoT scene cache is empty); "
                f"cannot verify scene IDs: {', '.join(sorted(wanted))}"
            )
        kv_repo = self._miot_proxy._kv_repo
        if not allowed_home_ids(kv_repo):
            raise ValidationException(
                "No home is enabled; enable a home before creating scene actions"
            )
        # get_all_scenes 返回账号名下所有家;与其余场景出口(get_miot_scene_list /
        # get_home_info_data)和执行侧 is_home_allowed 同口径,只认已启用家庭的
        # 场景——校验放行的,运行期必须真的触发得动。available 也不列白名单外的 id。
        scenes = filter_by_home(kv_repo, all_scenes)
        invalid = sorted(wanted - set(scenes))
        if invalid:
            raise ValidationException(
                f"Invalid or not-allowed scene IDs: {', '.join(invalid)}; "
                f"available: {', '.join(sorted(scenes)) or '(none)'}"
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

        v2 后 rule.task_id NOT NULL + FK CASCADE (DB 层硬拦), service 层前置校验
        提供可读 400 (双保险)。
        """
        if not rule.task_id:
            raise ValidationException("rule.task_id required (v2: NOT NULL)")

        if self._repo.exists_by_name(rule.name):
            raise ConflictException(f"Rule name '{rule.name}' already exists")

        if not self._task_repo.task_exists(rule.task_id):
            raise ResourceNotFoundException(
                f"task_not_found: rule.task_id={rule.task_id!r} 对应 task 不存在"
            )

        self._fill_default_duration_ratio(rule)

        await self._validate_perceive_devices_of(rule)
        _validate_rule_consistency(rule)
        self._validate_target_record(rule)
        self._validate_task_rule_set(rule)
        await self._validate_scene_ids(rule)

        rule_id = self._repo.create(rule)
        if not rule_id:
            raise BusinessException("Failed to create rule")

        rule.id = rule_id
        self._runner.add_rule(rule)
        # 顺序要紧: 先把动作写进 task 列, 再 reconfigure —— 后者刷的是 task 列的快照
        self.sync_rule_actions_to_task(rule)
        self.reconfigure_task(rule.task_id)
        logger.info("Rule created: %s", rule_id)
        return rule_id

    async def get_rule(self, rule_id: str) -> Rule:
        rule = self._repo.get_by_id(rule_id)
        if not rule:
            raise ResourceNotFoundException(f"Rule '{rule_id}' not found")
        return rule

    async def get_all_rules(self, enabled_only: bool = False) -> list[Rule]:
        return self._repo.get_all(enabled_only)

    async def get_effectively_enabled_rules(self) -> list[Rule]:
        """「有效启用」的 rule —— 用户意图 AND 所属 task 没被停用 (§19.9)。

        仍从 DB 读 ``enabled``, 与 task 停用会覆写 enabled 的旧行为同源, 只多滤
        一层 task 停用。感知侧每 cycle 的下发闸和 admin 状态数字都得用这个:
        只按 ``enabled`` 过滤会让停用的 task 继续下发、继续触发。
        """
        rules = self._repo.get_all(enabled_only=True)
        return [r for r in rules if not self._runner.is_task_paused(r.task_id)]

    def notify_record_rollover(
        self,
        task_id: str,
        pre_rollover_state: tuple[int | None, int] | None = None,
    ) -> None:
        """task_record rollover 完成后由 daily job 调入，触发 rule engine 跨日
        强制 on_exit + on_enter，并让 record 源按新一天重排。pre_rollover_state
        为 rollover_one 执行前 snapshot 的 ``(target_minutes, accumulated_minutes_today)``，
        用于兑现旧一天已达标但 timer 还没到点的场景。"""
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
        previous = self._repo.get_by_id(rule.id)
        if previous is None:
            raise ResourceNotFoundException(f"Rule '{rule.id}' not found")
        if self._repo.exists_by_name(rule.name, rule.id):
            raise ConflictException(f"Rule name '{rule.name}' already exists")

        self._fill_default_duration_ratio(rule)

        await self._validate_perceive_devices_of(rule)
        _validate_rule_consistency(rule)
        self._validate_target_record(rule)
        self._validate_task_rule_set(rule, previous)
        await self._validate_scene_ids(rule)

        success = self._repo.update(rule)
        if success:
            self._runner.add_rule(rule)
            self.sync_rule_actions_to_task(rule)
            self.reconfigure_task(rule.task_id)
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

        # 下面是就地合并, 合并完就读不到变更前的方向与归属了。
        previous = existing.model_copy(deep=True)

        fields = update.model_fields_set

        if "name" in fields and update.name is not None:
            if self._repo.exists_by_name(update.name, rule_id):
                raise ConflictException(f"Rule name '{update.name}' already exists")
            existing.name = update.name

        if "task_id" in fields and update.task_id is not None:
            existing.task_id = update.task_id

        if "mode" in fields and update.mode is not None:
            existing.mode = update.mode

        if "direction" in fields and update.direction is not None:
            existing.direction = update.direction

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
        self._validate_target_record(existing)
        self._validate_task_rule_set(existing, previous)
        # 与上面 perceive_device_ids 同口径:只校验这次 PATCH 真的动了的东西。
        # 无条件跑的话,场景被删 / 家庭被关之后连 `rule disable`(它本身就是一次
        # PATCH)都会 400 —— 规则坏掉的那一刻正好把「先关掉它」这条路堵死。
        if {"actions", "on_enter_actions", "on_exit_actions"} & fields:
            await self._validate_scene_ids(existing)

        success = self._repo.update(existing)
        if success:
            self._runner.add_rule(existing)
            self.sync_rule_actions_to_task(existing)
            self.reconfigure_task(existing.task_id)
        return success

    async def delete_rule(self, rule_id: str) -> bool:
        if not self._repo.exists(rule_id):
            raise ResourceNotFoundException(f"Rule '{rule_id}' not found")

        # 删之前先取归属 —— 删完就查不到了。exists 与 get_by_id 之间行可能已经
        # 消失, 取不到就跳过重新配置而不是崩在这里。
        existing = self._repo.get_by_id(rule_id)
        task_id = existing.task_id if existing is not None else None

        success = self._repo.delete(rule_id)
        if success:
            # 顺序要紧: 先 reconfigure。DB 行已删, 拓扑因此为空、会触发 on_exit,
            # 而 runner 内存里那条 rule 还在, 动作有 rule 可归属 (日志与冷却按
            # rule 记)。反过来先 remove_rule, 那次 on_exit 会因为"名下已无 rule"
            # 被跳过 —— 而这正是 §19.5 要解的那个卡死场景。
            if task_id:
                self.reconfigure_task(task_id)
            self._runner.remove_rule(rule_id)
            self._log_repo.delete_by_rule_id(rule_id)
        return success

    def remove_rule_from_runner(self, rule_id: str) -> None:
        """仅清 RuleRunner._rules 内存 dict, 不删 DB (供 TaskService.delete_task
        在 FK CASCADE 已清 rule 表行后清内存态)。老 delete_rule 走 DB + 内存
        双清, 但 task delete 场景 DB 由 CASCADE 走完, 只需清内存, 避免二次删表。
        """
        self._runner.remove_rule(rule_id)

    @property
    def decision_tracker(self):
        """给 task 层读判定摘要用。没接管时为 None。"""
        return self._runner.tracker

    @property
    def runner_state_machine(self):
        """给 task 层读运行态用。没接管时为 None。"""
        return self._runner.state_machine

    # ---- 重新配置路径 (§19.5) ----

    def sync_rule_actions_to_task(self, rule: Rule) -> None:
        """把 rule 上的动作写进它所属 task 的边界动作列。

        §10.3 阶段 A 说「CLI 加新 flag，旧 flag 仍可用」——**旧 flag 仍可用意味着
        它的写入必须落到新位置**。读侧的双路回退只解决了「读哪一份」, 写侧不透传
        的话, 迁移后用现有 CLI 改动作会静默不生效: rule 列改了、fire 读的是 task
        列的旧值, 而 CLI 返回成功、``rule get`` 也显示新值。

        一 task 多 rule 且动作不一致时跳过并告警 —— 口径与迁移一致 (§10.1): 从
        一条 rule 单向覆盖会把另一条的动作悄悄冲掉。

        阶段 B 动作 flag 落到 task 上之后这个函数整体删除。
        """
        from miloco.database.task_repo import TaskRepo

        siblings = [r for r in self._repo.list_by_task(rule.task_id) if r.id != rule.id]
        if siblings:
            logger.warning(
                "task %s 名下有 %d 条其它 rule, 不把 rule %s 的动作透传到 task 列; "
                "请改用 task 侧的动作入口",
                rule.task_id,
                len(siblings),
                rule.id,
            )
            return

        slots = _rule_action_slots(rule)
        if not slots:
            return
        # rule 写入已经成功并且是主要效果, 不能被 task 侧同步的失败带崩。但也不能
        # 静默 —— 同步没成功就意味着"动作只读"那个坑还在, 必须留下明显的线索。
        try:
            written = TaskRepo().set_boundary_actions(rule.task_id, **slots)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "把 rule %s 的动作同步到 task %s 失败: %s; "
                "该 task 若已被状态机接管, 这次动作改动不会生效",
                rule.id,
                rule.task_id,
                e,
            )
            return
        if not written:
            logger.warning(
                "task %s 没有对应行, rule %s 的动作没同步过去", rule.task_id, rule.id
            )

    def reconfigure_task(self, task_id: str) -> None:
        """rule 增删改、rule 单独启停、task 重新 enable 统一走这条。

        做四件事: 刷新 task 动作快照 → 重算拓扑 → 失去全部出路径且当前为 on 时
        先跑 on_exit → 清运行态从 off 起。前三件由 ``TaskStateMachine.reconfigure``
        承担, 这里只负责把最新的拓扑与动作喂进去。

        没接管该 task (没有边界动作 / 名下已无 rule) → 撤销登记, 回到旧路径。
        不 reconfigure 而是 unregister: 前者会把没接管的 task 登记进去。
        """
        sm = self._runner.state_machine
        if sm is None:
            return

        from miloco.database.task_repo import TaskRepo
        from miloco.task.state_machine import derive_directions

        self._runner.set_task_actions(task_id, TaskRepo().get_boundary_actions(task_id))
        # 从 DB 读而非内存: DB 是归属的权威源, 且删 rule 时行已落库、runner 内存
        # 还留着那条 —— 正需要这个错位, 空拓扑触发 on_exit 时动作还有 rule 可归属。
        rules = self._repo.list_by_task(task_id)
        if not rules or not self._runner.task_owns_actions(task_id):
            if sm.owns(task_id):
                # 空拓扑先走一次 reconfigure：删掉最后一条 rule 也是「失去全部
                # 出路径」，task 在 on 时必须先跑 on_exit，直接 unregister 会让
                # 那次退出动作永远不执行。
                sm.reconfigure(task_id, {})
                sm.unregister_task(task_id)
            return
        sm.reconfigure(
            task_id,
            derive_directions((r.id, r.resolved_direction.value) for r in rules),
        )

    def apply_task_status(self, task_id: str, active: bool) -> None:
        """task 启停 → 刷新派生的「有效启用」并走重新配置路径。

        **不写 rule.enabled** —— 它是用户意图, task 启停覆写它会把用户手动关掉
        的那条 rule 在 task 重新 enable 时错误地打开 (§19.9)。
        """
        self._runner.set_task_paused(task_id, not active)
        if active:
            # enable: 停用期间 rule 可能被改过, 要重新登记拓扑
            self.reconfigure_task(task_id)
            return
        self._runner.record_source.disarm(task_id)
        sm = self._runner.state_machine
        if sm is not None and sm.owns(task_id):
            sm.suspend(task_id)

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
    ) -> TriggerOutcome:
        """Per-frame, per-source state report from the perception engine.

        See :meth:`RuleRunner.update_state`. Returns the resulting
        ``TriggerOutcome`` (surfaced in the resident activity log).
        """
        return await self._runner.update_state(
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
