"""本地视觉引擎的起活路径:探活缓存、冷却、以及每一档「起不来」的落点。

这一整段此前**一行都没被执行过**:相邻的两条测试把 ``_init_local_engine`` 整个
mock 掉,只断言走了哪个分支,于是分支里面写什么都不会红。而这里的每一条失败落点,
regress 之后的表现都是同一个——感知安静地停摆,界面上看不出异常。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from miloco.config.settings import get_settings
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.local_vision import LocalVisionError

HEALTHY = {"status": "ok", "model_loaded": True, "device": "cuda:0", "backend": "codec"}


@pytest.fixture
def local_cfg():
    """一份切到 local 的配置副本。绝不动开发机上真实的那份。"""
    cfg = get_settings().model_copy(deep=True)
    cfg.perception.engine_backend = "local"
    cfg.perception.local_vision.base_url = "http://sidecar:18800"
    cfg.perception.local_vision.token = ""
    return cfg


def _build(local_cfg, health=None, exc=None, engine_boom=False):
    """构造一个真的走完 _init_local_engine 的 proxy。"""

    def _health(self):
        if exc:
            raise LocalVisionError(exc)
        return dict(health if health is not None else HEALTHY)

    ctx = [
        patch("miloco.perception.client.get_settings", return_value=local_cfg),
        patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health),
    ]
    if engine_boom:
        ctx.append(patch(
            "miloco.perception.local_vision.LocalVisionEngine",
            side_effect=RuntimeError("boom"),
        ))
    for c in ctx:
        c.start()
    try:
        return PerceptionEngineProxy()
    finally:
        for c in reversed(ctx):
            c.stop()


# ── 每一档「起不来」都要落在可自愈的等待态,而不是就绪或死态 ────────────────


def test_healthy_sidecar_produces_a_ready_engine(local_cfg):
    p = _build(local_cfg)
    assert p._status == "ready"
    assert p.perception_engine is not None


def test_unreachable_sidecar_waits_instead_of_failing_hard(local_cfg):
    p = _build(local_cfg, exc="connection refused")
    assert p._status == "local_vision_unreachable"
    assert p.perception_engine is None
    assert "不可达" in p._status_message


def test_model_still_loading_waits(local_cfg):
    p = _build(local_cfg, health={**HEALTHY, "model_loaded": False, "status": "loading"})
    assert p._status == "local_vision_unreachable"
    assert "加载" in p._status_message


def test_rejected_credentials_do_not_produce_a_ready_engine(local_cfg):
    """探活能通、模型也加载好了,但凭证不被接受。

    这是最危险的一档:放过去的话引擎会就绪,然后每一窗推理 401,而界面全绿。
    """
    p = _build(local_cfg, health={**HEALTHY, "auth_required": True, "auth_ok": False})
    assert p._status == "local_vision_unreachable"
    assert p.perception_engine is None
    assert "凭证" in p._status_message


def test_engine_construction_failure_is_not_left_as_ready(local_cfg):
    """构造异常若冒泡出去,会留下 _status='ready' 而 engine=None 的死态:
    ready 属性恒 False、try_reinit 又因"已 ready"拒绝重建,没有任何自愈路径。"""
    p = _build(local_cfg, engine_boom=True)
    assert p._status == "engine_init_failed"
    assert p.perception_engine is None


def test_construction_failure_is_recoverable_only_by_explicit_restart():
    """engine_init_failed 不进 tick 自愈(重跑重型构造会阻塞事件循环),
    但「重启感知」必须能救回来。"""
    assert "engine_init_failed" not in PerceptionEngineProxy._TICK_RECOVERABLE
    assert "engine_init_failed" in PerceptionEngineProxy._RESTART_RECOVERABLE
    assert "local_vision_unreachable" in PerceptionEngineProxy._TICK_RECOVERABLE


# ── 探活缓存与冷却 ────────────────────────────────────────────────────────


def test_failed_probe_arms_a_cooldown(local_cfg):
    """边车长时间不在时,不该每个 4s 的 tick 都去撞。"""
    p = _build(local_cfg, exc="down")
    assert p._local_probe_not_before > time.monotonic()


@pytest.mark.asyncio
async def test_changing_the_address_invalidates_the_cooldown(local_cfg):
    """用户刚把地址改对,却要先干等一整个冷却窗口才恢复 —— 而界面已经显示"已切到本地"。"""
    p = _build(local_cfg, exc="down")
    assert p._local_probe_error is not None
    frozen = p._local_probe_not_before

    calls: list = []

    def _health(self):
        calls.append(self.base_url)
        return dict(HEALTHY)

    local_cfg.perception.local_vision.base_url = "http://fixed:18800"
    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        await p.refresh_local_probe()

    assert calls == ["http://fixed:18800"], "改了地址却没有立刻重探"
    assert p._local_probe_error is None
    assert p._local_probe_not_before < frozen


@pytest.mark.asyncio
async def test_same_config_still_honours_the_cooldown(local_cfg):
    """配置没变就老老实实等 —— 否则冷却形同虚设。"""
    p = _build(local_cfg, exc="down")
    calls: list = []

    def _health(self):
        calls.append(1)
        return dict(HEALTHY)

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        await p.refresh_local_probe()

    assert calls == []


@pytest.mark.asyncio
async def test_probe_is_a_noop_once_the_engine_exists(local_cfg):
    p = _build(local_cfg)
    calls: list = []

    def _health(self):
        calls.append(1)
        return dict(HEALTHY)

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        await p.refresh_local_probe()
    assert calls == []


@pytest.mark.asyncio
async def test_probe_is_a_noop_on_the_cloud_backend(local_cfg):
    """云端部署不该因为这个特性多出任何一次网络请求。"""
    p = _build(local_cfg, exc="down")
    local_cfg.perception.engine_backend = "cloud"
    calls: list = []

    def _health(self):
        calls.append(1)
        return dict(HEALTHY)

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        await p.refresh_local_probe()
    assert calls == []


# ── 配置到引擎的透传 ──────────────────────────────────────────────────────


def test_every_configured_field_reaches_the_engine(local_cfg):
    """七个设置项逐一核对。此前只有 short_edge 被测过,其余六项在构造时写死
    都不会让任何测试变红 —— 用户在面板上改了却毫无效果,而且无从察觉。"""
    lv = local_cfg.perception.local_vision
    lv.container_fps, lv.crf, lv.max_new_tokens = 6, 31, 320
    lv.max_frames, lv.event_gate_threshold = 24, 0.4
    lv.scene_ask, lv.video_short_edge = "只描述有没有人", 288

    e = _build(local_cfg).perception_engine
    assert e._container_fps == 6
    assert e._crf == 31
    assert e._max_new_tokens == 320
    assert e._max_frames == 24
    assert e._gate_threshold == 0.4
    assert e._scene_ask == "只描述有没有人"
    assert e._short_edge_override == 288


def test_short_edge_none_is_passed_through_so_it_can_follow_the_shared_setting(local_cfg):
    """构造期把 None 换成具体值的话,面板上调分辨率对本通路就永久失效了 ——
    而该设置的契约是"写盘后下一帧即生效"。"""
    local_cfg.perception.local_vision.video_short_edge = None
    p = _build(local_cfg)
    assert p.perception_engine._short_edge_override is None

    local_cfg.perception.local_vision.video_short_edge = 256
    p2 = _build(local_cfg)
    assert p2.perception_engine._short_edge_override == 256


# ── 探活冷却的两种语义:会自愈的等,不会自愈的退避 ────────────────────────


def test_rejected_credentials_arm_a_cooldown(local_cfg):
    """凭证被拒不会自己好。不设冷却就是每 4s 一次、一天两万多次的空转。"""
    p = _build(local_cfg, health={**HEALTHY, "auth_required": True, "auth_ok": False})
    assert p._local_probe_not_before > time.monotonic()


def test_model_loading_arms_only_a_short_cooldown(local_cfg):
    """加载通常几十秒就好,不该压满 30 秒;但加载**失败**时边车会永远停在
    loading(它刻意不崩进程),完全不压就是无限轮询。短冷却两头都照顾到。"""
    import miloco.perception.client as pc

    p = _build(local_cfg, health={**HEALTHY, "model_loaded": False, "status": "loading"})
    wait = p._local_probe_not_before - time.monotonic()
    assert 0 < wait <= pc._LOADING_PROBE_COOLDOWN_SEC
    assert pc._LOADING_PROBE_COOLDOWN_SEC < pc._LOCAL_PROBE_COOLDOWN_SEC


@pytest.mark.asyncio
async def test_explicit_restart_recovers_inside_the_cooldown_window(local_cfg):
    """冷却是给自动轮询设的节流阀,不该管住用户的手。

    没有这条,边车其实已经恢复了,而用户按「重启感知」是个 no-op —— 还得再干等
    冷却走完,期间界面显示"已切到本地"而感知完全停摆。
    """
    p = _build(local_cfg, exc="down")
    assert p._status == "local_vision_unreachable"
    assert p._local_probe_not_before > time.monotonic()  # 冷却生效中

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync",
                  lambda self: dict(HEALTHY)):
        p.try_reinit(include_failed=True)   # 只作废缓存,不在这里探活
        assert p._local_probe_not_before == 0.0, "冷却没被显式重启清掉"
        await p.refresh_local_probe()       # tick 的线程化探活
        p.try_reinit(include_failed=True)

    assert p._status == "ready"
    assert p.perception_engine is not None


# ── 主事件循环上永远不做同步 HTTP ─────────────────────────────────────────


def test_rebuild_after_a_config_change_does_not_probe_inline(local_cfg):
    """admin 切换与「重启感知」都经这条路,而两者都跑在主事件循环上。

    这里同步探活的话,边车地址被防火墙 DROP 时,相机取帧 / SSE / 整个 API 会被
    卡住最多 3 秒 —— 而这个文件周围的注释三次声称"同步路径永远不碰网络"。
    """
    p = _build(local_cfg)
    assert p._status == "ready"

    calls: list = []

    def _health(self):
        calls.append(self.base_url)
        return dict(HEALTHY)

    local_cfg.perception.local_vision.base_url = "http://moved:18800"
    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        p.perception_engine = None
        p._init_engine()

    assert calls == [], f"在主事件循环上做了同步探活: {calls}"
    assert p._status == "local_vision_unreachable"
    assert "待探活" in p._status_message


def test_explicit_restart_does_not_probe_inline_either(local_cfg):
    p = _build(local_cfg, exc="down")
    calls: list = []

    def _health(self):
        calls.append(1)
        return dict(HEALTHY)

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync", _health):
        p.try_reinit(include_failed=True)
    assert calls == []


def test_construction_may_probe_synchronously(local_cfg):
    """构造期是唯一的例外(还没有事件循环在跑),否则冷启动会先报一次假故障。"""
    p = _build(local_cfg)
    assert p._status == "ready"
    assert p._allow_sync_probe is False, "构造完必须关掉同步探活的口子"


@pytest.mark.asyncio
async def test_processor_refresh_delegates_to_the_proxy():
    """processor 那一层不是转发就是断链 —— 而 tick 只认识它。

    把它的方法体换成 return,整条"主循环不做同步 HTTP"的设计就死了:tick 照常
    await 一个 no-op,重建路径于是自己去同步探活。实测这样改动 1316 条测试全绿。
    """
    from miloco.perception.processor import PipelineProcessor

    calls: list = []

    class _Proxy:
        async def refresh_local_probe(self):
            calls.append("refresh")

    proc = PipelineProcessor.__new__(PipelineProcessor)
    proc._perception_engine_proxy = _Proxy()
    await proc.refresh_local_probe()
    assert calls == ["refresh"], "processor 没有把探活刷新转发给 proxy"


@pytest.mark.asyncio
async def test_tick_refreshes_the_probe_before_attempting_a_rebuild():
    """整条"主循环不做同步 HTTP"的设计,全靠 tick 里这个先后顺序撑着。

    观察**调用顺序**,不是源码里的字符串位置:后者在无害的重构(把调用挪进一个
    小函数)下会假红,又会在"调用被换成别的含同名标识符的东西"时保持绿。
    """
    from miloco.perception.runner import PerceptionRunner

    order: list = []

    class _Proc:
        async def refresh_local_probe(self):
            order.append("refresh")

        def try_reinit_engine(self, **kw):
            order.append("reinit")
            return False

    class _Pipeline(_Proc):
        def drive_omni_probe(self):
            pass

        async def process_realtime(self):
            return None

    class _Collector:
        def get_all_active_sources(self):
            return ["cam1"]

    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._pipeline = _Pipeline()
    runner._collector = _Collector()
    runner._is_running = True
    await runner._tick()

    assert order[:2] == ["refresh", "reinit"], f"tick 的先后顺序不对: {order}"


# ── 边车中途死掉:引擎必须降级,好让既有的失败提示接手 ────────────────────


@pytest.mark.asyncio
async def test_sustained_sidecar_failure_demotes_the_engine(local_cfg):
    """边车挂掉后引擎若一直停在 ready,用户在界面上看不到任何异常。

    云端通路挂掉有全局红条 + 「立即重试」;本地此前什么都没有,唯一的信号是
    "事件不再出现",而那要过很久才会被注意到。降回等待态之后,既有那套
    (PREREQ_MISSING → 状态条 → tick 自愈)就能原样接手。
    """
    from unittest.mock import patch

    p = _build(local_cfg)
    assert p._status == "ready" and p.perception_engine is not None

    engine = p.perception_engine
    engine._consecutive_failures = 99   # 模拟连续多窗完全不可达
    assert engine.sustained_failure is True

    closed: list = []
    orig_close = engine.close

    async def _spy_close():
        # 拆卸必须在锁内发生 —— 在这里断言,比事后看状态更能钉住顺序。
        assert p._engine_lock.locked(), "close() 发生在 _engine_lock 之外"
        closed.append(True)
        await orig_close()

    engine.close = _spy_close

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync",
                  lambda self: (_ for _ in ()).throw(LocalVisionError("down"))):
        await p.refresh_local_probe()

    assert p.perception_engine is None, "引擎没有被降级,界面上仍然显示一切正常"
    assert p._status == "local_vision_unreachable"
    assert p._status in PerceptionEngineProxy._TICK_RECOVERABLE, "降级后必须能自愈"
    # 拆卸的两条不变量才是重点(邻居 stop_to_unconfigured 明写着):
    #  - 必须 await close():引擎持有常驻 httpx 连接池,直接置空就是每轮漏一个;
    #  - 必须在 _engine_lock 里做:否则会在主动查询跑到一半时把引擎抽走。
    assert closed == [True], "降级没有 close() 引擎,连接池漏了"
    assert not p._engine_lock.locked(), "降级结束后锁没释放"


@pytest.mark.asyncio
async def test_a_healthy_engine_is_never_demoted(local_cfg):
    """短暂抖动由退避吸收 —— 一次失败就降级会让界面无谓地闪"感知不可用"。"""
    from unittest.mock import patch

    p = _build(local_cfg)
    p.perception_engine._consecutive_failures = 1
    calls: list = []

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync",
                  lambda self: calls.append(1) or dict(HEALTHY)):
        await p.refresh_local_probe()

    assert p.perception_engine is not None
    assert p._status == "ready"
    assert calls == [], "引擎还在跑就不该去探活"


@pytest.mark.asyncio
async def test_demotion_does_not_flap_when_health_is_green(local_cfg):
    """边车 /health 绿、但每次推理都失败 —— 降级不能被下一个 tick 立刻撤销。

    没有重建冷却时会这样循环:降级 → 探活通过 → 重建 → 再失败 → 再降级。界面上
    那条提示每轮闪一下,httpx 连接池每轮换一个,而用户什么也做不了。
    """
    from unittest.mock import patch

    p = _build(local_cfg)
    p.perception_engine._consecutive_failures = 99

    with patch("miloco.perception.client.get_settings", return_value=local_cfg), \
            patch("miloco.perception.local_vision.client.LocalVisionClient.health_sync",
                  lambda self: dict(HEALTHY)):
        await p.refresh_local_probe()          # 触发降级
        assert p.perception_engine is None
        assert p._local_rebuild_not_before > time.monotonic()

        # 紧接着的 tick 会调 try_reinit —— 它必须**不**把引擎立刻建回来。
        p.try_reinit()
        assert p.perception_engine is None, "降级在同一轮就被撤销了(抖动)"
        assert p._status == "local_vision_unreachable"

        # 冷却过后允许重建。
        p._local_rebuild_not_before = 0.0
        await p.refresh_local_probe()
        p.try_reinit()
    assert p.perception_engine is not None, "冷却结束后应当能恢复"


@pytest.mark.asyncio
async def test_local_encode_failure_is_not_blamed_on_the_sidecar(local_cfg):
    """本地编码失败(PyAV 没编进 libx264)一次网络都没发过。

    把它算成"边车不可达"会做两件错事:把用户指向网络和边车,以及拆掉引擎 ——
    而拆引擎对一个本地编码问题毫无帮助。
    """
    import miloco.perception.local_vision.engine as eng
    import numpy as np
    from miloco.perception.local_vision.encode import EncodeError
    from miloco.perception.types import (
        BatchedSnapshot,
        DeviceSnapshot,
        PerceptionDevice,
        VideoFrame,
        VideoStream,
    )

    p = _build(local_cfg)
    e = p.perception_engine
    orig = eng.encode_snapshot_to_h264
    eng.encode_snapshot_to_h264 = lambda *a, **k: (_ for _ in ()).throw(
        EncodeError("Unknown encoder 'libx264'")
    )
    try:
        frames = [VideoFrame(data=np.zeros((32, 32, 3), dtype=np.uint8), timestamp=float(i))
                  for i in range(4)]
        batch = BatchedSnapshot(snapshots=[DeviceSnapshot(
            device=PerceptionDevice(did="cam1", name="x", device_type="camera", room_name="书房"),
            video=VideoStream(frames=frames, width=32, height=32), audio=None,
            start_timestamp=0.0, end_timestamp=4000.0)])
        for _ in range(8):
            res = await e.realtime_perceive(batch, [])
            assert res.skipped is True
    finally:
        eng.encode_snapshot_to_h264 = orig

    assert e._consecutive_failures == 0, "本地编码失败被算成了边车不可达"
    assert e.sustained_failure is False, "会因此拆掉引擎 —— 而拆引擎救不了编码问题"
    assert res.timing.get("_omni_error_cam1") == "encode_failed"
