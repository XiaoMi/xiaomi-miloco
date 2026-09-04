"""空输入窗口不得进入感知链路——三道闸各自钉住。

背景：视频轨在某窗内无数据（解码器等关键帧 / 缓冲区溢出清空 / 掉线重连）但音频轨有数据
时，窗口靠音频通过 ``stream_buffer`` 封窗、``DeviceSnapshot.has_data`` 与引擎入口的
``batch.empty``；随后拾音未开启的相机音频被 ``_strip_unauthorized_voice_audio`` 剥掉，
这台相机这一窗就什么都不剩了。此前无人复检，空 snapshot 一路走到 gate，被 visual hold
放行成空 packet，编码层无帧不加 video 块 → 变成"纯文本问模型这个场景里有什么"，模型只能
照着 schema 编。

三道闸，两套判据：

- ``PerceptionEngine._drop_empty_snapshots``（realtime 入口）：剥音频后逐 snapshot 剔除
  「既无帧也无音频」的设备。判据留一手是有意的——零帧但有音频的窗口要留给 audio route
- ``PerceptionEngine._drop_frameless_snapshots``（on_demand 入口）：判据更严，只看
  ``has_video``。query 路径跳过 gate 且**没有 audio route**，零帧 snapshot 只能在这里挡
- ``run_gate``：无帧时 hold 对下游不成立——音频过闸的走 audio route，音频没过闸的不建 packet

两个引擎入口的判据故意不同，``test_realtime_keeps_what_query_drops`` 钉住这个差异，
防后续有人图省事合并成一个函数。

trace 侧还有一条相关钉子在 ``test_pipeline.py::test_hold_trace_key_zero_when_gate_drops_frameless_window``：
gate 拦下零帧窗口时 ``gate_hold_{did}_pass`` 必须为 0，否则 processor 会误判 identity/omni
跑过。它与本文件 ``test_zero_frames_quiet_audio_in_hold_does_not_open`` 成对——后者钉
``timing.hold_pass`` 仍为 True（状态机要原始值），前者钉 trace 键取的是「有没有真拉起来」。
"""

from __future__ import annotations

import time

import numpy as np
from miloco.perception.engine import api as engine_api
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import GateConfig, PerceptionConfig
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.gate.visual_gate import _preprocess
from miloco.perception.engine.input.video_splitter import create_input_slice
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _make_engine() -> PerceptionEngine:
    """直接构造引擎实例，不依赖 omni / 模型外部资源（同 test_voice_mic_off）。"""
    return PerceptionEngine(PerceptionConfig())


def _loud_audio(n: int = 16000) -> np.ndarray:
    rng = np.random.default_rng(42)
    return (rng.standard_normal(n) * 20000).astype(np.int16)


def _quiet_audio(n: int = 16000) -> np.ndarray:
    """安静房间底噪：非空，但 RMS 远低于 audio_energy_threshold（默认 0.015）。"""
    rng = np.random.default_rng(42)
    return (rng.standard_normal(n) * 5).astype(np.int16)


def _still_frames(n: int = 6) -> list[np.ndarray]:
    """静止画面：不过视觉 gate，只有 hold / audio 能开窗。"""
    return [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(n)]


def _snapshot(
    did: str, *, with_video: bool = True, with_audio: bool = True
) -> DeviceSnapshot:
    video = (
        VideoStream(
            frames=[
                VideoFrame(data=f, timestamp=float(i))
                for i, f in enumerate(_still_frames())
            ],
            width=64,
            height=64,
        )
        if with_video
        else None
    )
    audio = (
        AudioStream(frames=[AudioFrame(data=_loud_audio(), timestamp=0.0)])
        if with_audio
        else None
    )
    return DeviceSnapshot(
        device=PerceptionDevice(did=did, name=f"cam-{did}", device_type="camera"),
        start_timestamp=0.0,
        end_timestamp=3000.0,
        video=video,
        audio=audio,
    )


# ─── gate：无帧且无音频时 hold 也不放行 ──────────────────────────────────────


class TestGateEmptyInput:
    config = GateConfig()

    async def test_hold_does_not_open_window_without_any_input(self):
        """零帧 + 零音频 + hold 生效 → 不建 packet。

        同时钉住 ``timing.hold_pass`` 仍为 True：pipeline 的 HOLD_START/EXPIRED/RECOVERED
        状态机读这个字段，且在 ``gate_packet is None`` 之前执行。若把空输入闸写成函数开头
        early return（返回 hold_pass=False 的空 timing），仍在 hold 期的设备会被误判成
        hold 结束，刷出假的 HOLD_EXPIRED 日志与 gate_hold_expired 事件。
        """
        slice_ = create_input_slice("room", [], np.array([], dtype=np.int16))
        packet, timing, *_ = await run_gate(
            slice_, self.config, last_visual_pass_ts=time.monotonic()
        )
        assert packet is None
        assert timing.hold_pass is True

    async def test_hold_still_opens_window_with_frames(self):
        """有帧（静止画面）+ hold 生效 → 照常开窗。hold 本身不被本次改动削弱。"""
        frames = _still_frames()
        prev = _preprocess(frames[0])
        slice_ = create_input_slice("room", frames, np.array([], dtype=np.int16))
        packet, timing, *_ = await run_gate(
            slice_,
            self.config,
            prev_frame=prev,
            last_visual_pass_ts=time.monotonic(),
        )
        assert packet is not None
        assert not timing.video_pass  # 静止画面本身不过视觉 gate
        assert timing.hold_pass is True  # 靠 hold 开的窗

    async def test_zero_frames_with_audio_still_opens_window(self):
        """零帧但音频过闸（拾音开启的相机）→ 照常开窗，交给 audio route。

        闸判的是"hold 能不能撑起 video 路由"，不是"没有视频就拦"——否则会误杀
        audio-only 感知。
        """
        slice_ = create_input_slice("room", [], _loud_audio())
        packet, timing, *_ = await run_gate(slice_, self.config)
        assert packet is not None
        assert timing.audio_pass
        assert packet.trigger.audio_active

    async def test_zero_frames_quiet_audio_in_hold_does_not_open(self):
        """拾音开启 + 零帧 + 安静音频（未过能量闸）+ hold 生效 → 不建 packet。

        这一格是 hold 与零帧的交叉：音频存在让 _drop_empty_snapshots 按设计放行
        （has_data 为真），若 gate 只判"有没有输入"就会放它过去，而下游
        ``_is_audio_only`` 见 hold=True 硬短路回 video 路由、编码层无帧不加 video 块
        → 又是纯文本脑补。
        """
        slice_ = create_input_slice("room", [], _quiet_audio())
        packet, timing, *_ = await run_gate(
            slice_, self.config, last_visual_pass_ts=time.monotonic()
        )
        assert packet is None
        assert timing.hold_pass is True  # 同上：不许刷假 HOLD_EXPIRED

    async def test_zero_frames_loud_audio_in_hold_routes_to_audio(self):
        """零帧 + 音频过闸 + hold 生效 → 建 packet，但 trigger.hold 必须为 False。

        hold 的语义是"视觉在滞回期内、别降级到 audio-only"，前提是真有画面。零帧时
        让 hold 对下游成立，等于把这一窗仅有的音频也一起扔掉：video 路由不加 video 块
        （无帧），也不会加 input_audio 块。
        """
        slice_ = create_input_slice("room", [], _loud_audio())
        packet, timing, *_ = await run_gate(
            slice_, self.config, last_visual_pass_ts=time.monotonic()
        )
        assert packet is not None
        assert packet.trigger.audio_active
        assert packet.trigger.hold is False  # → _is_audio_only 放行 → audio route
        assert timing.hold_pass is True  # 交回上层的滞回状态不受影响


# ─── 引擎入口：剥音频后剔除空 snapshot ───────────────────────────────────────


class TestDropEmptySnapshots:
    def test_mic_off_zero_frame_snapshot_dropped(self, monkeypatch):
        """线上实际路径：拾音关闭 + 零帧 + 有音频 → 剥音频后被剔除，batch 转空。

        剥离前 batch 非空（音频撑着），正是这一点让它通过了入口的第一道 batch.empty。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        s = _snapshot("cam_off", with_video=False, with_audio=True)
        batch = BatchedSnapshot(snapshots=[s])
        assert not batch.empty  # 音频撑着，入口第一道 batch.empty 拦不住

        eng._strip_unauthorized_voice_audio(batch)
        eng._drop_empty_snapshots(batch)

        assert batch.snapshots == []
        assert batch.empty

    def test_mixed_batch_only_empty_one_dropped(self, monkeypatch):
        """混合场景：cam A 零帧、cam B 正常 → 只剔 A，B 保留。

        这条是"逐 snapshot 剔除"与"整批判空"的分界钉：整批判空时 batch 非空（B 撑着），
        A 会带着空输入继续走完 gate → omni。同房间多相机是常态，不是边缘情况。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        s_empty = _snapshot("cam_a", with_video=False, with_audio=True)
        s_ok = _snapshot("cam_b", with_video=True, with_audio=True)
        batch = BatchedSnapshot(snapshots=[s_empty, s_ok])

        eng._strip_unauthorized_voice_audio(batch)
        eng._drop_empty_snapshots(batch)

        assert [s.device.did for s in batch.snapshots] == ["cam_b"]
        assert not batch.empty

    def test_mic_on_zero_frame_snapshot_kept(self, monkeypatch):
        """拾音已开启 + 零帧 + 有音频 → 不剔除，留给 audio route。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        s = _snapshot("cam_on", with_video=False, with_audio=True)
        batch = BatchedSnapshot(snapshots=[s])

        eng._strip_unauthorized_voice_audio(batch)
        eng._drop_empty_snapshots(batch)

        assert [x.device.did for x in batch.snapshots] == ["cam_on"]

    async def test_all_empty_batch_returns_skipped_not_none(self, monkeypatch):
        """整批被剔空 → 返回 skipped 结果，不是 None。

        processor 拿到 None 会直接 return，走不到 cycle trace 发布，零帧窗口在
        dashboard 上连行都不留、出现频率不可观测。与「全部 device 没过 gate」同款收尾。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        batch = BatchedSnapshot(
            snapshots=[_snapshot("cam_off", with_video=False, with_audio=True)]
        )

        result = await eng.realtime_perceive(batch)

        assert result is not None
        assert result.skipped is True

    async def test_query_path_drops_frameless_even_with_audio(self, monkeypatch):
        """主动查询 + 拾音开启 + 零帧 + 有音频 → 返回空答案。

        query 路径判据比 realtime 严：那边零帧+音频有 audio route 接住，query 路径
        `build_query_prompt` 只产 video_base64，零帧时连音频一起丢，模型会拿上一窗的
        last_caption 去回答"现在怎么样"——用户直接读到没有画面依据的现场描述。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        batch = BatchedSnapshot(
            snapshots=[_snapshot("cam_on", with_video=False, with_audio=True)]
        )

        result = await eng.on_demand_perceive(batch, "厨房现在怎么样")

        # 同时断言 batch 已被剔空：只断言 answer=="" 挡不住"换成 _drop_empty_snapshots
        # 后走进 run_query_pipeline、因别的原因也返回空答案"这种为错误理由通过的情况。
        assert batch.snapshots == []
        assert result.answer == ""

    def test_query_path_keeps_framed_device_in_mixed_batch(self, monkeypatch):
        """混合批：零帧的剔掉、有帧的保留，多设备查询不受影响。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        batch = BatchedSnapshot(
            snapshots=[
                _snapshot("cam_a", with_video=False, with_audio=True),
                _snapshot("cam_b", with_video=True, with_audio=True),
            ]
        )

        eng._drop_frameless_snapshots(batch)

        assert [s.device.did for s in batch.snapshots] == ["cam_b"]

    def test_realtime_keeps_what_query_drops(self, monkeypatch):
        """两条路径判据不同的对照：同一个「零帧+有音频」snapshot，realtime 留、query 剔。

        钉住这个差异，防后续有人图省事把两处合并成一个函数——realtime 侧按无帧剔除会
        误杀拾音相机的纯音频感知。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        make = lambda: BatchedSnapshot(  # noqa: E731
            snapshots=[_snapshot("cam_on", with_video=False, with_audio=True)]
        )

        b_realtime = make()
        eng._drop_empty_snapshots(b_realtime)
        assert len(b_realtime.snapshots) == 1

        b_query = make()
        eng._drop_frameless_snapshots(b_query)
        assert b_query.snapshots == []

    def test_warning_deduped_per_did_and_reset_on_recovery(self, monkeypatch, caplog):
        """日志按 did 去重：连续空窗只打一条；该相机恢复出数据后再空，重新打。

        掉线期间每个窗口（默认 4s）都会命中，不去重会刷屏。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()

        def _drop_empty_window() -> None:
            b = BatchedSnapshot(
                snapshots=[_snapshot("cam_x", with_video=False, with_audio=False)]
            )
            eng._drop_empty_snapshots(b)

        with caplog.at_level("WARNING", logger=engine_api.logger.name):
            _drop_empty_window()
            _drop_empty_window()
            assert sum("本窗无任何输入" in r.message for r in caplog.records) == 1
            assert "cam_x" in eng._empty_window_logged

            # 该相机恢复出数据 → 移出已打集
            recovered = BatchedSnapshot(snapshots=[_snapshot("cam_x")])
            eng._drop_empty_snapshots(recovered)
            assert "cam_x" not in eng._empty_window_logged

            # 再次为空 → 重新打一条
            _drop_empty_window()
            assert sum("本窗无任何输入" in r.message for r in caplog.records) == 2


# ─── 编码层最后一道可观测性兜底 ─────────────────────────────────────────────


class TestFusedNoMediaBlockWarning:
    """走到 video 路由却没拼出 video 块时必须留痕，且能区分两种排查方向。

    正常态由 gate 空输入闸 + 两个引擎入口闸挡住，这条 warning 是上游闸失效 /
    _AUDIO_ONLY_ENABLED 被回滚 / 编码异常时的最后一道观测点。它本身不在任何主路径上，
    所以必须有测试实际执行到——否则变量名写错会在生产最糟的时刻才暴露。
    """

    def _content(self, *, has_pets: bool, caplog, monkeypatch=None):
        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.prompt_builder import (
            FusedPromptConfig,
            _build_fused_user_content,
        )
        from miloco.perception.engine.omni.provider import get_adapter
        from miloco.perception.engine.types import OmniContext

        if monkeypatch is not None:
            # 假宠物参考图：不依赖档案与磁盘，只为验证媒体块计数
            monkeypatch.setattr(
                pb, "build_pet_reference_content",
                lambda max_pets: [
                    {"type": "text", "text": "宠物参考"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                ],
            )

        with caplog.at_level("WARNING"):
            return _build_fused_user_content(
                packets=[],
                context=OmniContext(room_name="客厅"),
                candidates=[],
                gallery_snapshot={},
                video_b64=None,  # ← 零帧：编码层返回 None
                media_info=None,
                adapter=get_adapter("mimo"),
                cfg=FusedPromptConfig(),
                has_pets=has_pets,
            )

    def test_warns_with_room_and_media_count(self, caplog):
        """video_b64 为 None → 打 warning，带房间名与剩余媒体块数。"""
        self._content(has_pets=False, caplog=caplog)

        recs = [r for r in caplog.records if "fused_no_media_block" in r.getMessage()]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "room=客厅" in msg
        assert "other_media_blocks=0" in msg  # 无宠物图 → 模型什么都没看到

    def test_counts_remaining_media_blocks_when_pet_refs_present(
        self, caplog, monkeypatch
    ):
        """配了宠物参考图时计数必须非 0。

        这条 warning 的全部价值在于区分两种排查方向：0 = 模型什么都没看到，非 0 = 模型
        只看到参考图、没有本窗画面（看图脑补的高风险形态）。只测 0 一侧等于没测这个区分。
        """
        content = self._content(has_pets=True, caplog=caplog, monkeypatch=monkeypatch)

        assert any(b.get("type") == "image_url" for b in content), "前提：参考图应已入 content"
        msg = next(
            r.getMessage() for r in caplog.records if "fused_no_media_block" in r.getMessage()
        )
        assert "other_media_blocks=1" in msg
        assert "看图脑补" in msg
