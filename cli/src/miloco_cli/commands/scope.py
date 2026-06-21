"""scope 命令组：管理 miloco 的感知范围（哪些家庭 / 摄像头接入）。"""

import click

from miloco_cli.client import api_get, api_put
from miloco_cli.output import print_result

_HOMES_PATH = "/api/miot/scope/homes"
_CAMERAS_PATH = "/api/miot/scope/cameras"


@click.group("scope")
def scope_group():
    """管理 miloco 的感知范围：哪些家庭、哪些摄像头接入。"""


# ─── scope home ─────────────────────────────────────────────────────────────


@scope_group.group("home")
def scope_home():
    """管理哪些家庭接入 miloco 感知。"""


@scope_home.command("list")
@click.option("--pretty", is_flag=True)
def scope_home_list(pretty):
    """列出全部家庭；in_use=true 表示已开启感知。"""
    print_result(api_get(_HOMES_PATH), pretty)


@scope_home.command("switch")
@click.argument("home_id")
@click.option("--pretty", is_flag=True)
def scope_home_switch(home_id, pretty):
    """切换到指定家庭（唯一启用），其余自动停用。"""
    result = api_put(_HOMES_PATH, {"home_id": home_id})
    print_result(result, pretty)


# ─── scope camera ───────────────────────────────────────────────────────────


@scope_group.group("camera")
def scope_camera():
    """管理哪些摄像头接入 miloco 感知。"""


@scope_camera.command("list")
@click.option("--pretty", is_flag=True)
def scope_camera_list(pretty):
    """列出全部摄像头；in_use=已开启，is_online=设备在线，connected=视频流已连接。"""
    print_result(api_get(_CAMERAS_PATH), pretty)


@scope_camera.command("enable")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_enable(dids, pretty):
    """开启指定摄像头感知。"""
    result = api_put(_CAMERAS_PATH, {"items": [{"did": d, "in_use": True} for d in dids]})
    print_result(result, pretty)


@scope_camera.command("disable")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_disable(dids, pretty):
    """关闭指定摄像头感知。"""
    result = api_put(_CAMERAS_PATH, {"items": [{"did": d, "in_use": False} for d in dids]})
    print_result(result, pretty)


@scope_camera.group("schedule")
def scope_camera_schedule():
    """管理摄像头每日感知时间段。"""


@scope_camera_schedule.command("get")
@click.argument("did")
@click.option("--pretty", is_flag=True)
def scope_camera_schedule_get(did, pretty):
    """查看指定摄像头的定时感知配置。"""
    result = api_get(_CAMERAS_PATH)
    cameras = result.get("data") or []
    for camera in cameras:
        if camera.get("did") == did:
            print_result({"code": 0, "message": "ok", "data": camera}, pretty)
            return
    raise click.ClickException(f"camera did not found: {did}")


@scope_camera_schedule.command("set")
@click.argument("did")
@click.option(
    "--window",
    "windows",
    multiple=True,
    required=True,
    help="允许感知时间段，格式 HH:MM-HH:MM；可重复传入。",
)
@click.option("--pretty", is_flag=True)
def scope_camera_schedule_set(did, windows, pretty):
    """设置指定摄像头的每日感知时间段。"""
    parsed = []
    for value in windows:
        try:
            start, end = value.split("-", 1)
        except ValueError as exc:
            raise click.BadParameter(
                "window must be HH:MM-HH:MM",
                param_hint="--window",
            ) from exc
        parsed.append({"start": start, "end": end})
    result = api_put(
        f"{_CAMERAS_PATH}/{did}/schedule",
        {"enabled": True, "windows": parsed},
    )
    print_result(result, pretty)


@scope_camera_schedule.command("off")
@click.argument("did")
@click.option("--pretty", is_flag=True)
def scope_camera_schedule_off(did, pretty):
    """关闭指定摄像头的定时限制。"""
    current = api_get(_CAMERAS_PATH)
    windows = []
    for camera in current.get("data") or []:
        if camera.get("did") == did:
            windows = (camera.get("schedule") or {}).get("windows") or []
            break
    result = api_put(
        f"{_CAMERAS_PATH}/{did}/schedule",
        {"enabled": False, "windows": windows},
    )
    print_result(result, pretty)
