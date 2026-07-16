"""scope 命令组：管理 miloco 的感知范围（哪些家庭 / 摄像头接入）。"""

import click

from miloco_cli.client import api_get, api_put
from miloco_cli.output import print_result

_HOMES_PATH = "/api/miot/scope/homes"
_CAMERAS_PATH = "/api/miot/scope/cameras"
_CAMERAS_VOICE_PATH = "/api/miot/scope/cameras/voice"


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
