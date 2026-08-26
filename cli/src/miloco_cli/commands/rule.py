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


@click.group("rule")
def rule_group():
    """规则操作：列表 / 创建 / 更新 / 启用 / 禁用 / 删除 / 触发 / 日志 / 日志清理。"""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@rule_group.command("list")
@click.option("--enabled-only", is_flag=True, help="仅显示已启用的规则")
@click.option("--pretty", is_flag=True)
def rule_list(enabled_only, pretty):
    """列出所有规则。"""
    from miloco_cli.client import api_get

    params = {}
    if enabled_only:
        params["enabled_only"] = "true"
    data = api_get(API_PREFIX, params or None)
    print_result(data, pretty)


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
        "不填 → 所有感知设备都跑该 rule（OR 聚合）；"
        "填 → 只在这些 did 上跑。"
        "用户未明确指定设备时优先不填。"
    ),
)
@click.option("--condition", "query_text", required=True, help="触发条件描述（自然语言）")
@click.option(
    "--mode",
    "mode_value",
    type=click.Choice(["event", "state"]),
    default="event",
    show_default=True,
    help="响应模式：event 单点触发；state 进入/退出配对",
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
        "event 模式设备直控动作 JSON（可重复）。\n"
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
    help="event 模式 Agent 回调描述（可重复）",
)
@click.option(
    "--on-enter-action",
    "on_enter_actions_raw",
    multiple=True,
    help="state on_enter 设备直控动作 JSON（可重复，格式同 --action）",
)
@click.option(
    "--on-enter-desc",
    "on_enter_desc",
    default=None,
    help="state on_enter Agent 回调提示文本",
)
@click.option(
    "--on-exit-action",
    "on_exit_actions_raw",
    multiple=True,
    help="state on_exit 设备直控动作 JSON（可重复，格式同 --action）",
)
@click.option(
    "--on-exit-desc",
    "on_exit_desc",
    default=None,
    help="state on_exit Agent 回调提示文本",
)
@click.option(
    "--on-target-desc",
    "on_target_desc",
    default=None,
    help=(
        "state on_target Agent 回调提示文本（duration record 累计达标瞬间触发）。"
        "仅在 task 配 duration record + target_minutes 时有效。"
    ),
)
@click.option(
    "--exit-debounce-seconds",
    "exit_debounce_seconds",
    type=int,
    default=None,
    help="state mode EXIT 防抖（秒），默认 60",
)
@click.option(
    "--duration-seconds",
    "duration_seconds",
    type=int,
    default=None,
    help=(
        "累计窗口（秒）。不填=立即 fire（现状）。EVENT：达标 fire 后清窗口周期 fire；"
        "STATE：达标 fire on_enter 一次，STILL_IN 不重复，EXITED 走 exit_debounce"
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
    mode_value,
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

    mode/action 组合 example 与 condition 写法见 miloco-create-task SKILL。
    """
    from miloco_cli.client import api_post

    # ---- 1. lifecycle ----
    if lifecycle_value == "temporary" and not terminate_when:
        _exit_error("lifecycle=temporary requires --terminate-when")

    # ---- 2. mode x action 矩阵 ----
    if mode_value == "event":
        if (
            on_enter_actions_raw
            or on_enter_desc
            or on_exit_actions_raw
            or on_exit_desc
            or on_target_desc
        ):
            _exit_error(
                "event mode must not set --on-enter-* / --on-exit-* / --on-target-desc"
            )
        if not actions_raw and not action_descs:
            _exit_error(
                "event mode requires --action or --action-desc"
            )
        if actions_raw and action_descs:
            _exit_error(
                "event mode: --action and --action-desc are mutually exclusive"
            )
    else:  # state mode
        if actions_raw or action_descs:
            _exit_error("state mode must not set --action / --action-desc")

        enter_static = bool(on_enter_actions_raw)
        enter_dynamic = bool(on_enter_desc)
        exit_static = bool(on_exit_actions_raw)
        exit_dynamic = bool(on_exit_desc)

        if enter_static and enter_dynamic:
            _exit_error(
                "state on_enter cannot have both --on-enter-action and --on-enter-desc"
            )
        if exit_static and exit_dynamic:
            _exit_error(
                "state on_exit cannot have both --on-exit-action and --on-exit-desc"
            )
        if not (enter_static or enter_dynamic or exit_static or exit_dynamic):
            _exit_error(
                "state mode requires at least one of "
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
        "mode": mode_value,
        "lifecycle": lifecycle_value,
        "condition": {
            "perceive_device_ids": list(perceive_devices),
            "query": query_text,
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
    if exit_debounce_seconds is not None:
        payload["exit_debounce_seconds"] = exit_debounce_seconds
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
        if duration_ratio is not None:
            payload["duration_ratio"] = duration_ratio

    data = api_post(API_PREFIX, payload)
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# update (partial)
# ---------------------------------------------------------------------------


@rule_group.command("update")
@click.argument("rule_id")
@click.option("--name", default=None, help="新规则名称")
@click.option("--task-id", "task_id", default=None, help="新 task_id")
@click.option("--condition", "query_text", default=None, help="新触发条件")
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
    help="变更响应模式",
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
    help="替换 event 模式设备直控 actions（可重复，全量替换；RuleAction JSON）",
)
@click.option(
    "--action-desc",
    "action_descs",
    multiple=True,
    help="替换 event 模式 Agent 回调描述（可重复，全量替换）",
)
@click.option(
    "--on-enter-action",
    "on_enter_actions_raw",
    multiple=True,
    help="替换 state on_enter 设备直控 actions（可重复，全量替换）",
)
@click.option("--on-enter-desc", "on_enter_desc", default=None)
@click.option(
    "--on-exit-action",
    "on_exit_actions_raw",
    multiple=True,
    help="替换 state on_exit 设备直控 actions（可重复，全量替换）",
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
    help="累计窗口（秒）。EVENT / STATE 都生效（语义见 rule create help）",
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
        "用于 mode 切换等需要显式清空场景，例如 event→state 时 --clear actions"
    ),
)
@click.option("--pretty", is_flag=True)
def rule_update(
    rule_id,
    name,
    task_id,
    query_text,
    perceive_devices,
    mode_value,
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
        _exit_error("event mode: --action and --action-desc are mutually exclusive")

    if duration_seconds is not None and (duration_seconds < 1 or duration_seconds > 86400):
        _exit_error(
            "--duration-seconds out of range [1, 86400]; "
            "use `--clear duration_seconds` (update only) to disable"
        )
    if duration_ratio is not None and (duration_ratio <= 0 or duration_ratio > 1.0):
        _exit_error("--duration-ratio must be in (0, 1]")

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if task_id is not None:
        payload["task_id"] = task_id
    if mode_value is not None:
        payload["mode"] = mode_value
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


def _exit_error(msg: str):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)
