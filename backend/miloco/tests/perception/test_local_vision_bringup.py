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


def test_short_edge_none_is_passed_through_so_it_can_follow_the_shared_setting(local_cfg):
    """构造期把 None 换成具体值的话,面板上调分辨率对本通路就永久失效了 ——
    而该设置的契约是"写盘后下一帧即生效"。"""
    local_cfg.perception.local_vision.video_short_edge = None
    p = _build(local_cfg)
    assert p.perception_engine._short_edge_override is None

    local_cfg.perception.local_vision.video_short_edge = 256
    p2 = _build(local_cfg)
    assert p2.perception_engine._short_edge_override == 256
