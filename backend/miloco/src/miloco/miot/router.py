# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Xiaomi IoT controller
Handles Xiaomi IoT device login, authorization, and device management
"""

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.websockets import WebSocketDisconnect

from miloco.config import get_settings
from miloco.manager import get_manager
from miloco.middleware import (
    BusinessException,
    verify_token,
    verify_websocket_token,
)
from miloco.middleware.exceptions import HTTPException
from miloco.miot.schema import (
    AuthorizeRequest,
    CameraToggleRequest,
    CameraVoiceToggleRequest,
    DeviceControlRequest,
    HomeSwitchRequest,
    MipsStatusResponse,
    SendNotifyRequest,
)
from miloco.miot.ws import (
    NalClipRecorder,
    miot_audio_stream_manager,
    miot_video_stream_manager,
)
from miloco.schema.common_schema import NormalResponse
from miloco.utils.common import escape_for_js_string

logger = logging.getLogger(name=__name__)


def _truncate_ws_reason(reason: str) -> str:
    """把 WS 关闭帧的 reason 截断到协议安全长度。

    WebSocket 关闭帧是 control frame,整帧 payload ≤125 字节;close code 占 2 字节,
    reason 只剩 ≤123 字节(RFC 6455 §5.5)。``websockets`` 库会严格校验,超长直接抛
    ``ProtocolError: control frame too long`` —— 于是"优雅关闭并带上原因"这步本身崩掉,
    连接被底层 abort,前端反而收到无信息的 1006 abnormal close,且每次都打一整条 ASGI
    traceback。典型触发:PPCS 没握手时 ``Camera ... not registered with SDK (likely PPCS
    not handshaken). Try ...`` 这条 100+ 字符的中英文 reason,UTF-8 编码后远超 123 字节。

    中文 3 字节/字,不能按字符数截 —— 按 UTF-8 字节截到 120(留 3 字节余量),且避免切在
    多字节字符中间(``errors="ignore"`` 丢掉被切碎的尾字节)。

    encode 用 ``errors="replace"``:reason 来自 ``f"Server error: {str(err)}"``,OS /
    文件系统类异常的 str 在 PEP 383 surrogateescape 下可能带孤立代理项,默认 strict
    encode 会抛 UnicodeEncodeError —— 那样本函数(本就是为防关闭路径崩溃而存在)反而
    自己崩在 encode 上,关闭失败退回无信息的 1006。replace 在**编码**方向把非法码位
    换成 ASCII ``?``(1 字节,不是解码方向的 U+FFFD �),稳且绝不会顶破 120 上限。
    """
    encoded = reason.encode("utf-8", errors="replace")
    # 注意:即便没超长也要返回 round-trip 后的串,不能 return 原 reason——原串可能含
    # 孤立代理项,直接交给 websocket.close() 仍会在它内部 encode 时崩。统一走 encoded。
    return encoded[:120].decode("utf-8", errors="ignore")


# 首帧看门狗:WS 注册成功(reg_id≥0)后,若摄像头在这么多秒内一帧都没出,判定为
# "连不上"(典型:摄像头跟 backend 不在同一局域网且 PPCS 中继也没建起来 / 摄像头离线 /
# 休眠)。给前端发一条明确的 error 信令再关,而不是让它永远停在"正在连接摄像头…"。
# 12s:米家 IPC 冷启动 + PPCS 握手 + 首个 IDR 通常 2-6s,12s 给弱网足够余量又不会让
# 真连不上的住户干等太久。
_FIRST_FRAME_TIMEOUT_S = 12.0


async def _first_frame_watchdog(
    websocket: WebSocket, camera_id: str, channel: int
) -> None:
    """等首帧;超时仍无帧 → 发 error 信令 + 主动关闭,让前端能明确告知住户连不上。

    被 ``video_stream_websocket`` 当后台 task 起。正常出帧时这个 task 等满
    ``_FIRST_FRAME_TIMEOUT_S`` 后发现 ``has_emitted_frame`` 为真,啥也不做退出。
    取消安全:住户在超时前主动关页 → 主流程 finally 里 cancel 本 task,
    ``CancelledError`` 直接向上抛,不吞。
    """
    await asyncio.sleep(_FIRST_FRAME_TIMEOUT_S)
    if miot_video_stream_manager.has_emitted_frame(camera_id, channel):
        return
    logger.warning(
        "First-frame watchdog fired, %s.%d — no frame in %.0fs, camera likely "
        "unreachable (cross-LAN / offline / PPCS relay not established)",
        camera_id, channel, _FIRST_FRAME_TIMEOUT_S,
    )
    try:
        # reason 是给将来按机器码分流预留的字段;前端 watch.html 当前只展示 message,
        # 不读 reason。两个都发,前端按需取。
        await websocket.send_text(
            json.dumps({
                "type": "error",
                "reason": "camera_unreachable",
                "message": "连不上摄像头(可能不在同一局域网,或摄像头离线)",
            })
        )
    except Exception as err:
        # send 失败基本意味着连接已被对端关掉——再 close 也是白搭,还会再抛一条
        # error 把"连接没了"这件正常事刷成两条 ERROR。直接收尾,主流程 finally 的
        # close_connection 负责清理。降到 info,不混进真 error。
        logger.info("watchdog send skipped (conn likely gone), %s.%d: %s",
                    camera_id, channel, err)
        return
    try:
        # 1011 + 短 reason(已被 _truncate_ws_reason 口径约束在 control frame 上限内)
        await websocket.close(
            code=1011, reason=_truncate_ws_reason("camera_unreachable")
        )
    except Exception as err:
        logger.info("watchdog close failed, %s.%d: %s", camera_id, channel, err)


router = APIRouter(prefix="/miot", tags=["Xiaomi IoT"])

manager = get_manager()


@router.post(
    "/authorize",
    summary="Submit Xiaomi authorization code obtained from redirect page",
    response_model=NormalResponse,
)
async def authorize_miot(
    request: AuthorizeRequest,
    current_user: str = Depends(verify_token),
):
    """Exchange the authorization code (pasted by user) for an access token."""
    logger.info("MiOT authorize API called, user: %s", current_user)
    await manager.miot_service.authorize_with_code(request.code, request.state)
    return NormalResponse(code=0, message="MiOT authorized successfully", data=None)


@router.get("/status", summary="Check MiOT bind status", response_model=NormalResponse)
async def get_miot_bind_status(current_user: str = Depends(verify_token)):
    """Check MiOT bind status"""
    logger.info("MiOT bind status API called, user: %s", current_user)
    result = await manager.miot_service.get_miot_bind_status()
    return NormalResponse(
        code=0, message="Bind status checked successfully", data=result
    )


@router.post("/bind", summary="Bind MiOT account", response_model=NormalResponse)
async def bind_miot(current_user: str = Depends(verify_token)):
    """Bind MiOT account: get OAuth URL for authorization"""
    logger.info("MiOT bind API called, user: %s", current_user)
    result = await manager.miot_service.bind_miot()
    return NormalResponse(
        code=0, message="OAuth URL generated successfully", data=result
    )


@router.post("/unbind", summary="Unbind MiOT account", response_model=NormalResponse)
async def unbind_miot(current_user: str = Depends(verify_token)):
    """Unbind MiOT account: clear all MiOT state"""
    logger.info("MiOT unbind API called, user: %s", current_user)
    await manager.miot_service.unbind_miot()
    return NormalResponse(code=0, message="MiOT unbound successfully", data=None)


@router.get(
    "/login_status", summary="Check MiOT login status", response_model=NormalResponse
)
async def get_miot_login_status(current_user: str = Depends(verify_token)):
    """Check MiOT login status"""
    logger.info("MiOT login status API called, user: %s", current_user)

    result = await manager.miot_service.get_miot_login_status()

    logger.info("MiOT login status: Login successful")
    return NormalResponse(
        code=0, message="Login status checked successfully", data=result
    )


@router.get(
    path="/user_info",
    summary="Get MiOT user information",
    response_model=NormalResponse,
)
async def get_miot_user_info(current_user: str = Depends(verify_token)):
    """Get MiOT user information"""
    logger.info("Get MiOT user info API called, user: %s", current_user)

    user_info = await manager.miot_service.get_miot_user_info()

    logger.info("Successfully retrieved Xiaomi Home user information")
    return NormalResponse(
        code=0, message="MiOT user information retrieved successfully", data=user_info
    )


@router.get(
    path="/camera_list", summary="Get MiOT camera list", response_model=NormalResponse
)
async def get_miot_camera_list(current_user: str = Depends(verify_token)):
    """Get MiOT camera list"""
    logger.info("Get MiOT camera list API called, user: %s", current_user)

    camera_list = await manager.miot_service.get_miot_camera_list()

    logger.info(
        "Successfully retrieved Xiaomi Home camera list - Count: %s", len(camera_list)
    )
    return NormalResponse(
        code=0, message="MiOT camera list retrieved successfully", data=camera_list
    )


@router.get(
    path="/device_list", summary="Get MiOT device list", response_model=NormalResponse
)
async def get_miot_device_list(current_user: str = Depends(verify_token)):
    """Get MiOT device list"""
    logger.info("get miot device list, user: %s", current_user)
    device_list = await manager.miot_service.get_miot_device_list()
    logger.info(
        "Successfully retrieved Xiaomi Home device list - Count: %s", len(device_list)
    )
    return NormalResponse(
        code=0, message="MiOT device list retrieved successfully", data=device_list
    )


@router.get(
    path="/home",
    summary="Get home info (devices, areas, scenes, persons)",
    response_model=NormalResponse,
)
async def get_home_info(
    current_user: str = Depends(verify_token),
    refresh: bool = Query(False, description="true = 先刷新云端设备/摄像头/场景"),
):
    """Get home info for CLI。refresh=true 触发 device refresh。"""
    logger.info("Get home info API called, user=%s, refresh=%s", current_user, refresh)
    data = await manager.miot_service.get_home_info(refresh=refresh)
    return NormalResponse(code=0, message="Home info retrieved successfully", data=data)


@router.get(
    path="/devices/{did}/spec",
    summary="Get single device spec",
    response_model=NormalResponse,
)
async def get_device_spec(did: str, current_user: str = Depends(verify_token)):
    """Get spec for a single device (轻量，不拉全量 home_info)。"""
    logger.info("Get device spec API called, user=%s, did=%s", current_user, did)
    data = await manager.miot_service.get_device_spec(did)
    return NormalResponse(code=0, message="ok", data=data)


@router.post(
    path="/devices/{did}/control",
    summary="Control device property or action",
    response_model=NormalResponse,
)
async def control_device(
    did: str,
    request: DeviceControlRequest,
    current_user: str = Depends(verify_token),
):
    """Control device: set_property / set_properties / call_action"""
    logger.info(
        "Control device API called, user: %s, did: %s, type: %s",
        current_user,
        did,
        request.type,
    )
    data = await manager.miot_service.control_device(did, request)
    return NormalResponse(
        code=0, message="Device control executed successfully", data=data
    )


@router.get(
    path="/device_history",
    summary="Get per-device recent-iid history (capacity-limited LRU)",
    response_model=NormalResponse,
)
async def get_device_history(current_user: str = Depends(verify_token)):
    """Return the full LRU snapshot keyed by did. Touches are written
    server-side by control_device / get_device_status; clients read but do
    not write."""
    data = await manager.miot_service.lru_snapshot()
    return NormalResponse(code=0, message="ok", data=data)


@router.get(
    path="/devices/{did}/status",
    summary="Get device property status",
    response_model=NormalResponse,
)
async def get_device_status(
    did: str,
    iid: str | None = None,
    current_user: str = Depends(verify_token),
):
    """Get device property values. iid: comma-separated prop IIDs, e.g. prop.2.1,prop.2.2"""
    logger.info(
        "Get device status API called, user: %s, did: %s, iid: %s",
        current_user,
        did,
        iid,
    )
    iids = [i.strip() for i in iid.split(",")] if iid else None
    data = await manager.miot_service.get_device_status(did, iids)
    return NormalResponse(
        code=0, message="Device status retrieved successfully", data=data
    )


@router.post(
    path="/scenes",
    summary="Create a manual scene (not yet implemented)",
    response_model=NormalResponse,
)
async def create_scene(
    current_user: str = Depends(verify_token),
):
    """Create a MIoT manual scene — not yet supported by MiOT API"""
    raise HTTPException(message="Scene creation is not yet supported", status_code=501)


@router.post(
    path="/scenes/{scene_id}/trigger",
    summary="Trigger a manual scene",
    response_model=NormalResponse,
)
async def trigger_scene(
    scene_id: str,
    current_user: str = Depends(verify_token),
):
    """Trigger a MIoT manual scene"""
    logger.info(
        "Trigger scene API called, user: %s, scene_id: %s", current_user, scene_id
    )
    success = await manager.miot_service.trigger_scene(scene_id)
    if not success:
        raise BusinessException("Scene trigger failed")
    return NormalResponse(code=0, message="Scene triggered successfully", data=None)


@router.get(
    path="/refresh_miot_all_info",
    summary="Refresh MiOT all information",
    response_model=NormalResponse,
)
async def refresh_miot_all_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT all information"""
    logger.info("Refresh MiOT all info API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_all_info()
    logger.info("MiOT information refresh completed: %s", result)
    return NormalResponse(
        code=0, message="MiOT information refresh completed", data=result
    )


@router.get(
    path="/refresh_camera_online",
    summary="Lightweight refresh of camera online status (does not perturb streams)",
    response_model=NormalResponse,
)
async def refresh_camera_online(current_user: str = Depends(verify_token)):
    """轻量刷新相机在线状态——只重拉 SDK 列表更新缓存元数据,不动解码/流。
    供前端「此刻」页加载前调,让"已离线/在线"判断不读陈旧缓存(相机重新上线后
    list_cameras_with_state 的缓存本不会自愈),且不像 refresh_miot_cameras 那样卡流。"""
    result = await manager.miot_service.refresh_camera_online()
    return NormalResponse(code=0, message="ok", data=result)


@router.get(
    path="/refresh_miot_cameras",
    summary="Refresh MiOT camera information",
    response_model=NormalResponse,
)
async def refresh_miot_cameras(current_user: str = Depends(verify_token)):
    """Refresh MiOT camera information"""
    logger.info("Refresh MiOT cameras API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_cameras()

    logger.info("Successfully refreshed Xiaomi Home camera information")
    return NormalResponse(
        code=0, message="MiOT camera information refreshed successfully", data=result
    )


@router.get(
    path="/refresh_miot_scenes",
    summary="Refresh MiOT scene information",
    response_model=NormalResponse,
)
async def refresh_miot_scenes(current_user: str = Depends(verify_token)):
    """Refresh MiOT scene information"""
    logger.info("Refresh MiOT scenes API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_scenes()

    logger.info("Successfully refreshed Xiaomi Home scene information")
    return NormalResponse(
        code=0, message="MiOT scene information refreshed successfully", data=result
    )


@router.get(
    path="/refresh_miot_user_info",
    summary="Refresh MiOT user information",
    response_model=NormalResponse,
)
async def refresh_miot_user_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT user information"""
    logger.info("Refresh MiOT user info API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_user_info()

    logger.info("Successfully refreshed Xiaomi Home user information")
    return NormalResponse(
        code=0, message="MiOT user information refreshed successfully", data=result
    )


@router.get(
    path="/refresh_miot_devices",
    summary="Refresh MiOT device information",
    response_model=NormalResponse,
)
async def refresh_miot_devices(current_user: str = Depends(verify_token)):
    """Refresh MiOT device information"""
    logger.info("Refresh MiOT devices API called, user: %s", current_user)

    result = await manager.miot_service.refresh_miot_devices()

    logger.info("Successfully refreshed Xiaomi Home device information")
    return NormalResponse(
        code=0, message="MiOT device information refreshed successfully", data=result
    )


@router.get(
    path="/mips_status",
    summary="Cloud MQTT (mips) subscription status",
    response_model=MipsStatusResponse,
)
async def mips_status(current_user: str = Depends(verify_token)):
    """Return cloud-MQTT connection / user-bind subscribe status.

    Useful for verifying whether real-time device-bind detection is
    currently working. ``last_error`` carries the broker reason code when
    the account-level subscribe was rejected.
    """
    return MipsStatusResponse.model_validate(manager.miot_service.get_mips_status())


@router.post(
    path="/send_notify", summary="Send notification", response_model=NormalResponse
)
async def send_notify(
    request: SendNotifyRequest, current_user: str = Depends(verify_token)
):
    """Send notification"""
    logger.info(
        "Send notify API called, notify: %s, user: %s", request.notify, current_user
    )
    await manager.miot_service.send_notify(request.notify)
    return NormalResponse(code=0, message="Notification sent successfully", data=None)



# ─── scope: 家庭 / 相机接入范围 ──────────────────────────────────────────────


@router.get(
    path="/scope/homes",
    summary="List all homes with in_use flag",
    response_model=NormalResponse,
)
async def list_scope_homes(current_user: str = Depends(verify_token)):
    homes = await manager.miot_service.list_homes()
    return NormalResponse(code=0, message="ok", data=homes)


@router.put(
    path="/scope/homes",
    summary="Switch to a home (others auto-disabled)",
    response_model=NormalResponse,
)
async def switch_scope_home(
    request: HomeSwitchRequest, current_user: str = Depends(verify_token)
):
    data = await manager.miot_service.switch_home(request.home_id)
    return NormalResponse(code=0, message="ok", data=data)


@router.get(
    path="/scope/cameras",
    summary="List cameras with cloud_online / lan_reachable / awake / in_use / connected",
    response_model=NormalResponse,
)
async def list_scope_cameras(current_user: str = Depends(verify_token)):
    cameras = await manager.miot_service.list_cameras_with_state()
    return NormalResponse(code=0, message="ok", data=cameras)


@router.put(
    path="/scope/cameras",
    summary="Batch toggle cameras enable state",
    response_model=NormalResponse,
)
async def toggle_scope_camera(
    request: CameraToggleRequest, current_user: str = Depends(verify_token)
):
    data = await manager.miot_service.toggle_camera(
        [{"did": i.did, "in_use": i.in_use} for i in request.items]
    )
    return NormalResponse(code=0, message="ok", data=data)


@router.put(
    path="/scope/cameras/voice",
    summary="Batch toggle per-camera audio pickup (mic-off) state",
    response_model=NormalResponse,
)
async def toggle_scope_camera_voice(
    request: CameraVoiceToggleRequest, current_user: str = Depends(verify_token)
):
    # 单独端点(不复用 /scope/cameras)：拾音开关无投喂上限/离线校验、不重启感知引擎,
    # 与相机启用开关的 refresh/sync/restart 逻辑正交,分开更清晰。
    data = await manager.miot_service.toggle_camera_voice(
        [{"did": i.did, "voice_in_use": i.voice_in_use} for i in request.items]
    )
    return NormalResponse(code=0, message="ok", data=data)


@router.post(
    "/record_clip",
    summary="Record N seconds from a camera and return mp4",
)
async def record_clip(
    camera_id: str = Query(..., description="MiOT camera did"),
    channel: int = Query(0, description="Camera channel (default 0)"),
    duration_ms: int = Query(
        15000, ge=2000, le=60000, description="Clip duration in ms (2–60s)"
    ),
    current_user: str = Depends(verify_token),
) -> Response:
    """Click-triggered camera clip → mp4.

    Piggybacks on :class:`MIoTVideoStreamManager`'s existing per-camera SDK
    subscription (multi_reg under the hood — no second PPCS stream against
    the camera). Lifecycle:

      1. Attach a :class:`NalClipRecorder` as a subscriber. If no WS client
         was watching this camera, ``start_video_stream`` runs now;
         otherwise we just join the existing fan-out.
      2. Recorder receives decoded BGR frames from the SDK callback and
         encodes them inline through libx264 (ultrafast / zerolatency) for
         ``duration_ms`` of wall time.
      3. mp4 container is flushed + closed in the recorder's worker thread.
      4. We detach. If we were the last subscriber, SDK teardown runs.

    Returns the mp4 bytes inline; the frontend feeds the blob straight into
    the existing ``/api/identity/persons/.../extract`` pipeline. No file
    is persisted to disk.

    Timeout = duration + 8s grace for the first BGR frame + encode flush.
    If the camera never yields a frame (offline / not handshaken), we
    return 504; register failures (camera not bound) return 503.
    """
    logger.info(
        "record_clip API called, user: %s, camera: %s.%d, dur=%dms",
        current_user, camera_id, channel, duration_ms,
    )
    recorder = NalClipRecorder(duration_ms=duration_ms)
    try:
        await miot_video_stream_manager.register_recorder(
            camera_id, channel, recorder,
        )
    except RuntimeError as e:
        # PPCS not handshaken / camera not bound — surface as 503 so the
        # browser can prompt the user to re-bind instead of hanging.
        logger.warning("record_clip register failed: %s", e)
        raise HTTPException(message=str(e), status_code=503)

    try:
        timeout_s = duration_ms / 1000.0 + 8.0
        try:
            mp4_bytes = await recorder.wait(timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "record_clip timeout, %s.%d — no keyframe within %.1fs",
                camera_id, channel, timeout_s,
            )
            raise HTTPException(
                message=(
                    "Recording timed out — camera produced no keyframe. "
                    "Check that the camera is online and bound."
                ),
                status_code=504,
            )
    finally:
        recorder.cancel()
        await miot_video_stream_manager.unregister_recorder(
            camera_id, channel, recorder,
        )

    logger.info(
        "record_clip OK, %s.%d, %d bytes",
        camera_id, channel, len(mp4_bytes),
    )
    return Response(
        content=mp4_bytes,
        media_type="video/mp4",
        headers={
            "Cache-Control": "no-store",
            # Suggested filename so the browser File API picks up a sensible
            # name if the blob is ever saved manually.
            "Content-Disposition":
                f'inline; filename="clip_{camera_id}_{channel}_{duration_ms}ms.mp4"',
        },
    )


@router.get("/watch", summary="Live camera view (browser)")
async def watch_page():
    """Serve the standalone live-view page.

    The page itself is unauthenticated (browsers can't set custom headers
    on the native WebSocket API, so auth is enforced at the WS level via
    ``?token=…``). To remove the friction of users having to paste the
    token into the URL, we substitute ``__MILOCO_TOKEN__`` in the template
    with the live ``server.token`` so the page can boot self-sufficiently.

    Trust note: anyone who can reach this URL effectively obtains the
    backend bearer token. That matches the existing trust model — the
    backend listens on 0.0.0.0 and trusts whoever holds ``server.token``.
    Don't expose this endpoint to untrusted networks.
    """
    settings = get_settings()
    token = settings.server.token or ""
    if not token:
        # token 真空(配置加载失败 / 运维清空)时返 503 比返一个连不上的 watch.html
        # 友好,住户看到错误页知道"配置出问题"而不是"摄像头连不上重试无效"。
        return HTMLResponse(
            content="<h1>503: server.token 未配置,无法启动 watch 页</h1>",
            status_code=503,
        )
    template = (settings.directories.static_dir / "watch.html").read_text(
        encoding="utf-8"
    )
    # 跟 main.py spa_handler 共用 escape_for_js_string 同 helper,改一边即同步两边。
    html = template.replace("__MILOCO_TOKEN__", escape_for_js_string(token))
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store"},
    )


@router.websocket("/ws/video_stream")
async def video_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int,
    current_user: str = Depends(verify_websocket_token),
):
    """Video stream WebSocket."""
    logger.info(
        "WebSocket connection request, %s, %s.%d", current_user, camera_id, channel
    )
    start_time: datetime = datetime.now()
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: str | None = None
    watchdog: asyncio.Task | None = None
    try:
        await websocket.accept()
        cid = await miot_video_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel,
        )
        # 注册成功(reg_id≥0)只代表 SDK 收下了订阅,不代表摄像头真能出帧。跨局域网
        # 且 PPCS 中继没建起来时,reg_id≥0 但永远没帧,前端会死等。起首帧看门狗,
        # 超时无帧就发 error 信令告知住户连不上。
        #
        # late-joiner 优化:摄像头已在出帧(_camera_seen_keyframe 已含该 tag,即已向
        # 某个 WS 广播过首帧)时,说明它显然可达,本连接不可能"连不上",起看门狗纯属
        # 白烧一个 12s noop task——高频刷新页时尤其浪费。此处无锁读 has_emitted_frame
        # 与 new_connection 内部的 lock 不同步,有极小 TOCTOU 窗口,但最坏后果仅是
        # "该起没起/不该起起了"一个 noop task,无正确性影响。
        # 注:recorder-only 窗口(只在录制、无 WS)keyframe 尚未向任何 WS 广播过,
        # has_emitted_frame 为 False,新开 tab 仍会起一次看门狗;首个真 IDR(≤~1.2s)
        # 会让它 no-op 退出,无害。
        # (watchdog 已在 try 外声明为 None,供 accept/new_connection 早抛时 finally 安全读)
        if not miot_video_stream_manager.has_emitted_frame(camera_id, channel):
            watchdog = asyncio.create_task(
                _first_frame_watchdog(websocket, camera_id, channel)
            )
            # 检索异常防 "Task exception was never retrieved" 噪音:看门狗体内已全
            # try/except,当前不会抛;但 task 从不被 await(只在 finally cancel),加这
            # 个 done-callback 兜住将来有人在 watchdog 里加未包裹 await 抛错的回归。
            # cancelled() 时不碰 exception()(否则抛 CancelledError),非取消才读。
            watchdog.add_done_callback(
                lambda t: None if t.cancelled() else t.exception()
            )
        while True:
            try:
                message = await websocket.receive_text()
                logger.info("Received message from client, %s", message)
            except WebSocketDisconnect:
                # 看门狗判定连不上后主动 close,或住户关页——recv 抛 disconnect 是
                # 预期的正常收尾,不是异常。降到 info,别跟真 error 混淆刷 ERROR 噪音。
                logger.info("Client closed, %s.%d", camera_id, channel)
                break
            except Exception as err:
                logger.error("WebSocket error: %s", err)
                break
    except WebSocketDisconnect:
        logger.info("Client disconnected, %s.%d", camera_id, channel)
    except Exception as err:
        logger.error("WebSocket error, %s", err)
        await websocket.close(
            code=1011, reason=_truncate_ws_reason(f"Server error: {str(err)}")
        )
    finally:
        # 住户主动关页 / 出帧正常退出时,看门狗可能还在 sleep——取消它,避免它在连接
        # 已关后再去 send/close 一个死 socket。task 还没起(accept 前就异常)时为 None。
        if watchdog is not None:
            watchdog.cancel()
        logger.info(
            "Websocket connect duration[%.2fs], %s.%d",
            (datetime.now() - start_time).total_seconds(),
            camera_id,
            channel,
        )
        if cid:
            await miot_video_stream_manager.close_connection(
                user_name=current_user,
                token_hash=token_hash,
                camera_id=camera_id,
                channel=channel,
                cid=cid,
            )


@router.websocket("/ws/audio_stream")
async def audio_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int,
    current_user: str = Depends(verify_websocket_token),
):
    """Audio stream WebSocket."""
    logger.info(
        "Audio WebSocket connection request, %s, %s.%d",
        current_user,
        camera_id,
        channel,
    )
    start_time: datetime = datetime.now()
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: str | None = None
    try:
        await websocket.accept()
        cid = await miot_audio_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel,
        )
        while True:
            try:
                message = await websocket.receive_text()
                logger.debug("Received message from audio client, %s", message)
            except WebSocketDisconnect:
                # 住户关页是正常收尾,不是异常——跟 video 端点对齐,降到 info 避免
                # 跟真 error 混淆刷 ERROR 噪音。
                logger.info("Audio client closed, %s.%d", camera_id, channel)
                break
            except Exception as err:
                logger.error("Audio WebSocket error: %s", err)
                break
    except WebSocketDisconnect:
        logger.info("Audio client disconnected, %s.%d", camera_id, channel)
    except Exception as err:
        logger.error("Audio WebSocket error, %s", err)
        await websocket.close(
            code=1011, reason=_truncate_ws_reason(f"Server error: {str(err)}")
        )
    finally:
        logger.info(
            "Audio WebSocket connect duration[%.2fs], %s.%d",
            (datetime.now() - start_time).total_seconds(),
            camera_id,
            channel,
        )
        if cid:
            await miot_audio_stream_manager.close_connection(
                user_name=current_user,
                token_hash=token_hash,
                camera_id=camera_id,
                channel=channel,
                cid=cid,
            )
