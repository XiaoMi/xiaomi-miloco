"""rule 命令组：list / create / update / enable / disable / delete / logs / logs-cleanup / trigger。"""

import json
import sys

import click

from miloco_cli.output import print_result

API_PREFIX = "/api/rules"

# 与 backend rule/schema.py 的 SCENE_IID 对齐。CLI 是独立包不依赖 miloco，
# 故复刻字面量；改动时两处同步。
SCENE_IID = "scene"


def _rule_cursor_file():
    from miloco_cli.config import miloco_home
    return miloco_home() / "rule_cursor.json"


def _load_rule_cursor() -> int | None:
    """读取本地 rule cursor（Unix ms）。"""
    cursor_file = _rule_cursor_file()
    if not cursor_file.exists():
        return None
    try:
        return json.loads(cursor_file.read_text()).get("cursor_ms")
    except (json.JSONDecodeError, OSError):
        return None


def _save_rule_cursor(cursor_ms: int) -> None:
    """原子写入 rule cursor。"""
    from miloco_cli.config import atomic_write
    atomic_write(_rule_cursor_file(), {"cursor_ms": cursor_ms})


_IOT_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")

# MIoT 的 format 取值域。整数族全部按 int 解析；bool 只接受 true / false ——
# 接受 1 / 0 会让「开关配了数值」这类错误在 CLI 层就溜过去。
_IOT_INT_FORMATS = (
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "int8",
    "int16",
    "int32",
    "int64",
)


def iot_condition_options(func):
    """``--iot-did/-iid/-op/-value`` 四件套。create 与 update 共用一份。"""
    options = (
        click.option("--iot-did", "iot_did", default=None, help="设备 did"),
        click.option(
            "--iot-iid",
            "iot_iid",
            default=None,
            help="属性 iid，形如 5.1（与状态容器的路径段一致）",
        ),
        click.option(
            "--iot-op",
            "iot_op",
            default=None,
            type=click.Choice(_IOT_OPS),
            help="比较符",
        ),
        click.option(
            "--iot-value",
            "iot_value",
            default=None,
            help="阈值。类型按该属性 spec 的 format 解析",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


def _reject_mixed_condition_args(query_text, perceive_devices, iot_given) -> None:
    """视觉那组条件参数与 iot 四件套互斥。

    ``--source`` 也算在这一组里：组装 payload 时它跟着 ``iot_given`` 一起被清空，
    不拦的话用户显式给的设备列表静默消失，而同样的组合在服务端是报错。
    """
    if iot_given and (query_text is not None or perceive_devices):
        raise click.UsageError(
            "--condition / --source 与 iot 四件套不能一起给: 一条规则只有一个条件项"
        )


def _iot_args_given(iot_did, iot_iid, iot_op, iot_value) -> bool:
    """四个同时给或同时不给。给了一部分直接报错 —— 半套参数建不出条件项。"""
    given = [x is not None for x in (iot_did, iot_iid, iot_op, iot_value)]
    if any(given) and not all(given):
        raise click.UsageError(
            "--iot-did / --iot-iid / --iot-op / --iot-value 要么四个都给, 要么都不给"
        )
    return all(given)


def _fetch_prop_format(did: str, iid: str) -> str:
    """去 ``device spec`` 拿这条属性的 format。

    **拿不到就报错退出，不猜类型。** 猜错的后果是规则建得成功、运行期恒判类型不兼容、
    条件恒未就绪 —— 一个查起来很远的失败。
    """
    from miloco_cli.client import api_get

    resp = api_get(f"/api/miot/devices/{did}/spec")
    spec = ((resp or {}).get("data") or {}).get("spec") or {}
    if not spec:
        raise click.UsageError(f"拿不到设备 {did} 的 spec, 无法解析 --iot-value")
    # CLI 参数用裸 siid.piid（与容器路径段一致），spec 输出里的键是 prop.<siid>.<piid>
    entry = spec.get(f"prop.{iid}")
    if not isinstance(entry, dict):
        raise click.UsageError(f"设备 {did} 的 spec 里没有属性 prop.{iid}")
    return str(entry.get("format") or "")


def _parse_iot_value(raw: str, fmt: str, iid: str):
    if fmt == "bool":
        if raw.lower() not in ("true", "false"):
            raise click.UsageError(
                f"属性 prop.{iid} 是布尔, --iot-value 只接受 true / false"
            )
        return raw.lower() == "true"
    if fmt in _IOT_INT_FORMATS:
        try:
            return int(raw)
        except ValueError as e:
            raise click.UsageError(
                f"属性 prop.{iid} 是整数, --iot-value 解析失败"
            ) from e
    if fmt == "float":
        try:
            return float(raw)
        except ValueError as e:
            raise click.UsageError(
                f"属性 prop.{iid} 是浮点, --iot-value 解析失败"
            ) from e
    if fmt == "string":
        return raw
    raise click.UsageError(
        f"属性 prop.{iid} 的 format={fmt!r} 不是标量, 不能当 iot 条件项"
    )


def _build_iot_dnf(iot_did, iot_iid, iot_op, iot_value) -> dict:
    fmt = _fetch_prop_format(iot_did, iot_iid)
    value = _parse_iot_value(iot_value, fmt, iot_iid)
    return {
        "any_of": [
            [
                {
                    "source_type": "iot",
                    "spec": {
                        "did": iot_did,
                        "iid": iot_iid,
                        "op": iot_op,
                        "value": value,
                    },
                    "negate": False,
                }
            ]
        ]
    }


@click.group("rule")
def rule_group():
    """规则操作：列表 / 创建 / 更新 / 启用 / 禁用 / 删除 / 触发 / 日志 / 日志清理。"""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@rule_group.command("list")
@click.option(
    "--enabled-only", is_flag=True, help="仅显示生效中的规则（task 停用的不算）"
)
@click.option(
    "--show-milestone", "show_milestone", is_flag=True,
    help="连服务端维护的达标规则一起显示（默认不显示）",
)
@click.option("--pretty", is_flag=True)
def rule_list(enabled_only, show_milestone, pretty):
    """列出所有规则。

    达标规则默认不显示：它由服务端按 task 的达标动作 + duration record 自动维护，
    不是手工建的。
    """
    from miloco_cli.client import api_get

    params = {}
    if enabled_only:
        params["enabled_only"] = "true"
    if show_milestone:
        params["include_milestone"] = "true"
    data = api_get(API_PREFIX, params or None)
    print_result(data, pretty)


@rule_group.command("iot-diagnostics")
@click.option("--pretty", is_flag=True)
def rule_iot_diagnostics(pretty):
    """iot 触发源的自述：每条条件项现在是真是假还是未就绪、为什么。

    \b
    reason 的取值：
    - ok              正常求值
    - device_offline  设备离线
    - path_missing    容器里没有这条属性叶子
    - eval_failed     类型不兼容，求值做不了
    - not_seeded      还没算过

    **consumer_alive 要单独看。** 消费协程一死，每条 rule 的 reason 都停在最后一次
    求值时的 ok —— 而那正是最需要报警的时刻。
    """
    from miloco_cli.client import api_get

    print_result(api_get(f"{API_PREFIX}/iot/diagnostics"), pretty=pretty)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@rule_group.command("create")
@click.option("--name", required=True, help="规则展示名（自由文本）")
@click.option("--task-id", "task_id", required=True, help="任务 id（snake_case）")
@click.option(
    "--source",
    "perceive_devices",
    multiple=True,
    required=False,
    help=(
        "感知源 did，可重复，可不填。"
        "不填 → 所有感知设备都跑该 rule；"
        "填 → 只在这些 did 上跑。"
        "用户未明确指定设备时优先不填。"
    ),
)
@click.option(
    "--condition",
    "query_text",
    required=False,
    default=None,
    help="触发条件描述（自然语言）—— 摄像头视觉判定走这个",
)
@iot_condition_options
@click.option(
    "--mode",
    "mode_value",
    type=click.Choice(["event", "state"]),
    default=None,
    help="旧入口，只能表达 enter / session 两种方向；新代码用 --direction",
)
@click.option(
    "--direction",
    "direction_value",
    type=click.Choice(["enter", "exit", "session"]),
    default=None,
    help=(
        "边沿如何映射成 task 的进/出：enter 条件成立就把 task 推进去；"
        "exit 条件成立就把 task 推出来；session 进入/退出配对。"
        "不传等价于 enter"
    ),
)
@click.option(
    "--lifecycle",
    "lifecycle_value",
    type=click.Choice(["permanent", "temporary"]),
    default="permanent",
    show_default=True,
    help=(
        "生命周期：permanent 常驻；temporary 由后台 evaluator 评估 "
        "terminate_when 自销毁（注意：当前 evaluator 为 stub，到期不会自动消失，"
        "需 miloco-terminate-task skill 或 rule delete 兜底）"
    ),
)
@click.option(
    "--terminate-when",
    "terminate_when",
    default=None,
    help="lifecycle=temporary 必填，自然语言终止条件",
)
@click.option(
    "--action",
    "actions_raw",
    multiple=True,
    help=(
        "enter / exit 方向的设备直控动作 JSON（可重复）。"
        "落 on_enter 还是 on_exit 由 --direction 决定，不用 --on-exit-action。\n"
        "设备控制（幂等）："
        "{\"did\":\"<id>\",\"iid\":\"prop.<siid>.<piid>\",\"value\":<v>,\"idempotent\":true}\n"
        "通知/播报（必带冷却）："
        "{\"did\":\"<id>\",\"iid\":\"action.<siid>.<aiid>\",\"params\":[\"<text>\"],"
        "\"idempotent\":false,\"cooldown_minutes\":10}\n"
        "触发米家场景（did 放 scene_id，必带冷却）："
        "{\"did\":\"<scene_id>\",\"iid\":\"scene\","
        "\"idempotent\":false,\"cooldown_minutes\":5}"
    ),
)
@click.option(
    "--action-desc",
    "action_descs",
    multiple=True,
    help=(
        "enter / exit 方向的 Agent 回调描述（可重复）。"
        "落哪个方向由 --direction 决定"
    ),
)
@click.option(
    "--on-enter-action",
    "on_enter_actions_raw",
    multiple=True,
    help="session 方向的 on_enter 设备直控动作 JSON（可重复，格式同 --action）",
)
@click.option(
    "--on-enter-desc",
    "on_enter_desc",
    default=None,
    help="session 方向的 on_enter Agent 回调提示文本",
)
@click.option(
    "--on-exit-action",
    "on_exit_actions_raw",
    multiple=True,
    help="session 方向的 on_exit 设备直控动作 JSON（可重复，格式同 --action）",
)
@click.option(
    "--on-exit-desc",
    "on_exit_desc",
    default=None,
    help="session 方向的 on_exit Agent 回调提示文本",
)
@click.option(
    "--on-target-desc",
    "on_target_desc",
    default=None,
    help=(
        "session 方向的达标回调提示文本（duration record 累计达标瞬间触发）。"
        "仅在 task 配 duration record + target_minutes 时有效。"
    ),
)
@click.option(
    "--exit-debounce-seconds",
    "exit_debounce_seconds",
    type=int,
    default=None,
    help="session 方向的 EXIT 防抖（秒），默认 60",
)
@click.option(
    "--duration-seconds",
    "duration_seconds",
    type=int,
    default=None,
    help=(
        "条件需持续该时长才算成立（秒）。不填=立即 fire。"
        "enter / exit：达标 fire 后清窗口周期 fire；"
        "session：达标 fire on_enter 一次，STILL_IN 不重复，EXITED 走 exit_debounce"
    ),
)
@click.option(
    "--duration-ratio",
    "duration_ratio",
    type=float,
    default=None,
    help=(
        "窗口内 True 比例阈值（0,1]，仅 --duration-seconds 设置时有效；"
        "不填用 backend 默认 0.8"
    ),
)
@click.option("--pretty", is_flag=True)
def rule_create(
    name,
    task_id,
    perceive_devices,
    query_text,
    iot_did,
    iot_iid,
    iot_op,
    iot_value,
    mode_value,
    direction_value,
    lifecycle_value,
    terminate_when,
    actions_raw,
    action_descs,
    on_enter_actions_raw,
    on_enter_desc,
    on_exit_actions_raw,
    on_exit_desc,
    on_target_desc,
    exit_debounce_seconds,
    duration_seconds,
    duration_ratio,
    pretty,
):
    """创建规则。执行方式由填了哪个动作 flag 决定（--action → 设备直控，--action-desc → 走 Agent）。

    动作也可以不填在这里 —— 多条同方向 rule 共用一份动作时装在 task 上
    （task set-actions）。direction/action 组合 example 与 condition 写法见
    miloco-create-task SKILL。
    """
    from miloco_cli.client import api_post

    # ---- 0. 条件：--condition 与 iot 四件套恰好给一组 ----
    # 这两条报的是**参数用法**，不是「条件不能为空」——真正拦住空条件的是服务端的
    # 非空校验，那是必经处（API 直调绕过 CLI）。摘掉 required=True 之后有它在，
    # 用户看到的是贴合 CLI 参数的错误，而不是一个从服务端回来的字段级报错。
    iot_given = _iot_args_given(iot_did, iot_iid, iot_op, iot_value)
    _reject_mixed_condition_args(query_text, perceive_devices, iot_given)
    if query_text is None and not iot_given:
        raise click.UsageError(
            "要给 --condition（摄像头视觉判定）或那四个 --iot-* 参数（设备属性变化）"
        )

    # ---- 1. lifecycle ----
    if lifecycle_value == "temporary" and not terminate_when:
        _exit_error("lifecycle=temporary requires --terminate-when")

    # ---- 2. direction x action 矩阵 ----
    direction = _resolve_direction(direction_value, mode_value)
    if direction != "session":
        # 单方向的 rule 只有一个边沿, 动作填在 --action / --action-desc 上;
        # 落 on_enter 还是 on_exit 由 direction 决定, 不用另一套 flag。
        if (
            on_enter_actions_raw
            or on_enter_desc
            or on_exit_actions_raw
            or on_exit_desc
            or on_target_desc
        ):
            _exit_error(
                f"direction={direction} must not set "
                "--on-enter-* / --on-exit-* / --on-target-desc"
            )
        if actions_raw and action_descs:
            _exit_error(
                f"direction={direction}: --action and --action-desc are "
                "mutually exclusive"
            )
    else:
        if actions_raw or action_descs:
            _exit_error("direction=session must not set --action / --action-desc")

        enter_static = bool(on_enter_actions_raw)
        enter_dynamic = bool(on_enter_desc)
        exit_static = bool(on_exit_actions_raw)
        exit_dynamic = bool(on_exit_desc)

        if enter_static and enter_dynamic:
            _exit_error(
                "session on_enter cannot have both --on-enter-action "
                "and --on-enter-desc"
            )
        if exit_static and exit_dynamic:
            _exit_error(
                "session on_exit cannot have both --on-exit-action and --on-exit-desc"
            )
        if not (enter_static or enter_dynamic or exit_static or exit_dynamic):
            _exit_error(
                "direction=session requires at least one of "
                "--on-enter-action / --on-enter-desc / --on-exit-action / --on-exit-desc"
            )

    if duration_ratio is not None and duration_seconds is None:
        _exit_error("--duration-ratio requires --duration-seconds")

    if duration_seconds is not None and (duration_seconds < 1 or duration_seconds > 86400):
        _exit_error("--duration-seconds out of range [1, 86400]")
    if duration_ratio is not None and (duration_ratio <= 0 or duration_ratio > 1.0):
        _exit_error("--duration-ratio must be in (0, 1]")

    # ---- 3. parse JSON actions ----
    actions = _parse_actions(actions_raw, "--action") if actions_raw else []
    on_enter_actions = (
        _parse_actions(on_enter_actions_raw, "--on-enter-action")
        if on_enter_actions_raw
        else []
    )
    on_exit_actions = (
        _parse_actions(on_exit_actions_raw, "--on-exit-action")
        if on_exit_actions_raw
        else []
    )

    # ---- 4. payload ----
    payload = {
        "name": name,
        "task_id": task_id,
        # mode 与 direction 并存: 表里 mode 是 NOT NULL, 而它表达不了 exit ——
        # 存一个自洽的占位值, 真实语义由 direction 承担。
        "mode": _DIRECTION_TO_MODE[direction],
        "direction": direction,
        "lifecycle": lifecycle_value,
        # iot rule 的 condition 是占位: 设备列表留空、query 由服务端按谓词渲染。
        "condition": {
            "perceive_device_ids": [] if iot_given else list(perceive_devices),
            "query": "" if iot_given else query_text,
        },
        "actions": actions,
        "action_descriptions": list(action_descs),
        "on_enter_actions": on_enter_actions,
        "on_enter_desc": on_enter_desc,
        "on_exit_actions": on_exit_actions,
        "on_exit_desc": on_exit_desc,
        "on_target_desc": on_target_desc,
        "terminate_when": terminate_when,
    }
    if iot_given:
        # iot rule 由 CLI 直接构造 condition_dnf —— 它没有旧字段要反推, DNF 就是它的
        # 原始形态。omni rule 保持现状传 condition, 由服务端反推: 让 CLI 也构造的话,
        # 「condition → DNF」会有第二份实现, 而 CLI 与 backend 跨进程没法共用函数。
        payload["condition_dnf"] = _build_iot_dnf(iot_did, iot_iid, iot_op, iot_value)
    if exit_debounce_seconds is not None:
        payload["exit_debounce_seconds"] = exit_debounce_seconds
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
        if duration_ratio is not None:
            payload["duration_ratio"] = duration_ratio

    data = api_post(API_PREFIX, payload)
    print_result(data, pretty)


@rule_group.command("update")
@click.argument("rule_id")
@click.option("--name", default=None, help="新规则名称")
@click.option("--condition", "query_text", default=None, help="新触发条件")
@iot_condition_options
@click.option(
    "--source",
    "perceive_devices",
    multiple=True,
    help="替换感知源列表（可重复，全量替换）",
)
@click.option(
    "--mode",
    "mode_value",
    type=click.Choice(["event", "state"]),
    default=None,
    help="旧入口，只能表达 enter / session；新代码用 --direction",
)
@click.option(
    "--direction",
    "direction_value",
    type=click.Choice(["enter", "exit", "session"]),
    default=None,
    help="变更方向：enter / exit / session",
)
@click.option(
    "--lifecycle",
    "lifecycle_value",
    type=click.Choice(["permanent", "temporary"]),
    default=None,
    help="变更生命周期",
)
@click.option("--terminate-when", "terminate_when", default=None)
@click.option(
    "--action",
    "actions_raw",
    multiple=True,
    help="替换 enter / exit 方向的设备直控 actions（可重复，全量替换；RuleAction JSON）",
)
@click.option(
    "--action-desc",
    "action_descs",
    multiple=True,
    help="替换 enter / exit 方向的 Agent 回调描述（可重复，全量替换）",
)
@click.option(
    "--on-enter-action",
    "on_enter_actions_raw",
    multiple=True,
    help="替换 session 方向的 on_enter 设备直控 actions（可重复，全量替换）",
)
@click.option("--on-enter-desc", "on_enter_desc", default=None)
@click.option(
    "--on-exit-action",
    "on_exit_actions_raw",
    multiple=True,
    help="替换 session 方向的 on_exit 设备直控 actions（可重复，全量替换）",
)
@click.option("--on-exit-desc", "on_exit_desc", default=None)
@click.option("--on-target-desc", "on_target_desc", default=None)
@click.option(
    "--exit-debounce-seconds",
    "exit_debounce_seconds",
    type=int,
    default=None,
)
@click.option(
    "--duration-seconds",
    "duration_seconds",
    type=int,
    default=None,
    help="条件需持续该时长才算成立（秒）。三个方向都生效（语义见 rule create help）",
)
@click.option(
    "--duration-ratio",
    "duration_ratio",
    type=float,
    default=None,
    help="窗口内 True 比例阈值（0,1]",
)
@click.option(
    "--clear",
    "clear_fields",
    multiple=True,
    type=click.Choice(
        [
            "actions",
            "action_descriptions",
            "on_enter_actions",
            "on_enter_desc",
            "on_exit_actions",
            "on_exit_desc",
            "on_target_desc",
            "terminate_when",
            "duration_seconds",
        ]
    ),
    help=(
        "把指定字段重置为空（list → []，str/int → null）；可重复。"
        "用于方向切换等需要显式清空场景，例如 enter→session 时 --clear actions"
    ),
)
@click.option("--pretty", is_flag=True)
def rule_update(
    rule_id,
    name,
    query_text,
    iot_did,
    iot_iid,
    iot_op,
    iot_value,
    perceive_devices,
    mode_value,
    direction_value,
    lifecycle_value,
    terminate_when,
    actions_raw,
    action_descs,
    on_enter_actions_raw,
    on_enter_desc,
    on_exit_actions_raw,
    on_exit_desc,
    on_target_desc,
    exit_debounce_seconds,
    duration_seconds,
    duration_ratio,
    clear_fields,
    pretty,
):
    """部分更新规则（仅传入字段会被替换；多值字段整体替换）。

    执行方式由动作 flag 推断（--action 走设备直控，--action-desc 走 Agent）；
    完整矩阵校验由 backend 在合并字段后执行。
    """
    from miloco_cli.client import api_patch

    if actions_raw and action_descs:
        _exit_error("--action and --action-desc are mutually exclusive")

    if duration_seconds is not None and (duration_seconds < 1 or duration_seconds > 86400):
        _exit_error(
            "--duration-seconds out of range [1, 86400]; "
            "use `--clear duration_seconds` (update only) to disable"
        )
    if duration_ratio is not None and (duration_ratio <= 0 or duration_ratio > 1.0):
        _exit_error("--duration-ratio must be in (0, 1]")

    iot_given = _iot_args_given(iot_did, iot_iid, iot_op, iot_value)
    _reject_mixed_condition_args(query_text, perceive_devices, iot_given)

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if iot_given:
        payload["condition_dnf"] = _build_iot_dnf(iot_did, iot_iid, iot_op, iot_value)
    if mode_value is not None or direction_value is not None:
        # 两个字段要一起改: mode 列 NOT NULL 且表达不了 exit, 只改一个会留下
        # 「mode=state 而 direction=exit」这种自相矛盾的行。
        direction = _resolve_direction(direction_value, mode_value)
        payload["direction"] = direction
        payload["mode"] = _DIRECTION_TO_MODE[direction]
    if lifecycle_value is not None:
        payload["lifecycle"] = lifecycle_value
    if terminate_when is not None:
        payload["terminate_when"] = terminate_when

    if perceive_devices or query_text is not None:
        condition: dict = {}
        if perceive_devices:
            condition["perceive_device_ids"] = list(perceive_devices)
        if query_text is not None:
            condition["query"] = query_text
        payload["condition"] = condition

    if actions_raw:
        payload["actions"] = _parse_actions(actions_raw, "--action")
    if action_descs:
        payload["action_descriptions"] = list(action_descs)
    if on_enter_actions_raw:
        payload["on_enter_actions"] = _parse_actions(
            on_enter_actions_raw, "--on-enter-action"
        )
    if on_enter_desc is not None:
        payload["on_enter_desc"] = on_enter_desc
    if on_exit_actions_raw:
        payload["on_exit_actions"] = _parse_actions(
            on_exit_actions_raw, "--on-exit-action"
        )
    if on_exit_desc is not None:
        payload["on_exit_desc"] = on_exit_desc
    if on_target_desc is not None:
        payload["on_target_desc"] = on_target_desc

    if exit_debounce_seconds is not None:
        payload["exit_debounce_seconds"] = exit_debounce_seconds
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if duration_ratio is not None:
        payload["duration_ratio"] = duration_ratio

    # 清空指定字段：在显式赋值之后处理，发现冲突直接报错（避免歧义）。
    _NULL_CLEAR_FIELDS = {
        "on_enter_desc",
        "on_exit_desc",
        "on_target_desc",
        "terminate_when",
        "duration_seconds",
    }
    for field in clear_fields:
        if field in payload:
            _exit_error(
                f"--clear {field} conflicts with explicit value for the same field"
            )
        payload[field] = None if field in _NULL_CLEAR_FIELDS else []

    if not payload:
        _exit_error("no fields to update")

    data = api_patch(f"{API_PREFIX}/{rule_id}", payload)
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@rule_group.command("enable")
@click.argument("rule_id")
@click.option("--pretty", is_flag=True)
def rule_enable(rule_id, pretty):
    """启用规则。"""
    from miloco_cli.client import api_patch

    data = api_patch(f"{API_PREFIX}/{rule_id}", {"enabled": True})
    print_result(data, pretty)


@rule_group.command("disable")
@click.argument("rule_id")
@click.option("--pretty", is_flag=True)
def rule_disable(rule_id, pretty):
    """禁用规则。"""
    from miloco_cli.client import api_patch

    data = api_patch(f"{API_PREFIX}/{rule_id}", {"enabled": False})
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@rule_group.command("delete")
@click.argument("rule_id")
@click.option("--pretty", is_flag=True)
def rule_delete(rule_id, pretty):
    """删除规则。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"{API_PREFIX}/{rule_id}")
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# trigger
# ---------------------------------------------------------------------------


@rule_group.command("trigger")
@click.argument("rule_id")
@click.option("--pretty", is_flag=True)
@click.option("--context", "context",help="规则触发额外的上下文信息")
def rule_trigger(rule_id, context, pretty):
    """主动触发规则执行。"""
    from miloco_cli.client import api_post
    body = {}
    if context:
        body["context"] = context
    data = api_post(f"{API_PREFIX}/{rule_id}/trigger", body or None)
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@rule_group.command("logs")
@click.option("--rule", "rule_id", default=None, help="过滤指定规则 ID")
@click.option("--limit", default=None, type=int, help="[仅供调试] 返回日志条数")
@click.option(
    "--since",
    default=None,
    help="[仅供调试] 相对时间窗口，不读写 cursor 文件。支持 h/m/d 单位，如 30m / 1h / 7d。",
)
@click.option(
    "--kind",
    default=None,
    type=click.Choice(["RULE_TRIGGER_SUCCESS", "RULE_TRIGGER_FAILURE"]),
    help="按 ExecutionLog kind 过滤",
)
@click.option("--pretty", is_flag=True)
def rule_logs(rule_id, limit, since, kind, pretty):
    """查询规则执行日志。

    \b
    Agent 用法（无参数）：自动从上次 cursor 处增量拉取，查完后更新 cursor，保证不重复。
      miloco-cli rule logs
      miloco-cli rule logs --rule <id>   # 同上，限定某条规则

    \b
    调试用法：--since / --limit 不读写 cursor 文件。
      miloco-cli rule logs --since 1h
      miloco-cli rule logs --since 1h --limit 20
      miloco-cli rule logs --limit 5
    """
    from datetime import datetime, timezone

    from miloco_cli.client import api_get

    if rule_id:
        path = f"{API_PREFIX}/{rule_id}/logs"
    else:
        path = f"{API_PREFIX}/logs"

    debug_mode = bool(since or limit)

    if debug_mode:
        # 调试模式：单次请求，不读写 cursor
        params: dict = {}
        if since:
            params["since"] = since
        if limit:
            params["limit"] = limit
        if kind:
            params["kind"] = kind
        data = api_get(path, params or None)
        print_result(data, pretty)
        return

    # Agent 模式：从上次 cursor 处开始增量拉取所有日志（循环翻页），最后把
    # cursor 推到本轮最新一条的 timestamp。
    cursor_ms = _load_rule_cursor()
    after_iso = (
        datetime.fromtimestamp(int(cursor_ms) / 1000, tz=timezone.utc).isoformat()
        if cursor_ms is not None
        else None
    )
    page_limit = 500  # backend Query 上限
    aggregated: list = []
    last_response: dict = {"code": 0, "data": {"rule_logs": [], "total_items": 0}}
    before_ts: int | None = None

    while True:
        page_params: dict = {"limit": page_limit}
        if after_iso is not None:
            page_params["after"] = after_iso
        if before_ts is not None:
            page_params["before"] = datetime.fromtimestamp(
                before_ts / 1000, tz=timezone.utc
            ).isoformat()
        if kind:
            page_params["kind"] = kind

        page = api_get(path, page_params)
        last_response = page
        if page.get("code") != 0:
            # 后端报错 → 透传给用户，cursor 不动（避免吞掉错误后跳过日志）
            print_result(page, pretty)
            return

        page_logs = page.get("data", {}).get("rule_logs", [])
        aggregated.extend(page_logs)
        if len(page_logs) < page_limit:
            break
        # 满页 → 还有更老的；下一轮把上限收紧到本批最旧那条之前
        oldest_ts = page_logs[-1].get("timestamp")
        if oldest_ts is None:
            # backend 应该总会带 timestamp；缺字段就停下，避免死循环
            break
        before_ts = oldest_ts

    if aggregated:
        latest_ts_ms = aggregated[0].get("timestamp")
        if latest_ts_ms:
            _save_rule_cursor(latest_ts_ms)

    # 把多页结果拼成一份返回，total_items 取实际累积数量
    merged = {
        "code": last_response.get("code", 0),
        "message": f"Retrieved {len(aggregated)} logs",
        "data": {"rule_logs": aggregated, "total_items": len(aggregated)},
    }
    print_result(merged, pretty)


# ---------------------------------------------------------------------------
# logs-cleanup
# ---------------------------------------------------------------------------


@rule_group.command("logs-cleanup")
@click.option("--keep-days", default=7, show_default=True, type=int, help="保留最近 N 天的日志")
@click.option("--pretty", is_flag=True)
def rule_logs_cleanup(keep_days, pretty):
    """清理规则日志，删除超过 N 天的记录。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"{API_PREFIX}/logs", params={"keep_days": keep_days})
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_DIRECTION_TO_MODE = {
    "enter": "event",
    "exit": "event",
    "session": "state",
}
_MODE_TO_DIRECTION = {"event": "enter", "state": "session"}


def _resolve_direction(direction_value: str | None, mode_value: str | None) -> str:
    """把 --direction / --mode 收敛成一个方向。

    --mode 只能表达 enter 与 session，exit 表达不了，所以 --direction 是正路。
    两个都传时必须自洽 —— 静默让一个赢，用户会以为另一个也生效了。
    """
    if direction_value is None:
        return _MODE_TO_DIRECTION[mode_value or "event"]
    expected = _DIRECTION_TO_MODE[direction_value]
    if mode_value is not None and mode_value != expected:
        _exit_error(
            f"--direction {direction_value} 对应 --mode {expected}，"
            f"不能同时传 --mode {mode_value}"
        )
    return direction_value


def _parse_actions(raw_actions: tuple[str, ...], flag_name: str = "--action") -> list[dict]:
    """Parse and validate multiple action JSON strings from the given flag.

    ``flag_name`` is the CLI flag the caller used (``--action`` /
    ``--on-enter-action`` / ``--on-exit-action``); error messages mirror it
    verbatim so agents see guidance tied to the exact flag they invoked.

    ``idempotent: false`` actions must declare ``cooldown_minutes`` to
    avoid spamming notifications; ``iid: scene`` must be non-idempotent.
    """
    parsed: list[dict] = []
    for raw in raw_actions:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            _exit_error(f"invalid {flag_name} JSON: {e}")
        if isinstance(obj, list):
            _exit_error(
                f"{flag_name} expects a single JSON object per invocation, not a JSON array. "
                f"To pass multiple actions, repeat the flag: "
                f"{flag_name} '{{...}}' {flag_name} '{{...}}'"
            )
        if not isinstance(obj, dict):
            _exit_error(f"{flag_name} must be a JSON object, got: {raw}")
        parsed.append(obj)
    _validate_actions(parsed, flag_name)
    return parsed


def _validate_actions(actions: list[dict], flag_name: str = "--action") -> None:
    for i, a in enumerate(actions):
        if a.get("iid") == SCENE_IID and a.get("idempotent") is not False:
            _exit_error(
                f"{flag_name}[{i}]: iid={SCENE_IID} requires idempotent=false"
            )
        if a.get("idempotent") is False and a.get("cooldown_minutes") is None:
            _exit_error(
                f"{flag_name}[{i}]: idempotent=false requires cooldown_minutes"
            )
        # 冷却是场景唯一的去重手段，填 0 等于每次 fire 都真触发一次。
        # 只对数值比较：agent 可能把数字写成 "5"，直接比会 TypeError traceback，
        # 破坏 _exit_error 的干净报错；字符串形态交给后端 pydantic 收敛或 400。
        cooldown = a.get("cooldown_minutes")
        if (
            a.get("iid") == SCENE_IID
            and isinstance(cooldown, (int, float))
            and cooldown < 1
        ):
            _exit_error(
                f"{flag_name}[{i}]: iid={SCENE_IID} requires cooldown_minutes >= 1"
            )


def _exit_error(msg: str):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)
