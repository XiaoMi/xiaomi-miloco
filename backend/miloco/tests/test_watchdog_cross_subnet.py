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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from miloco.miot import router

# 跨 NAT 文案的唯一来源是 web/src/i18n/locales/zh/hero.json —— 这里**读**它而不是
# 再抄一份字面量。整句在仓里共四处（hero.json / watch.html 中英 / router.py 兜底
# message / 本测试），前三处两两之间都有机械约束（watch.html 由
# web/tests/crossSubnetCopySync.test.ts 断言），本测试补上「后端 ↔ hero.json」这条，
# 否则改前端文案时全绿、后端那份静静漂移。
_HERO_ZH = (
    Path(__file__).resolve().parents[3] / "web/src/i18n/locales/zh/hero.json"
)
_CROSS_SUBNET_MSG = json.loads(_HERO_ZH.read_text(encoding="utf-8"))["hero"][
    "streamErrorCrossSubnetNat"
]
_GENERIC_MSG = router.UNREACHABLE_MESSAGES["camera_unreachable"]


def _run_watchdog(
    *,
    cross_subnet: bool = False,
    has_frame: bool = False,
    nat_blocked: bool = False,
):
    """跑一次看门狗，返回捕获的 send/close 调用。mock 掉 12s 等待与全局 manager。

    ``ws.sleeps`` 上挂着本次跑了几段等待：1 = 只等了首帧超时（续等被短路），
    2 = 首帧超时 + 续等。
    """
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()

    mgr = MagicMock()
    mgr.has_emitted_frame.return_value = has_frame

    sleeps: list[float] = []

    async def _fake_sleep(sec):
        sleeps.append(sec)

    with (
        patch.object(router, "miot_video_stream_manager", mgr),
        patch.object(router.asyncio, "sleep", new=_fake_sleep),
        patch.object(router, "_nat_blocked", return_value=nat_blocked),
    ):
        asyncio.run(
            router._first_frame_watchdog(ws, "cam1", 0, cross_subnet=cross_subnet)
        )
    ws.sleeps = sleeps
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


# ── 续等短路：已有确定性 NAT 阻断证据时不再空转 60s ──────────────────────────


def test_nat_blocked_skips_grace_extension():
    """跨网段 + 已判定 NAT 阻断 → 12s 就出提示，不再续等 60s。

    建连计时在原生管理器创建时就播种，通常远早于住户点开播放页，所以看门狗刚起步
    stream_nat_blocked 往往已为真。此时续等的 60s 既等不到帧（这条链一帧没出过），
    也等不到自愈（静默检测早已跑过、正在 5min 重建冷却里；即便重建也走同一条被 NAT
    阻断的路径），住户白盯着「正在连接摄像头…」多转一分钟。
    """
    ws = _run_watchdog(cross_subnet=True, nat_blocked=True)
    assert ws.sleeps == [router._FIRST_FRAME_TIMEOUT_S], (
        f"应只等首帧超时这一段，实际 {ws.sleeps}"
    )
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["reason"] == "camera_unreachable_cross_subnet"


def test_cross_subnet_without_evidence_keeps_grace():
    """跨网段但还没有 NAT 阻断证据 → 续等照旧，别把可能自愈的连接提前判死。"""
    ws = _run_watchdog(cross_subnet=True, nat_blocked=False)
    assert ws.sleeps == [router._FIRST_FRAME_TIMEOUT_S, router._GRACE_EXTENSION_S]


def test_same_subnet_never_skips_grace():
    """同网段相机的续等不受影响——这条短路只服务跨网段被阻断的那群。"""
    ws = _run_watchdog(cross_subnet=False, nat_blocked=True)
    assert ws.sleeps == [router._FIRST_FRAME_TIMEOUT_S, router._GRACE_EXTENSION_S]


def test_frame_arriving_during_grace_still_cancels_verdict():
    """续等期间出帧仍然解除判死（周期性静默自愈的本来目的）。"""
    ws = _run_watchdog(cross_subnet=True, nat_blocked=False, has_frame=True)
    assert ws.send_text.await_count == 0


def test_nat_blocked_probe_failure_falls_back_to_grace():
    """判据取值抛异常 → 回退成「证据不足」，保留续等，不因缓存抖动提前判死。"""
    mgr_holder = MagicMock()
    mgr_holder.miot_proxy.stream_nat_blocked.side_effect = RuntimeError("boom")
    with patch.object(router, "get_manager", return_value=mgr_holder):
        assert router._nat_blocked("cam1") is False
