"""MultiTrackSyncBuffer 丢包路径 + consume_drop_stats 行为。"""
from __future__ import annotations

from miloco.perception.collect.stream_buffer import MultiTrackSyncBuffer


def _fill_ready(buf: MultiTrackSyncBuffer, n: int, window_ms: int) -> int:
    """连续 put n 个完整窗口的 video+audio,返回最后一个 wall_ms。"""
    wall = 0
    for i in range(n):
        wall = i * window_ms + 100
        buf.put("video", b"v", wall, wall)
        buf.put("audio", b"a", wall, wall)
    # 再 put 一帧"跨过窗口"的数据,触发上一批 window 变 ready
    wall += window_ms + 1000  # 大于 settle
    buf.put("video", b"v", wall, wall)
    buf.put("audio", b"a", wall, wall)
    return wall


def test_consume_drop_stats_initial_zero():
    buf = MultiTrackSyncBuffer(["video", "audio"], window_ms=100)
    assert buf.consume_drop_stats() == (0, 0, 0, None)


def test_drop_action_counts_dropped_windows():
    buf = MultiTrackSyncBuffer(
        ["video", "audio"], window_ms=100,
        max_windows=2, window_settle_ms=50,
        buffer_full_action="drop",
    )
    # 灌 6 个完整窗口,触发 drop:max_windows=2 → 应丢若干 window
    _fill_ready(buf, 6, 100)

    dropped, ovf_cnt, max_depth, last_action = buf.consume_drop_stats()
    assert dropped > 0, "drop 路径应有 dropped 窗口被记到 stats"
    assert ovf_cnt >= 1
    assert max_depth > 2  # 超过 max_windows 才会触发
    assert last_action == "drop"
    # 再次拉应被清零
    assert buf.consume_drop_stats() == (0, 0, 0, None)


def test_clear_action_resets_and_counts():
    buf = MultiTrackSyncBuffer(
        ["video", "audio"], window_ms=100,
        max_windows=2, window_settle_ms=50,
        buffer_full_action="clear",
    )
    _fill_ready(buf, 6, 100)

    dropped, ovf_cnt, max_depth, last_action = buf.consume_drop_stats()
    assert dropped > 0
    assert ovf_cnt >= 1
    assert max_depth > 2
    assert last_action == "clear"


def test_keep_action_no_drop_no_stats():
    """keep 模式不触发 full_action,stats 全 0。"""
    buf = MultiTrackSyncBuffer(
        ["video", "audio"], window_ms=100,
        max_windows=2, window_settle_ms=50,
        buffer_full_action="keep",
    )
    _fill_ready(buf, 6, 100)
    assert buf.consume_drop_stats() == (0, 0, 0, None)


def test_max_depth_records_peak_not_last():
    """max_buffer_depth 应记触发瞬间的峰值,后续被 clear 拉低也保留峰值。"""
    buf = MultiTrackSyncBuffer(
        ["video", "audio"], window_ms=100,
        max_windows=2, window_settle_ms=50,
        buffer_full_action="clear",
    )
    _fill_ready(buf, 10, 100)
    _, _, max_depth, _ = buf.consume_drop_stats()
    # max_depth 至少 > max_windows;不要求等于精确值,实现细节
    assert max_depth >= 3


def test_drain_ready_returns_only_newest_window():
    """drain 只取最新 ready 窗口送推理,中间旧窗口跳过但仍可被 peek。"""
    buf = MultiTrackSyncBuffer(
        ["video"], window_ms=100, window_settle_ms=50,
        buffer_full_action="keep",  # 不触发 overflow,纯验证 drain 取最新
    )
    # 窗口 0 是单 track 的 partial first-window 会被 skip;窗口 1..5 各塞一帧带编号。
    for w in range(6):
        wall = w * 100 + 10  # 落在窗口 key=w*100
        buf.put("video", f"v{w}".encode(), wall, wall)
    # 远超 settle 的帧把前面所有窗口 expire 成 ready(本帧落在 active 窗口 1000)
    buf.put("video", b"trigger", 1000, 1000)

    ready = buf.drain_ready()
    assert ready is not None
    datas = [f.data for f in ready.tracks["video"]]
    assert datas == [b"v5"], f"应只返回最新窗口 v5,实际 {datas}"

    # 整个 ready_queue 一次排空,不会再逐个吐旧窗口
    assert buf.drain_ready() is None

    # 跳过的旧窗口仍进 _drained,active-perception 的 peek 仍看得到(数据不丢)
    peeked = buf.peek_latest(duration_ms=10_000)
    assert peeked is not None
    peeked_datas = {f.data for f in peeked["video"]}
    assert b"v1" in peeked_datas


def test_drain_skipped_windows_counted_in_stats():
    """drain 取最新时跳过的旧窗口计入 dropped 统计,action 标 skip,供 dashboard 可见。"""
    buf = MultiTrackSyncBuffer(
        ["video"], window_ms=100, window_settle_ms=50,
        buffer_full_action="keep",  # 隔离 put 侧 overflow,纯验证 drain skip 统计
    )
    for w in range(6):
        wall = w * 100 + 10
        buf.put("video", f"v{w}".encode(), wall, wall)
    buf.put("video", b"trigger", 1000, 1000)

    # ready 的是窗口 1..5(窗口 0 是 partial first-window 被 skip),drain 取 v5、跳过 v1~v4
    buf.drain_ready()

    dropped, ovf_cnt, max_depth, last_action = buf.consume_drop_stats()
    assert dropped == 4, f"应跳过 4 个旧窗口,实际 {dropped}"
    assert ovf_cnt == 0, "drain skip 不是 overflow,不计 overflow_count"
    assert max_depth == 5, "本轮排空深度应记为积压峰值"
    assert last_action == "skip"


def test_drain_single_window_no_skip_stats():
    """无积压(只 1 个 ready 窗口)时 drain 不产生 skip 统计。"""
    buf = MultiTrackSyncBuffer(
        ["video"], window_ms=100, window_settle_ms=50,
        buffer_full_action="keep",
    )
    buf.put("video", b"v0", 10, 10)       # 窗口 0:partial first-window,会被 skip
    buf.put("video", b"v1", 110, 110)     # 窗口 1
    buf.put("video", b"trigger", 400, 400)  # 让窗口 1 ready(仅此一个)

    buf.drain_ready()
    assert buf.consume_drop_stats() == (0, 0, 0, None)


def test_keep_action_trims_drained_to_max_windows():
    """keep 模式下 drain 也必须把 _drained 裁到 max_windows(issue #429 帧级泄漏加固)。

    正向覆盖主线 2 的行为变更:此前裁剪只在 full_action != "keep" 分支,keep 下 _drained
    永不收敛。用 max_windows=2 且 drain 5 个窗,确保真正走到 popleft(而非卡在
    drain 数 == max_windows 的边界 no-op),断言裁到上界、最旧窗淘汰、最新窗仍在。
    """
    buf = MultiTrackSyncBuffer(
        ["video"], window_ms=100, window_settle_ms=50,
        max_windows=2, buffer_full_action="keep",
    )
    for w in range(6):                          # 窗口 0..5,0 是 partial first-window 被 skip
        wall = w * 100 + 10
        buf.put("video", f"v{w}".encode(), wall, wall)
    buf.put("video", b"trigger", 1000, 1000)    # 把窗口 1..5 expire 成 ready

    buf.drain_ready()                            # 5 个窗进 _drained → 必须裁到 2
    assert len(buf._drained) == 2, "keep 模式 _drained 必须裁到 max_windows"

    # 只保留最新两窗(v4/v5),更早的(v1~v3)已被 popleft
    peeked = buf.peek_latest(duration_ms=10_000)
    assert peeked is not None
    got = {f.data for f in peeked["video"]}
    assert b"v1" not in got, "最旧窗应已被裁"
    assert b"v5" in got, "最新窗必须保留"
