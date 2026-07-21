"""cron 命令组: add / remove / enable / disable / list / get.

对应 backend /api/crons 全套 endpoint。CLI 只透传参数 + 友好打印。
仅装 internal cron (backend 强制 dispatch_owner='internal'); external 只通过
迁移脚本 / P2 系统 job 特殊路径入库, 不走这里。

at 类用 ``--at-iso`` 传带偏移的 ISO8601 字符串 (与 ``time-compute`` 输出、
record ``expires_at`` 同格式); backend router 边界统一解析 + 校验 (naive
拒收 / past 拒收 / 10y 上限), CLI 只透传字符串, 不做本地校验。
"""

from __future__ import annotations

import click

from miloco_cli.output import print_result

API_PREFIX = "/api/crons"


@click.group("cron")
def cron_group():
    """cron 操作: 添加 / 删除 / 启用 / 禁用 / 列表 / 详情。"""


@cron_group.command("add")
@click.option("--name", required=True, help="用户可见字符串 (如 [task_id] 提醒)")
@click.option(
    "--kind",
    type=click.Choice(["cron", "at", "every"]),
    required=True,
)
@click.option("--task-id", "task_id", default=None, help="关联 task_id (可选)")
@click.option("--message", required=True, help="触发时投递给 agent 的消息")
@click.option("--cron-expr", "cron_expr", default=None, help="kind=cron 必填")
@click.option(
    "--at-iso",
    "at_iso",
    default=None,
    help="kind=at 必填 (带时区偏移的 ISO8601, e.g. 2026-06-10T09:00:00+08:00; 与 time-compute 输出、record.expires_at 同格式)",
)
@click.option(
    "--every-ms",
    "every_ms",
    type=int,
    default=None,
    help="kind=every 必填 (>=60000)",
)
@click.option(
    "--anchor-ms",
    "anchor_ms",
    type=int,
    default=None,
    help="kind=every 可选起点 (Unix ms)",
)
@click.option("--tz", default=None, help="IANA 时区名 (kind=every 禁填)")
@click.option(
    "--light-context",
    "light_context",
    is_flag=True,
    default=False,
    help="启用 lightweight bootstrap (省 tool 集, 默认 false)",
)
@click.option(
    "--max-delay",
    "max_delay_seconds",
    type=int,
    default=None,
    help="misfire 兜底秒数; 0 仅 kind=at (termination 无限补跑)",
)
@click.option("--pretty", is_flag=True)
def cron_add(
    name,
    kind,
    task_id,
    message,
    cron_expr,
    at_iso,
    every_ms,
    anchor_ms,
    tz,
    light_context,
    max_delay_seconds,
    pretty,
):
    """新建 internal cron。backend 强制 dispatch_owner='internal'。"""
    from miloco_cli.client import api_post

    body: dict = {"name": name, "kind": kind, "message": message}
    if task_id is not None:
        body["task_id"] = task_id
    if cron_expr is not None:
        body["cron_expr"] = cron_expr
    if at_iso is not None:
        body["at_iso"] = at_iso
    if every_ms is not None:
        body["every_ms"] = every_ms
    if anchor_ms is not None:
        body["anchor_ms"] = anchor_ms
    if tz is not None:
        body["tz"] = tz
    if light_context:
        body["light_context"] = True
    if max_delay_seconds is not None:
        body["max_delay_seconds"] = max_delay_seconds

    data = api_post(API_PREFIX, body)
    print_result(data, pretty)


@cron_group.command("remove")
@click.argument("cron_id")
@click.option("--pretty", is_flag=True)
def cron_remove(cron_id, pretty):
    """删除 cron (internal 走 backend 内存清理; external 产 agent_pending)。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"{API_PREFIX}/{cron_id}")
    print_result(data, pretty)


@cron_group.command("enable")
@click.argument("cron_id")
@click.option("--pretty", is_flag=True)
def cron_enable(cron_id, pretty):
    """启用 cron。"""
    from miloco_cli.client import api_post

    data = api_post(f"{API_PREFIX}/{cron_id}/enable", {})
    print_result(data, pretty)


@cron_group.command("disable")
@click.argument("cron_id")
@click.option("--pretty", is_flag=True)
def cron_disable(cron_id, pretty):
    """禁用 cron。"""
    from miloco_cli.client import api_post

    data = api_post(f"{API_PREFIX}/{cron_id}/disable", {})
    print_result(data, pretty)


@cron_group.command("list")
@click.option("--task-id", "task_id", default=None, help="按 task_id 过滤")
@click.option("--orphan", is_flag=True, help="仅列 task_id=NULL 的独立 cron")
@click.option(
    "--dispatch-owner",
    "dispatch_owner",
    type=click.Choice(["internal", "external"]),
    default=None,
)
@click.option("--pretty", is_flag=True)
def cron_list(task_id, orphan, dispatch_owner, pretty):
    """列出 cron。"""
    from miloco_cli.client import api_get

    params: dict = {}
    if task_id is not None:
        params["task_id"] = task_id
    if orphan:
        params["orphan"] = "true"
    if dispatch_owner is not None:
        params["dispatch_owner"] = dispatch_owner

    data = api_get(API_PREFIX, params or None)
    print_result(data, pretty)


@cron_group.command("get")
@click.argument("cron_id")
@click.option("--pretty", is_flag=True)
def cron_get(cron_id, pretty):
    """查看单条 cron 详情。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/{cron_id}")
    print_result(data, pretty)
