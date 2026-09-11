"""perceive 命令组：devices / query / logs。"""

import json

import click

from miloco_cli.output import dump, print_result


def _cursor_file():
    from miloco_cli.config import miloco_home
    return miloco_home() / "perception_cursor.json"


def _load_cursor() -> str | None:
    """读取本地 perception cursor（ISO 8601 字符串）。

    兼容旧格式 {"cursor_ms": <int>}，自动转为 ISO 8601。
    """
    from datetime import datetime, timezone

    cursor_file = _cursor_file()
    if not cursor_file.exists():
        return None
    try:
        obj = json.loads(cursor_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # New format
    cursor = obj.get("cursor")
    if cursor:
        return cursor

    # Legacy format: cursor_ms (int)
    cursor_ms = obj.get("cursor_ms")
    if cursor_ms is not None:
        return datetime.fromtimestamp(
            int(cursor_ms) / 1000, tz=timezone.utc
        ).isoformat()

    return None


def _save_cursor(cursor_iso: str) -> None:
    """原子写入 perception cursor（ISO 8601）。"""
    from miloco_cli.config import atomic_write
    atomic_write(_cursor_file(), {"cursor": cursor_iso})


@click.group("perceive")
def perceive_group():
    """感知操作：设备列表 / 主动查询 / 感知日志。"""


@perceive_group.command("devices")
@click.option("--pretty", is_flag=True)
def perceive_devices(pretty):
    """列出具备感知能力的设备（摄像头等）。"""
    from miloco_cli.client import api_get

    data = api_get("/api/perception/devices")
    print_result(data, pretty)


@perceive_group.command("query")
@click.option(
    "--source",
    "sources",
    multiple=True,
    required=True,
    help="感知源 did（摄像头等感知设备），可重复。例：--source cam_001 --source cam_002",
)
@click.option("--query", "query_text", required=True, help="自然语言问题")
@click.option("--pretty", is_flag=True)
def perceive_query(sources, query_text, pretty):
    """主动多模态感知：指定感知源，回答具体问题。"""
    from miloco_cli.client import api_post

    data = api_post(
        "/api/perception/perceive",
        {"sources": list(sources), "query": query_text},
    )
    print_result(data, pretty)


@perceive_group.command("logs")
@click.option(
    "--since",
    default=None,
    help="[仅供调试] 相对时长，返回最近一段时间的日志，不读写 cursor 文件。"
         "支持 h/m/s/d 单位及组合，如 1h、30m、90s、7d、2h30m。",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="[仅供调试] 返回筛选窗口中最新的 N 条；默认无限制。",
)
@click.option("--jsonl", is_flag=True, help="JSONL 输出格式：每行输出一条 JSONL（时间: 日志JSON）。")
@click.option("--pretty", is_flag=True)
def perceive_logs(since, limit, jsonl, pretty):
    """查询感知日志。

    \b
    Agent 用法（结构化输出）：自动从上次 cursor 处增量拉取，查完后更新 cursor，保证不重复。
      miloco-cli perceive logs --jsonl

    \b
    调试用法：--since 手动指定时间范围，不读写 cursor 文件。
      miloco-cli perceive logs --since 1h
      miloco-cli perceive logs --since 1h --limit 50
    """
    from miloco_cli.client import api_get

    params: dict = {}

    debug_mode = bool(since or limit)

    if debug_mode:
        if since:
            params["since"] = since
        if limit:
            params["limit"] = limit
    else:
        cursor = _load_cursor()
        if cursor is not None:
            params["after"] = cursor

    data = api_get("/api/perception/logs", params or None)

    if not debug_mode:
        logs = data.get("data", {}).get("logs", [])
        if logs:
            last_t = logs[-1].get("t")
            if last_t:
                _save_cursor(last_t)

    if jsonl:
        logs = data.get("data", {}).get("logs", [])
        if not logs:
            print('No logs found')
            return
        
        for log in logs:
            t = log.get("t", "")
            d = log.get("d", "")
            print(f"{t}: {dump(d)}")
    else:
        print_result(data, pretty)

@perceive_group.command("clear")
@click.option("--pretty", is_flag=True)
def perceive_clear(pretty):
    """清除感知缓存（清空所有感知设备流缓冲区）。"""
    from miloco_cli.client import api_post

    data = api_post("/api/perception/clear")
    print_result(data, pretty)
