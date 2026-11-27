"""非消费式 peek(主动感知路径)的窗口边界必须是真实的采样跨度。

peek 没有 drain 出来的 ReadyWindow 可借边界。若 DeviceData 的起止时刻留成缺省 0/0,
unix 换算后二者同为 epoch_delta → snapshot.duration_ms == 0 →
``DeviceSnapshot.get_frames_at_fps`` 直接返回全部原始帧(不抽帧),而视频编码仍按配置帧率
(``frame_info.fps``)标注:容器时长 = 帧数 / 配置 fps,与实际采样跨度脱钩。
(例:24fps 相机 4s 缓存 → ~96 帧,omni_fps=1 抽成 ~32 帧 → 容器写成 32 秒。)

反过来,缓存不足 collect 时长(刚连上、突发投递、仅 1 帧)时也不能硬套名义窗口,
否则抽帧会重复同一帧、容器把不到 1s 的内容标成 4s。

这里用合成帧覆盖:边界计算、稳态窗口 = collect 时长、缓存不足时窗口 = 实际跨度、
下游抽帧不重复且编码后容器时长与实际跨度吻合。
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import av
import numpy as np
import pytest
from miloco.config.settings import PerceptionCollectSettings
from miloco.perception.collect.camera_adapter import (
    _CAMERA_TRACKS,
    CameraDeviceAdapter,
    _CameraDeviceState,
)
from miloco.perception.collect.stream_buffer import (
    MultiTrackSyncBuffer,
    StreamFragment,
)
from miloco.perception.engine.omni.prompt_builder import _encode_video_mp4
from miloco.perception.engine.pipeline import (
    _downsample_for_omni,
    downsample_snapshot,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    IdentityPacket,
    MotionState,
)
from miloco.perception.schema import DecodedAudioFrame, DecodedVideoFrame

EPOCH_DELTA = 1_700_000_000_000
COLLECT_MS = 4_000
INPUT_FPS = 3
OMNI_FPS = 1


class _Proxy:
    def get_cached_camera(self, did: str):
        return None


@pytest.fixture
def adapter(monkeypatch) -> CameraDeviceAdapter:
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter.get_settings",
        lambda: SimpleNamespace(
            perception=SimpleNamespace(
                collect=SimpleNamespace(window_size=COLLECT_MS // 1000)
            )
        ),
    )
    ad = CameraDeviceAdapter(miot_proxy=_Proxy())  # type: ignore[arg-type]
    # 与 connect_device 同参数建缓冲(生产口径:窗口几何 / 背压行为一致,测试结论才可迁移)
    cfg = PerceptionCollectSettings()
    assert cfg.window_size * 1000 == COLLECT_MS
    state = _CameraDeviceState(
        did="cam1",
        sync_buffer=MultiTrackSyncBuffer(
            track_names=_CAMERA_TRACKS,
            window_ms=cfg.window_size * 1000,
            max_windows=cfg.max_windows,
            window_settle_ms=cfg.settle_ms,
            buffer_full_action=cfg.full_action,
        ),
    )
    state.epoch_delta = EPOCH_DELTA
    ad._devices["cam1"] = state
    return ad


def _put_frames(adapter: CameraDeviceAdapter, walls: list[int]) -> None:
    state = adapter._devices["cam1"]
    for i, wall in enumerate(walls):
        frame = DecodedVideoFrame(
            frame=np.full((8, 8, 3), i % 256, dtype=np.uint8),
            stream_ts=wall,
            wall_ms=wall,
            unix_ms=wall + EPOCH_DELTA,
        )
        state.sync_buffer.put("decoded_video", frame, stream_ts=wall, wall_ms=wall)


def _put_audio(adapter: CameraDeviceAdapter, walls: list[int]) -> None:
    state = adapter._devices["cam1"]
    for wall in walls:
        frame = DecodedAudioFrame(
            frame=np.zeros(160, dtype=np.int16),
            stream_ts=wall,
            wall_ms=wall,
            unix_ms=wall + EPOCH_DELTA,
        )
        state.sync_buffer.put("decoded_audio", frame, stream_ts=wall, wall_ms=wall)


def _identity_packet(frames: list[np.ndarray]) -> IdentityPacket:
    return IdentityPacket(
        packet_id="p",
        room_name="r",
        timestamp=0.0,
        frame_info=FrameInfo(
            start_timestamp=0, end_timestamp=len(frames) / INPUT_FPS * 1000, fps=INPUT_FPS
        ),
        targets=[],
        scene_motion=MotionState.STATIC,
        frames=[],
        all_frames=frames,
        audio_clip=np.zeros(0, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE, is_urgent=False, energy_level=0.0
        ),
    )


def _container_duration_s(frames: list[np.ndarray], fps: int) -> float:
    b64, _ = _encode_video_mp4(
        frames, np.zeros(0, dtype=np.int16), 16000, fps=fps, short_edge=64
    )
    assert b64 is not None
    with av.open(io.BytesIO(base64.b64decode(b64))) as container:
        return container.duration / 1_000_000


class TestPeekWindowBounds:
    @staticmethod
    def _frag(wall):
        return StreamFragment(data=None, stream_ts=wall, wall_ms=wall)

    def test_full_history_uses_collect_duration_from_newest_fragment(self):
        tracks = {
            "decoded_video": [self._frag(5_000), self._frag(13_000)],
            "decoded_audio": [self._frag(10_500), self._frag(13_400)],
        }
        assert CameraDeviceAdapter._peek_window_bounds(tracks, 4_000) == (9_400, 13_400)

    def test_short_history_starts_at_oldest_fragment(self):
        tracks = {"decoded_video": [self._frag(10_000), self._frag(10_600)]}
        assert CameraDeviceAdapter._peek_window_bounds(tracks, 4_000) == (10_000, 10_600)

    def test_empty_tracks_do_not_raise(self):
        assert CameraDeviceAdapter._peek_window_bounds({"decoded_video": []}, 4_000) == (0, 0)


def _ids(frames: list[np.ndarray]) -> list[int]:
    return [int(f[0, 0, 0]) for f in frames]


def _walls(n: int, start: int, interval_ms: float) -> list[int]:
    return [start + int(i * interval_ms) for i in range(n)]


class TestPeekDeviceData:
    def test_steady_state_window_has_collect_duration(self, adapter):
        # 10s 历史、24fps:peek 只取最近 4s
        _put_frames(adapter, _walls(240, 5_000, 41.6))

        dd = adapter.collect("cam1", drain=False)

        assert dd is not None
        assert dd.window_end_ms == 5_000 + int(239 * 41.6)
        assert COLLECT_MS - 42 <= dd.window_end_ms - dd.window_start_ms <= COLLECT_MS
        assert dd.window_end_unix_ms == dd.window_end_ms + EPOCH_DELTA
        snap = dd.to_snapshot()
        assert snap is not None
        assert snap.duration_ms == dd.window_end_ms - dd.window_start_ms

    def test_short_buffer_window_is_actual_span(self, adapter):
        # 3.08s 内到达 75 帧(约 24fps 突发,不足 4s 缓存)
        walls = _walls(75, 5_000, 3_080 / 75)
        _put_frames(adapter, walls)

        dd = adapter.collect("cam1", drain=False)

        assert dd is not None
        returned = [f.wall_ms for f in dd.video]
        assert dd.window_end_ms - dd.window_start_ms < COLLECT_MS
        assert (dd.window_start_ms, dd.window_end_ms) == (min(returned), max(returned))


class TestDownstreamFrameCountMatchesWindow:
    def _run(self, adapter, walls: list[int]):
        _put_frames(adapter, walls)
        dd = adapter.collect("cam1", drain=False)
        assert dd is not None
        snap = dd.to_snapshot()
        assert snap is not None
        tracker = [f.data for f in downsample_snapshot(snap, INPUT_FPS).video.frames]
        omni = _downsample_for_omni(_identity_packet(tracker), INPUT_FPS, OMNI_FPS)
        return snap.duration_ms / 1000, tracker, omni

    def test_steady_state_is_resampled_to_input_fps(self, adapter):
        span_s, tracker, omni = self._run(adapter, _walls(240, 5_000, 41.6))

        assert len(tracker) == INPUT_FPS * COLLECT_MS // 1000
        assert len(omni.all_frames) == OMNI_FPS * COLLECT_MS // 1000
        assert omni.frame_info.fps == OMNI_FPS
        assert _container_duration_s(omni.all_frames, OMNI_FPS) == pytest.approx(
            COLLECT_MS / 1000, abs=0.2
        )

    @pytest.mark.parametrize(
        "walls",
        [
            pytest.param(_walls(240, 5_000, 41.6), id="steady-24fps"),
            pytest.param(_walls(30, 5_000, 333), id="steady-3fps"),
            pytest.param(_walls(75, 5_000, 3_080 / 75), id="burst-3s-75f"),
            pytest.param(_walls(3, 5_000, 333), id="just-connected-3f"),
            pytest.param(_walls(25, 5_000, 2), id="connect-burst-50ms"),
            pytest.param([5_000], id="single-frame"),
        ],
    )
    def test_no_repeated_frames_and_container_matches_span(self, adapter, walls):
        span_s, tracker, omni = self._run(adapter, walls)

        # 抽帧只做"选取",不得因窗口大于实际内容而重复同一帧
        assert len(set(_ids(tracker))) == len(tracker)
        assert len(set(_ids(omni.all_frames))) == len(omni.all_frames)
        # 容器时长与实际跨度一致(编码粒度 = 1 / omni_fps)
        container_s = _container_duration_s(omni.all_frames, omni.frame_info.fps)
        assert abs(container_s - max(span_s, 1 / OMNI_FPS)) <= 1 / OMNI_FPS


class TestMixedTrackEnvelope:
    """音视频两轨都参与边界:窗口 = 跨轨道包络。钉住该语义,防止无声漂移。

    连接爬坡期音频可能先于视频(视频要等首个关键帧)出帧,此时窗口会被音频前缀拉长、
    超过视频实际跨度——属于修复前(时长恒为 0)就存在的残留;以视频轨锚定边界会丢掉
    对查询有价值的音频上下文,属产品权衡,不在本修复范围。
    """

    def test_audio_head_start_stretches_window_beyond_video_span(self, adapter):
        # 连接爬坡:音频 8.0s 起出帧,视频 11.0s 才出首帧,11.5s 查询
        _put_audio(adapter, _walls(55, 8_000, 64))  # 8.0s ~ 11.46s
        _put_frames(adapter, _walls(11, 11_000, 41.6))  # 11.0s ~ 11.42s

        dd = adapter.collect("cam1", drain=False)

        assert dd is not None and dd.video and dd.audio
        walls = [f.wall_ms for f in dd.video] + [f.wall_ms for f in dd.audio]
        video_span = max(f.wall_ms for f in dd.video) - min(f.wall_ms for f in dd.video)
        assert (dd.window_start_ms, dd.window_end_ms) == (min(walls), max(walls))
        assert dd.window_end_ms - dd.window_start_ms > video_span
