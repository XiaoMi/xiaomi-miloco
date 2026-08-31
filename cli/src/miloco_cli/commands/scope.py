"""scope 命令组：管理 miloco 的感知范围（哪些家庭 / 摄像头接入）。"""

import click

from miloco_cli.client import api_delete, api_get, api_put
from miloco_cli.output import print_result

_HOMES_PATH = "/api/miot/scope/homes"
_CAMERAS_PATH = "/api/miot/scope/cameras"
_CAMERAS_VOICE_PATH = "/api/miot/scope/cameras/voice"
_CAMERAS_PROMPT_PATH = "/api/miot/scope/cameras/prompt"
_CAMERAS_CROP_PATH = "/api/miot/scope/cameras/crop"


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
    不精确到路)；crop-on/off 与 prompt-set 一样**精确到路**(裁不裁取决于该路镜头的视野)。
    crop_in_use=该路的智能裁切**存储偏好**(默认 true)；crop_effective=**三道闸是否全开**
    (= in_use AND 全局双闸 AND crop_in_use)。crop_effective=false 时按三种情形反查：in_use=false
    是这台不在感知范围里；crop_in_use=false 是这一路自己关的；两者都 true 则是被全局闸挡住、
    逐机位全白设。注意 crop_effective=true 只说明闸开着,**不含**
    "流是否真订阅上"(看同一行 connected；connected=false 时这一路没进感知窗、裁切判定一次都
    没跑),也**不保证每窗都在裁**——本窗无检测框且无显著运动块
    (reason=no_activity,空房间最常见、属正常)、裁切区域面积超/不足上下限、区域退化、本窗无帧、
    编码或 JPEG 产物过短等**内容层**回退只在后端日志 event=adaptive_crop_fallback 的 reason=
    里可见(完整 11 项见 _maybe_encode_adaptive 的 docstring;注意 per_camera_off 是 debug 级、
    默认级别下 grep 不到)。"""
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


# ── Smart Crop（智能裁切增强）逐机位开关：默认开，关掉即该路改走全景 ──
#
# 裁不裁是**机位级**判断：门口窄视野机位裁了收益小，客厅广角机位收益大，故逐路可配
# （did 精确到 :chN，同 prompt-set；裸多通道 did = 该台全部通道）。与全局双闸
# perception.engine.crop_enhance.enabled / user_enabled **相与** —— 全局关时这里设了也不
# 生效（不报错，允许预配置）。与启用/拾音开关正交、不重启引擎，下一感知窗即生效。
# 当前状态看 scope camera list 的 crop_in_use 字段。


@scope_camera.command("crop-on")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_crop_on(dids, pretty):
    """开启指定机位的智能裁切增强（回到默认；DIDS 可用 did:chN 精确到某一路）。"""
    result = api_put(
        _CAMERAS_CROP_PATH,
        {"items": [{"did": d, "crop_in_use": True} for d in dids]},
    )
    print_result(result, pretty)


@scope_camera.command("crop-off")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_crop_off(dids, pretty):
    """关闭指定机位的智能裁切增强（该路改走全景、不裁切；分辨率档不变）。"""
    result = api_put(
        _CAMERAS_CROP_PATH,
        {"items": [{"did": d, "crop_in_use": False} for d in dids]},
    )
    print_result(result, pretty)


# ── 每摄像头「感知须知」自定义 prompt：给该机位补环境说明 / 关注 / 忽略事项 ──
#
# 逐感知窗注入 omni 的 system prompt 尾部（video / audio 路由均注入），指导模型消解该机位的固定误识
# （如门口机位误把公共走廊电梯门当自家入户门）。与启用/拾音开关正交、不重启引擎，
# 下一窗即生效。清除用 prompt-clear（设置空文本会被 backend 拒）。上限见 backend
# （默认 500 字），超限由 backend 拒绝并透传。


@scope_camera.command("prompt-set")
@click.argument("did")
@click.argument("text")
@click.option("--pretty", is_flag=True)
def scope_camera_prompt_set(did, text, pretty):
    """设置某摄像头的感知须知（TEXT 建议加引号；含环境 / 关注 / 忽略，指导感知消解误识）。"""
    result = api_put(
        _CAMERAS_PROMPT_PATH,
        {"items": [{"did": did, "prompt": text}]},
    )
    print_result(result, pretty)


@scope_camera.command("prompt-clear")
@click.argument("dids", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
def scope_camera_prompt_clear(dids, pretty):
    """清除指定摄像头的感知须知（回到无自定义）。"""
    result = api_delete(
        _CAMERAS_PROMPT_PATH,
        params={"did": list(dids)},
    )
    print_result(result, pretty)
