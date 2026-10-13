"""``router._first_frame_watchdog`` 跨 NAT 分流单测。

看门狗在「注册成功但 12s 内一帧没出」时判定连不上。跨网段相机（探测/注册成功但
NAT 限制拉流建不起来）应收到跨 NAT 专属 reason + 文案，而不是笼统的「可能不在同一
局域网/离线」。

判据只有 ``_nat_blocked``（= 相机列表 ``stream_error`` 的同一个入口
``MIoTProxy.stream_nat_blocked``，其第一道门就是缓存里的 ``cross_subnet``），且必须
在**判定时刻现取**：调用方不再传跨网段快照——快照停在建连时刻，而局域网首次探到这台
相机（探测周期 5~45s）才会把跨网段标记从空翻真，「主机刚起、住户第一时间点开播放页」
这个最典型入口下快照恒为假，拿它合取会让短路永不生效、并与列表接口各说一套。

钉死四个契约：
- ``_nat_blocked`` 有证据 → ``reason="camera_unreachable_cross_subnet"`` + 跨 NAT
  文案，close 的 reason 也带同款机器码，且不再续等 60s
- ``_nat_blocked`` 无证据 → ``reason="camera_unreachable"`` + 通用文案 + 保留续等
  （典型：相机断电，云端已离线而 LAN 保活窗未到期——与列表接口 ``stream_error=null``
  同口径，别把住户指去折腾路由器）
- 判定式不含任何建连时刻的快照参数（见 ``test_watchdog_takes_no_snapshot_param``）
- 已出帧（has_emitted_frame=True）→ 不触发，不 send 不 close
"""

from __future__ import annotations

import asyncio
import inspect
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
        asyncio.run(router._first_frame_watchdog(ws, "cam1", 0))
    ws.sleeps = sleeps
    return ws


def _sent_payload(ws) -> dict | None:
    if ws.send_text.await_count == 0:
        return None
    return json.loads(ws.send_text.await_args.args[0])


def test_cross_subnet_sends_dedicated_reason():
    """已确诊 NAT 阻断 + 无帧 → 跨 NAT 专属 reason + 文案。"""
    ws = _run_watchdog(nat_blocked=True)
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["type"] == "error"
    assert payload["reason"] == "camera_unreachable_cross_subnet"
    assert payload["message"] == _CROSS_SUBNET_MSG


def test_watchdog_takes_no_snapshot_param():
    """签名里不能再出现建连时刻的跨网段快照参数。

    这条钉的是一类回归而不是一次改动：判据只要多一个「调用方传进来的布尔」，就会
    重新出现「快照 False ∧ 现取 True ⇒ 合取假」的错位——播放页发通用文案、列表接口
    同刻已给 cross_subnet_nat，两个界面各说一套，且 12s 短路在最常见入口失效。
    """
    params = inspect.signature(router._first_frame_watchdog).parameters
    assert "cross_subnet" not in params
    # 更宽的一道：任何带布尔默认值的参数都是「调用方声明判据」的形状。断意图而不是
    # 断精确参数集，日后加 timeout_s 之类的注入点不会误报。
    bool_defaults = [
        name
        for name, p in params.items()
        if p.default is not inspect.Parameter.empty and isinstance(p.default, bool)
    ]
    assert bool_defaults == [], f"判定式不接受调用方传入的快照:{bool_defaults}"


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
        # 判据打桩:本条测的是 send 抛错的收尾,不该顺带去读真实的 manager 单例。
        patch.object(router, "_nat_blocked", return_value=False),
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
    ws = _run_watchdog(nat_blocked=True)
    ws.close.assert_awaited_once()
    kwargs = ws.close.await_args.kwargs
    assert kwargs.get("code") == 1011
    assert kwargs["reason"] == "camera_unreachable_cross_subnet"


# ── 续等短路：已有确定性 NAT 阻断证据时不再空转 60s ──────────────────────────


def test_nat_blocked_skips_grace_extension():
    """跨网段 + 已判定 NAT 阻断 → 12s 就出提示，不再续等 60s。

    建连计时在原生管理器创建时就播种，通常远早于住户点开播放页，所以看门狗刚起步
    stream_nat_blocked 往往已为真。此时续等的 60s 既等不到帧（这条链一帧没出过），
    也等不到自愈（静默检测早已跑过、正在 5min 重建冷却里；即便重建也走同一条被 NAT
    阻断的路径），住户白盯着「正在连接摄像头…」多转一分钟。
    """
    ws = _run_watchdog(nat_blocked=True)
    assert ws.sleeps == [router._FIRST_FRAME_TIMEOUT_S], (
        f"应只等首帧超时这一段，实际 {ws.sleeps}"
    )
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["reason"] == "camera_unreachable_cross_subnet"


def test_without_nat_evidence_keeps_grace():
    """没有 NAT 阻断证据 → 续等照旧，别把可能自愈的连接提前判死。"""
    ws = _run_watchdog(nat_blocked=False)
    assert ws.sleeps == [router._FIRST_FRAME_TIMEOUT_S, router._GRACE_EXTENSION_S]


def test_without_nat_evidence_sends_generic_reason():
    """没有 NAT 阻断证据（同网段 / 还没探到 / 相机断电）→ 通用 reason + 文案。

    典型场景：相机被拔电，云端心跳先超时把 online 置 False，LAN 侧 100s 保活窗
    未到期。此刻 ``stream_nat_blocked`` 因云端离线门返回 False，列表接口不给 NAT
    诊断；播放页必须同口径。历史失败模式是绕开这个唯一入口另看一份跨网段标记：
    相机只是断电，住户却被指去改路由器 NAT，且通用文案里「或摄像头离线」这条真正
    有用的线索反而没出现。
    """
    ws = _run_watchdog(nat_blocked=False)
    payload = _sent_payload(ws)
    assert payload is not None
    assert payload["type"] == "error"
    assert payload["reason"] == "camera_unreachable"
    assert payload["message"] == _GENERIC_MSG


def test_frame_arriving_during_grace_still_cancels_verdict():
    """续等期间出帧仍然解除判死（周期性静默自愈的本来目的）。"""
    ws = _run_watchdog(nat_blocked=False, has_frame=True)
    assert ws.send_text.await_count == 0


def test_nat_blocked_probe_failure_falls_back_to_grace():
    """判据取值抛异常 → 回退成「证据不足」，保留续等，不因缓存抖动提前判死。"""
    mgr_holder = MagicMock()
    mgr_holder.miot_proxy.stream_nat_blocked.side_effect = RuntimeError("boom")
    with patch.object(router, "get_manager", return_value=mgr_holder):
        assert router._nat_blocked("cam1") is False


def _fired_wait_seconds(caplog) -> float:
    """从判死那条 warning 里取出播报的等待时长。"""
    for rec in caplog.records:
        if "watchdog fired" in rec.getMessage():
            # 格式串第 3 个参数是等待秒数（前两个是 camera_id / channel）。
            return rec.args[2]
    raise AssertionError("没找到 watchdog fired 日志")


def test_fired_log_reports_short_circuit_wait(caplog):
    """短路判死时日志必须播报 12s，而不是写死的两段之和 72s。

    这条 warning 是运维反推「到底给了相机多久」的主要依据（判死同时 1011 关掉 WS，
    住户侧只看到画面始终没出来）。写死 72s 会让人按 6 倍时长去推算，还和上一行刚打的
    "skipping 60s grace" 自相矛盾，读日志的人得翻源码才能确定哪条为准。
    """
    with caplog.at_level("WARNING", logger="miloco.miot.router"):
        _run_watchdog(nat_blocked=True)
    assert _fired_wait_seconds(caplog) == router._FIRST_FRAME_TIMEOUT_S


def test_fired_log_reports_full_wait_on_normal_path(caplog):
    """常规路径仍播报两段之和 72s。"""
    with caplog.at_level("WARNING", logger="miloco.miot.router"):
        _run_watchdog(nat_blocked=False)
    assert _fired_wait_seconds(caplog) == (
        router._FIRST_FRAME_TIMEOUT_S + router._GRACE_EXTENSION_S
    )
