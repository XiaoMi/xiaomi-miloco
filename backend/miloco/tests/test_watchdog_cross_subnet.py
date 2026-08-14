"""``router._first_frame_watchdog`` 跨 NAT 分流单测。

看门狗在「注册成功但 12s 内一帧没出」时判定连不上。新增 ``cross_subnet`` 参数
后，跨网段相机（探测/注册成功但 NAT 限制拉流建不起来）应收到跨 NAT 专属 reason
+ 文案，而不是笼统的「可能不在同一局域网/离线」。

钉死三个契约：
- ``cross_subnet=True`` → ``reason="camera_unreachable_cross_subnet"`` + 跨 NAT 文案，close 的 reason 也带同款机器码
- ``cross_subnet=False``（默认）→ ``reason="camera_unreachable"`` + 通用文案
- 已出帧（has_emitted_frame=True）→ 不触发，不 send 不 close
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from miloco.miot import router

# 跨 NAT 专属文案（与 web/public/watch.html / hero.json 口径一致，精简版）
_CROSS_SUBNET_MSG = "跨网段拉流失败，建议将摄像头与主机接入同一网络，或把路由器设置为 全锥/开放 NAT"
_GENERIC_MSG = "连不上摄像头(可能不在同一局域网,或摄像头离线)"


def _run_watchdog(*, cross_subnet: bool = False, has_frame: bool = False):
    """跑一次看门狗，返回捕获的 send/close 调用。mock 掉 12s 等待与全局 manager。"""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()

    mgr = MagicMock()
    mgr.has_emitted_frame.return_value = has_frame

    with (
        patch.object(router, "miot_video_stream_manager", mgr),
        patch.object(router.asyncio, "sleep", new=AsyncMock()),
    ):
        asyncio.run(
            router._first_frame_watchdog(ws, "cam1", 0, cross_subnet=cross_subnet)
        )
    return ws


def _sent_payload(ws) -> dict | None:
    if ws.send_text.await_count == 0:
        return None
    return json.loads(ws.send_text.await_args.args[0])


def test_cross_subnet_sends_dedicated_reason():
    """跨网段 + 无帧 → 跨 NAT 专属 reason + 文案。"""
    ws = _run_watchdog(cross_subnet=True)
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["type"] == "error"
    assert payload["reason"] == "camera_unreachable_cross_subnet"
    assert payload["message"] == _CROSS_SUBNET_MSG


def test_same_subnet_sends_generic_reason():
    """同网段（cross_subnet=False）+ 无帧 → 通用 reason + 文案（行为不变）。"""
    ws = _run_watchdog(cross_subnet=False)
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["reason"] == "camera_unreachable"
    assert payload["message"] == _GENERIC_MSG


def test_default_cross_subnet_false():
    """不传 cross_subnet 时默认 False → 通用文案。"""
    ws = _run_watchdog()
    payload = _sent_payload(ws)
    assert payload["reason"] == "camera_unreachable"


def test_frame_emitted_skips_entirely():
    """已有首帧（has_emitted_frame=True）→ 不触发，不 send 不 close。"""
    ws = _run_watchdog(has_frame=True)
    ws.send_text.assert_not_awaited()
    ws.close.assert_not_awaited()


def test_send_failure_swallowed():
    """send 抛错（连接已断）→ 不向上抛，也不 close（return 收尾）。"""
    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=RuntimeError("conn gone"))
    ws.close = AsyncMock()
    mgr = MagicMock()
    mgr.has_emitted_frame.return_value = False
    with (
        patch.object(router, "miot_video_stream_manager", mgr),
        patch.object(router.asyncio, "sleep", new=AsyncMock()),
    ):
        asyncio.run(router._first_frame_watchdog(ws, "cam1", 0))  # 必须不抛
    ws.close.assert_not_awaited()


def test_close_called_with_1011_after_send():
    """正常路径：send 成功 → close(code=1011, reason=truncated)。"""
    ws = _run_watchdog()
    ws.close.assert_awaited_once()
    kwargs = ws.close.await_args.kwargs
    assert kwargs.get("code") == 1011
    assert kwargs["reason"] == "camera_unreachable"


def test_close_reason_matches_cross_subnet():
    """跨网段时 close 的 reason 也要带跨 NAT 机器码，与发信令一致。

    否则日志 / 抓包看到的关闭原因是「通用连不上」，与前端展示的跨 NAT 文案对不上，
    排查时误导。
    """
    ws = _run_watchdog(cross_subnet=True)
    ws.close.assert_awaited_once()
    kwargs = ws.close.await_args.kwargs
    assert kwargs.get("code") == 1011
    assert kwargs["reason"] == "camera_unreachable_cross_subnet"


# ---------------------------------------------------------------------------
# _resolve_cross_subnet：调用处取跨网段判据的分支
# ---------------------------------------------------------------------------


def test_resolve_true_when_cache_has_flag():
    """相机缓存带 cross_subnet=True → 返回 True。"""
    cam = MagicMock()
    cam.cross_subnet = True
    proxy = MagicMock()
    proxy.get_cached_camera.return_value = cam
    manager = MagicMock()
    manager.miot_proxy = proxy
    with patch.object(router, "get_manager", return_value=manager):
        assert router._resolve_cross_subnet("did1") is True


def test_resolve_false_when_cache_flag_false():
    """相机缓存带 cross_subnet=False → 返回 False。"""
    cam = MagicMock()
    cam.cross_subnet = False
    proxy = MagicMock()
    proxy.get_cached_camera.return_value = cam
    manager = MagicMock()
    manager.miot_proxy = proxy
    with patch.object(router, "get_manager", return_value=manager):
        assert router._resolve_cross_subnet("did1") is False


def test_resolve_false_when_cache_missing():
    """相机不在缓存（get_cached_camera 返回 None）→ False，不抛。"""
    proxy = MagicMock()
    proxy.get_cached_camera.return_value = None
    manager = MagicMock()
    manager.miot_proxy = proxy
    with patch.object(router, "get_manager", return_value=manager):
        assert router._resolve_cross_subnet("missing") is False


def test_resolve_false_when_cache_raises():
    """get_cached_camera 抛异常（缓存未就绪等）→ False，不抛。"""
    proxy = MagicMock()
    proxy.get_cached_camera.side_effect = RuntimeError("cache not ready")
    manager = MagicMock()
    manager.miot_proxy = proxy
    with patch.object(router, "get_manager", return_value=manager):
        assert router._resolve_cross_subnet("did1") is False
