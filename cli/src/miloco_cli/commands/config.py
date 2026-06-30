"""``miloco-cli config`` 子命令：show / get / set / list-paths。

- ``config show`` 输出合并后（默认 + ``$MILOCO_HOME/config.json`` + env）的完整配置。
- ``config get <path>`` 按点号路径取值。
- ``config set <path> <value>`` schema 校验后原子写入 ``config.json``。
- ``config list-paths`` 列出全部合法配置路径与说明。

``config set`` 默认行为：写入后若后端正在运行则调用 ``service restart`` 使新配置
生效；传 ``--no-restart`` 显式跳过。
"""

from __future__ import annotations

import json
import sys

import click

from miloco_cli.commands._ordered_group import OrderedGroup
from miloco_cli.config import (
    describe,
    get_value,
    known_paths,
    set_values,
    show_config,
)
from miloco_cli.output import print_result


def _mask(data: dict) -> dict:
    """敏感字段展示时做掩码。"""
    masked = json.loads(json.dumps(data))  # 深拷贝
    server = masked.get("server") or {}
    if server.get("token"):
        server["token"] = "***"
    model = masked.get("model") or {}
    omni = model.get("omni") or {}
    if omni.get("api_key"):
        omni["api_key"] = "***"
    return masked


@click.group("config", cls=OrderedGroup)
def config_group():
    """配置管理（服务端 / 模型 / Agent）：查看 / 取值 / 设置 / 列出路径。"""


@config_group.command("show")
@click.option("--pretty", is_flag=True, help="缩进输出")
@click.option("--unmasked", is_flag=True, help="不对 token/api_key 做掩码（调试用）")
def config_show(pretty: bool, unmasked: bool):
    """显示合并后的配置。"""
    data = show_config()
    if not unmasked:
        data = _mask(data)
    print_result(data, pretty)


@config_group.command("get")
@click.argument("path")
@click.option("--pretty", is_flag=True)
@click.option(
    "--value-only",
    is_flag=True,
    help="仅输出值本身(裸文本, 不含 JSON 包装), 方便脚本无需 JSON 解析器",
)
def config_get(path: str, pretty: bool, value_only: bool):
    """按点号路径取值，如 ``server.url`` / ``model.omni.api_key``。"""
    try:
        value = get_value(path)
    except KeyError:
        print(json.dumps({"error": f"path not found: {path}"}), file=sys.stderr)
        sys.exit(1)
    if value_only:
        print("" if value is None else value)
        return
    print_result({"path": path, "value": value}, pretty)


@config_group.command("set")
@click.argument("items", nargs=-1, required=True)
@click.option(
    "--no-restart",
    is_flag=True,
    help="写入后不自动重启 miloco-backend（默认：运行中则重启使配置生效）",
)
@click.option("--pretty", is_flag=True)
@click.pass_context
def config_set(ctx, items: tuple[str, ...], no_restart: bool, pretty: bool):
    """写入配置：``miloco-cli config set <path> <value> [<path> <value> ...]``。

    支持一次提交多组 (path, value) ，读取-修改-写入一次完成，避免多次调用
    中途 Ctrl+C 造成 ``config.json`` 半更新状态。

    \b
    示例：
      miloco-cli config set server.url https://192.168.1.100:1810
      miloco-cli config set model.omni.api_key sk-xxxxx
      miloco-cli config set debug true --no-restart
      miloco-cli config set model.omni.model xiaomi/mimo-v2.5 \\
                            model.omni.base_url https://api.xiaomimimo.com/v1 \\
                            model.omni.api_key sk-xxxxx --no-restart
    """
    if len(items) % 2 != 0:
        print(
            json.dumps(
                {
                    "error": "config set 参数必须成对: <path> <value> [<path> <value> ...]"
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    pairs = [(items[i], items[i + 1]) for i in range(0, len(items), 2)]

    try:
        persisted = set_values(pairs)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)

    result: dict = {"code": 0, "message": "ok"}
    if len(pairs) == 1:
        only = pairs[0][0]
        result["path"] = only
        result["value"] = persisted[only]
    else:
        result["updated"] = [{"path": p, "value": persisted[p]} for p, _ in pairs]

    if not no_restart:
        restart_info = _restart_if_running(pretty)
        if restart_info:
            result["restart"] = restart_info

    print_result(result, pretty)


def _restart_if_running(pretty: bool) -> dict | None:
    """若后端当前处于运行态，触发一次 ``service restart``；否则返回 None。"""
    # 延迟导入，避免命令加载期触发网络/子进程调用
    from miloco_cli.commands.service import (
        _find_pid_by_port,
        _get_backend_pid_from_supervisor,
        _supervisord_is_running,
        service_restart,
    )
    from miloco_cli.config import load_config

    cfg = load_config()
    running = False
    if _supervisord_is_running() and _get_backend_pid_from_supervisor():
        running = True
    elif _find_pid_by_port(cfg["server"]["url"]):
        running = True

    if not running:
        return {"triggered": False, "reason": "not running"}

    # 通过 click context 调用 service restart，复用其完整逻辑
    try:
        ctx = click.get_current_context()
        ctx.invoke(service_restart, pretty=pretty)
        return {"triggered": True}
    except SystemExit as exc:
        # service restart 失败时会 sys.exit(1)；保留写入但把错误上浮
        return {"triggered": True, "failed": True, "exit_code": exc.code}
    except Exception as exc:  # pragma: no cover - defensive
        return {"triggered": True, "failed": True, "error": str(exc)}


@config_group.command("list-paths")
@click.option("--pretty", is_flag=True)
def config_list_paths(pretty: bool):
    """列出全部合法的配置路径与中文说明。"""
    items = [{"path": p, "description": describe(p)} for p in known_paths()]
    print_result(items, pretty)
