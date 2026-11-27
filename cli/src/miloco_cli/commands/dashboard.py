"""dashboard 命令：在浏览器打开家庭面板 web 页。"""

from __future__ import annotations

import os
import sys
import webbrowser

import click

from miloco_cli.output import print_result


def _is_healthy(url: str) -> bool:
    import httpx

    try:
        # 本地 backend 是 http（main.py 起 uvicorn 不带 ssl 参数），httpx 对 http 不做
        # 证书校验，不传 verify —— 照抄别处的 verify=False 对 http 无作用，还会挂一条
        # CodeQL request-without-cert-validation（回环地址上的误报）。与 service.py 的
        # _wait_for_health / _backend_ready 同口径。
        return httpx.get(url.rstrip("/") + "/health", timeout=2).status_code == 200
    except Exception:
        return False


def _can_open_browser() -> bool:
    # macOS / Windows 有系统默认浏览器；Linux 仅在有图形会话时才开，否则 webbrowser
    # 可能选中控制台浏览器(lynx/w3m)，其 open 是前台阻塞，会把命令挂住。
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@click.command("dashboard")
@click.option("--monitor", is_flag=True, help="打开性能调试视图（#perf），查看系统指标")
@click.option("--pretty", is_flag=True)
def dashboard_cmd(monitor, pretty):
    """在浏览器打开 Miloco 家庭面板。"""
    from miloco_cli.config import load_config

    base = load_config()["server"]["url"].rstrip("/") + "/"
    running = _is_healthy(base)
    url = base + "#perf" if monitor else base
    opened = _can_open_browser() and webbrowser.open(url)

    result = {"url": url, "running": running, "opened": opened}
    if not running:
        result["hint"] = "后端未运行，先执行 miloco-cli service start"
    print_result(result, pretty)
