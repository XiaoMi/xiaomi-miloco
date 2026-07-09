"""拾音 opt-in（默认关）= mic-off 硬切：引擎入口剥音频 → gate 视频-only → prompt 无音频任务。

第一道防线（唯一切点）在 ``PerceptionEngine._strip_unauthorized_voice_audio``：
- **不在拾音白名单**（未显式开启）的相机 snapshot.audio 置 None（不进 gate/identity/omni）
- 跨窗残留（audio_tail / pending_speech）一并清除
- 白名单为空（默认态）时**剥离全部**相机音频
下游全部自动收敛（本文件逐层钉住）：
- gate：空 audio → audio_active=False、VAD 跳过 → audio-only 刺激不开窗
- prompt：trigger.audio_active=False → mp4 不带音轨、schema 剥 speeches/env_sounds、
  总原则用无音频变体
dispatch/落库闸门（client._filter_voice_enabled）保持第二道防线（既有测试盯着）。
"""

from __future__ import annotations

import numpy as np
import pytest
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
    """直接构造引擎实例，不依赖 omni / 模型外部资源（同 test_api_gate_hold）。"""
    return PerceptionEngine(PerceptionConfig())


def _loud_audio(n: int = 16000) -> np.ndarray:
    rng = np.random.default_rng(42)
    return (rng.standard_normal(n) * 20000).astype(np.int16)


def _snapshot(did: str, *, with_audio: bool = True) -> DeviceSnapshot:
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    video = VideoStream(
        frames=[VideoFrame(data=frame, timestamp=float(i)) for i in range(6)],
        width=64,
        height=64,
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


# ─── 输入组装硬切：_strip_unauthorized_voice_audio ───────────────────────────


class TestStripUnauthorizedVoiceAudio:
    def test_unallowed_did_audio_stripped_allowed_untouched(self, monkeypatch):
        """白名单对照：不在白名单（未开启）的相机音频剥空，白名单内相机原样保留。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        s_off = _snapshot("cam_off")  # 未开启拾音
        s_on = _snapshot("cam_on")  # 已开启拾音
        batch = BatchedSnapshot(snapshots=[s_off, s_on])

        eng._strip_unauthorized_voice_audio(batch)

        assert s_off.audio is None
        assert s_off.audio_clip.size == 0
        assert s_on.audio is not None
        assert s_on.audio_clip.size > 0  # 白名单内相机不受影响

    def test_empty_allowlist_strips_all(self, monkeypatch):
        """**默认关**核心用例：白名单为空 = 无相机开启拾音 → 全部剥离音频。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        s = _snapshot("cam_a")
        eng._strip_unauthorized_voice_audio(BatchedSnapshot(snapshots=[s]))
        assert s.audio is None
        assert s.audio_clip.size == 0

    def test_stale_tail_and_pending_speech_cleared(self, monkeypatch):
        """未开启拾音的相机的跨窗残留必须清：audio_tail 不得拼进未来窗口，
        pending_speech 半句不得再注入 prompt（都是语音内容上云路径）。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        eng._audio_tail["cam_off"] = _loud_audio(800)
        eng._pending_speech["cam_off"] = [{"content": "开启前的半句"}]
        eng._pending_speech_rounds["cam_off"] = 2
        eng._audio_tail["cam_on"] = _loud_audio(800)

        eng._strip_unauthorized_voice_audio(
            BatchedSnapshot(snapshots=[_snapshot("cam_off")])
        )

        assert "cam_off" not in eng._audio_tail
        assert "cam_off" not in eng._pending_speech
        assert "cam_off" not in eng._pending_speech_rounds
        assert "cam_on" in eng._audio_tail  # 白名单内、且不在本批 → 不动

    def test_kv_failure_fails_closed(self, monkeypatch):
        """KV 读失败 fail-closed：allow-list 返回空集 = 全部剥离（宁可漏也不擅自处理）。"""
        monkeypatch.setattr(
            "miloco.miot.filter.voice_allowed_camera_dids",
            lambda kv: (_ for _ in ()).throw(RuntimeError("kv down")),
        )
        # 读取器兜异常返回空集
        assert engine_api._voice_allowed_dids() == set()
        # 空集在 allow-list 语义下 = 全部剥离
        eng = _make_engine()
        s = _snapshot("cam_x")
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng._strip_unauthorized_voice_audio(BatchedSnapshot(snapshots=[s]))
        assert s.audio is None

    def test_relog_after_reenable(self, monkeypatch):
        """拾音开启后再关闭，INFO 日志会重新打一次（_mic_off_logged 集维护）。"""
        allowed: set[str] = set()  # 初始未开启 → 被剥离并记日志
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set(allowed))
        eng = _make_engine()
        eng._strip_unauthorized_voice_audio(
            BatchedSnapshot(snapshots=[_snapshot("cam_x")])
        )
        assert "cam_x" in eng._mic_off_logged
        # 开启拾音 → 从已打日志集移除（下次再关会重新打）
        allowed.add("cam_x")
        eng._strip_unauthorized_voice_audio(
            BatchedSnapshot(snapshots=[_snapshot("cam_x")])
        )
        assert "cam_x" not in eng._mic_off_logged


# ─── gate：剥音频后 audio-only 刺激不开窗、视频刺激照常 ───────────────────────


class TestGateVideoOnlyAfterStrip:
    config = GateConfig()

    def test_audio_only_stimulus_does_not_fire_when_stripped(self, monkeypatch):
        """响亮音频 + 静止画面的未开启相机：剥离后 gate 不开窗（原本会 audio 触发）。

        传 prev_frame 基准帧隔离视觉 cold-start 放行——生产流式循环里 prev_frames
        字典常驻，静止画面本就不过视觉 gate，audio-only 是这类窗口唯一的开窗路径。
        """
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        s = _snapshot("cam_off")  # 静止画面 + 响亮音频，未开启拾音
        prev = _preprocess(s.frames[0])
        # 对照：未剥离时 audio 触发开窗
        slice_before = create_input_slice("room", s.frames, s.audio_clip)
        packet_before, timing_before, *_ = run_gate(
            slice_before, self.config, prev_frame=prev
        )
        assert packet_before is not None and timing_before.audio_pass

        # 剥离后：同样刺激不再开窗
        eng._strip_unauthorized_voice_audio(BatchedSnapshot(snapshots=[s]))
        slice_after = create_input_slice("room", s.frames, s.audio_clip)
        packet_after, timing_after, *_ = run_gate(
            slice_after, self.config, prev_frame=prev
        )
        assert packet_after is None
        assert not timing_after.audio_pass
        assert timing_after.speech_prob == 0.0  # VAD 也被跳过（不做任何音频处理）

    def test_video_stimulus_still_fires_with_audio_inactive(self, monkeypatch):
        """视频刺激照常开窗，但 packet 的 audio 触发为 False、audio_clip 为空。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: set())
        eng = _make_engine()
        gray = np.zeros((64, 64, 3), dtype=np.uint8)
        white = np.full((64, 64, 3), 255, dtype=np.uint8)
        s = _snapshot("cam_off")
        s.video = VideoStream(
            frames=[
                VideoFrame(data=f, timestamp=float(i))
                for i, f in enumerate([gray, gray, white, white, white, white])
            ],
            width=64,
            height=64,
        )
        eng._strip_unauthorized_voice_audio(BatchedSnapshot(snapshots=[s]))
        slice_ = create_input_slice("room", s.frames, s.audio_clip)
        packet, timing, *_ = run_gate(slice_, self.config)
        assert packet is not None and timing.video_pass
        assert not packet.trigger.audio_active
        assert packet.audio_clip.size == 0

    def test_voice_on_cam_unchanged(self, monkeypatch):
        """拾音已开启（在白名单内）的相机：audio 触发行为与改动前一致（对照组）。"""
        monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
        eng = _make_engine()
        s = _snapshot("cam_on")
        eng._strip_unauthorized_voice_audio(BatchedSnapshot(snapshots=[s]))
        slice_ = create_input_slice("room", s.frames, s.audio_clip)
        packet, timing, *_ = run_gate(slice_, self.config)
        assert packet is not None and timing.audio_pass
        assert packet.trigger.audio_active


# ─── prompt：audio_active=False → 无音轨、schema 剥 speeches/env_sounds ───────


class TestPromptNoAudioTasksAfterStrip:
    def _packet_from_stripped_gate(self):
        """走真实 run_gate 产出 trigger，再套进 IdentityPacket（与主链同构）。"""
        from miloco.perception.engine.types import (
            AudioAnalysis,
            AudioType,
            FrameInfo,
            FrameResolution,
            IdentityPacket,
            MotionState,
            SelectedFrame,
        )

        gray = np.zeros((64, 64, 3), dtype=np.uint8)
        white = np.full((64, 64, 3), 255, dtype=np.uint8)
        frames = [gray, gray, white, white, white, white]
        slice_ = create_input_slice("room", frames, np.array([], dtype=np.int16))
        gate_packet, *_ = run_gate(slice_, GateConfig())
        assert gate_packet is not None  # 视频触发

        return IdentityPacket(
            packet_id="p1",
            room_name="room",
            timestamp=1000.0,
            frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=1),
            targets=[],
            scene_motion=MotionState.STATIC,
            frames=[
                SelectedFrame(
                    frame_index=0, image=gray,
                    resolution=FrameResolution.HIGH, crops=[],
                )
            ],
            all_frames=frames,
            audio_clip=gate_packet.audio_clip,
            audio_analysis=AudioAnalysis(
                type=AudioType.SILENCE, is_urgent=False, energy_level=0.0
            ),
            trigger=gate_packet.trigger,
        )

    def test_schema_and_principle_drop_audio_tasks(self):
        from miloco.perception.engine.omni.prompt_builder import build_prompt
        from miloco.perception.engine.types import OmniContext

        payload = build_prompt(self._packet_from_stripped_gate(), OmniContext())
        sp = payload["system_prompt"]
        assert '"speeches"' not in sp
        assert '"env_sounds"' not in sp
        assert "本轮只有视频、没有音频" in sp  # _PRINCIPLE_VIDEO_NO_AUDIO

    def test_audio_tasks_present_for_voice_on(self):
        """对照组：audio_active=True（拾音开启）时 speeches/env_sounds 照常在 schema。"""
        from miloco.perception.engine.omni.prompt_builder import build_prompt
        from miloco.perception.engine.types import GateTrigger, OmniContext

        ep = self._packet_from_stripped_gate()
        ep.audio_clip = _loud_audio()
        ep.trigger = GateTrigger(
            visual_changed=True,
            visual_change_score=0.5,
            audio_active=True,
            audio_energy_level=0.6,
            speech_active=True,
        )
        payload = build_prompt(ep, OmniContext())
        sp = payload["system_prompt"]
        assert '"speeches"' in sp
        assert '"env_sounds"' in sp


# ─── realtime_perceive 入口接线：strip 先于 contexts / audio-tail ─────────────


@pytest.mark.asyncio
async def test_realtime_perceive_strips_before_pipeline(monkeypatch):
    """入口即剥：pipeline 收到的 batch 中未开启相机音频已空、audio-tail 未积累。"""
    monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
    eng = _make_engine()

    seen: dict[str, int] = {}

    async def _fake_run_batch_pipeline(batch, contexts, config, **kwargs):
        for s in batch.snapshots:
            seen[s.device.did] = s.audio_clip.size
        from miloco.perception.engine.types import BatchPipelineResult

        return BatchPipelineResult(rooms={}, timing={})

    monkeypatch.setattr(
        "miloco.perception.engine.pipeline.run_batch_pipeline",
        _fake_run_batch_pipeline,
    )
    batch = BatchedSnapshot(snapshots=[_snapshot("cam_off"), _snapshot("cam_on")])
    await eng.realtime_perceive(batch)

    assert seen["cam_off"] == 0
    assert seen["cam_on"] > 0
    assert "cam_off" not in eng._audio_tail  # 剥离后 tail 不积累
    assert "cam_on" in eng._audio_tail  # 拾音开启相机照常存 overlap tail


@pytest.mark.asyncio
async def test_on_demand_perceive_strips_before_pipeline(monkeypatch):
    """主动查询同样入口即剥：query pipeline 收到的 batch 中未开启相机音频已空。

    skill 明确承诺 perceive query 对未开启拾音相机听不到现场声音——隐私关键路径，
    钉住 on_demand_perceive 的 strip 接线（被移除即红）。
    """
    monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"cam_on"})
    eng = _make_engine()

    seen: dict[str, int] = {}

    async def _fake_run_query_pipeline(batch, query, config, **kwargs):
        for s in batch.snapshots:
            seen[s.device.did] = s.audio_clip.size
        return {}

    monkeypatch.setattr(
        "miloco.perception.engine.pipeline.run_query_pipeline",
        _fake_run_query_pipeline,
    )
    batch = BatchedSnapshot(snapshots=[_snapshot("cam_off"), _snapshot("cam_on")])
    await eng.on_demand_perceive(batch, "有没有人在说话")

    assert seen["cam_off"] == 0
    assert seen["cam_on"] > 0
