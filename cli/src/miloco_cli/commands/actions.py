"""actions 命令组:查 agent 控制设备 / 播 TTS / 触发场景的持久审计流水。

后端把每次 ``control_device`` / ``trigger_scene`` 落一行 action_ledger(见
backend observability.db)。本命令拉 ``/api/actions`` 并按 ``device list`` 同款
TSV 风格渲染(顶部 ``#`` 注释头行 + 竖线分隔),时间戳按部署时区人类可读。
"""

import re
import sys
from datetime import datetime, timedelta

import click

# 结果码释义已在后端 result_msg 里解好,这里只做展示;value_json 截断到 ~60 字符。
_VALUE_MAX = 60


def _parse_since(since: str) -> int:
    """``24h`` / ``7d`` / ``90m`` 相对量 或 ISO 8601 绝对时刻 → epoch ms。

    相对量按 ``now - Δ`` 解;ISO 直接解析(naive 视为部署时区)。解析失败 → SystemExit(1)。
    """
    m = re.fullmatch(r"(\d+)([dhm])", since.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n),
                 "m": timedelta(minutes=n)}[unit]
        return int((datetime.now().astimezone() - delta).timestamp() * 1000)

    # ISO 8601 绝对时刻
    from miloco_cli.deploy_tz import deploy_timezone

    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        click.echo(
            f"# error: 无法解析 --since {since!r}(用 24h / 7d / 90m 或 ISO 8601）",
            err=True,
        )
        sys.exit(1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=deploy_timezone())
    return int(dt.timestamp() * 1000)


def _fmt_ts(ms: int) -> str:
    """epoch ms → 部署时区人类可读 ``YYYY-MM-DD HH:MM:SS``。"""
    from miloco_cli.deploy_tz import deploy_timezone

    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=deploy_timezone()).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (ValueError, OSError, TypeError):
        return str(ms)


def _truncate(value_json: str | None) -> str:
    if not value_json:
        return "-"
    if len(value_json) > _VALUE_MAX:
        return value_json[: _VALUE_MAX - 1] + "…"
    return value_json


def _render_action_row(a: dict) -> str:
    """ts|action_type|did|device_name|room|iid|success|reason|value 竖线分隔。

    与 device list 同款:每字段转义 ``|``(房间名 / 别名用户可改,含 ``|`` 不是不可能)。
    """
    from miloco_cli.catalog import _escape

    reason = a.get("result_msg") or a.get("error") or "ok"
    return "|".join([
        _fmt_ts(a.get("timestamp") or 0),
        _escape(a.get("action_type")),
        _escape(a.get("did")),
        _escape(a.get("device_name")),
        _escape(a.get("room")),
        _escape(a.get("iid")),
        "ok" if a.get("success") else "fail",
        _escape(reason),
        _escape(_truncate(a.get("value_json"))),
    ])


@click.group("actions")
def actions_group():
    """动作审计:查 agent 控制设备 / 播 TTS / 触发场景的历史流水。"""


@actions_group.command("list")
@click.option("--since", default=None, help="起始时间:24h / 7d / 90m 相对量,或 ISO 8601")
@click.option("--did", default=None, help="按设备 did 过滤")
@click.option("--failed-only", is_flag=True, default=False, help="只看失败项")
@click.option("--limit", type=int, default=50, help="返回条数(默认 50,上限 500)")
def actions_list(since, did, failed_only, limit):
    """列出动作审计流水(新到旧)。

    顶部 TSV 头行,后跟:ts|action_type|did|device_name|room|iid|success|reason|value
    """
    from miloco_cli.client import api_get

    params: list[tuple[str, str | int]] = []
    if since:
        params.append(("since_ms", _parse_since(since)))
    if did:
        params.append(("did", did))
    if failed_only:
        params.append(("failed_only", 1))
    if limit:
        params.append(("limit", limit))

    resp = api_get("/api/actions", params=params or None)
    rows = resp if isinstance(resp, list) else resp.get("data", [])

    click.echo("# ts|action_type|did|device_name|room|iid|success|reason|value")
    for a in rows:
        click.echo(_render_action_row(a))
