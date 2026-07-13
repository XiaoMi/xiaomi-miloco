"""Tests for Gate Layer — Orchestrator."""

import numpy as np
from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.gate.visual_gate import _preprocess
from miloco.perception.engine.input.video_splitter import create_input_slice


def _solid_frame(r: int, g: int, b: int) -> np.ndarray:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:, :] = [b, g, r]
    return frame


def _baseline(r: int = 100, g: int = 100, b: int = 100) -> np.ndarray:
    """预处理后的基准帧。测 no-change / hold 逻辑时传给 run_gate(prev_frame=...),
    隔离"无 prev_frame 即 cold-start 放行"这条新路径。"""
    return _preprocess(_solid_frame(r, g, b))


def _silent_audio() -> np.ndarray:
    return np.zeros(16000, dtype=np.int16)


def _loud_audio() -> np.ndarray:
    return np.array(
        [int(np.sin(i * 0.1) * 10000) for i in range(16000)],
        dtype=np.int16,
    )


class TestGateOrchestrator:
    config = GateConfig()

    async def test_returns_none_when_no_change(self):
        frame = _solid_frame(100, 100, 100)
        s = create_input_slice("room", [frame] * 6, _silent_audio())
        # 有基准(非 cold-start)的静止窗才会被丢
        packet, timing, _, _, _ = await run_gate(s, self.config, prev_frame=_baseline())
        assert packet is None
        assert not (timing.video_pass or timing.audio_pass)

    async def test_triggers_on_visual_change(self):
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        s = create_input_slice("room", [gray, gray, white, white, white, white], _silent_audio())
        result, timing, _, _, _ = await run_gate(s, self.config)

        assert result is not None
        assert result.trigger.visual_changed
        assert not result.trigger.audio_active
        assert len(result.frames) == 6
        assert timing.video_pass and not timing.audio_pass

    async def test_triggers_on_audio(self):
        frame = _solid_frame(100, 100, 100)
        s = create_input_slice("room", [frame] * 6, _loud_audio())
        result, timing, _, _, _ = await run_gate(s, self.config, prev_frame=_baseline())

        assert result is not None
        assert not result.trigger.visual_changed
        assert result.trigger.audio_active
        assert not timing.video_pass and timing.audio_pass

    async def test_passes_all_data_through(self):
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        audio = _loud_audio()
        s = create_input_slice("room", [gray, gray, white, white, white, white], audio)
        result, _timing, _, _, _ = await run_gate(s, self.config)

        assert result is not None
        assert len(result.frames) == 6
        assert np.array_equal(result.audio_clip, audio)
        assert result.room_name == "room"
        assert result.packet_id


class TestGateHold:
    """Section 7.1 A 矩阵 — hold 滞回核心行为。"""

    config = GateConfig(hold_duration_sec=360.0)

    def _slice_visual_change(self, room="room"):
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        return create_input_slice(room, [gray, gray, white, white, white, white], _silent_audio())

    def _slice_no_change(self, room="room"):
        frame = _solid_frame(100, 100, 100)
        return create_input_slice(room, [frame] * 6, _silent_audio())

    def _slice_no_change_loud(self, room="room"):
        frame = _solid_frame(100, 100, 100)
        return create_input_slice(room, [frame] * 6, _loud_audio())

    def _patch_time(self, monkeypatch, now: float):
        monkeypatch.setattr(
            "miloco.perception.engine.gate.gate.time.monotonic",
            lambda: now,
        )

    async def test_A1_cold_start_both_pass(self, monkeypatch):
        self._patch_time(monkeypatch, 0.0)
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        s = create_input_slice("room", [gray, gray, white, white, white, white], _loud_audio())
        packet, timing, _last_checked, new_v, new_a = await run_gate(
            s, self.config, last_visual_pass_ts=None, last_audio_pass_ts=None,
        )
        assert packet is not None
        assert packet.trigger.visual_changed and packet.trigger.audio_active
        assert packet.trigger.hold is False
        assert timing.hold_pass is False
        assert new_v == 0.0 and new_a == 0.0

    async def test_A2_cold_start_audio_only(self, monkeypatch):
        self._patch_time(monkeypatch, 0.0)
        s = self._slice_no_change_loud()
        packet, timing, _, new_v, new_a = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=None, last_audio_pass_ts=None,
        )
        assert packet is not None
        assert packet.trigger.audio_active and not packet.trigger.visual_changed
        assert packet.trigger.hold is False
        assert new_v is None and new_a == 0.0

    async def test_A3_cold_start_all_silent(self, monkeypatch):
        self._patch_time(monkeypatch, 0.0)
        s = self._slice_no_change()
        packet, timing, _, new_v, new_a = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=None, last_audio_pass_ts=None,
        )
        assert packet is None
        assert timing.hold_pass is False
        assert new_v is None and new_a is None

    async def test_A4_hold_within_window_all_silent(self, monkeypatch):
        self._patch_time(monkeypatch, 3.0)
        s = self._slice_no_change()
        packet, timing, _, new_v, new_a = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.visual_changed is False
        assert packet.trigger.audio_active is False
        assert packet.trigger.hold is True
        assert timing.hold_pass is True
        assert new_v == 0.0 and new_a == 0.0

    async def test_A5_hold_within_window_audio_active(self, monkeypatch):
        self._patch_time(monkeypatch, 6.0)
        s = self._slice_no_change_loud()
        packet, _timing, _, new_v, new_a = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.hold is True
        assert new_v == 0.0 and new_a == 6.0

    async def test_A6_hold_recovered_visual(self, monkeypatch):
        self._patch_time(monkeypatch, 30.0)
        s = self._slice_visual_change()
        packet, _timing, _, new_v, new_a = await run_gate(
            s, self.config, last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.hold is False
        assert new_v == 30.0

    async def test_A7_hold_boundary_inclusive(self, monkeypatch):
        self._patch_time(monkeypatch, 360.0)
        s = self._slice_no_change()
        packet, _timing, _, _, _ = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.hold is True

    async def test_A8_hold_boundary_expired(self, monkeypatch):
        self._patch_time(monkeypatch, 363.0)
        s = self._slice_no_change()
        packet, timing, _, _, _ = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is None
        assert timing.hold_pass is False

    async def test_A9_hold_expired_audio_only(self, monkeypatch):
        self._patch_time(monkeypatch, 363.0)
        s = self._slice_no_change_loud()
        packet, _, _, _, new_a = await run_gate(
            s, self.config, last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.hold is False
        assert new_a == 363.0

    async def test_A10_post_expired_visual_recovers(self, monkeypatch):
        self._patch_time(monkeypatch, 400.0)
        s = self._slice_visual_change()
        packet, _, _, new_v, _ = await run_gate(
            s, self.config, last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is not None
        assert packet.trigger.hold is False
        assert new_v == 400.0

    async def test_A11_hold_disabled_zero(self, monkeypatch):
        cfg = GateConfig(hold_duration_sec=0.0)
        self._patch_time(monkeypatch, 3.0)
        s = self._slice_no_change()
        packet, _, _, _, _ = await run_gate(
            s, cfg, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert packet is None

    async def test_A12_hold_disabled_does_not_block_real_pass(self, monkeypatch):
        cfg = GateConfig(hold_duration_sec=0.0)
        self._patch_time(monkeypatch, 0.0)
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        s = create_input_slice("room", [gray, gray, white, white, white, white], _loud_audio())
        packet, _, _, _, _ = await run_gate(
            s, cfg, last_visual_pass_ts=None, last_audio_pass_ts=None,
        )
        assert packet is not None
        assert packet.trigger.hold is False

    async def test_A13_last_v_only_refreshed_on_visual_changed(self, monkeypatch):
        self._patch_time(monkeypatch, 10.0)
        s = self._slice_no_change_loud()
        _packet, _, _, new_v, new_a = await run_gate(
            s, self.config, prev_frame=_baseline(),
            last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert new_v == 0.0
        assert new_a == 10.0

    async def test_A14_last_a_only_refreshed_on_audio_active(self, monkeypatch):
        self._patch_time(monkeypatch, 10.0)
        s = self._slice_visual_change()
        _packet, _, _, new_v, new_a = await run_gate(
            s, self.config, last_visual_pass_ts=0.0, last_audio_pass_ts=0.0,
        )
        assert new_v == 10.0
        assert new_a == 0.0
