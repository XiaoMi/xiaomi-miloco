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

from types import SimpleNamespace

import numpy as np
import pytest
from miloco.perception.local_vision.engine import LocalVisionEngine
from miloco.perception.rule_scope import physical_did, rules_for_device
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _snapshot(did: str, room: str = "客厅", frames: int = 4) -> DeviceSnapshot:
    imgs = []
    for i in range(frames):
        data = np.zeros((64, 64, 3), dtype=np.uint8)
        # 每帧带自己的序号:采样是否真取到首末帧,只有帧可区分时才谈得上断言。
        data[0, 0, 0] = i % 256
        imgs.append(VideoFrame(data=data, timestamp=float(i * 1000)))
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

    # roster 是**必选**位置之外的具名参数,而不是 **kw 兜底:引擎无条件传它。
    # 这是有意的 —— 只在名册非空时才传的话,一个没跟上契约的客户端会一直正常,
    # 直到"家里真的走进来一个人"那一刻才炸,而那正是最需要它工作的时刻。
    async def perceive(self, video, rules, scene_ask=None, camera_note="",
                       max_new_tokens=256, want_gate=True, ngram_guard=None,
                       codec_target_canvas=None, roster=None, osd_watermark=False):
        from miloco.perception.local_vision.client import LocalVisionError

        self.calls.append({
            "rules": rules, "scene_ask": scene_ask,
            "camera_note": camera_note, "bytes": len(video),
            "max_new_tokens": max_new_tokens, "ngram_guard": ngram_guard,
            "codec_target_canvas": codec_target_canvas, "roster": roster,
            "osd_watermark": osd_watermark,
        })
        if self.fail:
            raise LocalVisionError("sidecar down")
        if not self.responses:
            return {"caption": "空", "rule_hits": [], "gate_p": None, "backend": "frames"}
        return self.responses.pop(0)


def _engine(client, **kw) -> LocalVisionEngine:
    return LocalVisionEngine(client, container_fps=2, **kw)


def test_fake_client_signature_matches_the_real_one():
    """替身的 perceive 签名必须与真客户端逐字一致。

    这条看着像形式主义,但它守的是一个真实事故:替身多/少一个参数时,被测代码
    在替身上跑得好好的,换成真客户端就 TypeError —— 而整套测试仍然全绿。签名是
    这两者之间**唯一**的契约,漂移必须在这里就红,不能等到线上那一窗。
    """
    import inspect

    from miloco.perception.local_vision.client import LocalVisionClient

    real = inspect.signature(LocalVisionClient.perceive)
    fake = inspect.signature(_FakeClient.perceive)
    assert [p.name for p in real.parameters.values()] == [
        p.name for p in fake.parameters.values()
    ]
    assert {n: p.default for n, p in real.parameters.items() if p.default is not p.empty} == {
        n: p.default for n, p in fake.parameters.items() if p.default is not p.empty
    }


@pytest.mark.asyncio
async def test_token_budget_grows_with_rule_count():
    """生成预算必须随规则条数放大。

    配置里的 max_new_tokens 是给场景描述定的;每条规则还要额外产出一行判定。
    不放大的话,规则一多就必然截断在判定块中间,而缺失的判定会被 fail-closed
    读成「未命中」—— 表现为"规则越多、越靠后的越容易静默失效",且无任何报错。
    """
    rules = [
        {"id": f"r{i}", "name": f"名字{i}",
         "condition": {"query": "有人", "perceive_device_ids": []}}
        for i in range(5)
    ]
    client = _FakeClient([{"caption": "x", "rule_hits": [],
                           "gate_p": None, "backend": "codec"}])
    await _engine(client, max_new_tokens=200).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    call = client.calls[0]
    assert len(call["rules"]) == 5
    assert call["max_new_tokens"] > 200, "预算没随规则数放大,末尾规则会被截断吃掉"


@pytest.mark.asyncio
async def test_on_demand_relaxes_the_repetition_guard():
    """主动查询要放宽复读硬禁。

    默认那个 n 是为定时场景描述调出来的;主动查询的提问由 agent 现编,答案里
    合理复现一个短句很常见,沿用严格值会把正常回答掐断。
    """
    client = _FakeClient([{"caption": "门是关着的", "rule_hits": [],
                           "gate_p": None, "backend": "frames"}])
    await _engine(client).on_demand_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), "门关了吗?"
    )
    assert client.calls[0]["ngram_guard"] == 32
    # 定时通路保持默认(由边车按有无规则自适应),不被这条覆盖。
    client2 = _FakeClient([{"caption": "x", "rule_hits": [],
                            "gate_p": None, "backend": "codec"}])
    await _engine(client2).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert client2.calls[0]["ngram_guard"] is None


@pytest.mark.asyncio
async def test_camera_note_is_sent_separately_not_folded_into_scene_ask(monkeypatch):
    """机位须知必须走独立字段。折进 scene_ask 有两个后果:默认配置下 scene_ask 为空,
    折出来只剩用户那句话、任务提问整个消失;而且它会落在输出格式约定之前,一句
    「只用一句话回答」就能让规则行不再出现,fail-closed 随后把它变成"该相机所有
    规则静默失效"。"""
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "camera_prompt_map", lambda: {"cam1": "这台对着门口"})
    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")
    client = _FakeClient([{"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    call = client.calls[0]
    assert call["camera_note"] == "这台对着门口"
    assert call["scene_ask"] is None        # 未被须知顶替


# ── 规则定向 ──────────────────────────────────────────────────────────────


def test_physical_did_strips_channel():
    assert physical_did("cam1:ch0") == "cam1"
    assert physical_did("cam1") == "cam1"


def test_rules_for_device_broadcast_and_targeted():
    rules = [
        {"id": "r1", "name": "广播", "condition": {"query": "q", "perceive_device_ids": []}},
        {"id": "r2", "name": "仅A", "condition": {"query": "q", "perceive_device_ids": ["camA"]}},
        {"id": "r3", "name": "绑物理机", "condition": {"query": "q", "perceive_device_ids": ["camB"]}},
    ]
    assert [r["id"] for r in rules_for_device(rules, "camA")] == ["r1", "r2"]
    # 规则绑整台相机的物理 did 时,该机任一通道都要命中
    assert [r["id"] for r in rules_for_device(rules, "camB:ch1")] == ["r1", "r3"]


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
    res = await _engine(client, event_gate_threshold=0.5).realtime_perceive(
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

    from miloco.config.settings import get_settings

    # 必须用**代码默认值**构造的 settings,不能读环境里那份 —— 开发机上跑着的
    # config.json 可能已经切到 local,那样这条测试就会随环境时绿时红。
    cfg = get_settings().model_copy(deep=True)
    cfg.perception.engine_backend = PerceptionSettings().engine_backend
    with patch("miloco.perception.client.get_settings", return_value=cfg), \
            patch.object(PerceptionEngineProxy, "_init_local_engine") as local_init, \
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
    res = await _engine(client, event_gate_threshold=0.5).realtime_perceive(
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


@pytest.mark.asyncio
async def test_blank_name_hit_is_dropped_not_index_matched():
    """空 name 不能落到索引兜底 —— 只回命中项的第三方边车会让「厨房明火」背上
    「有人跌倒」的判定,正是注释里警告的那个灾难。"""
    rules = [
        {"id": "r-fire", "name": "厨房明火", "condition": {"query": "q1", "perceive_device_ids": []}},
        {"id": "r-fall", "name": "有人跌倒", "condition": {"query": "q2", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "", "hit": True, "reason": "有人倒在地上"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert res.matched_rules == []

    # 上面那半其实是名字查找失败的兜底(规则都有名字,查不到自然丢弃)。守卫真正
    # 起作用的是**规则自己也没有名字**的时候 —— 此时按索引兜底就会把第一条规则
    # 认成命中。规则的 name 是可空的(engine 里取的是 r.get("name", "")),所以
    # 这不是构造出来的假想情形。
    nameless = [
        {"id": "r-fire", "name": "", "condition": {"query": "q1", "perceive_device_ids": []}},
        {"id": "r-fall", "name": "", "condition": {"query": "q2", "perceive_device_ids": []}},
    ]
    client2 = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "", "hit": True, "reason": "有人倒在地上"}],
        "gate_p": None, "backend": "codec",
    }])
    res2 = await _engine(client2).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), nameless
    )
    assert res2.matched_rules == [], "空名判定按索引认领了一条规则"


# ── 与 proxy 的接口兼容 ────────────────────────────────────────────────────


def test_engine_supports_the_lifecycle_calls_the_proxy_makes():
    """proxy / processor 会无条件调这些方法。ABC 只声明了两个抽象方法,
    漏掉任何一个都会让后端在启动或首次推理时 AttributeError —— 而只直接调用
    两个抽象方法的单测完全看不出来。"""
    import asyncio as _asyncio

    from miloco.perception.engine_base import BasePerceptionEngine

    e = LocalVisionEngine(_FakeClient(), container_fps=3)
    e.set_main_loop(object())
    e.set_tierc_frame_provider(lambda did: None)
    e.apply_omni_fps(2)

    cfg = e.get_input_config()
    # 返回 None 会让面板把三层帧率都显示成 0fps。本通路确有一个真实的送检帧率,
    # 显示 0 是错的而不是"没有";omni_fps 才是真的没有对应物。
    assert cfg is not None and cfg.fps == 3 and cfg.omni_fps == 0

    # 注意不能用 hasattr 对着基类公开面断言:LocalVisionEngine 继承自它,
    # 那样的 missing 永远是空,是一条不可能失败的断言。
    # 真正要守的是:抽象方法必须被实现,且本引擎语义不同的那几个必须**覆盖**掉
    # 基类的无害默认值(否则面板/上层拿到的是 None 而不是真值)。
    assert not getattr(LocalVisionEngine, "__abstractmethods__", frozenset())
    for name in ("get_input_config", "realtime_perceive", "on_demand_perceive"):
        assert getattr(LocalVisionEngine, name) is not getattr(
            BasePerceptionEngine, name, None
        ), f"{name} 没有被本地引擎覆盖,会拿到基类的占位实现"

    # 反过来,proxy/processor 无条件调用的那几个必须存在(继承来的也算)。
    for name in ("close", "set_main_loop", "set_tierc_frame_provider", "apply_omni_fps"):
        assert callable(getattr(e, name)), name


    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(e.close())
    finally:
        loop.close()


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
    res = await _engine(client, event_gate_threshold=0.5).realtime_perceive(
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
    # 只断言数量的话,把端点包含式采样换回 floor 采样(永远取不到 n-1)也照样绿。
    # 首末两帧才是这条不变量本身。
    assert picked[0][0, 0, 0] == 0, "丢掉了窗口的第一帧"
    assert picked[-1][0, 0, 0] == 99, "丢掉了窗口的最后一帧 —— floor 采样的典型症状"


def test_frame_budget_of_one_takes_the_newest_frame():
    """预算只有一帧时要最新那帧。取 frames[0] 会把一个 4 秒窗口压成它开始的瞬间。"""
    import miloco.perception.local_vision.encode as enc
    from miloco.perception.local_vision.encode import encode_snapshot_to_h264

    picked: list = []
    orig = enc._resize_bgr
    enc._resize_bgr = lambda a, w, h: (picked.append(a), orig(a, w, h))[1]
    try:
        encode_snapshot_to_h264(_snapshot("cam1", frames=100), fps=2,
                                max_frames=1, short_edge=32)
    finally:
        enc._resize_bgr = orig
    assert [p[0, 0, 0] for p in picked] == [99]


def test_local_resolution_is_configured_not_inherited():
    """本通路的短边来自 local_vision.video_short_edge,不再复用云端那个同名参数。"""
    e = _engine(_FakeClient(), short_edge=384)
    assert e._resolve_short_edge() == 384
    assert _engine(_FakeClient(), short_edge=256)._resolve_short_edge() == 256


@pytest.mark.asyncio
async def test_encoding_runs_off_the_event_loop():
    """libx264 是同步 CPU 活,必须在线程里跑。直接在协程里编码会占住所在事件
    循环 —— 主动查询走的正是主循环。项目的 miot/transcoder.py 为同一理由专门
    建了执行器,这里必须遵循同一约定。"""
    import threading

    loop_thread = threading.get_ident()
    seen: dict = {}

    def _fake_encode(snapshot, fps, crf, max_frames, short_edge):
        seen["thread"] = threading.get_ident()
        # 顺带钉住位置参数顺序:这几个是按位置传的,顺序错了不会报错,
        # 只会静默地把分辨率/画质设成别的值。
        seen["args"] = (fps, crf, max_frames, short_edge)
        return b"VIDEO"

    import miloco.perception.local_vision.engine as eng

    orig = eng.encode_snapshot_to_h264
    eng.encode_snapshot_to_h264 = _fake_encode
    try:
        client = _FakeClient([{"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}])
        await _engine(client, crf=30, max_frames=16, short_edge=384).realtime_perceive(
            BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
        )
    finally:
        eng.encode_snapshot_to_h264 = orig
    assert seen["thread"] != loop_thread, "编码发生在事件循环线程上"
    # 整条元组一起断言。只断言其中两个的话,把 crf 和 short_edge 对调(正是这段
    # 注释警告的那种错)照样绿 —— 而后果是画质与分辨率被静默换成对方的值。
    assert seen["args"] == (2, 30, 16, 384)


@pytest.mark.asyncio
async def test_local_resolution_ignores_the_cloud_setting_end_to_end(monkeypatch):
    """送到编码器的那个值,必须来自本通路自己的设置,而不是云端那个同名参数。

    两条通路的成本结构相反:云端每多一帧、每多一个像素都要付 token 费用;本通路
    的成本由 codec 的 token 预算封顶,分辨率高了只让每个 canvas 更贵而收益递减。
    共用一个旋钮会逼两边往相反方向调同一个值。
    """
    import miloco.perception.local_vision.engine as eng
    from miloco.config.settings import get_settings

    seen: list = []
    monkeypatch.setattr(
        eng, "encode_snapshot_to_h264",
        lambda snap, fps, crf, mf, se: (seen.append(se), b"V")[1],
    )
    cfg = get_settings().model_copy(deep=True)
    cfg.perception.engine.setdefault("input", {})["video_short_edge"] = 1080
    monkeypatch.setattr("miloco.config.get_settings", lambda: cfg)

    e = _engine(_FakeClient([
        {"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"},
    ]), short_edge=384)
    await e.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), [])

    assert seen == [384], f"本通路跟着云端的分辨率走了: {seen}"


# ── 本地通路不直接控制设备:靠**拒绝切换**兑现,而不是运行时改写用户的自动化 ──


def test_switch_to_local_is_blocked_by_enabled_direct_device_rules():
    """有启用中的 STATIC 规则时必须拒绝切换,并把规则名说出来。

    此前的做法是运行时把 STATIC 改写成"交给 agent" —— 那会静默丢掉 cooldown /
    idempotent(唯一的限流)和台账里 source=rule 的归属,还得把动作重新翻译成
    自然语言。宁可在切换这一步拦下来。
    """
    from unittest.mock import patch

    from miloco.admin import router as admin

    def _r(rid, name, enabled, **kw):
        return SimpleNamespace(
            id=rid, name=name, enabled=enabled,
            actions=kw.get("actions", []),
            on_enter_actions=kw.get("on_enter_actions", []),
            on_exit_actions=kw.get("on_exit_actions", []),
        )

    all_rules = [
        _r("r1", "厨房明火关阀", True, actions=[object()]),
        _r("r2", "纯动态", True),                                   # 没有动作
        _r("r3", "夜间关灯", True, on_exit_actions=[object()]),      # 状态规则的退出动作
        _r("r4", "已停用的直连", False, actions=[object()]),          # 停用的不该拦
    ]

    class _Repo:
        """按 enabled_only 真正过滤 —— 这个参数曾经在测试里被忽略,于是
        「只看启用中的规则」这半条契约完全没有被验证:一条**停用**的直连规则
        会平白挡住切换,而没有任何测试会红。"""

        def get_all(self, enabled_only=False):
            return [r for r in all_rules if r.enabled or not enabled_only]

    with patch("miloco.database.rule_repo.RuleRepo", _Repo):
        names = admin._rules_with_direct_device_actions()
    assert names == ["厨房明火关阀", "夜间关灯"]
    assert "纯动态" not in names, "没有直连动作的规则不该挡住切换"
    assert "已停用的直连" not in names, "停用的规则不该挡住切换"


def test_rule_lookup_failure_does_not_block_switching():
    """附带检查本身出问题时不该挡住切换 —— 它是保护,不是闸门。"""
    from unittest.mock import patch

    from miloco.admin import router as admin

    class _Boom:
        def get_all(self, enabled_only=False):
            raise RuntimeError("db down")

    with patch("miloco.database.rule_repo.RuleRepo", _Boom):
        assert admin._rules_with_direct_device_actions() == []


def test_health_probe_sends_the_configured_credentials():
    """探活不带凭证的话,边车回的 auth_ok 恒为 false —— 任何配了 token 的部署都会
    被判成"凭证不被接受"而永远切不过去。这条曾真的发生过,且只有实机能发现:
    上层测试把 health_sync 整个 mock 掉了,看不到它到底发了什么。"""
    import httpx
    from miloco.perception.local_vision import LocalVisionClient

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "ok", "model_loaded": True,
                                         "auth_required": True, "auth_ok": True})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    class _Patched(real_client):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    httpx.Client = _Patched
    try:
        out = LocalVisionClient("http://sidecar:18800", token="secret").health_sync()
    finally:
        httpx.Client = real_client

    assert seen["auth"] == "Bearer secret"
    assert out["auth_ok"] is True


def test_health_probe_sends_no_auth_header_when_no_token_configured():
    import httpx
    from miloco.perception.local_vision import LocalVisionClient

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "ok", "model_loaded": True})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    class _Patched(real_client):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    httpx.Client = _Patched
    try:
        LocalVisionClient("http://sidecar:18800").health_sync()
    finally:
        httpx.Client = real_client

    assert seen["auth"] is None


# ── 降级路径:每一条"该设备本窗跳过"都要真的被走过 ────────────────────────


@pytest.mark.asyncio
async def test_encode_failure_skips_the_device_without_faking_evidence(monkeypatch):
    """编码失败(磁盘/编码器问题)不该变成一条"什么都没看到"的事件。"""
    import miloco.perception.local_vision.engine as eng
    from miloco.perception.local_vision.encode import EncodeError

    def _boom(*a, **k):
        raise EncodeError("libx264 unavailable")

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", _boom)
    client = _FakeClient()
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert client.calls == [], "编码都失败了却还发了推理请求"
    assert res is not None and res.skipped is True
    assert res.device_rule_map == {}
    assert res.timing.get("_omni_error_cam1") == "encode_failed"


@pytest.mark.asyncio
async def test_malformed_sidecar_response_degrades_only_that_device(monkeypatch):
    """契约对第三方实现开放。回一个数组会在 .get() 处抛 AttributeError,
    穿透"任一环节失败 → 该设备跳过"的设计,把整窗所有设备一起毁掉。

    响应按**设备**寻址,不能按调用顺序:两台相机的编码跑在线程池里,完成顺序
    不定,用队列 pop 会让"谁拿到畸形响应"随机漂移 —— 那是一条会偶发的假测试。
    """
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(
        eng, "encode_snapshot_to_h264",
        lambda snap, *a, **k: snap.device.did.encode(),
    )

    class _ByDid(_FakeClient):
        async def perceive(self, video, rules, **kw):
            await super().perceive(video, rules, **kw)
            return (["not", "an", "object"] if video == b"cam1"
                    else {"caption": "客厅有人", "rule_hits": [],
                          "gate_p": None, "backend": "codec"})

    client = _ByDid()
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1"), _snapshot("cam2", room="客厅")]), []
    )
    assert res is not None and res.skipped is False
    assert [c.description for c in res.caption] == ["客厅有人"]
    assert res.timing.get("_omni_error_cam1") == "malformed_response"


@pytest.mark.asyncio
async def test_per_device_sidecar_failure_is_recorded_not_swallowed():
    """一台相机每窗都失败时,必须在 timing 里留下痕迹 —— 否则观测面板显示
    "零错误",而它上面的规则从此不再被评估,没有任何人会发现。"""
    client = _FakeClient(fail=True)
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res.skipped is True
    assert res.timing.get("_omni_error_cam1") == "LocalVisionError"


@pytest.mark.asyncio
async def test_clip_bytes_are_attached_to_the_event(monkeypatch):
    """云端在 omni 内部挂片段;本地绕过了 omni。不补的话日志页里每条事件都只剩
    文字、没有视频,而这个缺失既不在能力声明里也没有任何报错。"""
    import miloco.perception.local_vision.engine as eng
    from miloco.perception.snapshot_context import (
        OmniEventArtifacts,
        event_artifacts_scope,
    )

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"VIDEOBYTES")
    client = _FakeClient([{"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    artifacts = OmniEventArtifacts()
    with event_artifacts_scope(artifacts):
        await _engine(client).realtime_perceive(
            BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
        )
    assert artifacts.clips.get("cam1") == (b"VIDEOBYTES", "mp4")


# ── 主动查询的降级路径 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_demand_returns_none_when_the_sidecar_fails():
    out = await _engine(_FakeClient(fail=True)).on_demand_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), "现在有人吗?"
    )
    assert out is None


@pytest.mark.asyncio
async def test_on_demand_returns_none_on_an_empty_batch():
    assert await _engine(_FakeClient()).on_demand_perceive(
        BatchedSnapshot(snapshots=[]), "有人吗?"
    ) is None


@pytest.mark.asyncio
async def test_on_demand_returns_none_when_every_answer_is_blank():
    """空答案比没有答案更糟:agent 会把它当成"确实什么都没有"。"""
    out = await _engine(_FakeClient([
        {"caption": "   ", "rule_hits": [], "gate_p": None, "backend": "codec"},
    ])).on_demand_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), "有人吗?")
    assert out is None


@pytest.mark.asyncio
async def test_on_demand_labels_answers_by_room_when_multiple_cameras():
    """多相机时不标房间的话,agent 拿到两句互相矛盾的描述,无从判断说的是哪儿。"""
    client = _FakeClient([
        {"caption": "有人在看电视", "rule_hits": [], "gate_p": None, "backend": "codec"},
        {"caption": "没有人", "rule_hits": [], "gate_p": None, "backend": "codec"},
    ])
    out = await _engine(client).on_demand_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1", room="客厅"),
                                   _snapshot("cam2", room="书房")]), "有人吗?"
    )
    assert "客厅" in out.answer and "书房" in out.answer


# ── 客户端本体(替身平时顶替的那个真东西) ────────────────────────────────


def _mock_transport(handler):
    """把 httpx.AsyncClient 换成走 MockTransport 的版本。"""
    import httpx

    real = httpx.AsyncClient

    class _Patched(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return real, _Patched


@pytest.mark.asyncio
async def test_client_encodes_video_and_omits_empty_optionals():
    import httpx
    from miloco.perception.local_vision import LocalVisionClient

    seen: dict = {}

    def _h(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"caption": "ok", "rule_hits": []})

    real, patched = _mock_transport(_h)
    httpx.AsyncClient = patched
    try:
        await LocalVisionClient("http://s:1").perceive(b"BYTES", rules=[])
    finally:
        httpx.AsyncClient = real

    import base64 as _b64
    assert _b64.b64decode(seen["video_b64"]) == b"BYTES"
    # 空的可选字段不该出现:边车侧对 scene_ask=None 有自己的默认提问,送一个
    # 空串过去会把那个默认顶掉。
    assert "scene_ask" not in seen and "camera_note" not in seen
    assert "ngram_guard" not in seen


@pytest.mark.asyncio
async def test_client_turns_http_errors_into_local_vision_error():
    """异常若原样冒出去,会穿透"该设备跳过"的降级设计,毁掉整窗。"""
    import httpx
    from miloco.perception.local_vision import LocalVisionClient, LocalVisionError

    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "busy: too many in-flight"})

    real, patched = _mock_transport(_h)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(LocalVisionError) as ei:
            await LocalVisionClient("http://s:1").perceive(b"B", rules=[])
    finally:
        httpx.AsyncClient = real
    assert "503" in str(ei.value) and "busy" in str(ei.value)


@pytest.mark.asyncio
async def test_client_turns_transport_errors_into_local_vision_error():
    import httpx
    from miloco.perception.local_vision import LocalVisionClient, LocalVisionError

    def _h(request: httpx.Request):
        raise httpx.ConnectError("no route to host")

    real, patched = _mock_transport(_h)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(LocalVisionError):
            await LocalVisionClient("http://s:1").perceive(b"B", rules=[])
    finally:
        httpx.AsyncClient = real


def test_health_sync_turns_failures_into_local_vision_error():
    import httpx
    from miloco.perception.local_vision import LocalVisionClient, LocalVisionError

    real = httpx.Client

    class _Patched(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(
                lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            )
            super().__init__(*a, **kw)

    httpx.Client = _Patched
    try:
        with pytest.raises(LocalVisionError):
            LocalVisionClient("http://s:1").health_sync()
    finally:
        httpx.Client = real


@pytest.mark.asyncio
async def test_events_carry_the_time_window_in_deploy_timezone():
    """事件文本里的「时间」字段取自 time_window。不填的话每条本地事件都少一行时间,
    而 agent 判断作息、判断"刚才"是何时全靠它。

    格式化必须复用云端那一份(走 deploy_timezone),不能自己再写一遍 —— 用裸
    fromtimestamp 会在 UTC 主机上把北京 10:52 说成「凌晨02:52」,这是发生过的事故。
    """
    rules = [{"id": "r1", "name": "有人", "condition": {"query": "有人", "perceive_device_ids": []}}]
    client = _FakeClient([{
        "caption": "有人在看书",
        "rule_hits": [{"name": "有人", "hit": True, "reason": "有人"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    # 断言**值**,不是形状:形状正则分不出任何两个时区,而"把北京 10:52 说成
    # 凌晨 02:52"恰恰是形状完全合法、值全错。
    from miloco.perception.engine.pipeline import _fmt_time_window

    snap = _snapshot("cam1")
    expected = _fmt_time_window(snap.start_timestamp, snap.end_timestamp)
    assert res.caption[0].time_window == expected
    assert res.matched_rules[0].time_window == expected


@pytest.mark.asyncio
async def test_time_window_follows_the_deployment_timezone_not_the_process_clock(
    monkeypatch,
):
    """换一个部署时区,输出必须跟着变。

    裸 fromtimestamp 的实现在这条上会纹丝不动 —— 它读的是进程/OS 时钟。UTC 主机
    上因此把北京 10:52 标成「凌晨02:52」,agent 据此判断作息,是发生过的事故。
    """
    from datetime import timedelta, timezone

    import miloco.perception.engine.pipeline as pl

    seen: list = []
    client = _FakeClient([
        {"caption": "有人", "rule_hits": [], "gate_p": None, "backend": "codec"},
        {"caption": "有人", "rule_hits": [], "gate_p": None, "backend": "codec"},
    ])
    e = _engine(client)
    batch = BatchedSnapshot(snapshots=[_snapshot("cam1")])

    for offset in (0, 8):
        monkeypatch.setattr(
            pl, "deploy_timezone", lambda o=offset: timezone(timedelta(hours=o))
        )
        res = await e.realtime_perceive(batch, [])
        seen.append(res.caption[0].time_window)

    assert seen[0] != seen[1], f"换了部署时区,时间窗却没变:{seen}"


def test_health_accepts_json_ints_for_boolean_fields():
    """契约对第三方实现开放,而 JSON 里用 0/1 表示布尔是完全自然的写法。

    丢掉它会让 auth_ok=0 变成"字段缺失",而缺失被读作"不需要鉴权" —— 一个
    fail-open 的判定,恰是这套字段存在意义的反面。
    """
    from miloco.perception.local_vision.client import _sanitize_health

    out = _sanitize_health({
        "status": "ok", "model_loaded": 1, "gate_available": 0,
        "auth_required": 1, "auth_ok": 0,
    })
    assert out["model_loaded"] is True
    assert out["gate_available"] is False
    assert out["auth_required"] is True and out["auth_ok"] is False


@pytest.mark.asyncio
async def test_token_budget_is_capped_at_the_sidecar_limit():
    """规则再多,预算也不能越过边车接受的上限。

    越过了每一窗都会 422,而在日志里与其它边车故障长得一模一样。上限的字面值
    分属两个独立部署的构件(见 engine 里的说明),这里至少保证本侧不会算超。
    """
    from miloco.perception.local_vision.engine import _MAX_NEW_TOKENS_CEILING

    rules = [
        {"id": f"r{i}", "name": f"n{i}",
         "condition": {"query": "有人", "perceive_device_ids": []}}
        for i in range(40)
    ]
    client = _FakeClient([{"caption": "x", "rule_hits": [],
                           "gate_p": None, "backend": "codec"}])
    await _engine(client, max_new_tokens=1000).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert client.calls[0]["max_new_tokens"] == _MAX_NEW_TOKENS_CEILING
    # 字面值也钉住:两侧各测各的话,任一侧单独改动都不会红,而后果是每一窗 422。
    # 边车侧有一条对称的测试(1025 必须被拒),两条一起才构成契约。
    assert _MAX_NEW_TOKENS_CEILING == 1024


@pytest.mark.asyncio
async def test_http_connection_pool_is_reused_across_windows():
    """每次推理新建再销毁连接池的话,4 台相机 × 4 秒窗 = 每秒一次 TCP 握手,
    对边车永远拿不到 keep-alive。"""
    import httpx
    from miloco.perception.local_vision import LocalVisionClient

    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"caption": "ok", "rule_hits": []})

    real, patched = _mock_transport(_h)
    httpx.AsyncClient = patched
    try:
        c = LocalVisionClient("http://s:1")
        await c.perceive(b"A", rules=[])
        first = c._async_client
        await c.perceive(b"B", rules=[])
        assert c._async_client is first, "每次推理都新建了连接池"
        assert not first.is_closed

        await c.aclose()
        assert first.is_closed, "close 之后连接池没有被释放"
    finally:
        httpx.AsyncClient = real


@pytest.mark.asyncio
async def test_engine_close_releases_the_connection_pool():
    """proxy 在停引擎时会调 close();不接上的话连接池会跟着旧引擎实例泄漏。"""
    closed: list = []

    class _C(_FakeClient):
        async def aclose(self):
            closed.append(True)

    await _engine(_C()).close()
    assert closed == [True]


# ── 边车整体不可达时的退避 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_total_failure_backs_off_instead_of_burning_cpu(monkeypatch):
    """引擎已建好时 tick 的探活会 no-op,于是边车挂掉后每个窗口仍然照常给每台相机
    做一次 libx264 编码(同步 CPU 活)再去连一个死掉的端口 —— 4 台相机就是每 4 秒
    4 次白烧。实机观察到的正是这个。云端通路对同类情形有熔断器。"""
    import miloco.perception.local_vision.engine as eng

    encodes: list = []
    monkeypatch.setattr(
        eng, "encode_snapshot_to_h264",
        lambda *a, **k: (encodes.append(1), b"V")[1],
    )
    e = _engine(_FakeClient(fail=True))
    batch = BatchedSnapshot(snapshots=[_snapshot("cam1")])

    for _ in range(3):
        res = await e.realtime_perceive(batch, [])
        assert res.skipped is True
    assert len(encodes) == 3, "退避之前每窗都该照常试"

    # 第 4 窗起进入退避:不再编码,也不再发请求。
    before = len(encodes)
    res = await e.realtime_perceive(batch, [])
    assert res.skipped is True
    assert res.error_code == "local_vision_unavailable"
    assert len(encodes) == before, "退避窗口内仍在编码"


@pytest.mark.asyncio
async def test_backoff_clears_as_soon_as_the_sidecar_answers(monkeypatch):
    """边车重启一般十几秒就好。恢复必须是即时的 —— 一次成功就把退避清零。"""
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")
    e = _engine(_FakeClient(fail=True))
    batch = BatchedSnapshot(snapshots=[_snapshot("cam1")])
    for _ in range(3):
        await e.realtime_perceive(batch, [])
    assert e._backoff_until > 0

    e._backoff_until = 0.0            # 模拟退避到点,放一窗过去试探
    e._client = _FakeClient([{"caption": "客厅有人", "rule_hits": [],
                              "gate_p": None, "backend": "codec"}])
    res = await e.realtime_perceive(batch, [])
    assert res.skipped is False
    assert e._consecutive_failures == 0 and e._backoff_until == 0.0


@pytest.mark.asyncio
async def test_connection_pool_is_rebuilt_when_the_event_loop_changes():
    """AsyncClient 绑定到创建时所在的 loop;那个 loop 关闭后继续用会抛
    "Event loop is closed"。而 InferenceWorker 每次 start() 都建一个新 loop。

    此前只靠 close() 在换代之前把它置空 —— 一条隔着三个模块的不变量,不是这个类
    自己的性质。仓库为同一件事写过 _get_fused_http_client。
    """
    import asyncio as _asyncio

    from miloco.perception.local_vision import LocalVisionClient

    c = LocalVisionClient("http://s:1")
    first = c._client()
    assert c._client() is first, "同一个 loop 内应复用"

    # 换一个 loop:必须重建,而不是把绑在旧 loop 上的那个接着用。
    # asyncio.run 不能在已有 loop 里调,所以放到线程里跑一个独立的 loop。
    import concurrent.futures

    async def _second():
        return c._client()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        second = pool.submit(lambda: _asyncio.run(_second())).result()
    assert second is not first, "换了 event loop 却仍在用绑定到旧 loop 的连接池"


@pytest.mark.asyncio
async def test_warns_once_when_a_window_outruns_the_perception_period(monkeypatch, caplog):
    """边车用一把锁串行推理,所以窗口耗时约等于各相机之和 —— 相机一多就追不上
    周期,而症状是"事件变稀疏"这种很难联想到原因的表现。

    只提示一次:这是个容量结论,不是每窗都要重复的事件。
    """
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")

    class _Slow(_FakeClient):
        async def perceive(self, video, rules, **kw):
            import asyncio as _a
            await _a.sleep(0.02)
            return {"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}

    # 不去 patch time.monotonic:那是 time 模块本身的属性,事件循环也用它 ——
    # 冻住它会让 asyncio.sleep 永远不返回(我第一版就是这么挂死的)。改成把感知
    # 周期设成极小值,任何真实耗时都会超过它。
    e = _engine(_Slow())
    monkeypatch.setattr(
        e, "get_input_config", lambda: SimpleNamespace(fps=4, omni_fps=0, period_sec=0.001)
    )
    batch = BatchedSnapshot(snapshots=[_snapshot("cam1")])

    with caplog.at_level("WARNING"):
        await e.realtime_perceive(batch, [])
        await e.realtime_perceive(batch, [])

    hits = [r for r in caplog.records if "超过感知周期" in r.getMessage()]
    assert len(hits) == 1, f"同样的相机台数应当只提示一次,实际 {len(hits)} 次"
    assert "相机" in hits[0].getMessage()


@pytest.mark.asyncio
async def test_adding_a_camera_re_arms_the_capacity_warning(monkeypatch, caplog):
    """相机台数变了就要重新提示。

    实测撞到过:冷启动时单相机偶尔超一次,把"只提示一次"的额度用掉;之后用户
    加了一台相机——一个全新的、而且是稳定的容量问题——反而一声不吭。
    """
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")

    class _Slow(_FakeClient):
        async def perceive(self, video, rules, **kw):
            import asyncio as _a
            await _a.sleep(0.02)
            return {"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}

    e = _engine(_Slow())
    monkeypatch.setattr(
        e, "get_input_config", lambda: SimpleNamespace(fps=4, omni_fps=0, period_sec=0.001)
    )
    one = BatchedSnapshot(snapshots=[_snapshot("cam1")])
    two = BatchedSnapshot(snapshots=[_snapshot("cam1"), _snapshot("cam2", room="次卧")])

    with caplog.at_level("WARNING"):
        await e.realtime_perceive(one, [])
        await e.realtime_perceive(one, [])   # 同样台数,不再提示
        await e.realtime_perceive(two, [])   # 台数变了,必须重新提示

    hits = [r for r in caplog.records if "超过感知周期" in r.getMessage()]
    assert len(hits) == 2, f"加相机后没有重新提示(实际 {len(hits)} 条)"
    assert "1 台" in hits[0].getMessage() and "2 台" in hits[1].getMessage()


# ── 认人接线 ──────────────────────────────────────────────────────────────
#
# 上面 test_identity.py 测的是 resolver 自己算得对不对;这里测的是它**真的被接上了**
# —— 名册有没有随请求发出去、失败时会不会把整窗拖下水。两者都错过的话,认人在单测
# 里全绿而线上一个名字都出不来。


class _StubIdentity:
    def __init__(self, hits=None, boom: bool = False):
        self._hits = hits or []
        self._boom = boom
        self.calls = 0

    def resolve(self, frames, source=""):
        self.calls += 1
        self.last_source = source
        if self._boom:
            raise RuntimeError("reid exploded")
        return self._hits


def _hit(name: str, bbox=(1, 2, 3, 4), score: float = 0.9):
    from miloco.perception.local_vision.identity import PersonHit

    return PersonHit("pid-" + name, name, None, bbox, score)


@pytest.mark.asyncio
async def test_roster_reaches_the_sidecar_request():
    client = _FakeClient([{"caption": "有人在看书", "rule_hits": [], "gate_p": None,
                           "backend": "codec"}])
    eng = _engine(client, identity=_StubIdentity([_hit("小亮", (357, 242, 467, 785))]))
    await eng.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules=[])
    assert client.calls[0]["roster"] == [{"name": "小亮", "bbox": [357, 242, 467, 785]}]


@pytest.mark.asyncio
async def test_identity_failure_does_not_lose_the_window():
    """认人是旁路 —— 它炸了,这一窗的画面理解与规则判定必须照常产出。

    反过来(异常冒泡)的后果是:身份库里出现一个坏文件,这台相机从此整个不工作,
    而用户失去的本来只是"名字"这一项。
    """
    client = _FakeClient([{"caption": "有人在看书", "rule_hits": [], "gate_p": None,
                           "backend": "codec"}])
    eng = _engine(client, identity=_StubIdentity(boom=True))
    res = await eng.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules=[])
    assert res is not None and res.caption
    assert client.calls[0]["roster"] == []


@pytest.mark.asyncio
async def test_no_identity_layer_sends_an_empty_roster_not_none():
    """未开启认人时行为必须与改动前一致 —— 边车侧 roster=[] 与不传等价。"""
    client = _FakeClient([{"caption": "空", "rule_hits": [], "gate_p": None,
                           "backend": "codec"}])
    eng = _engine(client, identity=None)
    await eng.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules=[])
    assert client.calls[0]["roster"] == []


@pytest.mark.asyncio
async def test_identity_cost_is_visible_in_timing():
    """认人是新加的串行开销。混进 total 的话,它变慢只会表现为"感知整体变慢",
    查不到头上 —— 面板上必须能单独看到这一项。"""
    client = _FakeClient([{"caption": "有人", "rule_hits": [], "gate_p": None,
                           "backend": "codec"}])
    eng = _engine(client, identity=_StubIdentity([_hit("小亮")]))
    res = await eng.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules=[])
    assert any(k.startswith("identity_") and k.endswith("_ms") for k in res.timing)


# ── 时间水印开关 ──────────────────────────────────────────────────────────
#
# 挂上「忽略时间水印」这句话的代价是**压掉屋里真实存在的钟**(实测 30 段配对
# 8/30 → 1/30,McNemar p=0.039)。所以"不确定时不挂"是有依据的方向,不是随手
# 选的保守值:漏挂只是退回本来就存在的"模型可能读错水印",误挂却会主动删掉
# 画面里的真实信息。


@pytest.mark.asyncio
async def test_watermark_flag_defaults_off_when_the_property_is_unreadable(monkeypatch):
    """非小米相机、机型没这个属性、账号未绑定、网络抖动 —— 都该落在"当作没有"。"""
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")
    client = _FakeClient([{"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    eng_obj = _engine(client)
    # 读属性会因为没有 manager 而抛 —— 正是要覆盖的那条路
    await eng_obj.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), [])
    assert client.calls[0]["osd_watermark"] is False


@pytest.mark.asyncio
async def test_watermark_flag_is_cached_per_camera(monkeypatch):
    """这是用户在米家里设的**配置项**,不是状态 —— 每窗一次 MIoT 往返会给常驻
    感知平白加一次网络依赖。"""
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")
    calls = []

    async def _fake_status(did, iids):
        calls.append(did)
        return {"properties": [{"iid": "prop.2.5", "value": True, "code": 0}]}

    eng_obj = _engine(_FakeClient([
        {"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"},
        {"caption": "y", "rule_hits": [], "gate_p": None, "backend": "codec"},
    ]))
    monkeypatch.setattr(eng_obj, "_has_osd_watermark", eng_obj._has_osd_watermark)
    import miloco.manager as mgr
    monkeypatch.setattr(
        mgr, "manager",
        SimpleNamespace(miot_service=SimpleNamespace(get_device_status=_fake_status)),
        raising=False,
    )
    for _ in range(2):
        await eng_obj.realtime_perceive(BatchedSnapshot(snapshots=[_snapshot("cam1")]), [])
    assert len(calls) == 1, f"应只查一次,实际 {len(calls)} 次"


@pytest.mark.asyncio
async def test_watermark_flag_reaches_the_sidecar_when_the_camera_has_one(monkeypatch):
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")

    async def _fake_status(did, iids):
        return {"properties": [{"iid": "prop.2.5", "value": True, "code": 0}]}

    import miloco.manager as mgr
    monkeypatch.setattr(
        mgr, "manager",
        SimpleNamespace(miot_service=SimpleNamespace(get_device_status=_fake_status)),
        raising=False,
    )
    client = _FakeClient([{"caption": "x", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert client.calls[0]["osd_watermark"] is True


@pytest.mark.asyncio
async def test_property_read_error_never_breaks_the_window(monkeypatch):
    """读属性失败只该让开关落 False,不该让这台相机整窗没有输出。"""
    import miloco.perception.local_vision.engine as eng

    monkeypatch.setattr(eng, "encode_snapshot_to_h264", lambda *a, **k: b"V")

    async def _boom(did, iids):
        raise RuntimeError("miot down")

    import miloco.manager as mgr
    monkeypatch.setattr(
        mgr, "manager",
        SimpleNamespace(miot_service=SimpleNamespace(get_device_status=_boom)),
        raising=False,
    )
    client = _FakeClient([{"caption": "有人在看书", "rule_hits": [], "gate_p": None,
                           "backend": "codec"}])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res is not None and res.caption
    assert client.calls[0]["osd_watermark"] is False
