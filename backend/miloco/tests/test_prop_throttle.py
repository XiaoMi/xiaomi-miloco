# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for PropChangeThrottle — 属性推送落库前的节流。

真实流量特征(实测 2026-07-29,cn-ha.mqtt.io.mi.com,72 台设备):

* 空调功率 `12/3` 每 1–2 秒推一次,120 秒内一台占 95 条推送;
* 设备关→开会**整包重发**全部属性(~25 条/秒),其中含同值条目;
* 开关类属性 `2/1` 只在用户操作时推,是历史查询真正要的信号。

节流必须做到:遥测被压掉、开关一条不丢、同值重发去重。
"""

from __future__ import annotations

from miloco.miot.prop_throttle import DISCRETE, TELEMETRY, PropChangeThrottle

SEC = 1000


def _t(**kw) -> PropChangeThrottle:
    return PropChangeThrottle(**kw)


# --------------------------------------------------------------- 同值去重


def test_same_value_repost_dropped():
    """整包重发里的同值条目不是一次变化。"""
    t = _t()
    assert t.allow("d", 2, 1, True, 0) is True
    assert t.allow("d", 2, 1, True, 1 * SEC) is False
    assert t.allow("d", 2, 1, False, 2 * SEC) is True
    assert t.stats()["dropped_same_value"] == 1


def test_bool_and_int_are_not_the_same_value():
    """Python 里 True == 1;开关 bool 与枚举 int 的互变不能被当成同值吞掉。"""
    t = _t()
    assert t.allow("d", 2, 1, True, 0) is True
    assert t.allow("d", 2, 1, 1, 1 * SEC) is True
    assert t.allow("d", 2, 1, False, 2 * SEC) is True
    assert t.allow("d", 2, 1, 0, 3 * SEC) is True


# ------------------------------------------------------- 离散属性无条件落库


def test_discrete_switch_never_throttled():
    """开关狂按也一条不丢——历史查询的核心信号。"""
    t = _t(burst=2, min_interval_sec=300)
    for i in range(20):
        assert t.allow("d", 2, 1, i % 2 == 0, i * SEC) is True


def test_string_and_null_values_never_throttled():
    t = _t(burst=2)
    for i, v in enumerate(["45-65", "0,0,0,0", None, "45-65", None]):
        assert t.allow("d", 10, 6, v, i * SEC) is True


# ------------------------------------------------------- 高频数值遥测去抖


def test_high_frequency_telemetry_is_throttled():
    """空调功率的真实节奏:1140→1150→1130… 每秒一条,微幅漂移。"""
    t = _t(window_sec=60, burst=5, min_interval_sec=300, rel_delta=0.2)
    values = [1140, 1150, 1130, 1170, 1150, 1140, 1150, 1130, 1160, 1150]
    kept = [t.allow("ac", 12, 3, v, i * SEC) for i, v in enumerate(values)]
    # burst 前视作低频照常落库,达到 burst 后微幅漂移被压掉。
    assert kept[0] is True
    assert sum(kept) < len(values)
    assert kept[-1] is False


def test_low_frequency_numeric_not_throttled():
    """设定温度 26→27 幅度只有 3.8%,但节奏低,属于用户操作,必须落库。"""
    t = _t(window_sec=60, burst=5, min_interval_sec=300, rel_delta=0.2)
    assert t.allow("ac", 2, 4, 26.0, 0) is True
    assert t.allow("ac", 2, 4, 27.0, 600 * SEC) is True
    assert t.allow("ac", 2, 4, 26.0, 1200 * SEC) is True


def test_large_jump_breaks_through_throttle():
    """开机瞬间功率 0→1140:幅度远超阈值,不等最小间隔立刻落库。"""
    t = _t(window_sec=60, burst=3, min_interval_sec=300, rel_delta=0.2)
    for i, v in enumerate([1140, 1150, 1130, 1145]):
        t.allow("ac", 12, 3, v, i * SEC)
    assert t.allow("ac", 12, 3, 0, 5 * SEC) is True


def test_min_interval_lets_slow_drift_through():
    """长期漂移不能被永久吞掉:超过最小间隔无条件落库。"""
    t = _t(window_sec=60, burst=3, min_interval_sec=300, rel_delta=0.2)
    for i, v in enumerate([1140, 1150, 1130, 1145, 1150]):
        t.allow("ac", 12, 3, v, i * SEC)
    assert t.allow("ac", 12, 3, 1155, 400 * SEC) is True


def test_throttled_change_does_not_move_the_baseline():
    """幅度阈值必须相对**上次落库值**,否则每次微涨都过不了阈值,
    累积起来的长期漂移会被完全吞掉。"""
    t = _t(window_sec=600, burst=2, min_interval_sec=10_000, rel_delta=0.2)
    assert t.allow("ac", 12, 3, 100, 0) is True
    for i, v in enumerate([104, 108, 112, 116], start=1):
        assert t.allow("ac", 12, 3, v, i * SEC) is False
    # 相对基线 100 已涨 20%,应放行;若基线被中间值污染则会误判为仅涨 3.4%
    assert t.allow("ac", 12, 3, 120, 5 * SEC) is True


def test_zero_baseline_keeps_any_change():
    """基线为 0 时相对幅度无意义,任何非零变化都落库。"""
    t = _t(window_sec=60, burst=2, min_interval_sec=300, rel_delta=0.2)
    t.allow("d", 11, 1, 0.0, 0)
    t.allow("d", 11, 1, 0.0, 1 * SEC)
    assert t.allow("d", 11, 1, 0.3, 2 * SEC) is True


# ------------------------------------------------------------------ 其它


def test_keys_are_independent():
    """一台设备的遥测刷屏不能拖累同设备的开关属性。"""
    t = _t(window_sec=60, burst=3, min_interval_sec=300, rel_delta=0.5)
    for i in range(10):
        t.allow("ac", 12, 3, 1140 + i, i * SEC)
    assert t.allow("ac", 2, 1, True, 10 * SEC) is True
    assert t.allow("ac", 2, 1, False, 11 * SEC) is True


def test_first_value_is_always_kept():
    t = _t(burst=1)
    assert t.allow("d", 9, 5, 318.7, 0) is True


def test_eviction_bounds_memory():
    t = _t(max_keys=1000)
    for i in range(1200):
        t.allow(f"d{i}", 2, 1, True, i)
    assert t.stats()["keys"] <= 1200
    assert len(t._state) < 1200


# ------------------------------------------------------------ spec 分类接入


def _classifier(mapping):
    return lambda did, s, p: mapping.get((did, s, p))


def test_spec_discrete_beats_frequency_heuristic():
    """spec 说是枚举:哪怕以遥测的节奏狂推,也一条不丢(风机档位连打)。"""
    t = _t(burst=2, classify=_classifier({("d", 3, 2): (DISCRETE, None)}))
    for i, v in enumerate([1, 2, 3, 1, 2, 3, 1, 2]):
        assert t.allow("d", 3, 2, v, i * SEC) is True


def test_spec_telemetry_throttles_from_the_second_sample():
    """spec 说是遥测:不给「低频宽限」,冷启动头几条也按遥测处理。

    没有这条,重启后每个遥测属性都会先放行 burst 条噪声。
    """
    t = _t(burst=5, min_interval_sec=900, rel_delta=0.3,
           classify=_classifier({("d", 4, 9): (TELEMETRY, 100.0)}))
    assert t.allow("d", 4, 9, 68, 0) is True           # 首见基线
    assert t.allow("d", 4, 9, 69, 1 * SEC) is False    # 满量程 1% → 压掉
    assert t.allow("d", 4, 9, 67, 2 * SEC) is False


def test_full_scale_span_beats_relative_delta_for_small_ints():
    """摄像头「人数」0..10 量程:1→2 相对变化 100%,满量程只有 10%,是噪声。"""
    t = _t(burst=2, min_interval_sec=900, rel_delta=0.3,
           classify=_classifier({("cam", 8, 30): (TELEMETRY, 10.0)}))
    assert t.allow("cam", 8, 30, 1, 0) is True
    assert t.allow("cam", 8, 30, 2, 1 * SEC) is False
    assert t.allow("cam", 8, 30, 1, 2 * SEC) is False
    # 但真正的大跳变(0→5,满量程 50%)必须放行
    assert t.allow("cam", 8, 30, 5, 3 * SEC) is True


def test_writeable_setpoint_stays_discrete():
    """空调设定温度带 unit=celsius 却是用户意图,spec 侧判为 discrete;
    26→27 幅度仅 3.8%,若被当成遥测就会丢掉「几点调到 27 度」。"""
    t = _t(burst=2, min_interval_sec=900, rel_delta=0.3,
           classify=_classifier({("ac", 2, 4): (DISCRETE, None)}))
    for i, v in enumerate([26.0, 27.0, 26.0, 28.0, 26.0]):
        assert t.allow("ac", 2, 4, v, i * SEC) is True


def test_classifier_returning_none_falls_back_to_heuristic():
    """spec 缓存冷:退回频率启发式,低频照常落库。"""
    t = _t(burst=5, classify=_classifier({}))
    assert t.allow("d", 2, 4, 26.0, 0) is True
    assert t.allow("d", 2, 4, 27.0, 600 * SEC) is True


def test_classifier_exception_does_not_break_persistence():
    """分类器抛异常不能连累落库——历史采集必须比分类更可靠。"""

    def boom(did, s, p):
        raise RuntimeError("spec cache exploded")

    t = _t(burst=5, classify=boom)
    assert t.allow("d", 2, 1, True, 0) is True
    assert t.allow("d", 2, 1, False, SEC) is True


def test_stats_accounting():
    t = _t(window_sec=60, burst=2, min_interval_sec=300, rel_delta=0.5)
    t.allow("d", 2, 1, True, 0)
    t.allow("d", 2, 1, True, SEC)          # same value
    t.allow("ac", 12, 3, 100, 0)
    t.allow("ac", 12, 3, 101, SEC)
    t.allow("ac", 12, 3, 102, 2 * SEC)     # throttled
    s = t.stats()
    assert s["dropped_same_value"] == 1
    assert s["dropped_throttled"] >= 1
    assert s["kept"] >= 2
