"""miloco-cli 入口。

所有命令默认输出 JSON（紧凑），加 --pretty 输出缩进格式。
退出码：0=成功，1=参数/校验错误，2=网络错误，3=业务错误。

调试日志：``$MILOCO_HOME/config.json`` 中设置 ``{"debug": true}`` 后，每次调用会将
原始指令写入 ``$MILOCO_HOME/log/miloco-cli.log``。
"""

import functools
import sys
from datetime import datetime, timezone

import click

from miloco_cli.commands.account import account_group
from miloco_cli.commands.actions import actions_group
from miloco_cli.commands.admin import admin_group
from miloco_cli.commands.config import config_group
from miloco_cli.commands.dashboard import dashboard_cmd
from miloco_cli.commands.debug import debug_group
from miloco_cli.commands.device import device_group
from miloco_cli.commands.doctor import doctor_cmd
from miloco_cli.commands.home_profile import home_profile_group
from miloco_cli.commands.identity import identity_group
from miloco_cli.commands.monitor import monitor_group
from miloco_cli.commands.notify import notify_group
from miloco_cli.commands.perceive import perceive_group
from miloco_cli.commands.person import person_group
from miloco_cli.commands.rule import rule_group
from miloco_cli.commands.scene import scene_group
from miloco_cli.commands.scope import scope_group
from miloco_cli.commands.service import service_group
from miloco_cli.commands.task import task_group
from miloco_cli.commands.time_compute import time_compute_cmd


@functools.lru_cache(maxsize=1)
def _resolve_version() -> str:
    """运行时版本：优先读已安装包元数据，未安装时回退 git describe，最后兜底。

    纯展示用，不发布，故 git describe 的非 PEP440/semver 形式可接受。导入期绝不抛异常。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("miloco-cli")
    except PackageNotFoundError:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
        return "0.0.0+unknown"


_VERSION = _resolve_version()


@click.group()
def cli():
    """miloco-cli — Miloco 面向未来的全屋智能命令行工具。"""


# ─── 子命令组 ─────────────────────────────────────────────────────────────────

cli.add_command(device_group)
cli.add_command(actions_group)
cli.add_command(account_group)
cli.add_command(scene_group)
cli.add_command(perceive_group)
cli.add_command(rule_group)
cli.add_command(task_group)
cli.add_command(person_group)
cli.add_command(identity_group)
cli.add_command(home_profile_group)
cli.add_command(admin_group)
cli.add_command(service_group)
cli.add_command(config_group)
cli.add_command(debug_group)
cli.add_command(notify_group)
cli.add_command(scope_group)
cli.add_command(doctor_cmd)
cli.add_command(monitor_group)
cli.add_command(dashboard_cmd)
cli.add_command(time_compute_cmd)


# ─── 全局命令 ─────────────────────────────────────────────────────────────────


@cli.command("version")
@click.option("--pretty", is_flag=True)
def version_cmd(pretty):
    """显示版本信息。"""
    from miloco_cli.output import print_result

    print_result({"version": _VERSION}, pretty)


# ─── 入口 ──────────────────────────────────────────────────────────────────────


def _debug_log_invocation() -> None:
    """``config.json`` ``debug: true`` 时把命令行写入 ``$MILOCO_HOME/log/miloco-cli.log``。"""
    from miloco_cli.config import load_config, miloco_home

    try:
        cfg = load_config()
    except Exception:
        return
    debug = cfg.get("debug", False)
    if isinstance(debug, str):
        debug = debug.lower() in ("true", "1", "yes", "on")
    if not debug:
        return
    log_file = miloco_home() / "log" / "miloco-cli.log"
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    line = f"{ts} [DEBUG] {' '.join(sys.argv)}\n"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main() -> None:
    _debug_log_invocation()
    cli()
