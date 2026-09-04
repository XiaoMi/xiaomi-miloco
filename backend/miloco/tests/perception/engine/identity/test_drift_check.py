"""Track 身份漂移自检单测(observe + enforce)。

覆盖:
- config: drift_check 段从 yaml 正确加载(不像 tier_u 段被丢弃)+ 默认 enforce@0.55
- library: get_person_recent_tier_c_centroid —— 时间窗过滤 / 同摄隔离 / tier_a 兜底 /
  none / mean+L2 正确性
- engine._run_drift_check: mode 门控(off/observe/enforce)、sim 与 drift_consec_low
  增减、enforce 批量撤回、采信复认护栏、min_track_emb 门

track 质心来源(DeepSortTracker.get_track_centroid)的零额外推理护栏见
``test_deep_sort_v12.py::TestZeroExtraReIDExtract``(需 ONNX 模型)。本文件全 model-free:
用真实 .npy 喂库、用 fake pool 喂 track 质心,精确隔离漂移逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from miloco.perception.engine.config import (
    IdentityEngineConfig,
    identity_engine_config_from_dict,
)
from miloco.perception.engine.identity.config_loader import load_identity_engine_config
from miloco.perception.engine.identity.dispatcher import FusedDispatcher
from miloco.perception.engine.identity.engine import IdentityEngine
from miloco.perception.engine.identity.library import IdentityLibrary, _sanitize_cam_did
from miloco.perception.engine.identity.state import TrackIdentityState

# 现实 epoch 量级 now_ts:让 tier_c 文件名 ts_ms = int(ts*1000) > 1e12, 被
# _npy_capture_ts 认作时间戳(而非 tier_a 序号)。
_NOW = 1_700_000_000.0
_PID = "11111111-1111-4111-8111-111111111111"
_CAM = "cam-test"


def _unit(i: int, dim: int = 128) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


def _vec_with_sim(s: float, dim: int = 128) -> np.ndarray:
    """构造单位向量，使其与 ``_unit(0)`` 的余弦相似度恰为 ``s``。

    漂移自检把「与上一窗逐字节相同的 sim」视为无新证据、不计不清，所以需要制造
    「窗与窗之间 sim 不同但都低于阈值」的场景，不能靠换一个正交基向量（那样 sim
    仍是 0.0、会被正确判成同一份证据）。
    """
    v = np.zeros(dim, dtype=np.float32)
    v[0] = s
    v[1] = float(np.sqrt(max(0.0, 1.0 - s * s)))
    return v


def _write_npy(path: Path, vec: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(path), vec.astype(np.float32))


def _write_tier_c(lib: IdentityLibrary, pid: str, cam: str, ts: float, vec: np.ndarray) -> None:
    ts_ms = int(ts * 1000)
    d = lib.persons_dir / pid / "tier_c" / _sanitize_cam_did(cam)
    _write_npy(d / f"body_{ts_ms}.npy", vec)


def _write_tier_a(lib: IdentityLibrary, pid: str, idx: int, vec: np.ndarray, mtime: float | None = None) -> Path:
    d = lib.persons_dir / pid / "tier_a"
    p = d / f"body_{idx:03d}.npy"  # tier_a 文件名是序号, 不含时间戳 → 走 mtime
    _write_npy(p, vec)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# =============================================================================
# config: drift_check 加载
# =============================================================================


class TestDriftCheckConfig:
    def test_default_mode_enforce(self):
        # 默认即 enforce@0.55(单一来源, 见 default_config.yaml / DriftCheckConfigDC)
        assert IdentityEngineConfig().drift_check.mode == "enforce"
        assert IdentityEngineConfig().drift_check.threshold == 0.55

    def test_loads_from_dict_not_dropped(self):
        """drift_check 段被 sub_factories 接管 → 转成 dataclass(不像 tier_u 段被丢)。"""
        cfg = identity_engine_config_from_dict(
            {"drift_check": {"mode": "enforce", "threshold": 0.7, "consecutive_windows": 3}}
        )
        from miloco.perception.engine.config import DriftCheckConfigDC
        assert isinstance(cfg.drift_check, DriftCheckConfigDC)
        assert cfg.drift_check.mode == "enforce"
        assert cfg.drift_check.threshold == 0.7
        assert cfg.drift_check.consecutive_windows == 3
        # 未覆盖字段保留默认
        assert cfg.drift_check.recency_sec == 900.0
        assert cfg.drift_check.min_track_emb == 3

    def test_default_yaml_drift_check_enforce(self):
        """全链路: default_config.yaml → load → drift_check 默认 enforce@0.55。"""
        cfg = load_identity_engine_config()
        assert cfg.drift_check.mode == "enforce"
        assert cfg.drift_check.threshold == 0.55
        assert cfg.drift_check.recency_sec == 900.0
        assert cfg.drift_check.consecutive_windows == 2

    # 这里曾有一条用例，按配置算「证据指纹判据当前会不会被走到」。它读的是部署现场
    # 配置（会叠本机 config.json），于是持续集成上没有那份文件、断言基本恒绿，而在调过
    # 参的机器上又会因与那人改动无关的原因变红——两头都不成立，已移除。
    # 这类跨模块关系值得有个集中的校验落点，但「配置热更之后由谁重新校验」需要单独
    # 设计，不在本次改动范围内。


# =============================================================================
# library: get_person_recent_tier_c_centroid
# =============================================================================


class TestRecentTierCCentroid:
    @pytest.fixture
    def lib(self, tmp_path: Path) -> IdentityLibrary:
        return IdentityLibrary(tmp_path / "identity_lib")

    def test_recent_tier_c_returns_mean_l2(self, lib):
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        _write_tier_c(lib, _PID, _CAM, _NOW - 20, _unit(0))
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert kind == "tierc"
        assert n == 2
        assert abs(float(np.linalg.norm(c)) - 1.0) < 1e-6  # L2-normalized
        np.testing.assert_allclose(c, _unit(0), atol=1e-6)

    def test_old_tier_c_outside_window_falls_through(self, lib):
        """超出时间窗的 tier_c 不算; 无近期 tier_a → none。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10000, _unit(0))  # 远超 900s
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert c is None and n == 0 and kind == "none"

    def test_cam_isolation(self, lib):
        """A 相机的 tier_c 不被 B 相机取到。"""
        _write_tier_c(lib, _PID, "cam-A", _NOW - 10, _unit(0))
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, "cam-B", 900.0, _NOW)
        assert c is None and kind == "none"

    def test_falls_back_to_recent_tier_a(self, lib):
        """无近期 tier_c → 退近期 tier_a(mtime 在窗内)。"""
        _write_tier_a(lib, _PID, 1, _unit(3), mtime=_NOW - 5)
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert kind == "tiera"
        assert n == 1
        np.testing.assert_allclose(c, _unit(3), atol=1e-6)

    def test_old_tier_a_excluded(self, lib):
        """tier_a 太旧(mtime 超窗)→ 不兜底 → none。"""
        _write_tier_a(lib, _PID, 1, _unit(3), mtime=_NOW - 10000)
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert c is None and kind == "none"

    def test_no_samples_returns_none(self, lib):
        c, n, kind = lib.get_person_recent_tier_c_centroid("nobody", _CAM, 900.0, _NOW)
        assert c is None and n == 0 and kind == "none"

    def test_tier_c_preferred_over_tier_a(self, lib):
        """近期 tier_c 与 tier_a 同时在 → 取 tier_c(优先级)。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        _write_tier_a(lib, _PID, 1, _unit(3), mtime=_NOW - 5)
        c, n, kind = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert kind == "tierc"
        np.testing.assert_allclose(c, _unit(0), atol=1e-6)

    def test_memoized_same_window_skips_load(self, lib, monkeypatch):
        """同 now_ts、样本集不变的重复调用命中缓存, 零额外 np.load; 新写一条才重算。"""
        calls = {"n": 0}
        orig = IdentityLibrary._mean_l2_from_npys

        def counting(npy_paths):
            calls["n"] += 1
            return orig(npy_paths)

        monkeypatch.setattr(IdentityLibrary, "_mean_l2_from_npys", staticmethod(counting))

        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        _write_tier_c(lib, _PID, _CAM, _NOW - 20, _unit(0))
        c1, n1, k1 = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        first = calls["n"]
        assert first > 0 and k1 == "tierc"
        c2, n2, k2 = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert calls["n"] == first          # 命中缓存, 不再 np.load
        assert (n2, k2) == (n1, k1)
        np.testing.assert_allclose(c2, c1, atol=1e-6)
        _write_tier_c(lib, _PID, _CAM, _NOW - 5, _unit(0))  # 在窗集变 → 失效重算
        lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert calls["n"] > first

    def test_recency_invalidates_without_file_change(self, lib):
        """now_ts 推进致旧样本滑出窗 → 即便无文件变化也重算, 不返回过期质心。

        朴素的"整目录指纹"缓存会在此误返 n=2 的陈旧质心; 正确实现按 now_ts 过滤后的
        在窗集做指纹, 旧样本滑出即失效。
        """
        _write_tier_c(lib, _PID, _CAM, _NOW - 800, _unit(0))  # 仅在 now=_NOW 时在窗
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(1))
        c1, n1, _ = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert n1 == 2
        # 时间推进 200s, 不动任何文件: cutoff=_NOW-700, _NOW-800 那条滑出
        c2, n2, _ = lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW + 200)
        assert n2 == 1
        np.testing.assert_allclose(c2, _unit(1), atol=1e-6)   # 只剩近的那条
        assert not np.allclose(c1, c2)                         # 质心确实变了

    def test_invalidate_person_cache_drops_drift_ref(self, lib):
        """delete/merge/split/写盘走的 _invalidate_person_cache 也清该 person 的参考质心缓存。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        lib.get_person_recent_tier_c_centroid(_PID, _CAM, 900.0, _NOW)
        assert any(k[0] == _PID for k in lib._drift_ref_cache)
        lib._invalidate_person_cache(_PID)
        assert not any(k[0] == _PID for k in lib._drift_ref_cache)


# =============================================================================
# engine._run_drift_check
# =============================================================================


class _FakePool:
    """只实现 get_track_centroid 的最小 pool,精确控制 track 质心 + emb 数。"""

    def __init__(self, centroid: np.ndarray | None = None, n: int = 0) -> None:
        self.centroid = centroid
        self.n = n

    def get_track_centroid(self, cam_id, track_id):
        return self.centroid, self.n


class TestRunDriftCheck:
    @pytest.fixture
    def lib(self, tmp_path: Path) -> IdentityLibrary:
        return IdentityLibrary(tmp_path / "identity_lib")

    def _make_engine(self, lib: IdentityLibrary, mode: str, track_vec, n_emb) -> IdentityEngine:
        return self._make_engine_with_pool(lib, mode, _FakePool(track_vec, n_emb))

    def _make_engine_with_pool(self, lib: IdentityLibrary, mode: str, pool) -> IdentityEngine:
        """同 _make_engine，但由调用方持有 pool 引用（用例需中途改质心时用）。"""
        config = IdentityEngineConfig()
        config.drift_check.mode = mode
        config.drift_check.threshold = 0.5
        config.drift_check.consecutive_windows = 2
        config.drift_check.min_track_emb = 3
        config.drift_check.recency_sec = 900.0
        eng = IdentityEngine(
            config=config,
            library=lib,
            dispatcher=FusedDispatcher(config=config.dispatch),
            scope_label=_CAM,
            device_id=_CAM,
            engine_fps=1.0,
            tier_u_pool=pool,
        )
        return eng

    def _confirmed_state(self, eng: IdentityEngine, tid: int, pid: str) -> TrackIdentityState:
        st = TrackIdentityState(track_id=tid, status="confirmed", committed_person_id=pid)
        eng._states[tid] = st
        return st

    def test_off_is_noop(self, lib):
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        eng = self._make_engine(lib, "off", _unit(1), 5)  # 完全偏离
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 0  # off 早退,完全不算
        assert st.committed_person_id == _PID

    def test_observe_increments_but_no_revoke(self, lib):
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        pool = _FakePool(_unit(1), 5)                        # sim=0 < 0.5
        eng = self._make_engine_with_pool(lib, "observe", pool)
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 1
        pool.centroid = _vec_with_sim(0.3)       # 第 2 窗:各自独立的低相似度证据
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 2          # 已达阈但 observe 不撤
        assert st.status == "confirmed"
        assert st.committed_person_id == _PID

    def test_observe_resets_on_recovery(self, lib):
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        pool = _FakePool(_unit(1), 5)
        config = IdentityEngineConfig()
        config.drift_check.mode = "observe"
        config.drift_check.threshold = 0.5
        config.drift_check.consecutive_windows = 2
        config.drift_check.min_track_emb = 3
        eng = IdentityEngine(
            config=config, library=lib, dispatcher=FusedDispatcher(config=config.dispatch),
            scope_label=_CAM, device_id=_CAM, engine_fps=1.0, tier_u_pool=pool,
        )
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 1
        pool.centroid = _unit(0)  # 外观回到参考 → sim=1 ≥ 0.5
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 0

    def test_same_evidence_votes_only_once(self, lib):
        """同一份证据只投一票：sim 与上一窗逐字节相同 ⟹ 两端都没有新证据，不计不清。

        不加这道判断时：第 1 窗记 1 票，第 2 窗（质心与参考都没变、sim 完全相同）又记
        1 票 → 达到 consecutive_windows=2 → 身份被撤回，而真实证据只有一窗。

        判据刻意不用「本帧是否真检测命中」：那是末帧快照，而这里比的是整窗累积的质心，
        出厂窗长大于 track 存活上限，末帧漏检的窗照样有新证据（见 engine 侧注释）。
        """
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        pool = _FakePool(_unit(1), 5)                          # sim=0 < 0.5,持续偏离
        eng = self._make_engine_with_pool(lib, "enforce", pool)
        st = self._confirmed_state(eng, 7, _PID)

        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 1                        # 第 1 窗:新证据,记一票

        eng._run_drift_check({7}, _NOW, {})                    # 第 2 窗:输入一动不动
        assert st.drift_consec_low == 1, "同一份证据不该再投一票"
        assert st.status == "confirmed"                        # 未达阈,身份保持
        assert st.committed_person_id == _PID

        # 第 3 窗:质心变了(track 侧有了新外观证据),sim 随之不同但仍低于阈值
        pool.centroid = _vec_with_sim(0.3)
        eng._run_drift_check({7}, _NOW, {})
        # 攒满两窗**各自独立**的低相似度证据,此时才撤(撤回顺带清零,故只断言撤回结果)
        assert st.status == "pending"
        assert st.committed_person_id is None
        assert st.drift_last_sim is None, "撤回时证据指纹应一并清空"

    def test_enforce_revokes_after_m_windows(self, lib):
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        pool = _FakePool(_unit(1), 5)                        # 持续偏离
        eng = self._make_engine_with_pool(lib, "enforce", pool)
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 1
        assert st.committed_person_id == _PID       # 第 1 窗不撤
        pool.centroid = _vec_with_sim(0.3)          # 第 2 窗:各自独立的低相似度证据
        eng._run_drift_check({7}, _NOW, {})
        # 第 2 窗达阈 → 撤回
        assert st.status == "pending"
        assert st.committed_person_id is None
        assert st.candidate_person_id is None
        assert st.stability_count == 0
        assert st.drift_suppressed_pid == _PID      # 采信复认护栏武装
        assert st.drift_consec_low == 0             # 撤后清 0

    def test_reconfirm_same_pid_suppressed(self, lib):
        """撤回后 omni 复认回同一 person → 不再 body 二次撤(防震荡)。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        pool = _FakePool(_unit(1), 5)
        eng = self._make_engine_with_pool(lib, "enforce", pool)
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        pool.centroid = _vec_with_sim(0.3)          # 各窗独立证据
        eng._run_drift_check({7}, _NOW, {})         # 撤回, drift_suppressed_pid=_PID
        assert st.drift_suppressed_pid == _PID
        # 模拟 omni 复认回 _PID
        st.status = "confirmed"
        st.committed_person_id = _PID
        eng._run_drift_check({7}, _NOW, {})         # 应被护栏跳过
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 0             # 没再累加
        assert st.committed_person_id == _PID        # 没再撤
        assert st.status == "confirmed"

    def test_suppress_rearmed_when_committed_changes(self, lib):
        """committed 变成另一个新身份 → 清 drift_suppressed_pid(重新武装)。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        eng = self._make_engine(lib, "enforce", _unit(1), 5)
        st = self._confirmed_state(eng, 7, _PID)
        st.drift_suppressed_pid = _PID
        other = "22222222-2222-4222-8222-222222222222"
        st.committed_person_id = other  # 变成新身份
        eng._run_drift_check({7}, _NOW, {})
        # 护栏被清(重新武装); other 无近期 tier_c → ref None 跳过, 不撤
        assert st.drift_suppressed_pid is None

    def test_min_track_emb_gate(self, lib):
        """track emb 不足 min_track_emb → 跳过, 不拿噪声质心误判。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        eng = self._make_engine(lib, "observe", _unit(1), 2)  # n_emb=2 < min 3
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 0

    def test_no_reference_skips(self, lib):
        """无近期 tier_c/tier_a 参考 → 跳过(不误判)。"""
        eng = self._make_engine(lib, "enforce", _unit(1), 5)  # 库里没写任何样本
        st = self._confirmed_state(eng, 7, _PID)
        eng._run_drift_check({7}, _NOW, {})
        assert st.drift_consec_low == 0
        assert st.committed_person_id == _PID

    def test_only_targets_confirmed_members(self, lib):
        """pending / unknown track 不在射程(只盯已绑成员的 confirmed)。"""
        _write_tier_c(lib, _PID, _CAM, _NOW - 10, _unit(0))
        eng = self._make_engine(lib, "enforce", _unit(1), 5)
        st_pending = TrackIdentityState(track_id=8, status="pending", candidate_person_id=_PID)
        eng._states[8] = st_pending
        eng._run_drift_check({8}, _NOW, {})
        assert st_pending.drift_consec_low == 0
        assert st_pending.status == "pending"
