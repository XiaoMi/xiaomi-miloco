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
import math
from collections.abc import Iterable
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
from miloco.miot.client import MiotProxy, is_subscribable_did
from miloco.miot.filter import allowed_home_ids, filter_by_home
from miloco.rule.condition import (
    condition_to_dnf,
    dnf_structure_error,
    is_server_rendered,
    render_iot_condition,
    single_item_of,
)
from miloco.rule.iot_source import (
    BOOL_FAMILY,
    NUMBER_FAMILY,
    ORDERING_OPS,
    STR_FAMILY,
    SUPPORTED_OPS,
    value_family,
)
from miloco.rule.record_source import (
    milestone_condition_dnf,
    milestone_legacy_condition,
    milestone_rule_name,
    record_ref_of,
)
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    _DIRECTION_TO_MODE,
    _MODE_TO_DIRECTION,
    IOT_SOURCE_TYPE,
    OMNI_SOURCE_TYPE,
    SCENE_IID,
    Rule,
    RuleCondition,
    RuleConditionDNF,
    RuleDirection,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleLogKind,
    RuleMode,
    RuleUpdate,
    TriggerOutcome,
    parse_device_iid,
    task_rule_set_error,
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


# MIoT 的 format 取值域里的标量那些。iids / array / struct 不在里面 —— 它们在容器里
# 是元组或更复杂的形状，比较不出结果。
_SCALAR_FORMAT_FAMILY = {
    "bool": BOOL_FAMILY,
    "string": STR_FAMILY,
    "float": NUMBER_FAMILY,
    **{
        name: NUMBER_FAMILY
        for name in (
            "uint8",
            "uint16",
            "uint32",
            "uint64",
            "int8",
            "int16",
            "int32",
            "int64",
        )
    },
}


def _validate_iot_value(entry: dict, op: str, value: Any, iid: str) -> None:
    """iot 条件项的 format / 取值域校验。

    配错的后果分两种，都看不出来：``eq`` 恒假是永远不触发，``ne`` 恒真是持续误
    触发 —— 后者比前者更糟。
    """
    fmt = str(entry.get("format") or "")
    family = _SCALAR_FORMAT_FAMILY.get(fmt)
    if family is None:
        raise ValidationException(
            f"属性 prop.{iid} 的 format={fmt!r} 不是标量, 不能当 iot 条件项"
        )
    if family != value_family(value):
        raise ValidationException(
            f"属性 prop.{iid} 的 format={fmt!r} 与 value={value!r} 类型不兼容"
        )
    if family is not NUMBER_FAMILY and op in ORDERING_OPS:
        kind = "字符串" if family is STR_FAMILY else "开关"
        raise ValidationException(
            f"属性 prop.{iid} 是{kind}, 不支持 op={op!r} 这种大小比较"
        )

    choices = entry.get("value_list") or []
    if choices:
        allowed = {c.get("value") for c in choices if isinstance(c, dict)}
        if op in ("eq", "ne"):
            if value not in allowed:
                raise ValidationException(
                    f"value={value!r} 不在属性 prop.{iid} 的取值列表里: "
                    f"{sorted(allowed, key=repr)}"
                )
            return
        # 枚举上的大小比较按它实际的取值域判，判据与 value_range 同一条
        numeric = sorted(v for v in allowed if isinstance(v, (int, float)))
        if numeric:
            _validate_satisfiable(numeric[0], numeric[-1], op, value, iid)
        return

    value_range = entry.get("value_range")
    if isinstance(value_range, (list, tuple)) and len(value_range) >= 2:
        _validate_against_range(value_range, op, value, iid)


def _validate_satisfiable(low, high, op: str, value: Any, iid: str) -> None:
    """大小比较必须**既可能成立、也可能不成立**。端点含在内（MIoT 的区间是闭区间）。

    只判一个方向的话另一个方向的配置错误会溜过去：区间 ``[-40,125]`` 上 ``gt -100``
    恒真，seed 那一刻产生一次凭空的进入边沿、之后再没有边沿；而 ``lt -100`` 恒假，
    永远不触发。两种都看不出来，规则本身看起来配得完全正常。

    值本身不必可达 —— ``gt 3`` 在步长为 5 的区间上是有意义的（设备报 5 时成立），
    所以这里不查步长。
    """
    can_be_true = {
        "gt": high > value,
        "gte": high >= value,
        "lt": low < value,
        "lte": low <= value,
    }[op]
    if not can_be_true:
        raise ValidationException(
            f"属性 prop.{iid} 的取值范围是 [{low}, {high}], "
            f"没有任何取值满足 {op} {value!r}"
        )
    can_be_false = {
        "gt": low <= value,
        "gte": low < value,
        "lt": high >= value,
        "lte": high > value,
    }[op]
    if not can_be_false:
        raise ValidationException(
            f"属性 prop.{iid} 的取值范围是 [{low}, {high}], "
            f"每个取值都满足 {op} {value!r} —— 这个条件恒成立"
        )


def _is_on_step_grid(low, value, step) -> bool:
    """``value`` 落在从 ``low`` 起、每 ``step`` 一档的格子上吗。

    浮点取模的误差会把合法值判成越界，按 step 的量级取绝对容差。
    """
    offset = value - low
    remainder = offset - round(offset / step) * step
    return abs(remainder) <= abs(step) * 1e-6


def _reachable_high(low, high, step) -> Any:
    """最大的可达值。上界不一定落在步长格上：``[0,100,3]`` 上设备最多报到 99。

    最小值那侧不用收 —— 偏移恒为 0，永远在格上。

    在格上就原样返回，判据与步长可达校验共用一份 —— 商的浮点误差足以凭空少一档
    （``0.3 / 0.1`` 算出来是 2.9999…，向下取整会把上界从 0.3 收成 0.2）。
    """
    if not step:
        return high
    if _is_on_step_grid(low, high, step):
        return high
    return low + math.floor((high - low) / step) * step


def _validate_against_range(value_range, op: str, value: Any, iid: str) -> None:
    """校验的是「这个谓词有没有可能成立」，不是「这个值本身可达」。

    ``eq`` / ``ne`` 要求值**可达**（闭区间内且满足步长）：range ``[0,100,5]`` 上
    ``eq 3`` 恒假、``ne 3`` 恒真，设备永远不上报 3。

    大小比较只要区间里存在可达值满足它就行，值本身不必可达 —— ``gt 3`` 在那个
    range 上是有意义的（设备报 5 时成立），要求它满足步长会把正常配置拒掉。
    """
    low, high = value_range[0], value_range[1]
    step = value_range[2] if len(value_range) > 2 else None
    if op in ORDERING_OPS:
        # 拿声明的上界判会把恒假的 `gt 99` 与恒真的 `lte 99` 一起放行 —— 正是本函数
        # 要拦的那两种，而设备根本报不出 100。
        _validate_satisfiable(low, _reachable_high(low, high, step), op, value, iid)
        return

    if not (low <= value <= high):
        raise ValidationException(
            f"value={value!r} 不在属性 prop.{iid} 的取值范围 [{low}, {high}] 内"
        )
    if not step:
        return
    if not _is_on_step_grid(low, value, step):
        raise ValidationException(
            f"value={value!r} 落不到属性 prop.{iid} 的步长上 "
            f"(从 {low} 起每 {step} 一档)"
        )


def _sync_legacy_condition_from(rule: Rule) -> None:
    """整项替换 DNF 之后，把旧 ``condition`` 列照 DNF 那一项的 spec 对齐。

    不写死占位: 紧接着那道「condition 与 DNF 要对得上」的校验会拿服务端自己刚清空的
    值去比, omni 无论怎么传都过不了, 而报错指向一个调用方根本没碰的字段。

    服务端渲染的那些源不必单独分支 —— 它们的 spec 里没有这两个键, 回填出来的正是空
    占位, 而 ``query`` 随后由渲染覆盖。
    """
    item = single_item_of(rule.condition_dnf)
    spec = (item.spec or {}) if item is not None else {}
    rule.condition.perceive_device_ids = list(spec.get("perceive_device_ids") or [])
    rule.condition.query = str(spec.get("query") or "")


def _reject_task_move(previous_task_id: str, new_task_id: str) -> None:
    """改 ``task_id`` 一律拒，PATCH 与 PUT 都拒。

    正确处理跨 task 移动要「旧 task 先退出 → 清运行态 → 新 task 再进入」，那是重排
    一条既有的收尾链。今天那条链有两处会漏：旧 task 走到「失去全部出路径 → 强制
    on_exit」时，代表 rule 在 runner 里已经属于新 task、动作槽也清了，**那次退出动作
    丢掉**；而 ``add_rule`` 的 reset 条件不含 ``task_id``，rule 在旧 task 里已为真、
    移过去后 ``last_bool`` 仍是真，新拓扑起始 off，喂真时没有跳变 —— **新 task 也进
    不去**。

    换 task 本来就该算一条新规则：删了重建。
    """
    if new_task_id and previous_task_id != new_task_id:
        raise ValidationException(
            f"不支持把规则从 task {previous_task_id!r} 移到 {new_task_id!r}: "
            "旧 task 的退出动作会丢、新 task 也进不去。请删掉重建。"
        )


def _reject_source_change(previous: Rule, updated: Rule) -> None:
    """跨源改（omni 改成 iot，或反过来）直接拒。

    允许的话旧源的字段会留成脏数据（omni 的 did 列表、iot 的 did）—— 收口后读侧不
    会读它们，但 ``dump`` 和排障时会误导。换源等于换一条规则。
    """
    before, after = previous.resolved_source_type, updated.resolved_source_type
    if before != after:
        raise ValidationException(
            f"不支持把规则的触发源从 {before!r} 改成 {after!r}: 请删掉重建。"
        )


def _validate_query_not_empty(query: str) -> None:
    """空 query 的规则永远不触发, 而它看起来配得完全正常。

    对 omni 是一句空 prompt; 对服务端渲染的那些源, 空串说明渲染没生效。两种都要拦,
    所以这条管全部源 —— 与只管 omni 的措辞校验是两个独立判断, 不合成一个函数。
    """
    if not query.strip():
        raise ValidationException("condition.query 不能为空")


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


def _validate_rule_consistency(rule: Rule) -> None:
    """Apply V3 validation matrix to a fully-formed Rule.

    Raises ValidationException on any violation. See rule-design.md §6.1.
    """
    # condition.query 的两条校验 (非空 / 措辞) 不在这里 —— 它们必须排在服务端渲染
    # 之后, 见 RuleService._prepare_condition 的五步。

    if rule.resolved_source_type == IOT_SOURCE_TYPE and rule.duration_seconds:
        # _evaluate_duration 的滑窗按墙上时钟分 round、采样断流用 0 补齐, 且窗口未填满
        # 就早返。事件驱动的喂法填不满窗口 ——「空调开了两小时」这条规则永远不会触发。
        # 拒绝比静默不触发好: 后者用户看不出来, 而且规则看起来配得完全正确。
        #
        # 装在这里而不是条件项那五步里: 那五步只在 PATCH 真的碰了条件时才跑, 而
        # `rule update --duration-seconds` 一个条件字段都不碰。本函数是三条写入路径
        # 的必经处。
        raise ValidationException(
            "iot 条件项不支持 duration_seconds: 累计滑窗要连续采样, "
            "而属性是被推来的、填不满窗口, 规则会永远不触发"
        )

    if rule.resolved_direction is RuleDirection.MILESTONE:
        raise ValidationException(
            "达标规则由服务端按 task 的达标动作 + duration record 自动维护, "
            "不接受手工创建。配达标通知: "
            'miloco-cli task set-actions <task_id> --on-target-desc "..."'
        )

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


async def init_rule_service(
    miot_proxy: MiotProxy, state_store=None, pull_props=None
) -> RuleService:
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
    if state_store is not None:
        # 排在状态机接管之后: 源起来就 seed, 而 seed 会一路走到 task 状态机。
        rule_runner.attach_iot_source(state_store, pull_props)

    return RuleService(
        rule_repo,
        rule_log_repo,
        rule_runner,
        miot_proxy,
        task_record_service=task_record_service,
    )


def _rule_action_slots(
    rule: Rule, changed_fields: set[str] | None = None
) -> dict[str, Any]:
    """rule 的动作字段 → 它**管辖**的那些 task 槽, 按槽整体给。

    管辖的单位是槽 (on_enter / on_exit / on_target) 而不是单个列。一个槽有"设备
    直控"和"交给 Agent"两列且互斥, 所以只要管辖这个槽就把两列一起写 —— 只写填了
    的那一列的话, 用户把动作从直控改成 Agent 文案时, task 上残留的直控列会继续赢
    (选槽时静态优先), 这次改动静默失效。

    ``changed_fields`` 是本次 PATCH 显式动过的 rule 字段名。给了就只透传被动过的
    槽 —— 别的槽 rule 上为空不等于用户要清空: 新模型里动作的正规配法是只写 task 列、
    rule 行一直空着, 无条件透传等于改一次触发条件就把 task 上那份清成 None, 还连带
    把代建的达标规则 reconcile 掉。反过来, 只要这个槽真被动过就照写, 哪怕新值是空
    —— 否则 ``rule update --clear on_enter_desc`` 会 CLI 报成功而 task 照旧执行。
    不给 (create / 整体替换) 则只透传自己填了值的槽, 那两条路上没有"清空"可言。

    单方向的 rule (enter / exit) 只有一个边沿, 动作就填在 ``actions`` /
    ``action_descriptions`` 上, 不区分进出; 方向决定它落 on_enter 还是 on_exit。
    多条 agent 回调描述在这里合成一条 —— 读侧只读合成后的那份, 不再自己拼。
    """
    direction = rule.resolved_direction
    if direction is RuleDirection.MILESTONE:
        # 达标动作在 task 列上, milestone rule 自己的动作字段恒空。
        return {}
    if direction is RuleDirection.SESSION:
        # 达标槽 rule 侧只有 desc 一列, 静态那列恒空 —— 这正说明它管辖达标槽时
        # task 上那列就该是空的, 不能因为"表达不了"就不写。
        owned: tuple[tuple[str, list[Any], str | None, set[str]], ...] = (
            (
                "on_enter",
                rule.on_enter_actions,
                rule.on_enter_desc,
                {"on_enter_actions", "on_enter_desc"},
            ),
            (
                "on_exit",
                rule.on_exit_actions,
                rule.on_exit_desc,
                {"on_exit_actions", "on_exit_desc"},
            ),
            ("on_target", [], rule.on_target_desc, {"on_target_desc"}),
        )
    else:
        prefix = "on_exit" if direction is RuleDirection.EXIT else "on_enter"
        joined = "\n".join(
            f"{i + 1}. {d}" for i, d in enumerate(rule.action_descriptions)
        )
        owned = ((prefix, rule.actions, joined, {"actions", "action_descriptions"}),)

    slots: dict[str, Any] = {}
    for name, actions, desc, source_fields in owned:
        if changed_fields is None:
            if not (actions or desc):
                continue
        elif not changed_fields & source_fields:
            continue
        slots[f"{name}_actions"] = [a.model_dump(mode="json") for a in actions]
        slots[f"{name}_desc"] = desc or None
    return slots


def _live_topology(rules: Iterable[Rule]) -> dict:
    """算状态机拓扑 —— 只收还会喂条件层的 rule。

    停用的 rule 被 ``update_state`` 第一道闸挡回去, 它的条件此后恒为"没观测到"。
    留在拓扑里, ``_should_stay_on`` 会把它算成一条活着的出路径: task 卡在 on 出
    不去, 挂在退出槽上的计时段收尾也跟着永远不发。

    全都停用时拓扑为空, 走"失去全部出路径"那条分支正常退出。
    """
    from miloco.task.state_machine import derive_directions

    return derive_directions(
        (r.id, r.resolved_direction.value) for r in rules if r.enabled
    )


def attach_task_state_machine(rule_runner: RuleRunner, rule_repo: RuleRepo) -> None:
    """建 task 状态机并把每个 task 的拓扑与边界动作登记进去。

    名下有 rule 就登记, 与"动作配没配"无关。两者绑在一起的话, 清空 task 的动作槽
    会连带把整条 task 退回旧的 per-rule 引擎 —— 而多条 rule 的 task (非互反 / OR
    聚合 / exit 方向) 在那条引擎上的语义本来就是错的。没配动作的 task 照样登记,
    条件照判、状态照推, 只是选不到槽、不做事。

    重启一律从 ``off`` 起 (§7): 拓扑登记不恢复任何运行态。
    """
    from miloco.database.task_repo import TaskRepo
    from miloco.task.state_machine import TaskStateMachine
    from miloco.task.tracking import DecisionTracker
    from miloco.utils.time_utils import now_ms

    task_repo = TaskRepo()
    tracker = DecisionTracker()
    state_machine = TaskStateMachine(
        is_condition_satisfied=rule_runner.is_condition_satisfied,
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

    for task_id, rules in rules_by_task.items():
        rule_runner.set_task_actions(task_id, task_repo.get_boundary_actions(task_id))
        state_machine.register_task(task_id, _live_topology(rules))
    logger.info("task state machine attached: %d task(s)", len(rules_by_task))
    _seed_reached_targets(rule_runner)


def _seed_reached_targets(rule_runner: RuleRunner, task_id: str | None = None) -> None:
    """把"今天已经达标"的条件直接置真, 不产边沿 (§7)。

    防重复的载体是条件项的值, 而它只在内存 —— 凡是把条件层状态清掉的路径事后都得
    补一次, 否则当天的达标会重发。启动是一条 (从假起), task 停用再启用是另一条
    (停用时按 task 清掉了)。

    ``task_id`` 给了就只 seed 那个 task 名下的。

    误判的是"达标了但还没发出去"那一小段窗口: 达标那一刻进程崩, 或 timer 到点时
    task 不在 session 而退出兜底也没赶上。它比重启窄得多, 而重启本身是常态 (升级、
    崩溃、supervisord 重拉)。
    """
    record_service = rule_runner._task_record_service
    if record_service is None:
        return
    for rule in rule_runner.get_all_rules():
        if task_id is not None and rule.task_id != task_id:
            continue
        ref = record_ref_of(rule)
        if ref is None:
            continue
        try:
            state = record_service.read_duration_target_state(ref.task_id)
        except Exception:
            logger.exception("启动时读 task %s 的累计失败, 不 seed", ref.task_id)
            continue
        if state is None:
            continue
        target, accumulated = state
        if target is None or accumulated < target:
            continue
        rule_runner.seed_reached_target(ref.rule_id)
        logger.info(
            "RECORD_TARGET_SEEDED: rule=%s task=%s 已达标 "
            "(accumulated_min=%s target_min=%s), 按已通知处理",
            ref.rule_id, ref.task_id, accumulated, target,
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

        代建的达标规则不走本校验（它是派生物, 建的时候三样已经齐备）, 所以这里
        只管用户在 rule 上填 ``on_target_desc`` 这条旧路径。
        """
        return rule.task_id if rule.on_target_desc else None

    def _validate_task_rule_set(self, rule: Rule, previous: Rule | None = None) -> None:
        """这次变更有没有让 task 的 rule 集合变非法 (spec §9)。

        已经非法的放行, 只拦这次引入的: 存量迁移和删 rule 都可能留下非法的 task,
        一律拦住会把「改回合法」和「先停用它」这两条自救路一起堵死。

        停用的 rule 照样计入: ``enabled`` 是用户意图 (§19.9), 停用一条进方向的
        规则是正常操作, 不是配置非法。
        """
        if rule.resolved_direction is RuleDirection.MILESTONE:
            return

        others = list(self._repo.list_by_task(rule.task_id))
        if previous is not None:
            # 排除只在改一条已有 rule 时做。create 路径上 ``rule.id`` 是客户端传的,
            # 而 repo 建行时另生 uuid —— 拿它去排除等于让请求方指定"忽略哪条兄弟"。
            others = [r for r in others if r.id != rule.id]

        sibling_directions = [r.resolved_direction for r in others]
        after = [*sibling_directions, rule.resolved_direction]
        before = list(sibling_directions)
        if previous is not None and previous.task_id == rule.task_id:
            before.append(previous.resolved_direction)

        has_target = self._task_has_target_action(rule.task_id)
        error = task_rule_set_error(after, has_target)
        if error and task_rule_set_error(before, has_target) is None:
            raise ValidationException(error)

    def _validate_action_reachable(self, rule: Rule) -> None:
        """新建 / 改完之后, 这条 enter rule 自己得有动作可落。

        动作已归 task, rule 侧必填是 v2 遗留: 多条 enter 共用一份动作时, 第二条起
        只能写一段被 ``sync_rule_actions_to_task`` 丢弃的文案。exit 不查 —— 无动作
        的 exit 只推状态, 是让 task 可重入的正常配置。

        只判**当前真实状态**, 不预演这次写入的后果。预演过的版本连着四轮出问题
        (读写前视图 → 漏一条透传支路 → 支路里取数对象错 → 清空动作害到旁人), 根因
        是校验在复刻透传的分支, 而复刻永远滞后于被复刻者。"配出来的规则会不会静默
        不做事"改由 ``report_muted_enter_rules`` 在 ``reconfigure_task`` 里按真实的
        写后状态诊断: 那里是所有写入路径的收敛点, 判据直接问读侧, 不需要枚举入口。
        """
        if rule.resolved_direction is not RuleDirection.ENTER:
            return
        if rule.actions or rule.action_descriptions:
            return
        try:
            slots = (self._task_repo.get_full_view(rule.task_id) or {}).get(
                "actions"
            ) or {}
        except Exception as e:  # noqa: BLE001
            # 读不出来时放行等于让哑规则建成, 与达标那道闸的默认方向相反。
            raise BusinessException(
                f"读 task {rule.task_id} 的动作配置失败, 无法确认 enter 规则有动作可落"
            ) from e

        if not (slots.get("on_enter_actions") or slots.get("on_enter_desc")):
            raise ValidationException(
                "direction=enter 的规则没有动作可落。"
                f'miloco-cli task set-actions {rule.task_id} --on-enter-desc "..."'
            )

    def _task_has_target_action(self, task_id: str) -> bool:
        """task 配没配达标动作。读不出来按"没配"处理 —— 这道闸不该把 rule 写入带崩。"""
        try:
            slots = (self._task_repo.get_full_view(task_id) or {}).get("actions") or {}
        except Exception:  # noqa: BLE001
            logger.warning("读 task %s 的达标配置失败, 这次按未配达标处理", task_id)
            return False
        return bool(slots.get("on_target_actions") or slots.get("on_target_desc"))

    def report_muted_enter_rules(self, task_id: str) -> list[str]:
        """哪些 enter 规则此刻选不到动作 —— 条件照判、状态照推, 就是不做事。

        判据不自己写: 直接问读侧那条选槽链路 (``runner._select_slot``, 只读 task
        的动作槽)。所以这里永远与真正执行时一致。
        """
        muted = [
            rule.id
            for rule in self._repo.list_by_task(task_id)
            if rule.resolved_direction is RuleDirection.ENTER
            and self._runner.selects_no_action_on_enter(rule)
        ]
        return muted

    def report_task_config_problems(self, task_id: str) -> list[str]:
        """这个 task 此刻有哪些「配置合法但永不执行」的毛病。

        做成收敛点诊断而不是写入闸: 这类不变式的破坏入口不止一条, 而闸只守得住
        写它时看见的那个 ——

        - 哑的 enter 规则: 清进入槽、rule 换方向或改挂 task 的收尾清理、同方向
          兄弟争槽导致 rule 侧动作根本没写进去
        - 方向组合非法 (没有进路径 / session 与别人混挂 / 配了达标却没有出路径):
          写 rule 那条路上有闸, 而 ``rule delete`` 删掉最后一条 enter 规则造成同样
          的状态、一道校验都没有 —— 那条路上也不该有闸, 拒绝删除会把「先删掉它」
          这条自救路堵死

        判据一律复用写入侧那几份 (``task_rule_set_error`` / 读侧选槽), 不在这里
        重写一遍: 复刻永远滞后于被复刻者, 这个 PR 已经为此改过四轮。
        """
        problems: list[str] = []

        # 空集不用自己挡: task_rule_set_error 对「一条 rule 都还没有」是放行的
        # (装配分步进行)。再包一层守卫就是把它的判据抄了一半。
        error = task_rule_set_error(
            [r.resolved_direction for r in self._repo.list_by_task(task_id)],
            self._task_has_target_action(task_id),
        )
        if error:
            problems.append(error)

        muted = self.report_muted_enter_rules(task_id)
        if muted:
            problems.append(
                f"这些 enter 规则选不到动作, 触发后什么都不做: {', '.join(muted)}。"
                f'把 task 的进入动作配回来: miloco-cli task set-actions {task_id} '
                '--on-enter-desc "..."'
            )

        for problem in problems:
            logger.warning("task %s 配置有问题: %s", task_id, problem)
        return problems

    def require_exit_path_for_target(self, task_id: str) -> None:
        """配达标动作前先确认 task 有出路径。判据与建 rule 时同一份。

        名下一条 rule 都还没有时放行 —— 装配是分步的, 先配动作后建规则是正常顺序,
        那一刻判不出形态。真装出没有出路径的组合会在建那条 rule 时被拦下。
        """
        directions = [r.resolved_direction for r in self._repo.list_by_task(task_id)]
        error = task_rule_set_error(directions, has_target_action=True)
        if error:
            raise ValidationException(error)

    def _validate_target_record(self, rule: Rule) -> None:
        """用户在 rule 上填 ``on_target_desc`` 那条旧路径的校验。"""
        task_id = self._target_record_task_id(rule)
        if not task_id:
            return
        self.require_duration_target(task_id)

    def require_duration_target(self, task_id: str) -> None:
        """配达标动作要求这个 task 有 duration record + target_minutes。

        没有阈值的达标通知永远不会触发，配成功等于留一个用户发现不了的失效项。
        报错按当前 record 状态分三种 case，每种附可执行的 CLI 修复命令。

        两条写达标动作的路径都要走: rule 上的旧 flag 和 ``task set-actions``。
        只挡一条的话另一条就成了绕过它的口子。
        """
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

    # ---- 条件项：补齐 → 校验 → 渲染 ----

    async def _prepare_condition(self, rule: Rule, *, stored_query: str | None) -> None:
        """把 ``condition`` / ``condition_dnf`` 两列弄成一致且合法，就地改 ``rule``。

        五步的先后有约束，写错顺序会让其中几步空转 —— 而它们会打出绿灯，让人以为
        查过了：

        1. 补齐 ``condition_dnf``（没带就从 ``condition`` 反推）
        2. 校验 DNF 结构 + 源特有校验。**不能排在补齐之前** —— 那样 omni rule 会
           因为「没带 DNF」被自己的闸挡住
        3. 校验调用方传上来的**原始** ``condition`` 与 DNF 的关系。**必须排在渲染
           之前** —— 渲染会覆盖 ``query``，覆盖之后「用户传了非空 query」与「用户
           传的是空占位」长得一模一样
        4. 渲染 ``condition.query``（服务端渲染的那些源）
        5. 校验 ``query``：非空（全部源）+ 措辞（仅 omni）。**必须排在渲染之后** ——
           排在前面的话 iot 传的空串占位会被非空校验拦掉，而渲染失效反倒查不出来

        第 3 步与第 5 步别合并：前者查的是渲染前的原始值，后者查的是最终要落库的值。

        ``stored_query`` 是这条 rule 库里已存的 query（新建时 None），第 3 步用它 ——
        见 ``_validate_condition_against_dnf``。
        """
        dnf_was_given = rule.condition_dnf is not None
        if not dnf_was_given:
            rule.condition_dnf = condition_to_dnf(rule.condition)

        error = dnf_structure_error(rule.condition_dnf)
        if error:
            raise ValidationException(error)

        source_type = rule.resolved_source_type
        iot_context: tuple[str, dict] | None = None
        if source_type == IOT_SOURCE_TYPE:
            iot_context = await self._validate_iot_item(rule)

        if dnf_was_given:
            self._validate_condition_against_dnf(rule, source_type, stored_query)

        if is_server_rendered(source_type):
            assert iot_context is not None
            device_name, prop_entry = iot_context
            item = single_item_of(rule.condition_dnf)
            rule.condition.query = render_iot_condition(
                device_name, prop_entry, item.spec["op"], item.spec["value"]
            )

        _validate_query_not_empty(rule.condition.query)
        if source_type == OMNI_SOURCE_TYPE:
            _validate_query_phrasing(rule.condition.query)

    def _validate_condition_against_dnf(
        self, rule: Rule, source_type: str, stored_query: str | None
    ) -> None:
        """调用方同时带了 ``condition`` 和 ``condition_dnf`` 时，两者要对得上。

        同时带是 create / PUT 上的**常态**不是异常：``Rule.condition`` 必填，所以
        每一次都必然带 ``condition``，iot rule 就是「带占位的 condition + 带真实的
        condition_dnf」。这里照搬 PATCH 的「两个都给就拒」会把 iot rule 自己挡死。

        非 omni 要求空占位：带了别的非空 ``query`` 会被渲染静默覆盖、用户输入无声
        丢失；带了真实 did 会留成收口后没人读的脏数据。

        **例外是「这条 rule 库里已存的那句」。** GET 返回的是渲染后的非空 query，
        客户端原样 PUT 回来是最自然的用法，只认空串的话这条路直接被拒。取库里已存
        的值而不是「现在渲染出来的那一句」：渲染文本含设备名，而设备名用户随时能
        改 —— 按现渲染值比的话，改完名这条 rule 就 PUT 不动了。
        """
        if source_type == OMNI_SOURCE_TYPE:
            spec = single_item_of(rule.condition_dnf).spec or {}
            same = (
                list(spec.get("perceive_device_ids") or [])
                == list(rule.condition.perceive_device_ids)
                and (spec.get("query") or "") == rule.condition.query
            )
            if not same:
                raise ValidationException(
                    "condition 与 condition_dnf 里的 omni 条件项不一致: "
                    "无论以哪份为准都是静默覆盖另一份"
                )
            return

        if rule.condition.perceive_device_ids:
            raise ValidationException(
                f"source_type={source_type} 的规则不看摄像头, "
                "condition.perceive_device_ids 要留空"
            )
        allowed = {""} if stored_query is None else {"", stored_query}
        if rule.condition.query not in allowed:
            raise ValidationException(
                f"source_type={source_type} 的 condition.query 由服务端按谓词渲染, "
                "创建时传空串占位即可"
            )

    async def _validate_iot_item(self, rule: Rule) -> tuple[str, dict]:
        """iot 条件项的静态校验。返回 ``(设备名, 属性 entry)`` 供渲染复用。

        全部可在创建时查完 —— 抄错一位的话规则建得成功、永远不触发，用户和 agent
        都拿不到反馈。
        """
        item = single_item_of(rule.condition_dnf)
        spec_item = item.spec or {}
        did = str(spec_item.get("did") or "")
        iid = str(spec_item.get("iid") or "")
        op = spec_item.get("op")
        if not did or not iid:
            raise ValidationException("iot 条件项要写 did 与 iid")
        if "value" not in spec_item:
            raise ValidationException("iot 条件项要写 value")
        if op not in SUPPORTED_OPS:
            raise ValidationException(
                f"iot 条件项不支持 op={op!r}: 可用的是 "
                f"{', '.join(sorted(SUPPORTED_OPS))}"
            )
        if not is_subscribable_did(did):
            raise ValidationException(
                f"did {did!r} 含 '/', 拿不到属性推送 —— 桥接子设备不能当 iot 触发源"
            )

        from miloco.manager import get_manager

        manager = get_manager()
        # **`get_device_spec` 答不了作用域**: 它走 `get_devices()`, 那是账号全量, 过滤
        # 由各调用方自己做 (同文件的 control_device 就为此额外查了一次)。而容器的三条
        # 写入通道都按启用家庭过滤, 所以未启用家庭的设备那条叶子永远不进容器 —— 规则
        # 建得成功、恒 path_missing、永远不触发。
        #
        # 入口能穷举 (create / PUT / PATCH 三条都走 _prepare_condition, 迁移与代建都不
        # 产 iot 条件项), 所以这道闸放在写入时。
        if did not in await manager.miot_proxy.devices_in_current_home():
            raise ValidationException(
                f"设备 {did!r} 不在当前启用的家庭里, 它的属性不会进状态容器"
            )
        device = await manager.miot_service.get_device_spec(did)
        spec = device.get("spec") or {}
        if not spec:
            # 拿不到 spec 与「这台设备真没属性」在调用侧长得一模一样, 不区分 ——
            # 几种情形对建规则的结论相同（校验做不了）。
            raise ValidationException(
                f"拿不到设备 {did!r} 的 spec, 无法校验 iot 条件项"
            )
        entry = spec.get(f"prop.{iid}")
        if not isinstance(entry, dict):
            raise ValidationException(f"属性 prop.{iid} 不在设备 {did!r} 的 spec 里")
        if not entry.get("notify"):
            raise ValidationException(
                f"属性 prop.{iid} 的 access 不含 notify, 拿不到推送: "
                "规则只会在启动那一刻算一次, 之后永远没有输入"
            )

        _validate_iot_value(entry, op, spec_item["value"], iid)
        return str(device.get("name") or did), entry

    async def _validate_perceive_devices_of(self, rule: Rule) -> None:
        """只有 omni rule 校验感知设备列表。

        非 omni rule 那一列是空占位 (§3.4), 收口之后没人读它。改内层
        ``_validate_perceive_device_ids`` 不行: 它的入参是 ``list[str]``, 看不到
        source_type, 在那里加判断要把 rule 再传一遍, 而它有三个调用方。
        """
        if rule.resolved_source_type != OMNI_SOURCE_TYPE:
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

        self._require_task_exists(rule.task_id)

        self._fill_default_duration_ratio(rule)

        await self._prepare_condition(rule, stored_query=None)
        _validate_rule_consistency(rule)
        await self._validate_perceive_devices_of(rule)
        self._validate_target_record(rule)
        self._validate_task_rule_set(rule)
        self._validate_action_reachable(rule)
        await self._validate_scene_ids(rule)

        rule_id = self._repo.create(rule)
        if not rule_id:
            raise BusinessException("Failed to create rule")

        rule.id = rule_id
        self._runner.add_rule(rule)
        # 顺序要紧: 先把动作写进 task 列, 再 reconfigure —— 后者刷的是 task 列的快照
        self.sync_rule_actions_to_task(rule)
        self.reconfigure_task(rule.task_id)
        # seed 必须排在 reconfigure 之后: 挂在 add_rule 里的话, 新建一条「条件已经
        # 为真」的 iot rule 会立刻产生 ENTERED, 而此刻 task 还没登记新拓扑、动作快照
        # 也还没同步 —— 动作被跳过, 而且之后属性不再变化就不会补发。
        self._runner.seed_iot_rule(rule_id)
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

        # 与 create 同一道闸: task_id 是 FK, 指向不存在的 task 会让 sqlite 抛
        # IntegrityError, 它不在 repo 那层的 except 里, 一路冒到全局处理器变成
        # 500 —— 而这只是一个参数填错。
        self._require_task_exists(rule.task_id)
        _reject_task_move(previous.task_id, rule.task_id)

        self._fill_default_duration_ratio(rule)

        # PUT 收的是完整 Rule, 输入形态与 create 相同 (pydantic 强制带 condition),
        # 所以走 create 那套而不是 PATCH 那套 —— 照搬 PATCH 的「不许同时给」会让
        # 任何 iot rule 都改不了。
        # 跨源判定排在五步之前: 排在后面的话, 换成 omni 的那次会先撞上「condition 与
        # DNF 不一致」, 错误文案指的是形状而不是这次真正做错的事。
        _reject_source_change(previous, rule)
        await self._prepare_condition(rule, stored_query=previous.condition.query)
        _validate_rule_consistency(rule)
        await self._validate_perceive_devices_of(rule)
        self._validate_target_record(rule)
        self._validate_task_rule_set(rule, previous)
        self._validate_action_reachable(rule)
        await self._validate_scene_ids(rule)

        success = self._repo.update(rule)
        if success:
            self._runner.add_rule(rule)
            # 与 PATCH 同一份收尾。整体替换同样能换方向、改挂 task, 少了这两步
            # 旧槽里留着一份没人认领也没人读得到的动作, 旧 task 的拓扑还挂着一条
            # 已经不属于它的 rule。
            if (
                previous.resolved_direction is not rule.resolved_direction
                or previous.task_id != rule.task_id
            ):
                self._clear_task_slots(previous)
            self.sync_rule_actions_to_task(rule)
            self.reconfigure_task(rule.task_id)
            if previous.task_id != rule.task_id:
                self.reconfigure_task(previous.task_id)
            self._runner.seed_iot_rule(rule.id)
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
            self._require_task_exists(update.task_id)
            _reject_task_move(existing.task_id, update.task_id)

        # mode 与 direction 是同一个语义的两种存储形态, 必须一起定。Rule 没开
        # validate_assignment, 逐字段赋值不会重跑构造期那条一致性校验 —— 只改一个
        # 就写出互相矛盾的行, 下次从库里构造 Rule 时校验器抛 ValidationError, 它是
        # ValueError 子类, 正好落进各读取口的 except: 规则从所有列表里消失。
        if {"mode", "direction"} & fields:
            new_direction = update.direction
            if new_direction is None and update.mode is not None:
                new_direction = _MODE_TO_DIRECTION[update.mode.value]
            if new_direction is not None:
                expected_mode = _DIRECTION_TO_MODE[new_direction]
                if update.mode is not None and update.mode is not expected_mode:
                    raise ValidationException(
                        f"direction={new_direction.value} 对应 "
                        f"mode={expected_mode.value}, 收到 mode={update.mode.value}"
                    )
                existing.direction = new_direction
                existing.mode = expected_mode

        if "lifecycle" in fields and update.lifecycle is not None:
            existing.lifecycle = update.lifecycle

        if "enabled" in fields and update.enabled is not None:
            existing.enabled = update.enabled

        # condition 与 condition_dnf 是改条件的两条路, 同时给就是两份真相。
        # 只在本次 PATCH 真的碰了条件时才判 —— 写成「恰好给一个」的话，只改
        # --name 的请求（两个字段都不给）会被误杀，而那是正常的。
        if {"condition", "condition_dnf"} <= fields:
            raise ValidationException(
                "condition 与 condition_dnf 不能同时给: 改条件只有一条路"
            )

        if "condition_dnf" in fields:
            if update.condition_dnf is None:
                raise ValidationException(
                    "condition_dnf cannot be cleared (rule must have a condition)"
                )
            # 整项替换。合并没有定义 —— 合并到哪一层、any_of 的第几项，都答不上来。
            existing.condition_dnf = update.condition_dnf
            _sync_legacy_condition_from(existing)

        if "condition" in fields:
            # condition 不允许显式置 null：Rule.condition 必填，整体清空没语义。
            if update.condition is None:
                raise ValidationException(
                    "condition cannot be cleared (rule must have a condition)"
                )
            if existing.resolved_source_type != OMNI_SOURCE_TYPE:
                # 非 omni rule 的这两列是占位: query 由服务端按谓词渲染、设备列表恒
                # 空。放行的话用户的输入会被下一次渲染静默覆盖。改条件走
                # --condition-dnf(CLI 的 --iot-* 四件套)。
                raise ValidationException(
                    f"source_type={existing.resolved_source_type} 的规则不能改 "
                    "condition.query / perceive_device_ids: 它的条件由服务端按谓词"
                    "渲染。改条件请改 condition_dnf。"
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
            # 两列要一起走: 只改 condition 会让 DNF 停在旧值, 而 omni 的 prompt 从
            # DNF 取 —— 界面上改了、判定用的还是旧的那句。
            existing.condition_dnf = None

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

        if {"condition", "condition_dnf"} & fields:
            _reject_source_change(previous, existing)
            await self._prepare_condition(
                existing, stored_query=previous.condition.query
            )
        _validate_rule_consistency(existing)
        self._validate_task_rule_set(existing, previous)
        # 下面三道都依赖 rule 之外的状态 (task 的动作槽 / record 的阈值 / 场景是否
        # 还在), 一律按"这次 PATCH 真的动了什么"跑, 口径同上面的 perceive_device_ids。
        # 无条件跑的话, 那些状态一变, 连 `rule disable` (它本身就是一次 PATCH) 都会
        # 400 —— 规则坏掉的那一刻正好把「先关掉它」这条自救路堵死。清空 task 的进入
        # 槽是本模型认可的合法操作 (由 report_muted_enter_rules 诊断, 不由闸拦),
        # 更不该让它把名下的规则变成既不做事、也关不掉。
        if fields & {"on_target_desc", "task_id"}:
            self._validate_target_record(existing)
        if fields & {
            "mode", "direction", "task_id",
            "actions", "action_descriptions",
            "on_enter_actions", "on_enter_desc",
        }:
            self._validate_action_reachable(existing)
        if {"actions", "on_enter_actions", "on_exit_actions"} & fields:
            await self._validate_scene_ids(existing)

        success = self._repo.update(existing)
        if success:
            self._runner.add_rule(existing)
            moved_home = (
                previous.resolved_direction is not existing.resolved_direction
                or previous.task_id != existing.task_id
            )
            if moved_home:
                # 换方向或改挂 task = 这份动作整体换了个家。旧的那份必须清 ——
                # 留着就是一份没有 rule 认领、也再没人读得到的动作; 新的那份必须
                # 写 —— 不写就是"规则照常触发、一个动作都选不到", 读侧只认 task
                # 列、不看 rule 行。这里不传动过的字段: 动作字段本身没变,
                # 变的是它该落哪个槽。
                self._clear_task_slots(previous)
                self.sync_rule_actions_to_task(existing)
            else:
                # 带上这次动过的字段: 只透传被动过的槽, 别的槽保留 task 侧那份
                self.sync_rule_actions_to_task(existing, fields)
            self.reconfigure_task(existing.task_id)
            if previous.task_id != existing.task_id:
                # 原 task 少了一条 rule, 拓扑得跟着变 —— 与删 rule 同一条路径。
                self.reconfigure_task(previous.task_id)
            self._runner.seed_iot_rule(rule_id)
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
            # 顺序要紧: 先 reconfigure。DB 行已删, 拓扑失去这条出路径、会触发 on_exit,
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

    def forget_task(self, task_id: str) -> None:
        """task 被删 —— 清掉所有 per-task 的内存态。

        rule 维度走 ``remove_rule_from_runner``, 它清不到按 task_id 存的那些:
        状态机拓扑、运行态、判定跟踪、动作快照、停用标记、达标源的轮次计数。
        record timer 不在这里撤 —— 它按 rule_id 存, 逐条清 rule 时已经撤掉了。

        ``task_id`` 是用户自己起的名字, 删掉再用同名重建是正常操作 —— 不清的话新
        task 会继承上一条的运行态(建第一条 rule 时派一次没来由的 on_exit)和停用
        标记(有效启用恒假, 一条 rule 都不触发, 直到重启)。
        """
        sm = self._runner.state_machine
        if sm is not None and sm.owns(task_id):
            # unregister_task 内含 on_forget → 判定跟踪一并清
            sm.unregister_task(task_id)
        self._runner.set_task_actions(task_id, None)
        self._runner.set_task_paused(task_id, False)
        self._runner.record_source.forget_task(task_id)

    @property
    def decision_tracker(self):
        """给 task 层读判定摘要用。没接管时为 None。"""
        return self._runner.tracker

    @property
    def iot_source(self):
        """iot 源。容器没接上来时是 None（单测和退化启动）。"""
        return self._runner.iot_source

    @property
    def runner_state_machine(self):
        """给 task 层读运行态用。没接管时为 None。"""
        return self._runner.state_machine

    # ---- 重新配置路径 (§19.5) ----

    def _target_notified_today(self, rule_name: str) -> bool:
        """今天这条达标规则的通知发出去过没有。

        按名字问而不是按 id: 这条规则随阈值增删反复消失重建, 每次都是新 id。触发
        日志按设计不跟着删, 是"到底发过没有"的唯一凭据。

        成功和失败两种日志都算: 这里问的是"条件今天翻真过没有", 而这条规则只为
        达标触发, 有日志就说明翻过。发不出去是投递的事, 与该不该重发无关 —— 条件
        的值当时也不看投递结果。
        """
        from datetime import datetime

        from miloco.utils.time_utils import deploy_timezone

        day_start = datetime.now(deploy_timezone()).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (
            self._log_repo.count_by_rule_name(
                rule_name,
                # 减一是因为 after_ts 是严格大于, 零点整那一条也要算进今天
                after_ts=int(day_start.timestamp() * 1000) - 1,
            )
            > 0
        )

    def _require_task_exists(self, task_id: str) -> None:
        """rule.task_id 是 FK, 指向不存在的 task 只能在写之前拦。"""
        if not self._task_repo.task_exists(task_id):
            raise ResourceNotFoundException(
                f"task_not_found: rule.task_id={task_id!r} 对应 task 不存在"
            )

    def _slots_cleared_by(self, rule: Rule) -> set[str]:
        """rule 不再管辖时会被清掉的那些槽。兄弟 rule 也管着的排掉 —— 否则会把
        别人的动作一起抹掉。

        只服务 ``_clear_task_slots``: 换方向 / 改挂 task 时要清掉旧家那份, 而
        "兄弟也管着"这一层判断只此一份。
        """
        stale = set(_rule_action_slots(rule))
        if not stale:
            return stale
        return stale - {
            name
            for r in self._repo.list_by_task(rule.task_id)
            if r.id != rule.id
            for name in _rule_action_slots(r)
        }

    def _slots_contended_by_siblings(self, rule: Rule, slots: set[str]) -> list[str]:
        """slots 里有哪些槽兄弟 rule 也管着 —— 争用的那些透传会整体跳过。

        与 ``_slots_cleared_by`` 问的是同一层"兄弟也管着吗", 服务的却是另一条路:
        这里决定透传跳不跳, 那里决定清空清哪几个。争用的槽整体跳过 —— 从一条
        rule 单向覆盖会把另一条的动作悄悄冲掉。
        """
        return sorted(
            slots
            & {
                name
                for r in self._repo.list_by_task(rule.task_id)
                if r.id != rule.id
                for name in _rule_action_slots(r)
            }
        )

    def _clear_task_slots(self, rule: Rule) -> None:
        """把 rule 之前管辖的槽清空。只在它不再管辖那些槽时调 (换方向 / 改挂 task)。"""
        stale = self._slots_cleared_by(rule)
        if not stale:
            return
        self._task_repo.set_boundary_actions(
            rule.task_id,
            **{k: ([] if k.endswith("_actions") else None) for k in stale},
        )

    def sync_rule_actions_to_task(
        self, rule: Rule, changed_fields: set[str] | None = None
    ) -> None:
        """把 rule 上的动作写进它所属 task 的边界动作列。

        §10.3 阶段 A 说「CLI 加新 flag，旧 flag 仍可用」——**旧 flag 仍可用意味着
        它的写入必须落到新位置**。读侧只认 task 列, 写侧不透传的话, 用现有 CLI 改
        动作会静默不生效: rule 列改了、fire 读的是 task 列的旧值, 而 CLI 返回成功、
        ``rule get`` 也显示新值。

        ``changed_fields`` 传 PATCH 显式动过的字段名, 决定透传哪几个槽 (见
        ``_rule_action_slots``)。不传按"只写自己填了值的槽"处理。

        多条 rule 争同一个槽时跳过并告警 —— 从一条 rule 单向覆盖会把另一条的动作
        悄悄冲掉。不同方向的 rule 各写各的槽, 不算争。

        阶段 B 动作 flag 落到 task 上之后这个函数整体删除。
        """
        # 代建的达标规则不算 —— 它是派生物, 用户根本看不到它。算进来的话凡是配了
        # 达标通知的 task 都会走进下面那条跳过分支, 之后每一次改动作都只写 rule 行、
        # 不写 task 列, 而 fire 读的正是 task 列: CLI 返回成功、rule get 显示新值、
        # 实际行为不变。
        slots = _rule_action_slots(rule, changed_fields)
        if not slots:
            return

        # 只有争同一个槽才跳过。不同方向的 rule 各写各的槽 —— enter 写 on_enter、
        # exit 写 on_exit, 本来就不冲突; 一律按"有没有兄弟"跳过的话, 非互反 task
        # (enter + exit) 用 rule 侧 flag 建, 第二条起的动作全部静默丢失。
        contended = self._slots_contended_by_siblings(rule, set(slots))
        if contended:
            logger.warning(
                "task %s 名下有别的 rule 也管着 %s, 不把 rule %s 的动作透传到 "
                "task 列; 请改用 task 侧的动作入口",
                rule.task_id,
                contended,
                rule.id,
            )
            return
        # rule 写入已经成功并且是主要效果, 不能被 task 侧同步的失败带崩。但也不能
        # 静默 —— 同步没成功就意味着"动作只读"那个坑还在, 必须留下明显的线索。
        try:
            written = self._task_repo.set_boundary_actions(rule.task_id, **slots)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "把 rule %s 的动作同步到 task %s 失败: %s; "
                "这次动作改动不会生效 —— 读侧只认 task 列",
                rule.id,
                rule.task_id,
                e,
            )
            return
        if not written:
            logger.warning(
                "task %s 没有对应行, rule %s 的动作没同步过去", rule.task_id, rule.id
            )

    # ---- 达标规则是派生物, 不是用户建的东西 (spec §6.4 / §9) ----

    def _milestone_is_wanted(self, task_id: str) -> bool | None:
        """这个 task 该不该有达标规则。``None`` = 读不出来, 调用方按"别动"处理。

        判据三样齐备: task 配了达标动作、有活跃 duration record、record 上有阈值。
        """
        try:
            view = self._task_repo.get_full_view(task_id) or {}
            slots = view.get("actions") or {}
            if not (slots.get("on_target_actions") or slots.get("on_target_desc")):
                return False
            state = self._task_record_service.read_duration_target_state(task_id)
        except Exception:
            logger.exception("读 task %s 的达标配置失败, 这次不动达标规则", task_id)
            return None
        return state is not None and state[0] is not None

    def _build_milestone_rule(self, task_id: str) -> Rule:
        """代建的那条 rule 长什么样。

        不带阈值: 阈值每次去 record 上现读, 存副本会在用户改目标后分叉。名字与条件
        都取自 ``record_source`` 里那份共用形状, 迁移补建走的是同一份。
        """
        return Rule(
            name=milestone_rule_name(task_id),
            task_id=task_id,
            # mode 是 NOT NULL 且表达不了 milestone, 存一个自洽的占位值。
            mode=RuleMode.EVENT,
            direction=RuleDirection.MILESTONE,
            lifecycle=RuleLifecycle.PERMANENT,
            condition=RuleCondition(**milestone_legacy_condition(task_id)),
            condition_dnf=RuleConditionDNF(**milestone_condition_dnf(task_id)),
        )

    def _milestone_shape_is_current(self, rule: Rule) -> bool:
        """这条代建规则的形状还是当前这一版吗。

        比的是求值真正读的那几样: 条件项 (``record_ref_of`` 只看 source_type 与
        spec)、旧 condition 列上的设备列表、名字 (判重名的键)。阈值不在里面 ——
        它本来就不进形状。
        """
        want = self._build_milestone_rule(rule.task_id)
        return (
            rule.name == want.name
            and rule.condition_dnf == want.condition_dnf
            and rule.condition.perceive_device_ids
            == want.condition.perceive_device_ids
        )

    def reconcile_milestone_rule(self, task_id: str) -> None:
        """让代建的达标规则跟上"该不该有"。

        做成派生量而不是让用户维护成对关系, 是因为装配分步进行 —— 先配动作还是
        先建 record, 中间态必然有一半不成立。派生量没有中间态可言, 齐备的那一刻
        就有了。

        不走 ``create_rule`` / ``delete_rule``: 那两条会再调一次重新配置, 而本函数
        正是被它调用的。
        """
        wanted = self._milestone_is_wanted(task_id)
        if wanted is None:
            return

        existing = [
            r
            for r in self._repo.list_by_task(task_id)
            if r.resolved_direction is RuleDirection.MILESTONE
        ]
        # 齐备时留一条形状还对得上的; 多出来的、形状过期的、"不该有"的一起清掉,
        # 下面照当前形状重建。只数个数的话, 存量里形状漂移的那条会被永久留着 ——
        # 用户观察到的是"同样配了达标提醒, 新建的 task 会响、老的不响"。
        current = [r for r in existing if self._milestone_shape_is_current(r)]
        keep = current[:1] if wanted else []
        keep_ids = {r.id for r in keep}
        for rule in existing:
            if not rule.id or rule.id in keep_ids:
                continue
            logger.info("达标规则 %s 不再需要, 删除 (task=%s)", rule.id, task_id)
            self._repo.delete(rule.id)
            self._runner.remove_rule(rule.id)
            # 触发日志留着: 这条规则会随阈值增删反复消失重建, 跟着删等于用户改一次
            # 目标就丢掉全部达标历史, 而那是查"到底发过没有"的唯一凭据。

        if not wanted or keep:
            return

        rule = self._build_milestone_rule(task_id)
        if self._repo.exists_by_name(rule.name):
            logger.warning(
                "task %s 的达标规则重名, 不代建; 改掉同名规则后会自动补上", task_id
            )
            return
        rule_id = self._repo.create(rule)
        if not rule_id:
            logger.warning("task %s 的达标规则代建失败", task_id)
            return
        rule.id = rule_id
        self._runner.add_rule(rule)
        if self._target_notified_today(rule.name):
            # 条件层状态按 rule_id 存, 重建换了 id 就等于把"今天发过了"擦掉 ——
            # 与启动、停用再启用同一个失败模式, 只是这次丢的载体是 rule 身份。
            # 清掉阈值再设回来就会当天重发一次。今天头一回配达标的 task 查不到
            # 日志, 照常发。
            self._runner.seed_reached_target(rule_id)
        logger.info("代建达标规则 %s (task=%s)", rule_id, task_id)

    def notify_record_changed(self, task_id: str) -> None:
        """record 的 kind / 阈值变了 —— 达标规则该不该有可能跟着变。"""
        self.reconfigure_task(task_id)

    def reconfigure_task(self, task_id: str) -> None:
        """rule 增删改、rule 单独启停、task 重新 enable 统一走这条。

        做四件事: 刷新 task 动作快照 → 重算拓扑 → 失去全部出路径且当前为 on 时
        先跑 on_exit → 清运行态从 off 起。前三件由 ``TaskStateMachine.reconfigure``
        承担, 这里只负责把最新的拓扑与动作喂进去。

        名下已无 rule → 撤销登记。动作配没配不参与这个判断 (理由见
        ``attach_task_state_machine``)。
        """
        # 必须排在读拓扑之前: 代建 / 删掉的那条也要算进这次的拓扑。
        self.reconcile_milestone_rule(task_id)

        sm = self._runner.state_machine
        if sm is None:
            return

        from miloco.task.state_machine import TaskRuntimeState

        self._runner.set_task_actions(
            task_id, self._task_repo.get_boundary_actions(task_id)
        )
        # 快照刚刷完 —— 此刻问读侧拿到的就是接下来 fire 时的答案。
        self.report_task_config_problems(task_id)
        # 从 DB 读而非内存: DB 是归属的权威源, 且删 rule 时行已落库、runner 内存
        # 还留着那条 —— 正需要这个错位, 空拓扑触发 on_exit 时动作还有 rule 可归属。
        rules = self._repo.list_by_task(task_id)
        if not rules:
            if sm.owns(task_id):
                # 空拓扑先走一次 reconfigure：删掉最后一条 rule 也是「失去全部
                # 出路径」，task 在 on 时必须先跑 on_exit，直接 unregister 会让
                # 那次退出动作永远不执行。
                sm.reconfigure(task_id, {})
                sm.unregister_task(task_id)
            return
        sm.reconfigure(task_id, _live_topology(rules))
        # 排 timer 只挂在「进入会话」那个边沿上, 而装配是分步的 —— 三样齐备的那
        # 一刻可能落在会话开始之后, 那时进入边沿早过去了, 这一天的达标就只能靠
        # 退出兜底或跨零点补发, 而这条通知的全部意义是到点提醒。已经在态内就补
        # 排一次: arm 自带撤旧, 等于按当前累计重排, 阈值改了也一并跟上。
        if sm.runtime_state(task_id) is TaskRuntimeState.ON:
            self._runner.record_source.arm(task_id)

    def apply_task_status(self, task_id: str, active: bool) -> None:
        """task 启停 → 刷新派生的「有效启用」并走重新配置路径。

        **不写 rule.enabled** —— 它是用户意图, task 启停覆写它会把用户手动关掉
        的那条 rule 在 task 重新 enable 时错误地打开 (§19.9)。
        """
        self._runner.set_task_paused(task_id, not active)
        if active:
            # enable: 停用期间 rule 可能被改过, 要重新登记拓扑
            self.reconfigure_task(task_id)
            # 停用清掉了条件层状态, 达标那条"今天发过了"一并没了 —— 与重启同一个
            # 失败模式、同一份修法。必须排在 reconfigure 之后: 代建的那条 rule 可能
            # 正是它刚补上的。
            _seed_reached_targets(self._runner, task_id)
            # iot 同一个失败模式: 停用清掉了条件层状态, 属性持续为真的话没有任何变更
            # 到达, rule 永远等不到 ENTERED。**不放 reconfigure_task 里 record_source
            # .arm 那一位** —— 那里被 `runtime_state is ON` 守着, 而 suspend 停用时
            # 已经把运行态置成 OFF, 重新启用走到那儿时守卫恒假。
            self._runner.seed_iot_rules_of_task(task_id)
            return
        self._runner.record_source.disarm(task_id)
        # 不派发 on_exit 是对的 (见 suspend), 但计时段的收尾也挂在那个槽上, 得自
        # 己收。收段失败不能拖垮下面的 suspend —— 那一步不做, 停用就只写了 DB。
        try:
            self._task_record_service.close_active_session(task_id)
        except Exception:
            logger.exception("停用 task %s 时收尾计时段失败", task_id)
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
