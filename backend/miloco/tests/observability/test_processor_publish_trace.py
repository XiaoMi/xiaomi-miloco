"""processor._publish_trace 单测:不启动真 engine,只验聚合 + 入队。"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
from miloco.observability.metrics_client import (
    MetricsClient,
    set_metrics_client,
)
from miloco.observability.metrics_db import connect
from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import (
    DecodedVideoFrame,
    DeviceData,
    PerceptionBatch,
    PerceptionLatency,
)
from miloco.perception.types import PerceptionDevice


def _make_batch_with_two_devices() -> PerceptionBatch:
    batch = PerceptionBatch()
    for did in ("d1", "d2"):
        dd = DeviceData(meta=PerceptionDevice(
            did=did, name=did, device_type="camera", room_name=f"room-{did}",
        ))
        dd.window_start_unix_ms = 100_000
        dd.window_end_unix_ms = 103_000
        dd.video.append(DecodedVideoFrame(
            frame=np.zeros((10, 10, 3), dtype=np.uint8),
            stream_ts=0, recv_unix_ms=99_500,
        ))
        dd.decode_video_avg_ms = 1.5
        dd.decode_audio_avg_ms = 0.0
        # 模拟 d1 触发 2 个 overflow,丢 5 个窗口;d2 无丢包
        if did == "d1":
            dd.dropped_windows = 5
            dd.overflow_count = 2
            dd.max_buffer_depth = 7
            dd.last_overflow_action = "clear"
        batch.devices[did] = dd
    batch.window_first_frame_recv_ms = 99_500
    batch.decode_avg_ms = 1.5
    batch.video_frame_count = 2
    batch.audio_frame_count = 0
    return batch


async def test_publish_trace_writes_main_and_two_device_rows(tmp_path):
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(
            in_delay_ms=10.0, out_delay_ms=20.0,
            decode_ms=1.5, collect_ms=2.0, convert_ms=3.0, log_ms=4.0,
            cycle_total_ms=100.0, pipeline_total_ms=90.0,
            window_duration_ms=3000.0, stream_lag_ms=3500.0,
            gate_ms=2.0, identity_ms=4.0, omni_ms=180.0,
            device_count=2, skipped=False,
        )
        # api._merge_results 实际下发的 key 形如 "{room}/gate_video_{did}_ms"
        timing = {
            "room-d1/gate_video_d1_ms": 0.5, "room-d1/gate_audio_d1_ms": 0.5,
            "room-d1/gate_video_d1_pass": 1, "room-d1/gate_audio_d1_pass": 0,
            "room-d1/identity_d1_ms": 2.0, "room-d1/omni_d1_ms": 90.0,
            "room-d2/gate_video_d2_ms": 0.5, "room-d2/gate_audio_d2_ms": 0.5,
            "room-d2/gate_video_d2_pass": 0, "room-d2/gate_audio_d2_pass": 1,
            "room-d2/identity_d2_ms": 2.0, "room-d2/omni_d2_ms": 90.0,
        }
        proc._publish_trace(
            trace_id="trace-int",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing=timing,
            stream_lag_ms=3500.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT trace_id, gate_ms, gate_video_pass, gate_audio_pass, "
                "       omni_call_count, stream_lag_ms, "
                "       dropped_windows_total, overflow_count_total FROM traces"
            ).fetchone()
            assert row[0] == "trace-int"
            assert row[1] == 2.0
            assert row[2] == 1  # any video pass
            assert row[3] == 1  # any audio pass
            assert row[4] == 1  # cycle 级 1:batch 一次 omni 调用,N 设备不累加
            assert row[5] == 3500.0
            assert row[6] == 5  # d1 丢 5,d2 无丢
            assert row[7] == 2  # d1 触发 2 次 overflow

            d_rows = conn.execute(
                "SELECT device_id, video_frame_count, gate_video_pass, gate_audio_pass, "
                "       dropped_windows_count, overflow_count, max_buffer_depth, "
                "       last_overflow_action "
                "FROM traces_device WHERE cycle_id=? ORDER BY device_id",
                ("trace-int",),
            ).fetchall()
            assert d_rows == [
                ("d1", 1, 1, 0, 5, 2, 7, "clear"),
                ("d2", 1, 0, 1, 0, 0, 0, None),
            ]
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_reuses_device_trace_id_from_timing(tmp_path):
    """pipeline 写入 timing 的 _device_trace_id_{did} 应被 traces_device 行复用,
    给 SQLite 行稳定的 device_trace_id。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        timing = {
            # pipeline.py 写在 room_timing 后经 _merge_results 不前缀化,顶层保留
            "_device_trace_id_d1": "uuid-from-pipeline-d1",
            "_device_trace_id_d2": "uuid-from-pipeline-d2",
            "room-d1/gate_video_d1_pass": 1,
            "room-d2/gate_audio_d2_pass": 1,
        }
        proc._publish_trace(
            trace_id="trace-reuse",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing=timing,
            stream_lag_ms=0.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            rows = conn.execute(
                "SELECT device_id, device_trace_id FROM traces_device "
                "WHERE cycle_id=? ORDER BY device_id",
                ("trace-reuse",),
            ).fetchall()
            assert rows == [
                ("d1", "uuid-from-pipeline-d1"),
                ("d2", "uuid-from-pipeline-d2"),
            ]
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_reuses_gate_scores_from_timing(tmp_path):
    """pipeline 写入 timing 的 _gate_video_score_{did} / _gate_audio_energy_{did}
    应入库 traces_device.gate_video_score / gate_audio_energy。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        timing = {
            "_gate_video_score_d1": 0.0042,
            "_gate_audio_energy_d1": 0.018,
            "_gate_video_score_d2": 0.001,
            "_gate_audio_energy_d2": 0.0,
            "room-d1/gate_video_d1_pass": 1,
            "room-d2/gate_audio_d2_pass": 1,
        }
        proc._publish_trace(
            trace_id="trace-scores",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing=timing,
            stream_lag_ms=0.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            rows = conn.execute(
                "SELECT device_id, gate_video_score, gate_audio_energy "
                "FROM traces_device WHERE cycle_id=? ORDER BY device_id",
                ("trace-scores",),
            ).fetchall()
            assert rows == [
                ("d1", 0.0042, 0.018),
                ("d2", 0.001, 0.0),
            ]
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_hold_only_window_not_skipped(tmp_path):
    """hold-only 窗口(visual=0, audio=0, hold=1):processor 不能误判为 gate skipped。

    实际 pipeline 已生成 packet 并跑完 identity + omni,traces_device 必须:
      - gate_hold_pass=1, gate_video_pass=0, gate_audio_pass=0
      - gate_skipped=0 (不是 skip)
      - identity_ms / omni_ms 落实际值,不是 NULL
    traces_v.gate_passed=1 才能在 perf 页面体现 hold cycle 真有 omni 投递。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        # d1: hold-only 窗口;d2: 普通 video 通过(对照)
        timing = {
            "room-d1/gate_video_d1_pass": 0,
            "room-d1/gate_audio_d1_pass": 0,
            "room-d1/gate_hold_d1_pass": 1,
            "room-d1/identity_d1_ms": 2.5,
            "room-d1/omni_d1_ms": 88.0,
            "room-d2/gate_video_d2_pass": 1,
            "room-d2/identity_d2_ms": 2.0,
            "room-d2/omni_d2_ms": 90.0,
        }
        proc._publish_trace(
            trace_id="trace-hold-only",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing=timing,
            stream_lag_ms=0.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            rows = conn.execute(
                "SELECT device_id, gate_video_pass, gate_audio_pass, gate_hold_pass, "
                "       gate_skipped, identity_ms, omni_ms "
                "FROM traces_device WHERE cycle_id=? ORDER BY device_id",
                ("trace-hold-only",),
            ).fetchall()
            assert rows == [
                ("d1", 0, 0, 1, 0, 2.5, 88.0),
                ("d2", 1, 0, 0, 0, 2.0, 90.0),
            ]
            # traces_v.gate_passed (cycle 级)应识别本 cycle 因 hold 拉起为通过
            (gp,) = conn.execute(
                "SELECT gate_passed FROM traces_v WHERE trace_id=?",
                ("trace-hold-only",),
            ).fetchone()
            assert gp == 1
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_gate_scores_null_when_timing_missing(tmp_path):
    """timing 缺 _gate_video_score / _gate_audio_energy 时(系统异常 fallback /
    on-demand bypass),traces_device 行的 gate_video_score / gate_audio_energy 落 NULL,
    P50-P99 视图过滤掉。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        proc._publish_trace(
            trace_id="trace-null",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing={},
            stream_lag_ms=0.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            rows = conn.execute(
                "SELECT gate_video_score, gate_audio_energy "
                "FROM traces_device WHERE cycle_id=?",
                ("trace-null",),
            ).fetchall()
            assert all(vs is None and ae is None for vs, ae in rows)
            assert len(rows) == 2
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_fallback_uuid_when_timing_missing(tmp_path):
    """timing 里没有 _device_trace_id_{did} 时(系统异常 timing={}),
    fallback 生成新 UUID 保证 PRIMARY KEY 非空。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        proc._publish_trace(
            trace_id="trace-fallback",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing={},
            stream_lag_ms=0.0,
        )
        await client.flush()

        conn = connect(db)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT device_trace_id FROM traces_device WHERE cycle_id=?",
                ("trace-fallback",),
            ).fetchall()]
            assert len(ids) == 2
            assert all(isinstance(x, str) and x for x in ids)
            assert ids[0] != ids[1]  # 两 device fallback 各自独立 UUID
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


async def test_publish_trace_noop_when_no_client(tmp_path):
    set_metrics_client(None)
    proc = PipelineProcessor(
        collector=MagicMock(),
        perception_engine_proxy=MagicMock(),
        log_repo=MagicMock(),
    )
    batch = _make_batch_with_two_devices()
    latency = PerceptionLatency(window_duration_ms=3000.0)
    # 不抛错即可
    proc._publish_trace(
        trace_id="t", cycle_start_unix_ms=0, batch=batch,
        latency=latency, timing={}, stream_lag_ms=0.0,
    )


async def test_publish_trace_with_cycle_error_msg_forces_skipped_false(tmp_path):
    """cycle_error_msg 非空 -> 显式 skipped=False,与 gate skip 区分。

    系统异常路径 timing={},aggregate 算出 all(gate.skipped)=True;若不显式置 False,
    stats.AVG(skipped) 的 skip_rate 会被系统异常 trace 污染。
    """
    db = tmp_path / "obs.db"
    client = MetricsClient(db_path=db)
    await client.start()
    set_metrics_client(client)
    try:
        proc = PipelineProcessor(
            collector=MagicMock(),
            perception_engine_proxy=MagicMock(),
            log_repo=MagicMock(),
        )
        batch = _make_batch_with_two_devices()
        latency = PerceptionLatency(window_duration_ms=3000.0)
        proc._publish_trace(
            trace_id="trace-err",
            cycle_start_unix_ms=100_000,
            batch=batch,
            latency=latency,
            timing={},  # 模拟系统异常,timing 全空
            stream_lag_ms=0.0,
            cycle_error_msg="RuntimeError: boom",
        )
        await client.flush()

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT skipped, cycle_error_msg FROM traces WHERE trace_id=?",
                ("trace-err",),
            ).fetchone()
            assert row is not None
            assert row[0] == 0  # skipped=False 兑现 docstring 意图
            assert row[1] == "RuntimeError: boom"
        finally:
            conn.close()
    finally:
        set_metrics_client(None)
        await client.stop()


# ─── _aggregate_stage_ms helper(纯函数,无副作用) ─────────────────────────────


def test_aggregate_stage_ms_gate_excludes_submodal_and_pass():
    """gate 聚合只取 device 级总耗时,不重复加子模态(gate_video/audio_*_ms)和 pass 标志。"""
    from miloco.perception.processor import _aggregate_stage_ms

    # 模拟 pipeline.run_batch_pipeline 写的 timing dict(单 device d1, room=r)
    timing = {
        "r/gate_d1_ms": 8.0,          # 总(=video+audio)
        "r/gate_video_d1_ms": 5.0,    # 子模态拆分
        "r/gate_audio_d1_ms": 3.0,    # 子模态拆分
        "r/gate_video_d1_pass": 1,    # 标志,不是 ms
        "r/gate_audio_d1_pass": 1,    # 标志,不是 ms
        "r/identity_d1_ms": 2.0,
        "r/omni_d1_ms": 90.0,
        "_proxy_internal": 999,       # 下划线开头跳过
    }
    gate, identity, omni = _aggregate_stage_ms(timing)
    assert gate == 8.0  # 只算总,bug 前会算成 8+5+3+1+1=18
    assert identity == 2.0
    assert omni == 90.0


def test_aggregate_stage_ms_multi_device():
    """多 device 时 gate 总 = sum(各 device 总),仍排除子模态;omni 取 max(并发墙钟)非 sum。"""
    from miloco.perception.processor import _aggregate_stage_ms

    timing = {
        "r1/gate_d1_ms": 8.0, "r1/gate_video_d1_ms": 5.0, "r1/gate_audio_d1_ms": 3.0,
        "r1/gate_video_d1_pass": 1, "r1/gate_audio_d1_pass": 0,
        "r2/gate_d2_ms": 6.0, "r2/gate_video_d2_ms": 2.0, "r2/gate_audio_d2_ms": 4.0,
        "r2/gate_video_d2_pass": 0, "r2/gate_audio_d2_pass": 1,
        "r1/omni_d1_ms": 100.0, "r2/omni_d2_ms": 50.0,
    }
    gate, _, omni = _aggregate_stage_ms(timing)
    assert gate == 14.0  # 8 + 6 (sum)
    assert omni == 100.0  # max(100,50) 并发墙钟,非 sum(150)


def test_aggregate_stage_ms_empty():
    from miloco.perception.processor import _aggregate_stage_ms
    assert _aggregate_stage_ms({}) == (0.0, 0.0, 0.0)
