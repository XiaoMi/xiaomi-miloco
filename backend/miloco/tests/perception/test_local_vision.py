# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""本地视觉感知通路的单测(不需要 GPU / 不需要边车进程)。

覆盖三类不变量:
1. **纯视觉不变量** —— speeches / env_sounds / suggestions 恒空。这不是"还没做",
   是刻意不产:让看不见音频的模型填这些字段只会得到脑补结果。
2. **规则语义** —— per-device 定向下发、命中回填 rule_id、fail-closed。
3. **失败降级** —— 编码失败 / 边车不可达时不产假事件,标 skipped 交由上层跳过。
"""

from __future__ import annotations

import numpy as np
import pytest
from miloco.perception.local_vision.engine import (
    LocalVisionEngine,
    _physical_did,
    _rules_for_device,
)
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _snapshot(did: str, room: str = "客厅", frames: int = 4) -> DeviceSnapshot:
    imgs = [
        VideoFrame(data=np.zeros((64, 64, 3), dtype=np.uint8), timestamp=float(i * 1000))
        for i in range(frames)
    ]
    return DeviceSnapshot(
        device=PerceptionDevice(did=did, name=f"cam-{did}", device_type="camera", room_name=room),
        video=VideoStream(frames=imgs, width=64, height=64),
        audio=None,
        start_timestamp=0.0,
        end_timestamp=float(frames * 1000),
    )


class _FakeClient:
    """替身边车:记录收到的请求,按脚本返回。"""

    def __init__(self, responses: list[dict] | None = None, fail: bool = False):
        self.responses = responses or []
        self.fail = fail
        self.calls: list[dict] = []

    async def perceive(self, video, rules, scene_ask=None, max_new_tokens=256, want_gate=True):
        from miloco.perception.local_vision.client import LocalVisionError

        self.calls.append({"rules": rules, "scene_ask": scene_ask, "bytes": len(video)})
        if self.fail:
            raise LocalVisionError("sidecar down")
        if not self.responses:
            return {"caption": "空", "rule_hits": [], "gate_p": None, "backend": "frames"}
        return self.responses.pop(0)


def _engine(client, **kw) -> LocalVisionEngine:
    return LocalVisionEngine(client, fps=2, **kw)


# ── 规则定向 ──────────────────────────────────────────────────────────────


def test_physical_did_strips_channel():
    assert _physical_did("cam1:ch0") == "cam1"
    assert _physical_did("cam1") == "cam1"


def test_rules_for_device_broadcast_and_targeted():
    rules = [
        {"id": "r1", "name": "广播", "condition": {"query": "q", "perceive_device_ids": []}},
        {"id": "r2", "name": "仅A", "condition": {"query": "q", "perceive_device_ids": ["camA"]}},
        {"id": "r3", "name": "绑物理机", "condition": {"query": "q", "perceive_device_ids": ["camB"]}},
    ]
    assert [r["id"] for r in _rules_for_device(rules, "camA")] == ["r1", "r2"]
    # 规则绑整台相机的物理 did 时,该机任一通道都要命中
    assert [r["id"] for r in _rules_for_device(rules, "camB:ch1")] == ["r1", "r3"]


# ── 纯视觉不变量 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_only_never_emits_audio_or_suggestions():
    """边车即使回了音频类字段也不该出现在结果里 —— 本通路没有音频输入。"""
    client = _FakeClient([{
        "caption": "客厅有人在看书",
        "rule_hits": [],
        "gate_p": 0.9,
        "backend": "codec",
        # 故意塞入不该被采信的字段
        "speeches": [{"speaker": "人", "content": "开灯"}],
        "env_sounds": ["玻璃碎裂声"],
        "suggestions": [{"event": "危险"}],
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res is not None
    assert res.speeches == []
    assert res.env_sounds == []
    assert res.suggestions == []
    assert res.caption[0].description == "客厅有人在看书"


def test_static_rule_execution_is_declared_false():
    """本通路不执行设备动作。声明做成类属性,admin 接口直接读它告知用户 ——
    别处再硬编码一份,两处一漂移界面就在骗人。"""
    assert LocalVisionEngine.STATIC_RULE_EXECUTION is False


# ── 命中回填 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matched_rule_carries_rule_id_and_device():
    rules = [
        {"id": "r-sofa", "name": "沙发有人", "condition": {"query": "有人在沙发上", "perceive_device_ids": []}},
        {"id": "r-pet", "name": "有宠物", "condition": {"query": "有宠物", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "有人躺在沙发上",
        "rule_hits": [
            {"name": "沙发有人", "hit": True, "reason": "沙发上躺着一个人"},
            {"name": "有宠物", "hit": False, "reason": "没有宠物"},
        ],
        "gate_p": None,
        "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1", room="客厅")]), rules
    )
    assert res is not None
    assert len(res.matched_rules) == 1
    m = res.matched_rules[0]
    assert m.rule_id == "r-sofa"  # 回填的是 miloco 的 rule_id,不是模型给的名字
    assert m.room_name == "客厅"
    assert m.source_device_ids == ["cam1"]


@pytest.mark.asyncio
async def test_hit_name_mismatch_falls_back_to_name_lookup():
    """模型把顺序打乱时按名字找回,而不是错配到别的规则上。"""
    rules = [
        {"id": "r1", "name": "A", "condition": {"query": "qa", "perceive_device_ids": []}},
        {"id": "r2", "name": "B", "condition": {"query": "qb", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "B", "hit": True, "reason": "b 成立"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert [m.rule_id for m in res.matched_rules] == ["r2"]


# ── 门控 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_default_observes_but_never_skips():
    """默认阈值 0:门控概率只记录不决策(参考实现的门控是体育域训练的)。"""
    client = _FakeClient([{
        "caption": "静止的客厅", "rule_hits": [], "gate_p": 0.01, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert len(res.caption) == 1                      # 低概率也没被丢
    assert res.timing["_gate_p_cam1"] == pytest.approx(0.01)  # 但留了痕


@pytest.mark.asyncio
async def test_gate_skips_when_threshold_configured():
    client = _FakeClient([{
        "caption": "静止的客厅", "rule_hits": [], "gate_p": 0.1, "backend": "codec",
    }])
    res = await _engine(client, gate_threshold=0.5).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res.caption == []
    # 一条规则都没下发 + 叙述被压制 = 真的无事发生
    assert res.skipped is True


# ── 失败降级 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sidecar_failure_marks_skipped_not_fake_event():
    """边车挂了要标 skipped,绝不能产一条内容为空的"感知事件"。"""
    res = await _engine(_FakeClient(fail=True)).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res is not None
    assert res.skipped is True
    assert res.error_code == "local_vision_unavailable"
    assert res.caption == []


@pytest.mark.asyncio
async def test_empty_batch_returns_none():
    assert await _engine(_FakeClient()).realtime_perceive(BatchedSnapshot(snapshots=[]), []) is None


@pytest.mark.asyncio
async def test_on_demand_uses_query_as_scene_ask():
    client = _FakeClient([{"caption": "有两个人在看电视", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    out = await _engine(client).on_demand_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), "现在有人吗?"
    )
    assert out is not None
    assert out.answer == "有两个人在看电视"
    assert client.calls[0]["scene_ask"] == "现在有人吗?"
    assert client.calls[0]["rules"] == []


# ── 健康响应回显面 ─────────────────────────────────────────────────────────


def test_health_sanitizer_drops_unknown_fields():
    """base_url 用户可填、health 会经 admin 端点回显 —— 只放行已知字段,
    否则这个端点就成了能读任意 URL 响应体的探针。"""
    from miloco.perception.local_vision.client import _sanitize_health

    out = _sanitize_health({
        "status": "ok", "model_loaded": True, "device": "cuda:0",
        "secret": "AKIA-should-not-leak", "nested": {"a": 1}, "count": 42,
    })
    assert out == {"status": "ok", "model_loaded": True, "device": "cuda:0"}


def test_health_sanitizer_handles_non_dict_and_truncates():
    from miloco.perception.local_vision.client import _sanitize_health

    assert _sanitize_health(["not", "a", "dict"]) == {}
    assert len(_sanitize_health({"gate_error": "x" * 900})["gate_error"]) == 300


# ── 默认不变式 ────────────────────────────────────────────────────────────


def test_default_backend_is_cloud_and_local_path_not_taken():
    """没主动切换的用户必须走原来的云端分支 —— 这是本特性对上游的核心承诺。

    直接盯 _init_engine 的分派:默认配置下不得走进本地分支(否则所有存量部署
    会在没配边车的情况下被判 PREREQ_MISSING,感知直接停摆)。
    """
    from unittest.mock import patch

    from miloco.config.settings import PerceptionSettings
    from miloco.perception.client import PerceptionEngineProxy

    assert PerceptionSettings().engine_backend == "cloud"

    with patch.object(PerceptionEngineProxy, "_init_local_engine") as local_init, \
            patch.object(PerceptionEngineProxy, "_create_engine", return_value=object()):
        PerceptionEngineProxy()
    local_init.assert_not_called()


def test_local_backend_setting_routes_to_local_init():
    """反向:配了 local 就必须走本地分支(不能悄悄回落到云端要 Key)。"""
    from unittest.mock import patch

    from miloco.config.settings import get_settings
    from miloco.perception.client import PerceptionEngineProxy

    cfg = get_settings().model_copy(deep=True)
    cfg.perception.engine_backend = "local"
    with patch("miloco.perception.client.get_settings", return_value=cfg), \
            patch.object(PerceptionEngineProxy, "_init_local_engine") as local_init:
        PerceptionEngineProxy()
    local_init.assert_called_once()


# ── 状态机供给(规则不能命中一次就哑掉) ──────────────────────────────────


@pytest.mark.asyncio
async def test_device_rule_map_lists_judged_rules_per_device():
    """必须回填 device_rule_map:上层靠它给"下发了但没命中"的组合喂 False。
    不填的话规则状态机是边沿触发的,命中一次后 last_state 永远停在 True,
    同一条规则此后再也不会触发。"""
    rules = [
        {"id": "r1", "name": "A", "condition": {"query": "qa", "perceive_device_ids": []}},
        {"id": "r2", "name": "B", "condition": {"query": "qb", "perceive_device_ids": ["cam1"]}},
    ]
    client = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "A", "hit": True, "reason": "r"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.device_rule_map == {"cam1": ["r1", "r2"]}


@pytest.mark.asyncio
async def test_failed_device_absent_from_device_rule_map():
    """边车失败的设备没有任何证据 —— 登记进去等于凭空把它的规则推退成未命中。"""
    res = await _engine(_FakeClient(fail=True)).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]),
        [{"id": "r1", "name": "A", "condition": {"query": "q", "perceive_device_ids": []}}],
    )
    assert res.device_rule_map == {}


@pytest.mark.asyncio
async def test_gate_suppresses_caption_but_still_judges_rules():
    """门控只压制叙述。判定已经算出来了,连同 device_rule_map 一起丢会让
    状态机断供 —— 规则卡死在上一态。"""
    rules = [{"id": "r1", "name": "A", "condition": {"query": "q", "perceive_device_ids": []}}]
    client = _FakeClient([{
        "caption": "安静的客厅",
        "rule_hits": [{"name": "A", "hit": True, "reason": "成立"}],
        "gate_p": 0.1, "backend": "codec",
    }])
    res = await _engine(client, gate_threshold=0.5).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.caption == []                        # 叙述被压制
    assert [m.rule_id for m in res.matched_rules] == ["r1"]  # 判定照常
    assert res.device_rule_map == {"cam1": ["r1"]}          # 状态机照常供给


@pytest.mark.asyncio
async def test_unresolvable_hit_is_dropped_not_credited_to_index_rule():
    """名字对不上又找不到时必须丢弃。回落到索引位那条规则会让「厨房明火」
    背上「有人跌倒」的判定,触发完全无关的动作 —— 宁可漏报。"""
    rules = [
        {"id": "r-fire", "name": "厨房明火", "condition": {"query": "q1", "perceive_device_ids": []}},
        {"id": "r-fall", "name": "有人跌倒", "condition": {"query": "q2", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "第三方边车的陌生名字", "hit": True, "reason": "?"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.matched_rules == []


# ── 与 proxy 的接口兼容 ────────────────────────────────────────────────────


def test_engine_supports_the_lifecycle_calls_the_proxy_makes():
    """proxy / processor 会无条件调这些方法。ABC 只声明了两个抽象方法,
    漏掉任何一个都会让后端在启动或首次推理时 AttributeError —— 而只直接调用
    两个抽象方法的单测完全看不出来。"""
    import asyncio as _asyncio

    e = _engine(_FakeClient())
    e.set_main_loop(object())
    e.set_tierc_frame_provider(lambda did: None)
    e.apply_omni_fps(2)
    assert e.get_input_config() is None
    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(e.close())


@pytest.mark.asyncio
async def test_no_video_devices_is_not_recorded_as_an_error():
    """一轮里没有带画面的设备 —— 什么都没失败。填 error_code 会把它记成
    一次推理错误、污染错误率面板。"""
    # 只有音频轨的设备(如音箱):batch 非空,但没有画面可看。
    from miloco.perception.types import AudioFrame, AudioStream

    snap = _snapshot("spk1")
    snap.video = None
    snap.audio = AudioStream(
        frames=[AudioFrame(data=np.zeros(160, dtype=np.int16), timestamp=0.0)]
    )
    res = await _engine(_FakeClient()).realtime_perceive(
        BatchedSnapshot(snapshots=[snap]), []
    )
    assert res.skipped is True
    assert res.error_code is None


@pytest.mark.asyncio
async def test_result_not_skipped_when_rules_were_judged_without_hits():
    """有设备成功判过规则 = 本轮**有证据**,不能标 skipped —— 上层在 skipped 时
    直接 return,连 device_rule_map 都不看,状态机就又断供了。
    触发场景很常见:模型只回判定不写描述(caption 空)且这轮无命中。"""
    rules = [{"id": "r1", "name": "A", "condition": {"query": "q", "perceive_device_ids": []}}]
    client = _FakeClient([{
        "caption": "",   # 模型只回了判定,没写描述
        "rule_hits": [{"name": "A", "hit": False, "reason": "不成立"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.skipped is False          # 有证据 → 必须交给上层消费
    assert res.device_rule_map == {"cam1": ["r1"]}
    assert res.matched_rules == []


@pytest.mark.asyncio
async def test_gate_suppressed_and_no_hit_still_reports_evidence():
    """门控压制叙述 + 本轮无命中 —— 规则依然判过了,证据必须交上去。"""
    rules = [{"id": "r1", "name": "A", "condition": {"query": "q", "perceive_device_ids": []}}]
    client = _FakeClient([{
        "caption": "安静的客厅",
        "rule_hits": [{"name": "A", "hit": False, "reason": "不成立"}],
        "gate_p": 0.05, "backend": "codec",
    }])
    res = await _engine(client, gate_threshold=0.5).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.caption == []
    assert res.skipped is False
    assert res.device_rule_map == {"cam1": ["r1"]}


def test_frame_budget_keeps_the_last_frame():
    """均匀 floor 采样永远取不到最后一帧,等于把窗口末尾(最可能含事件的一段)
    整段丢掉,而 end_timestamp 还宣称覆盖了整个窗口。"""
    from miloco.perception.local_vision.encode import encode_snapshot_to_h264

    snap = _snapshot("cam1", frames=100)
    # 给最后一帧打上可识别的亮度,编码后仍应存在(此处只验采样索引,不解码)
    picked: list = []
    real = encode_snapshot_to_h264

    import miloco.perception.local_vision.encode as enc

    orig_resize = enc._resize_bgr

    def _spy(arr, w, h):
        picked.append(arr)
        return orig_resize(arr, w, h)

    enc._resize_bgr = _spy
    try:
        real(snap, fps=2, max_frames=8, short_edge=32)
    finally:
        enc._resize_bgr = orig_resize
    assert len(picked) == 8
