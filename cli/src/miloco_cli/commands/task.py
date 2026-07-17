"""task 命令组: create / record / update / list / get / disable / enable / delete (v2)。

对应 backend `/api/tasks` 全套 endpoint + ``/api/tasks/{id}/record`` 系列。
v2 起 ``task link`` 子命令已移除: rule 关联由 ``rule create`` 完成 (rule.task_id
FK CASCADE), cron 关联由 ``cron add`` (阶段 3) 完成。
"""

import json
import sys
from typing import Any

import click

from miloco_cli.output import print_result

API_PREFIX = "/api/tasks"


def _strip_quotes(s: str) -> str:
    """剥一层匹配的首尾引号。

    OpenClaw exec tool 把 command 当 argv 直接传，shell 引号不被解析；
    agent 习惯给 ISO 时间戳加引号包裹（`--at "2026-06-15T10:44:05+08:00"`），
    CLI 收到的就是带字面引号的字符串。这里做兜底剥引号，让 agent 加不加
    引号都能跑通。
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_json(value: str, label: str) -> Any:
    """解析 CLI 传入的 JSON 字符串，失败时友好报错。"""
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        print(
            json.dumps(
                {"error": f"{label} 不是合法 JSON: {e.msg} at pos {e.pos}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)


@click.group("task")
def task_group():
    """任务操作:创建 / 关联 / 更新 / 列表 / 详情 / 启停 / 删除。"""


@task_group.command("create")
@click.option(
    "--task-id", "task_id", required=True, help="snake_case task_id, [a-z0-9_]{1,32}"
)
@click.option(
    "--description", required=True, help="任务整体自然语言摘要 (≤200 字符)"
)
@click.option("--pretty", is_flag=True)
def task_create(task_id, description, pretty):
    """建 task 占位行 (v2)。

    rule / cron / record 关联挂载由后续命令完成:

    - ``miloco-cli rule create --task-id X ...``  写 rule.task_id FK
    - ``miloco-cli cron add --task-id X ...``     阶段 3 装配 internal cron
    - ``miloco-cli task record init X ...``       挂 record
    """
    from miloco_cli.client import api_post

    body = {"task_id": task_id, "description": description}
    data = api_post(API_PREFIX, body)
    print_result(data, pretty)


@task_group.command("update")
@click.argument("task_id")
@click.option("--description", required=True, help="新 description (≤200)")
@click.option("--pretty", is_flag=True)
def task_update(task_id, description, pretty):
    """改 description。"""
    from miloco_cli.client import api_patch

    data = api_patch(f"{API_PREFIX}/{task_id}", {"description": description})
    print_result(data, pretty)


@task_group.command("list")
@click.option("--pretty", is_flag=True)
def task_list(pretty):
    """列所有活跃 task 的聚合视图(含 rule_briefs)。"""
    from miloco_cli.client import api_get

    data = api_get(API_PREFIX)
    print_result(data, pretty)


@task_group.command("summary")
@click.option(
    "--window",
    type=click.Choice(["day", "all"]),
    default="day",
    help="record 派生窗口,默认 day。progress 永远 snapshot 无视 window;duration/event 按 window。",
)
@click.option("--pretty", is_flag=True)
def task_summary(window, pretty):
    """一次性出所有 task 当前状态(基础 + rule_briefs + links + record 派生)。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/summary?window={window}")
    print_result(data, pretty)


@task_group.command("get")
@click.argument("task_id")
@click.option("--pretty", is_flag=True)
def task_get(task_id, pretty):
    """单 task 完整视图。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/{task_id}")
    print_result(data, pretty)


@task_group.command("disable")
@click.argument("task_id")
@click.option("--pretty", is_flag=True)
def task_disable(task_id, pretty):
    """暂停 task:backend 部分(meta+rules) + 返回 agent_pending。"""
    from miloco_cli.client import api_post

    data = api_post(f"{API_PREFIX}/{task_id}/disable")
    print_result(data, pretty)


@task_group.command("enable")
@click.argument("task_id")
@click.option("--pretty", is_flag=True)
def task_enable(task_id, pretty):
    """恢复 task:对应 disable 反向操作。"""
    from miloco_cli.client import api_post

    data = api_post(f"{API_PREFIX}/{task_id}/enable")
    print_result(data, pretty)


@task_group.command("delete")
@click.argument("task_id")
@click.option(
    "--reason",
    type=click.Choice(["completed", "expired", "abandoned"]),
    default="completed",
    help="terminate 原因；透传到 backend query 参数",
)
@click.option("--pretty", is_flag=True)
def task_delete(task_id, reason, pretty):
    """删 task:backend 部分先 commit + 返回 agent_pending。``--reason`` 走 query string。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"{API_PREFIX}/{task_id}", params={"reason": reason})
    print_result(data, pretty)


# ── task record 子组（spec §5.1） ─────────────────────────────────────────────


@task_group.group("record")
def record_group():
    """任务记录（进度/累计时长/事件计数）操作。"""


@record_group.command("init")
@click.argument("task_id")
@click.option(
    "--kind",
    type=click.Choice(["progress", "duration", "event"]),
    required=True,
)
@click.option(
    "--content",
    required=True,
    help='content JSON，按 kind 不同：'
    'progress {target,unit,window:day|week|month|longterm,recurring_pattern,expires_at,...}；'
    'duration {target_minutes,recurring_pattern,expires_at,...}；'
    'event {recurring_pattern,expires_at,...}。'
    'progress/duration 必须明示 recurring_pattern 或 expires_at 之一：'
    '周期 rollover → recurring_pattern={"window":"day|week|month"}；'
    '永久累计 → recurring_pattern={"window":"longterm"}；'
    '限期一次性 → expires_at=<RFC3339>',
)
@click.option("--pretty", is_flag=True)
def record_init(task_id, kind, content, pretty):
    """方案 P 阶段 A'：插主表活跃行。重复 init 返 409。"""
    from miloco_cli.client import api_post

    content_dict = _parse_json(content, "--content")
    if kind in ("progress", "duration"):
        if not content_dict.get("recurring_pattern") and not content_dict.get("expires_at"):
            raise click.ClickException(
                f"{kind} kind 必须明示 recurring_pattern 或 expires_at 之一：\n"
                '  周期 rollover → recurring_pattern={"window":"day|week|month"}\n'
                '  永久累计     → recurring_pattern={"window":"longterm"}\n'
                "  限期一次性   → expires_at=<RFC3339>"
            )
    body = {"kind": kind, "content": content_dict}
    data = api_post(f"{API_PREFIX}/{task_id}/record", body)
    print_result(data, pretty)


@record_group.command("get")
@click.argument("task_id")
@click.option("--pretty", is_flag=True)
def record_get(task_id, pretty):
    """读主表活跃行 + 子表 + derived。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/{task_id}/record")
    print_result(data, pretty)


def _resolve_date_alias(date_str: str) -> str:
    """``yesterday`` / ``today`` 别名解析为 ``YYYY-MM-DD``（本地时区）。

    兜底用——SKILL.md 仍要求 cron message 内显式用 shell ``$(date -d 'yesterday'
    +%Y-%m-%d)`` 算具体值（deterministic、可读），CLI 兜底是怕 agent 偶尔写
    字面量时不至于静默错查空集。
    """
    from datetime import datetime, timedelta

    if date_str == "yesterday":
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == "today":
        return datetime.now().strftime("%Y-%m-%d")
    return date_str


@record_group.command("compute")
@click.argument("task_id")
@click.option(
    "--window",
    type=click.Choice(["all", "day", "week", "month"]),
    default=None,
    help="窗口模式（与 --date 互斥；不传默认 all 当前活跃行）",
)
@click.option(
    "--date",
    help="历史日期 YYYY-MM-DD（按 archived_at 查）；也接 yesterday / today 别名。与 --window 互斥",
)
@click.option(
    "--from",
    "from_",
    help="区间起 YYYY-MM-DD（与 --to 配对，与 --date / --window 互斥）",
)
@click.option(
    "--to",
    help="区间止 YYYY-MM-DD（与 --from 配对，含两端）",
)
@click.option("--pretty", is_flag=True)
def record_compute(task_id, window, date, from_, to, pretty):
    """派生量计算（含历史日期 / 跨窗口 / 区间聚合）。

    四套用法互斥：

    \b
    - 默认（什么都不传）：当前活跃行 derived
    - --window day|week|month：按当前 period 聚合（按 kind 限制）
    - --date YYYY-MM-DD：单日历史归档行 derived（不要同时传 --window）
    - --from X --to Y：区间聚合 derived
    """
    from miloco_cli.client import api_post

    if (from_ is not None) ^ (to is not None):
        print(
            json.dumps(
                {"error": "--from 和 --to 必须成对提供"}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    if date is not None and window is not None:
        print(
            json.dumps(
                {"error": "--date 与 --window 互斥（backend 拒）"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    params: dict[str, str] = {}
    if from_ is not None:
        params["from"] = _resolve_date_alias(from_)
        params["to"] = _resolve_date_alias(to)
    elif date is not None:
        params["date"] = _resolve_date_alias(date)
    elif window is not None:
        params["window"] = window
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_PREFIX}/{task_id}/record/compute"
    if qs:
        url = f"{url}?{qs}"
    data = api_post(url)
    print_result(data, pretty)


@record_group.group("archive")
def archive_group():
    """归档查询（rollover 历史快照 / event 全部 entry 按日聚合）。"""


@archive_group.command("list")
@click.argument("task_id")
@click.option("--pretty", is_flag=True)
def record_archive_list(task_id, pretty):
    """列该 task 全部归档行 + 每日 derived 快照。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/{task_id}/record/archives")
    print_result(data, pretty)


@record_group.command("update")
@click.argument("task_id")
@click.option(
    "--patch",
    required=True,
    help='patch JSON（白名单字段按 kind 不同：progress 5 字段、duration 3 字段、event 2 字段）',
)
@click.option("--pretty", is_flag=True)
def record_update(task_id, patch, pretty):
    """PATCH 活跃 record 业务字段（target / unit / recurring_pattern 等）。"""
    from miloco_cli.client import api_patch

    body = _parse_json(patch, "--patch")
    if not isinstance(body, dict):
        print(
            json.dumps({"error": "--patch 必须是 JSON object"}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
    data = api_patch(f"{API_PREFIX}/{task_id}/record", body)
    print_result(data, pretty)


@record_group.command("progress-inc")
@click.argument("task_id")
@click.option(
    "--delta",
    type=int,
    default=1,
    help="正数累加；负数撤销（current 下穿 0 floor，completed 可回退）",
)
@click.option("--pretty", is_flag=True)
def record_progress_inc(task_id, delta, pretty):
    """progress mutate：自动 cap target + completed flip。"""
    from miloco_cli.client import api_post

    data = api_post(
        f"{API_PREFIX}/{task_id}/record/progress/increment", {"delta": delta}
    )
    print_result(data, pretty)


@record_group.command("event-append")
@click.argument("task_id")
@click.option("--description", required=True)
@click.option("--at", help="ISO8601 时间，省略用 backend now")
@click.option("--pretty", is_flag=True)
def record_event_append(task_id, description, at, pretty):
    """event 子表 INSERT 一行。"""
    from miloco_cli.client import api_post

    body: dict[str, Any] = {"description": description}
    if at:
        body["at"] = _strip_quotes(at)
    data = api_post(f"{API_PREFIX}/{task_id}/record/event/append", body)
    print_result(data, pretty)


@record_group.command("session-start")
@click.argument("task_id")
@click.option("--at", help="ISO8601 时间，省略用 backend now")
@click.option("--pretty", is_flag=True)
def record_session_start(task_id, at, pretty):
    """duration 主表设 active_session_start_at；已 active 返 already_active。"""
    from miloco_cli.client import api_post

    body: dict[str, Any] = {}
    if at:
        body["at"] = _strip_quotes(at)
    data = api_post(f"{API_PREFIX}/{task_id}/record/session/start", body)
    print_result(data, pretty)


@record_group.command("session-end")
@click.argument("task_id")
@click.option("--at", help="ISO8601 时间，省略用 backend now")
@click.option("--pretty", is_flag=True)
def record_session_end(task_id, at, pretty):
    """duration 主表清 active_session_start_at + 子表 INSERT 一行。"""
    from miloco_cli.client import api_post

    body: dict[str, Any] = {}
    if at:
        body["at"] = _strip_quotes(at)
    data = api_post(f"{API_PREFIX}/{task_id}/record/session/end", body)
    print_result(data, pretty)
