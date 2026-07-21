"""scope 命令组：管理 miloco 的感知范围（哪些家庭 / 摄像头接入）。"""

import click

from miloco_cli.client import api_get, api_put
from miloco_cli.output import print_result

_HOMES_PATH = "/api/miot/scope/homes"
_CAMERAS_PATH = "/api/miot/scope/cameras"
_CAMERAS_VOICE_PATH = "/api/miot/scope/cameras/voice"

_ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]
_WEEKDAY_ALIASES = {
    "1": 0,
    "mon": 0,
    "monday": 0,
    "周一": 0,
    "星期一": 0,
    "2": 1,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "周二": 1,
    "星期二": 1,
    "3": 2,
    "wed": 2,
    "wednesday": 2,
    "周三": 2,
    "星期三": 2,
    "4": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "周四": 3,
    "星期四": 3,
    "5": 4,
    "fri": 4,
    "friday": 4,
    "周五": 4,
    "星期五": 4,
    "6": 5,
    "sat": 5,
    "saturday": 5,
    "周六": 5,
    "星期六": 5,
    "7": 6,
    "sun": 6,
    "sunday": 6,
    "周日": 6,
    "星期日": 6,
    "周天": 6,
    "星期天": 6,
}


def _parse_weekdays(values) -> list[int]:
    if not values:
        return list(_ALL_WEEKDAYS)

    weekdays: set[int] = set()
    for value in values:
        key = value.strip().lower()
        if key not in _WEEKDAY_ALIASES:
            raise click.BadParameter(
                "weekday must be mon..sun, 1..7, or 周一..周日",
                param_hint="--weekday",
            )
        weekdays.add(_WEEKDAY_ALIASES[key])
    return sorted(weekdays)


def _normalize_window_time(value: str) -> str:
    """Normalize HH:MM to zero-padded 24-hour form accepted by the backend."""
    parts = value.strip().split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise click.BadParameter(
            "time must be HH:MM in 24-hour format",
            param_hint="--window",
        )
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise click.BadParameter(
            "time must be HH:MM in 24-hour format",
            param_hint="--window",
        )
    return f"{hour:02d}:{minute:02d}"



def _compose_channel_dids(resp: dict) -> dict:
    """CLI 展示层：把**多通道相机**每行的 ``did`` 显示成合成 did ``{did}:ch{n}``、去掉
    ``channel`` / ``channel_count`` 列（通道号已编码进 did），单摄保持裸 did。

    纯展示变换，不动后端：backend 仍按物理 did + channel 建模；这里只是让双摄两行不再
    「did / name 都相同、只差一个 channel 数字」难以区分。合成 did 也能**直接复制**给
    ``scope camera enable/disable <did:chN>`` 精确到某一路（backend 解析 ``:ch`` 后处理）。
    多通道判定用后端透出的权威信号 **``channel_count > 1``**（与 backend/前端同口径，不用行数代理）。
    """
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        return resp
    for row in data:
        if not isinstance(row, dict):
            continue
        cc = row.pop("channel_count", 1) or 1  # 取判据并从展示里去掉
        ch = row.pop("channel", None)  # 通道号已并入合成 did，展示层去掉
        did = row.get("did")
        if did is not None and ch is not None and cc > 1:
            row["did"] = f"{did}:ch{ch}"
    return resp


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
    """列出全部摄像头；in_use=当下真正开启(活跃集,≤4)，三态可用性 cloud_online(云端在线)/
    lan_reachable(局域网可达)/awake(镜头开关:true=开/false=关/null=未知)，connected=视频流已连接。
    多通道相机(双摄)每路一行，did 显示为合成 did did:chN(单摄保持裸 did)——该 did 可直接复制给
    enable/disable 精确到某一路；mic-on/off 是相机级(拾音只在球机/ch0，:chN 会被归一到整台，
    不精确到路)。"""
    print_result(_compose_channel_dids(api_get(_CAMERAS_PATH)), pretty)


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


# ── 拾音开关（mic-off 语义）：与 enable/disable 同款批量 did 语义，走 voice 端点 ──
#
# 关闭 = 该相机声音完全不被处理（引擎入口剥离音频：不转写、不上云、语音指令不
# dispatch），视频照常感知。从属规则：仅感知已启用(in_use=true)的相机可设，感知已
# 关闭时 backend 整批拒绝——api_put 透传其错误信息并以业务错误码退出，CLI 不吞。


@scope_camera.command("mic-on")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_mic_on(dids, pretty):
    """开启指定摄像头声音（声音重新参与感知）。"""
    result = api_put(
        _CAMERAS_VOICE_PATH,
        {"items": [{"did": d, "voice_in_use": True} for d in dids]},
    )
    print_result(result, pretty)


@scope_camera.command("mic-off")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_mic_off(dids, pretty):
    """关闭指定摄像头声音（该相机声音完全不被处理：不识别、不理解、不上云）。"""
    result = api_put(
        _CAMERAS_VOICE_PATH,
        {"items": [{"did": d, "voice_in_use": False} for d in dids]},
    )
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
    help="允许感知时间段，格式 HH:MM-HH:MM（小时可省略前导零，如 7:00-08:00）；可重复传入。",
)
@click.option(
    "--weekday",
    "weekdays",
    multiple=True,
    help="允许感知星期，可重复传入；支持 mon..sun、1..7、周一..周日。不传表示每天。",
)
@click.option("--pretty", is_flag=True)
def scope_camera_schedule_set(did, windows, weekdays, pretty):
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
        parsed.append(
            {
                "start": _normalize_window_time(start),
                "end": _normalize_window_time(end),
            }
        )
    result = api_put(
        f"{_CAMERAS_PATH}/{did}/schedule",
        {"enabled": True, "weekdays": _parse_weekdays(weekdays), "windows": parsed},
    )
    print_result(result, pretty)


@scope_camera_schedule.command("off")
@click.argument("did")
@click.option("--pretty", is_flag=True)
def scope_camera_schedule_off(did, pretty):
    """关闭指定摄像头的定时限制。"""
    current = api_get(_CAMERAS_PATH)
    weekdays = list(_ALL_WEEKDAYS)
    windows = []
    for camera in current.get("data") or []:
        if camera.get("did") == did:
            schedule = camera.get("schedule") or {}
            weekdays = schedule.get("weekdays") or list(_ALL_WEEKDAYS)
            windows = schedule.get("windows") or []
            break
    result = api_put(
        f"{_CAMERAS_PATH}/{did}/schedule",
        {"enabled": False, "weekdays": weekdays, "windows": windows},
    )
    print_result(result, pretty)
