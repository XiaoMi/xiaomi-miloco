"""陌生人池(TierU)单测。

覆盖三个核心不变量(I1/I2/I3)+ 关键路径 + 零额外推理硬约束护栏。
- I1: 写入 gate 关闭后 push_crop 静默丢弃
- I2: L1 累计满才晋级
- I3: intra_cam 去重不物理合并 entry,通过 cluster_id 挂关系

测试不依赖任何 ONNX 模型(全部 mock ReIDProvider 注入 fake embedding)。
"""

from __future__ import annotations

import logging
from typing import Iterator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from miloco.perception.engine.identity.tier_u import (
    CropEntry,
    DeepSortReIDProvider,
    ReIDProvider,
    TierUConfig,
    TierUPool,
    _aspect_dist_normalized,
    quality_score,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_crop(cam: str, tid: int, frame: int, ts: float, sharpness: float = 100.0) -> CropEntry:
    """64×128 BGR body(过 quality_gate),无 face。"""
    return CropEntry(
        cam_id=cam,
        track_id=tid,
        frame_index=frame,
        captured_at=ts,
        body_crop=np.ones((128, 64, 3), dtype=np.uint8) * 128,  # h=128,w=64,aspect=0.5(in [0.25,2.0])
        face_crop=None,
        sharpness=sharpness,
        bbox_xyxy=(10, 10, 74, 138),
        detector_conf=0.8,
    )


class _MockReIDProvider(ReIDProvider):
    """注入指定 embedding 给 (cam, tid)。"""

    def __init__(self) -> None:
        self._table: dict[tuple[str, int], np.ndarray] = {}
        self.calls: list[tuple[str, int]] = []

    def set_embedding(self, cam: str, tid: int, emb: np.ndarray) -> None:
        # L2 归一化
        n = np.linalg.norm(emb)
        if n > 0:
            emb = emb / n
        self._table[(cam, tid)] = emb.astype(np.float32)

    def get_embedding(self, cam_id: str, track_id: int):
        self.calls.append((cam_id, track_id))
        return self._table.get((cam_id, track_id))


def _clock() -> Iterator[float]:
    """可控时钟,每次调用 next() 推进 1 秒。"""
    t = [1_700_000_000.0]
    def fn() -> float:
        return t[0]
    fn.advance = lambda dt: t.__setitem__(0, t[0] + dt)  # type: ignore[attr-defined]
    fn.set = lambda v: t.__setitem__(0, v)  # type: ignore[attr-defined]
    return fn


# =============================================================================
# I1: 写入 gate 关闭后静默丢弃
# =============================================================================


class TestL1HoldsReidEmb:
    """L1 crop 在 push 阶段就持有 reid_embedding(每次 push 都尝试拉一次)。"""

    def test_push_crop_pulls_emb_for_l1_crop(self):
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        pool = TierUPool(config=TierUConfig(l1_capacity=30), reid_provider=provider)

        # 第一次 push,L1 crop 应当直接拿到 emb
        pool.push_crop(_make_crop("cam-a", 1, 0, 1.0))
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l1) == 1
        c = entry.crops_l1[0]
        assert c.reid_embedding is not None, "push_crop 应给 L1 crop 拉 emb"
        np.testing.assert_array_equal(c.reid_embedding, emb)

    def test_push_crop_does_not_overwrite_existing_emb(self):
        """如果 CropEntry 自带 reid_embedding,push 不应覆盖。"""
        provider = _MockReIDProvider()
        new_emb = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
        provider.set_embedding("cam-a", 1, new_emb)
        pool = TierUPool(config=TierUConfig(l1_capacity=30), reid_provider=provider)

        pre_emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        crop = _make_crop("cam-a", 1, 0, 1.0)
        crop.reid_embedding = pre_emb
        pool.push_crop(crop)
        # 调用方传进来的 emb 优先
        np.testing.assert_array_equal(
            pool._entries[("cam-a", 1)].crops_l1[0].reid_embedding, pre_emb,
        )

    def test_push_crop_provider_miss_leaves_emb_none(self):
        """provider 返 None 时 L1 crop emb 仍是 None,不报错。"""
        provider = _MockReIDProvider()
        # 不 set_embedding,get_embedding 会返 None
        pool = TierUPool(config=TierUConfig(l1_capacity=30), reid_provider=provider)
        pool.push_crop(_make_crop("cam-a", 1, 0, 1.0))
        assert pool._entries[("cam-a", 1)].crops_l1[0].reid_embedding is None


class TestWriteGate:
    def test_push_after_close_silently_discards(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=5))
        # 先 push 满 L1 触发 entry 创建 + 晋级
        for i in range(5):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()

        # close gate
        closed = pool.close_write_gate("cam-a", 1)
        assert closed == 1

        # 再 push,L1 不增长
        pool.push_crop(_make_crop("cam-a", 1, 100, 200.0))
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l1) == 0  # 被静默丢弃
        assert entry.write_open is False

    def test_disabled_pool_silently_discards(self):
        pool = TierUPool(config=TierUConfig(enabled=False, l1_capacity=5))
        pool.push_crop(_make_crop("cam-a", 1, 0, 1.0))
        assert pool._entries == {}  # enabled=False 时连 entry 都不创建


# =============================================================================
# I2: L1 累计满才晋级
# =============================================================================


class TestL1Flush:
    def test_under_capacity_does_not_promote(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=10))
        for i in range(9):  # 不满
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l1) == 9  # 没动
        assert len(entry.crops_l2) == 0  # 没晋级

    def test_at_capacity_promotes_best(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=5, l2_capacity=10))
        # sharpness 递增,期望挑最后一张
        for i in range(5):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i, sharpness=100.0 + i * 10))
        pool.flush_if_due()
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l1) == 0  # 清空
        assert len(entry.crops_l2) == 1
        assert entry.crops_l2[0].sharpness == 140.0  # 最高的

    def test_l2_fifo_evicts_oldest(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=2, l2_capacity=3))
        # 灌 4 轮 (each 2 张 → 触发 flush) → L2 满 3 后第 4 轮弹最旧
        for round_i in range(4):
            for j in range(2):
                pool.push_crop(_make_crop(
                    "cam-a", 1, round_i * 2 + j, 1.0 + round_i,
                    sharpness=100.0 + round_i,
                ))
            pool.flush_if_due()
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l2) == 3
        # FIFO 满弹最旧 → 当前 L2 应该是 round 1/2/3 的代表帧(sharpness 101/102/103)
        sharps = [c.sharpness for c in entry.crops_l2]
        assert sharps == [101.0, 102.0, 103.0]

    def test_all_fail_quality_gate_clears_l1(self):
        """L1 满了但全部不合格 → 清 L1 重新累积,不强行写。"""
        pool = TierUPool(config=TierUConfig(l1_capacity=3, sharpness_min=999.0))  # 极端阈值
        for i in range(3):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i, sharpness=10.0))  # 都低
        pool.flush_if_due()
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l1) == 0  # 清空了
        assert len(entry.crops_l2) == 0  # 但没写 L2


# =============================================================================
# I3: intra_cam 去重不物理合并 entry,通过 cluster_id 挂关系
# =============================================================================


class TestIntraCamDedup:
    def test_two_tracks_same_person_share_cluster(self):
        provider = _MockReIDProvider()
        # 同人不同 track:embedding 高度相似(余弦 = 1.0)
        emb = np.array([1.0, 0.0, 0.0] + [0.0] * 125, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        provider.set_embedding("cam-a", 2, emb.copy())

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i))
        pool.flush_if_due()

        # 两个 entry 都还在(I3:不物理合并)
        assert len(pool._entries) == 2
        e1 = pool._entries[("cam-a", 1)]
        e2 = pool._entries[("cam-a", 2)]
        # cluster_id 相同(挂同一等价类)
        assert e1.cluster_id is not None
        assert e1.cluster_id == e2.cluster_id
        # cluster 成员含两条
        cluster = pool._clusters[e1.cluster_id]
        assert cluster.members == {("cam-a", 1), ("cam-a", 2)}

    def test_two_tracks_different_persons_separate_clusters(self):
        provider = _MockReIDProvider()
        # 不同人:正交 embedding(余弦 = 0)
        emb1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb1)
        provider.set_embedding("cam-a", 2, emb2)

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i))
        pool.flush_if_due()

        e1 = pool._entries[("cam-a", 1)]
        e2 = pool._entries[("cam-a", 2)]
        assert e1.cluster_id is not None and e2.cluster_id is not None
        assert e1.cluster_id != e2.cluster_id  # 独立 cluster

    def test_short_lived_track_dedups_when_l1_reaches_20pct(self):
        """G 方案:短命 track 在 L1 累积到 20% 容量时跑首次 dedup,不必等 L1 满。

        构造:cluster X 已成熟(L1 满过 flush,有稳定 reid_embedding);
        新建短命 track (cam-a, 99),L1 容量 30 × 20% = 6 张时该触发首次 dedup,
        余弦超阈值应并入 cluster X。

        延迟前 5 张时:did_first_dedup 应仍为 False,cluster_id 还没挂上。
        """
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        provider.set_embedding("cam-a", 99, emb.copy())  # 同人短命 track

        pool = TierUPool(
            config=TierUConfig(l1_capacity=30, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )

        # 1) 让 track 1 走完 L1 满 flush,形成稳定 cluster
        for i in range(30):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        cluster_id_1 = pool._entries[("cam-a", 1)].cluster_id
        assert cluster_id_1 is not None

        # 2) 短命 track 99 推前 5 张(< 20% = 6 张),应该还没触发 dedup
        for i in range(5):
            pool.push_crop(_make_crop("cam-a", 99, i, 999.0 + i))
        entry_99 = pool._entries[("cam-a", 99)]
        assert not entry_99.did_first_dedup, "L1 < 20% 不该触发"
        assert entry_99.cluster_id is None, "未触发 dedup 时不该有 cluster_id"

        # 3) 推第 6 张,L1 累积 = 6 = 20% × 30,应触发首次 dedup,合并到 cluster_id_1
        pool.push_crop(_make_crop("cam-a", 99, 5, 999.5))
        assert entry_99.did_first_dedup, "L1 达 20% 应已触发首次 dedup"
        assert entry_99.cluster_id == cluster_id_1, (
            "G 方案应该在 L1 达 20% 容量时把短命 track 合并到稳定 cluster"
        )

        # 4) 第 7 张 push 不该重复触发 G 方案 dedup(did_first_dedup 已 True)。
        #    注:push_crop 每次仍会调 provider 给 L1 crop 拉 emb,但不再走 G 方案
        #    重试路径——通过 last_g_attempt_frame 不变来判定。
        g_frame_before = entry_99.last_g_attempt_frame
        pool.push_crop(_make_crop("cam-a", 99, 6, 999.6))
        # 仍应保持 cluster 不变(已合并)
        assert entry_99.cluster_id == cluster_id_1
        # G 方案的"上次尝试帧"指针不变(说明 G 路径没再走)
        assert entry_99.last_g_attempt_frame == g_frame_before

    def test_per_crop_emb_populates_cluster_mean(self):
        """flush 进 L2 的 crop 应该携带 per-crop emb;_cluster_mean_embedding
        优先用 L2 crops 的 emb 而非 entry 级快照。"""
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, l2_capacity=5),
            reid_provider=provider,
        )
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()

        entry = pool._entries[("cam-a", 1)]
        # L2 内进的 crop 应当带 reid_embedding
        assert len(entry.crops_l2) == 1
        assert entry.crops_l2[0].reid_embedding is not None
        np.testing.assert_array_equal(entry.crops_l2[0].reid_embedding, emb)

        # cluster mean 算出来跟单 emb 一致(只 1 张 crop)
        cluster_mean = pool._cluster_mean_embedding(entry.cluster_id)
        assert cluster_mean is not None
        # mean 跟 emb 几乎一致(模长=1 已归一化)
        np.testing.assert_allclose(cluster_mean, emb, atol=1e-6)

    def test_centroid_linkage_rejects_boundary_emb_bridging(self):
        """centroid linkage 比 single-link 稳健:cluster 内有一个"边界 emb"时
        不会因为新进的 entry 跟那个边界 emb 余弦高就被错误并进来。

        构造:cluster X 已有 emb_main(主向量)+ emb_edge(边界向量),
        emb_main 与 emb_edge 余弦 = 0.88(< 0.9 阈值但接近)。
        新 entry e_new 跟 emb_edge 余弦 = 0.95,跟 emb_main 余弦 = 0.6。
        - single-link 会因 max(0.95, 0.6) = 0.95 ≥ 0.9 合并(错误,因 e_new
          实际跟 cluster 主体方向偏离很大)。
        - centroid linkage 用 mean(emb_main, emb_edge) 当代表,余弦计算
          落到 mean 上,值会显著低于 0.95(因为 mean 偏向 emb_main),不合并。
        """
        provider = _MockReIDProvider()

        # 构造三个向量:主向量、边界向量、新进向量
        emb_main = np.zeros(128, dtype=np.float32)
        emb_main[0] = 1.0
        # emb_edge:跟 emb_main 夹角约 28°(cosine ≈ 0.88)
        emb_edge = np.zeros(128, dtype=np.float32)
        emb_edge[0] = 0.88
        emb_edge[1] = np.sqrt(1 - 0.88 * 0.88)
        # emb_new:跟 emb_edge 几乎一致(0.95),跟 emb_main 偏离(~0.66)
        emb_new = np.zeros(128, dtype=np.float32)
        emb_new[0] = 0.66
        emb_new[1] = np.sqrt(1 - 0.66 * 0.66)

        provider.set_embedding("cam-a", 1, emb_main)
        provider.set_embedding("cam-a", 2, emb_edge)
        provider.set_embedding("cam-a", 3, emb_new)

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        # 先让 1 和 2 形成同 cluster(余弦 ≈ 0.88 < 0.9 不会自动合
        # 但我们手动合一下来构造场景)
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i))
        pool.flush_if_due()
        # 用户级"合并"(模拟某用户操作)——直接改 cluster_id 把 1、2 挂同 cluster
        e1 = pool._entries[("cam-a", 1)]
        e2 = pool._entries[("cam-a", 2)]
        cid = e1.cluster_id
        pool._merge_into_cluster(e2, cid)

        # 现在新 entry 3 进来,余弦跟 e2 (edge) = 0.95 高,跟 e1 (main) = 0.66 低
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 3, i, 100.0 + i))
        pool.flush_if_due()

        e3 = pool._entries[("cam-a", 3)]
        # centroid linkage:用 mean(e1, e2) 当代表 → mean 偏向 main → e3 vs mean
        # 余弦 ≈ 0.83 < 0.9 → 不合并
        assert e3.cluster_id != cid, (
            "centroid linkage 应该拒绝边界 emb 桥接;single-link 会错合"
        )

    def test_g_method_retries_when_emb_unavailable(self):
        """Fix A 护栏:G 方案 emb 拉取失败时不能"一次就死",必须按
        g_retry_interval_frames 节流重试,直到 emb 真的到位。

        构造:cluster X 已稳定,track 99 是同人短命 track。
        - 让 provider 在 frame 5 时仍返 None,frame 10 才返真值
        - L1 累计 6 帧时 G 方案触发(frame=5),provider 返 None
            → did_first_dedup 应保持 False
        - frame=10 时第二次重试,provider 返 emb → 合并到 cluster X
        """
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        # cam-a/99 的 emb 一开始不在表里(get_embedding 返 None)

        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=30, reid_threshold_intra_cam=0.9,
                g_retry_interval_frames=3,
            ),
            reid_provider=provider,
        )

        # 1) 让 track 1 走完 L1 满 flush,形成稳定 cluster
        for i in range(30):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        cluster_id_1 = pool._entries[("cam-a", 1)].cluster_id
        assert cluster_id_1 is not None

        # 2) 短命 track 99 推 6 张,触发 G 方案但 provider 返 None
        for i in range(6):
            pool.push_crop(_make_crop("cam-a", 99, i, 999.0 + i))
        entry_99 = pool._entries[("cam-a", 99)]
        assert entry_99.did_first_dedup is False, "emb 拉不到不该设 done"
        assert entry_99.cluster_id is None
        assert entry_99.last_g_attempt_frame == 5, "G 方案首次尝试应记 frame 5"

        # 3) frame=6,7 推,gap=1,2 < 3,G 方案不应重试(last_g_attempt_frame 不更新)。
        #    注:push_crop 每次都会调 provider 给 L1 crop 拉 emb,所以 provider.calls
        #    会增长——通过 last_g_attempt_frame 判定 G 路径是否走过更精确。
        for i in range(6, 8):
            pool.push_crop(_make_crop("cam-a", 99, i, 999.0 + i))
        assert entry_99.last_g_attempt_frame == 5, "retry_gap 内 G 方案不该重新尝试"
        assert entry_99.did_first_dedup is False

        # 4) frame=8,gap=3 满足重试,但 provider 仍返 None(emb 还没到位)
        pool.push_crop(_make_crop("cam-a", 99, 8, 999.0 + 8))
        assert entry_99.did_first_dedup is False, "emb 还没拉到,不该设 done"
        assert entry_99.last_g_attempt_frame == 8, "frame 8 应重试 1 次"

        # 5) frame=11,gap=3 满足重试,provider 现在能返 emb → 合并到 cluster X
        provider.set_embedding("cam-a", 99, emb.copy())
        pool.push_crop(_make_crop("cam-a", 99, 11, 1011.0))
        assert entry_99.did_first_dedup is True, "emb 到手后应设 done"
        assert entry_99.cluster_id == cluster_id_1, "应合并到稳定 cluster"


# =============================================================================
# fetch 端 crop 级去重(ReID + pHash 联合)
# =============================================================================


class TestFetchCropDedup:
    """`_cluster_candidate_for` 内 crop 级去重:双维度联合判定,同姿势同视角
    的近副本被丢,同人不同姿势保留。"""

    @staticmethod
    def _crop_with_pattern(
        cam: str, tid: int, frame: int, ts: float, pattern: int,
        emb: np.ndarray | None = None, sharpness: float = 100.0,
    ) -> CropEntry:
        """生成带可控视觉模式的 crop。pattern 决定 pHash:
        0/0' → 同模式但加微小噪声(pHash 接近)
        1     → 完全不同(pHash 远离)
        """
        rng = np.random.default_rng(seed=pattern * 7919 + 1)
        img = rng.integers(0, 256, (128, 64, 3), dtype=np.uint8)
        # 加点 frame 相关的微噪声让 pHash 略微浮动但仍判同
        if pattern >= 100:
            # "noisy variant" 模式 100+:在 base pattern 0 上加小噪声
            base = np.random.default_rng(seed=1).integers(0, 256, (128, 64, 3), dtype=np.uint8)
            noise = (rng.integers(-5, 6, (128, 64, 3))).astype(np.int16)
            img = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return CropEntry(
            cam_id=cam, track_id=tid, frame_index=frame, captured_at=ts,
            body_crop=img,
            face_crop=None,
            sharpness=sharpness,
            bbox_xyxy=(10, 10, 74, 138),
            detector_conf=0.8,
            reid_embedding=emb,
        )

    def test_phash_close_and_reid_close_dedups(self):
        """pHash 接近 + ReID 余弦接近 → 视为重复,只保留最锐那张。"""
        from miloco.perception.engine.identity.tier_u import _dedup_crops_for_fetch
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        c1 = self._crop_with_pattern("cam-a", 1, 0, 1.0, pattern=100, emb=emb, sharpness=200.0)
        c2 = self._crop_with_pattern("cam-a", 1, 1, 2.0, pattern=100, emb=emb.copy(), sharpness=150.0)
        # 输入按 sharpness 降序,c1 最锐先入 selected
        out = _dedup_crops_for_fetch([c1, c2])
        assert len(out) == 1, "pHash + ReID 都接近的近副本应被去重"
        assert out[0] is c1, "保留最锐那张"

    def test_phash_far_keeps_both_even_if_reid_close(self):
        """pHash 远(不同姿势)但 ReID 接近(同人)→ 不去重,保留多样性。"""
        from miloco.perception.engine.identity.tier_u import _dedup_crops_for_fetch
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        c1 = self._crop_with_pattern("cam-a", 1, 0, 1.0, pattern=0, emb=emb, sharpness=200.0)
        c2 = self._crop_with_pattern("cam-a", 1, 1, 2.0, pattern=1, emb=emb.copy(), sharpness=150.0)
        out = _dedup_crops_for_fetch([c1, c2])
        assert len(out) == 2, "ReID 接近但 pHash 远(不同姿势)应都保留"

    def test_reid_far_keeps_both_even_if_phash_close(self):
        """ReID 远(不同人)→ 不去重,即使 pHash 偶然接近。"""
        from miloco.perception.engine.identity.tier_u import _dedup_crops_for_fetch
        e1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        # 正交 emb,余弦 = 0
        e2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
        c1 = self._crop_with_pattern("cam-a", 1, 0, 1.0, pattern=100, emb=e1, sharpness=200.0)
        c2 = self._crop_with_pattern("cam-a", 1, 1, 2.0, pattern=100, emb=e2, sharpness=150.0)
        out = _dedup_crops_for_fetch([c1, c2])
        assert len(out) == 2, "ReID 远(不同人)应都保留"

    def test_no_emb_falls_back_to_strict_phash(self):
        """ReID 缺失 → 纯 pHash 用更严阈值(< 22)判重复。"""
        from miloco.perception.engine.identity.tier_u import _dedup_crops_for_fetch
        # 都没 emb,pattern 完全相同 → 严格 pHash 也判重
        c1 = self._crop_with_pattern("cam-a", 1, 0, 1.0, pattern=100, emb=None, sharpness=200.0)
        c2 = self._crop_with_pattern("cam-a", 1, 1, 2.0, pattern=100, emb=None, sharpness=150.0)
        out = _dedup_crops_for_fetch([c1, c2])
        assert len(out) == 1, "ReID 缺失 + pHash 严判接近 → 视为重复"

    def test_fetch_fills_missing_reid_emb_from_entry_snapshot(self):
        """L1 / L2 crop 在 push 时 provider 暂时返 None,但后来 entry 级 emb
        (G 方案重试或 flush 时)被填上 → fetch 时应该兜底拷贝给所有 crop,
        让下游 BodySample → _write_embedding 能落 .npy。

        构造:provider 一开始返 None → 推 6 张 L1 全是 emb=None; 然后
        provider 才能返 emb → 触发 G 方案重试,entry 级 emb 拉到;
        最后 fetch 时 L1 crop 仍带不到 push 时的 emb(因为只在 push 那刻拉),
        但应该从 entry 级 emb 兜底拷过来。
        """
        provider = _MockReIDProvider()
        # 一开始 provider 不知道 cam-a/1,get_embedding 返 None
        pool = TierUPool(
            config=TierUConfig(l1_capacity=30, reid_threshold_intra_cam=0.9,
                               g_retry_interval_frames=3),
            reid_provider=provider,
        )
        # 推 6 张:provider 全返 None,L1 crop emb 全是 None
        for i in range(6):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        entry = pool._entries[("cam-a", 1)]
        assert entry.reid_embedding is None
        assert all(c.reid_embedding is None for c in entry.crops_l1)

        # 现在 provider 有 emb 了
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        # 再 push 几张触发 G 方案重试(retry_gap=3 满足)
        for i in range(6, 9):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        assert entry.reid_embedding is not None, "G 重试应已拉到 entry emb"
        # 前 6 张 push 时 provider 还没有数据,emb 仍 None
        early_l1 = entry.crops_l1[:6]
        assert all(c.reid_embedding is None for c in early_l1)

        cands = pool.fetch()
        cand = cands[0]
        # 关键断言:fetch 兑底后所有 L1 crop 都带上 entry 级 emb,
        # 包括早期 push 时 emb=None 的那批
        assert all(c.reid_embedding is not None for c in cand.all_l1_crops), (
            "fetch 应给早期 emb=None 的 L1 crop 用 entry 级 emb 兜底"
        )

    def test_fetch_extracts_emb_on_demand_and_merges_singletons(self):
        """fetch 时给 entry.reid_embedding=None 的 entry 现场抽 emb,回写,
        重跑 intra_cam_dedup → 同人的 singleton 被合到现有 cluster。

        构造:
          - cluster X (track 1) 已成熟,entry.reid_embedding 已有
          - track 4 是同人短命 phantom,provider 一直返 None,entry.reid_embedding
            停留 None,intra_cam_dedup_tick 过滤掉它,变成 singleton
          - fetch(reid_extractor=...) 给 track 4 现场抽 emb(从 L1 最锐 crop)、
            回写 entry,重跑 dedup → 合并到 cluster X
        """
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        # cam-a/4 故意不 set,provider 永远返 None
        pool = TierUPool(
            config=TierUConfig(l1_capacity=30, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        # 1) track 1 走完 L1 满 flush → 稳定 cluster
        for i in range(30):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        cluster_id_1 = pool._entries[("cam-a", 1)].cluster_id
        assert cluster_id_1 is not None

        # 2) track 4 短命:推 3 张,provider 返 None,entry.reid_embedding 留 None
        for i in range(3):
            pool.push_crop(_make_crop("cam-a", 4, 100 + i, 200.0 + i))
        entry_4 = pool._entries[("cam-a", 4)]
        assert entry_4.reid_embedding is None
        assert entry_4.cluster_id is None  # intra_cam_dedup_tick 过滤了它

        # 3) fetch 不带 reid_extractor → track 4 仍是 singleton
        cands = pool.fetch()
        ids = [c.cluster_id for c in cands]
        assert any(cid.startswith("singleton:") for cid in ids), (
            "无 reid_extractor 时 track 4 应该仍是 singleton"
        )

        # 4) fetch 带 reid_extractor(返与 track 1 同向 emb)→ track 4 应合并
        class _FakeExtractor:
            calls = 0
            def extract_feature(self, body_crop):
                _FakeExtractor.calls += 1
                return emb.copy()

        extractor = _FakeExtractor()
        cands = pool.fetch(reid_extractor=extractor)
        assert _FakeExtractor.calls >= 1, "应至少调用一次 extract_feature 给 track 4"

        # entry 4 现在应有 emb 回写
        assert entry_4.reid_embedding is not None
        np.testing.assert_array_equal(entry_4.reid_embedding, emb)
        # 应合并到 cluster X
        assert entry_4.cluster_id == cluster_id_1, (
            "现场抽 emb 后 dedup tick 应把 track 4 合到稳定 cluster"
        )

    def test_fetch_reid_extractor_skipped_for_entry_with_emb(self):
        """已经有 entry.reid_embedding 的 entry,fetch 时不应再调 extract_feature。"""
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()

        class _FakeExtractor:
            calls = 0
            def extract_feature(self, body_crop):
                _FakeExtractor.calls += 1
                return emb.copy()

        extractor = _FakeExtractor()
        pool.fetch(reid_extractor=extractor)
        assert _FakeExtractor.calls == 0, "entry 已有 emb 时不该再抽"

    def test_fetch_emb_writeback_persists_across_fetches(self):
        """第一次 fetch 抽到的 emb 回写到 entry,后续 fetch 不再走兜底抽取。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=30), reid_provider=provider)
        # 只推 3 张,provider 全返 None
        for i in range(3):
            pool.push_crop(_make_crop("cam-a", 99, i, 1.0 + i))

        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)

        class _FakeExtractor:
            calls = 0
            def extract_feature(self, body_crop):
                _FakeExtractor.calls += 1
                return emb.copy()

        extractor = _FakeExtractor()
        pool.fetch(reid_extractor=extractor)
        first_calls = _FakeExtractor.calls
        assert first_calls >= 1

        # 第二次 fetch,emb 已回写
        pool.fetch(reid_extractor=extractor)
        assert _FakeExtractor.calls == first_calls, (
            "回写后第二次 fetch 不应再调 extract_feature"
        )

    def test_fetch_dedups_cluster_l2_crops(self):
        """端到端:cluster 内多 entry 的 L2 crop 经 fetch 时按 ReID+pHash 去重。

        构造同人 2 track,各 flush 1 张 L2,pHash 和 ReID 都接近 → fetch 出来
        cluster 的 all_l2_crops 长度 = 1(去重后)。
        """
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        provider.set_embedding("cam-a", 2, emb.copy())

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        # 两个 track,同人,L1 都满 2 → 各 flush 1 张到 L2(用同模式生成 crop
        # 让 pHash 接近)
        for cam_tid, frame_base in [((("cam-a", 1)), 0), ((("cam-a", 2)), 10)]:
            for i in range(2):
                c = self._crop_with_pattern(
                    cam_tid[0], cam_tid[1], frame_base + i, 1.0 + frame_base + i,
                    pattern=100, emb=emb.copy(), sharpness=200.0,
                )
                pool.push_crop(c)
        pool.flush_if_due()

        candidates = pool.fetch()
        # intra_cam dedup 把两 track 合到一个 cluster
        assert len(candidates) == 1, (
            f"同人两 track 应合并为 1 个 cluster,got {len(candidates)}"
        )
        cand = candidates[0]
        # fetch 端 crop 级去重后只剩 1 张(两张近副本)
        assert len(cand.all_l2_crops) == 1, (
            f"cluster 内两张近副本 L2 应去重到 1 张,got {len(cand.all_l2_crops)}"
        )


# =============================================================================
# 零额外推理硬约束护栏
# =============================================================================


class TestZeroExtraExtract:
    def test_pool_never_calls_extract_feature(self):
        """陌生人池任何代码路径都不应触发 HumanReID.extract_feature。

        监视全局 HumanReID.extract_feature——监控池里跑完 push/flush/intra_cam_dedup/
        fetch/close 一整套后调用次数 = 0。
        """
        # 直接 patch HumanReID 类(模块级)
        with patch(
            "miloco.perception.engine.identity.tracker.human_reid.HumanReID.extract_feature"
        ) as spy:
            provider = _MockReIDProvider()
            emb = np.array([1.0, 0.0, 0.0] + [0.0] * 125, dtype=np.float32)
            provider.set_embedding("cam-a", 1, emb)
            provider.set_embedding("cam-b", 1, emb.copy())

            pool = TierUPool(
                config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
                reid_provider=provider,
            )
            # 完整跑一遍流水
            for cam in ("cam-a", "cam-b"):
                for i in range(2):
                    pool.push_crop(_make_crop(cam, 1, i, 1.0 + i))
            pool.flush_if_due()
            # fetch 触发跨 cam union
            pool.fetch()
            pool.close_write_gate("cam-a", 1)
            pool.tick_ttl()
            pool.gc_lru_if_over_budget()

        # 全程零调用
        assert spy.call_count == 0, (
            f"陌生人池触发了 {spy.call_count} 次 HumanReID.extract_feature,"
            "违反'零额外推理'硬约束"
        )


# =============================================================================
# fetch
# =============================================================================


class TestFetch:
    def _seed_two_cams(self, pool: TierUPool, provider: _MockReIDProvider, sim: float):
        """给两 cam 各 push 一个 track,emb 模长根据 sim 控制相似度。"""
        if sim >= 1.0:
            e_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
            e_b = e_a.copy()
        else:
            e_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
            # 构造 cosine = sim 的两向量
            e_b = np.array([sim, np.sqrt(1 - sim * sim)] + [0.0] * 126, dtype=np.float32)
        provider.set_embedding("cam-a", 1, e_a)
        provider.set_embedding("cam-b", 1, e_b)
        for cam in ("cam-a", "cam-b"):
            for i in range(2):
                pool.push_crop(_make_crop(cam, 1, i, 1.0 + i))
        pool.flush_if_due()

    def test_fetch_target_returns_cluster_members(self):
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2),
            reid_provider=provider,
        )
        # 单 track,自成 cluster
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()

        cands = pool.fetch(cam_id="cam-a", target_track_id=1)
        assert len(cands) == 1
        assert cands[0].members == [("cam-a", 1)]
        assert cands[0].span_cam_count == 1

    def test_fetch_by_cluster_id_returns_members_ignoring_window(self):
        """by-id 查询不套 window —— entry push_ts 远早于 default window 仍返回。

        钉住"陌生人页能看到的候选一定能注册"这条 UX 承诺(SKILL §2.4)。
        关键:用可控 clock + 推进时间, 让 entry 真的落在窗口外,
        再用 list 模式的 negative assertion 证实"window 这条路径确实过滤生效",
        这样 by-id 仍返回结果才有意义 —— 否则两条路径都拿回来,等于没测 bypass。
        """
        provider = _MockReIDProvider()
        provider.set_embedding(
            "cam-a", 1, np.array([1.0] + [0.0] * 127, dtype=np.float32),
        )
        clock = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, fetch_window_sec=300.0),
            reid_provider=provider,
            now_fn=clock,
        )
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        cluster_id = next(iter(pool._clusters))

        # 推进时钟让 entry 的 last_l1_push_ts 落到窗口外
        clock.advance(1000)  # 1000 > fetch_window_sec=300

        # baseline: list 模式被 window 过滤掉 (否则下一行 by-id 等于啥也没证)
        assert pool.fetch() == []

        # by-id: 不套 window, 仍返回该 cluster
        cands = pool.fetch(target_cluster_id=cluster_id)
        assert len(cands) == 1
        assert cands[0].cluster_id == cluster_id
        assert cands[0].members == [("cam-a", 1)]

    def test_fetch_by_cluster_id_unknown_returns_empty(self):
        """target_cluster_id 不存在 → 返 [](而非抛错)。"""
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2),
            reid_provider=_MockReIDProvider(),
        )
        cands = pool.fetch(target_cluster_id="ffffffffffffffffffffffffffffffff")
        assert cands == []

    # 注: 原 ``test_fetch_global_cross_cam_union`` 在主线 D 改 ``_cross_cam_union``
    # → ``_cluster_pairwise_union`` 后跟 ``TestClusterPairwiseUnion::
    # test_cross_cam_still_works`` 覆盖重叠, 已删除以避免 grep 误导 + 命名混乱。

    def test_fetch_global_different_persons_stay_separate(self):
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.9,
                reid_threshold_cross_cam=0.9,
            ),
            reid_provider=provider,
        )
        # 两 cam 不同人
        self._seed_two_cams(pool, provider, sim=0.3)
        cands = pool.fetch()
        assert len(cands) == 2  # 两个独立 cluster
        for c in cands:
            assert c.span_cam_count == 1

    def test_fetch_empty_pool_returns_empty(self):
        pool = TierUPool()
        assert pool.fetch() == []


# =============================================================================
# close_write_gate(决策 1.1 α:关闭 cluster 全部成员)
# =============================================================================


class TestCloseWriteGate:
    def test_close_propagates_to_all_cluster_members(self):
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        provider.set_embedding("cam-a", 2, emb.copy())

        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.9),
            reid_provider=provider,
        )
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i))
        pool.flush_if_due()

        # 关闭 (cam-a, 1) 应该连带关闭 (cam-a, 2)
        closed = pool.close_write_gate("cam-a", 1)
        assert closed == 2  # 两个成员都被关闭
        assert pool._entries[("cam-a", 1)].write_open is False
        assert pool._entries[("cam-a", 2)].write_open is False

    def test_close_clears_l1_l2(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=3, l2_capacity=5))
        for i in range(3):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()  # L2 有 1 张
        pool.push_crop(_make_crop("cam-a", 1, 100, 100.0))  # L1 有 1 张
        pool.close_write_gate("cam-a", 1)
        e = pool._entries[("cam-a", 1)]
        assert len(e.crops_l1) == 0
        assert len(e.crops_l2) == 0


# =============================================================================
# TTL / LRU
# =============================================================================


class TestTTL:
    def test_inactive_entry_evicted_after_ttl(self):
        clock = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, ttl_inactive_sec=3600.0),
            now_fn=clock,
        )
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        assert ("cam-a", 1) in pool._entries

        # 推进时钟超 TTL
        clock.advance(3700.0)  # type: ignore[attr-defined]
        n = pool.tick_ttl()
        assert n == 1
        assert ("cam-a", 1) not in pool._entries

    def test_active_entry_not_evicted(self):
        clock = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, ttl_inactive_sec=3600.0),
            now_fn=clock,
        )
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, clock()))
        pool.flush_if_due()

        # 推进 1 小时,但中途仍 push(刷新 last_l1_push_ts)
        clock.advance(1800.0)  # type: ignore[attr-defined]
        pool.push_crop(_make_crop("cam-a", 1, 100, clock()))
        clock.advance(1800.0)  # type: ignore[attr-defined]  # 累计 3600s,但上次 push 是 1800s 前

        n = pool.tick_ttl()
        assert n == 0


class TestLRU:
    def test_over_budget_evicts_oldest(self):
        clock = _clock()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2, l2_capacity=10,
                memory_budget_mb=0,  # 极端:任何占用都触发
            ),
            now_fn=clock,
        )
        # 三个 entry,时间不同
        for tid in (1, 2, 3):
            clock.advance(10.0)  # type: ignore[attr-defined]
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, clock()))
            pool.flush_if_due()

        assert len(pool._entries) == 3
        n = pool.gc_lru_if_over_budget()
        assert n >= 1
        # 最旧的应该最先被弹(tid=1)
        assert ("cam-a", 1) not in pool._entries


# =============================================================================
# DeepSortReIDProvider 适配器
# =============================================================================


class TestDeepSortReIDProvider:
    def test_dispatches_by_cam_id(self):
        t1 = MagicMock()
        t1.get_track_embedding.return_value = np.ones(128, dtype=np.float32)
        t2 = MagicMock()
        t2.get_track_embedding.return_value = np.zeros(128, dtype=np.float32)

        provider = DeepSortReIDProvider(trackers={"cam-a": t1, "cam-b": t2})
        emb_a = provider.get_embedding("cam-a", 7)
        emb_b = provider.get_embedding("cam-b", 7)
        assert np.array_equal(emb_a, np.ones(128, dtype=np.float32))
        assert np.array_equal(emb_b, np.zeros(128, dtype=np.float32))
        t1.get_track_embedding.assert_called_once_with(7)
        t2.get_track_embedding.assert_called_once_with(7)

    def test_unknown_cam_returns_none(self):
        provider = DeepSortReIDProvider(trackers={"cam-a": MagicMock()})
        assert provider.get_embedding("cam-unknown", 1) is None

    def test_single_tracker_mode(self):
        t = MagicMock()
        t.get_track_embedding.return_value = np.ones(128, dtype=np.float32)
        provider = DeepSortReIDProvider(trackers=t)  # 非 dict
        emb = provider.get_embedding("cam-anything", 1)
        assert np.array_equal(emb, np.ones(128, dtype=np.float32))


# =============================================================================
# status
# =============================================================================


class TestStatus:
    def test_status_reports_counts(self):
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i))
        pool.flush_if_due()
        st = pool.status()
        assert st["entries_total"] == 2
        assert st["entries_active"] == 2
        assert st["memory_mb"] > 0
        assert st["memory_budget_mb"] == 128  # 默认值


# =============================================================================
# dump_to / load_from(离线调试快照)
# =============================================================================


class TestDumpAndLoad:
    """round-trip:dump 完整状态后 load 出来,内部结构 + 行为应当一致。"""

    def test_round_trip_preserves_full_state(self, tmp_path):
        provider = _MockReIDProvider()
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb_a)
        provider.set_embedding("cam-a", 2, emb_b)
        cfg = TierUConfig(l1_capacity=2, l2_capacity=3, reid_threshold_intra_cam=0.85)
        pool = TierUPool(config=cfg, reid_provider=provider)

        # 注入两个 track 各 2 帧 → L1 满 flush → 各自挂 cluster_id
        for tid in (1, 2):
            for i in range(2):
                pool.push_crop(_make_crop("cam-a", tid, i, 1.0 + i, sharpness=200.0 + i))
        pool.flush_if_due()

        # 应当两个 cluster(不同 emb,sim ~ 0 < 0.85)
        before_status = pool.status()
        assert before_status["entries_total"] == 2
        assert before_status["clusters"] == 2

        # dump
        target = str(tmp_path / "snap")
        summary = pool.dump_to(target)
        assert summary["entries"] == 2
        assert summary["clusters"] == 2
        assert summary["arrays"] > 0
        assert (tmp_path / "snap" / "manifest.json").exists()
        assert (tmp_path / "snap" / "arrays.npz").exists()

        # load(不传 config / provider,验证从快照里恢复 config)
        restored = TierUPool.load_from(target)

        # 配置一致
        assert restored.config.l1_capacity == cfg.l1_capacity
        assert restored.config.l2_capacity == cfg.l2_capacity
        assert restored.config.reid_threshold_intra_cam == cfg.reid_threshold_intra_cam

        # 状态计数一致
        rstatus = restored.status()
        assert rstatus["entries_total"] == before_status["entries_total"]
        assert rstatus["clusters"] == before_status["clusters"]

        # entry 关键字段一致
        for key in pool._entries:
            assert key in restored._entries
            orig, rest = pool._entries[key], restored._entries[key]
            assert orig.cluster_id == rest.cluster_id
            assert orig.write_open == rest.write_open
            assert orig.did_first_dedup == rest.did_first_dedup
            assert len(orig.crops_l2) == len(rest.crops_l2)
            assert len(orig.crops_l1) == len(rest.crops_l1)
            # emb 一致
            if orig.reid_embedding is not None:
                np.testing.assert_array_equal(orig.reid_embedding, rest.reid_embedding)
            # L2 crop 像素一致
            for c_orig, c_rest in zip(orig.crops_l2, rest.crops_l2):
                np.testing.assert_array_equal(c_orig.body_crop, c_rest.body_crop)
                assert c_orig.sharpness == c_rest.sharpness

        # cluster 成员一致
        for cid, c_orig in pool._clusters.items():
            assert cid in restored._clusters
            assert c_orig.members == restored._clusters[cid].members

    def test_load_with_override_config(self, tmp_path):
        """显式传入新 config → 离线调阈值场景:同一快照不同阈值对比"""
        provider = _MockReIDProvider()
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        provider.set_embedding("cam-a", 1, emb)
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        target = str(tmp_path / "snap")
        pool.dump_to(target)

        # load 时换阈值 0.7,验证 config 用入参不是快照里的
        new_cfg = TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.7)
        restored = TierUPool.load_from(target, config=new_cfg)
        assert restored.config.reid_threshold_intra_cam == 0.7

    def test_load_drops_fields_removed_since_snapshot(self, tmp_path, caplog):
        """快照里带着本版本已删掉的字段 → 丢弃并告警，而不是抛 TypeError。

        dump 侧是 ``vars(self.config)`` 原样落盘，所以任何删过字段的版本去读旧快照都会
        踩到这条路径。此前它零覆盖：三处快照用例都只 load 当前进程 dump 出来的新快照，
        跨版本那条路一次都没走过，所以删字段时 CI 全绿、值班的人在排障现场才吃到一个
        什么都不提示的 TypeError。

        版本闸不背这个锅：纯死字段的删除不改变任何语义，为它把存量快照全部作废不划算。
        """
        import json

        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        pool.push_crop(_make_crop("cam-a", 1, 0, 1.0))
        target = str(tmp_path / "snap")
        pool.dump_to(target)

        # 模拟"旧版本 dump 出来的快照"：manifest 里多一个当前 TierUConfig 没有的字段
        manifest_path = tmp_path / "snap" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["config"]["area_ratio_min"] = 0.05  # 本 PR 删掉的那个死字段
        manifest["config"]["some_future_dead_field"] = 123
        manifest_path.write_text(json.dumps(manifest))

        with caplog.at_level(logging.WARNING):
            restored = TierUPool.load_from(target)

        assert restored.config.l1_capacity == 2  # 认得的字段照常还原
        assert not hasattr(restored.config, "area_ratio_min")
        assert "area_ratio_min" in caplog.text and "some_future_dead_field" in caplog.text, (
            "被丢掉的字段必须出现在告警里，否则排障的人不知道快照与代码差在哪"
        )

    def test_load_version_mismatch_raises(self, tmp_path):
        """快照 version 不匹配 → raise ValueError,防止跨版本悄悄加载错乱"""
        import json
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        pool.push_crop(_make_crop("cam-a", 1, 0, 1.0))
        target = str(tmp_path / "snap")
        pool.dump_to(target)
        # 篡改 version
        manifest_path = tmp_path / "snap" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = 999
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="version mismatch"):
            TierUPool.load_from(target)

    def test_v1_snapshot_rejected(self, tmp_path):
        """v1 真实快照 (entry.cam_id=scope_label) 必须被 load_from 拒掉。

        防止退化:谁要是把 _SNAPSHOT_VERSION 改回 1 或写软兼容,这条会红 ——
        因为 v1 → v2 是 cam_id 字段语义从 scope_label 切到 device_id, 静默 load
        会让离线 fetch 错位。
        """
        import json

        import numpy as np
        target = tmp_path / "snap_v1"
        target.mkdir()
        # 最小 v1 manifest:version=1, 一个 entry with scope_label 风格 cam_id
        manifest = {
            "version": 1,
            "dumped_at": 1700000000.0,
            "config": {},
            "entries": [{
                "cam_id": "客厅-dev0",  # v1 风格 scope_label
                "track_id": 7,
                "last_l1_push_ts": 1.0,
                "embedding_snapshot_ts": 0.0,
                "embedding_sharpness": 0.0,
                "cluster_id": None,
                "embedding_dirty": False,
                "write_open": True,
                "did_first_dedup": False,
                "last_g_attempt_frame": -1,
                "crops_l1": [],
                "crops_l2": [],
            }],
            "clusters": [],
            "match_cache": [],
        }
        (target / "manifest.json").write_text(json.dumps(manifest))
        np.savez_compressed(target / "arrays.npz")  # 空 arrays, load 走到 version 检查就 raise
        # match 只验证"看到了 v1 输入",不绑定当前 _SNAPSHOT_VERSION 字面值——
        # 未来 v2→v3 bump 时这条不用跟着改
        with pytest.raises(ValueError, match=r"got 1"):
            TierUPool.load_from(str(target))

    def test_dump_handles_face_crop(self, tmp_path):
        """face_crop 不为 None 时 → 落盘 + 还原成功"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        crop = _make_crop("cam-a", 1, 0, 1.0)
        # 用 < face_crop_max_height(128) 的尺寸,push_crop 不会动它
        crop.face_crop = np.ones((48, 48, 3), dtype=np.uint8) * 64
        pool.push_crop(crop)
        target = str(tmp_path / "snap")
        pool.dump_to(target)
        restored = TierUPool.load_from(target)
        rcrop = restored._entries[("cam-a", 1)].crops_l1[0]
        assert rcrop.face_crop is not None
        np.testing.assert_array_equal(rcrop.face_crop, crop.face_crop)


# =============================================================================
# push_crop 入口等比 resize(对齐 omni gallery 设计,body 高 256 / face 高 128)
# =============================================================================


class TestResizeOnPush:
    """body / face 入池前等比缩放到 max_height,只 downscale 不 upscale。"""

    def test_large_body_shrunk_to_256_keeps_aspect(self):
        """800×400 body(h>256) → 缩到 h=256,宽按比例 128,宽高比保留"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        crop = _make_crop("cam-a", 1, 0, 1.0)
        crop.body_crop = np.ones((800, 400, 3), dtype=np.uint8) * 200  # h=800,w=400
        original_aspect = 400 / 800
        pool.push_crop(crop)
        stored = pool._entries[("cam-a", 1)].crops_l1[0].body_crop
        h, w = stored.shape[:2]
        assert h == 256
        assert w == 128  # 400 * 256 / 800 = 128
        assert abs((w / h) - original_aspect) < 1e-6

    def test_small_body_left_alone_no_upscale(self):
        """h=128 < 256 → 不动,避免无意义放大"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        crop = _make_crop("cam-a", 1, 0, 1.0)  # body 128×64
        original = crop.body_crop.copy()
        pool.push_crop(crop)
        stored = pool._entries[("cam-a", 1)].crops_l1[0].body_crop
        np.testing.assert_array_equal(stored, original)

    def test_large_face_shrunk_to_128(self):
        """200×200 face(h>128) → 缩到 h=128,正方形仍正方形"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        crop = _make_crop("cam-a", 1, 0, 1.0)
        crop.face_crop = np.ones((200, 200, 3), dtype=np.uint8) * 100
        pool.push_crop(crop)
        stored = pool._entries[("cam-a", 1)].crops_l1[0].face_crop
        assert stored is not None
        assert stored.shape == (128, 128, 3)

    def test_resize_disabled_when_max_height_zero(self):
        """config.body_crop_max_height=0 → resize 关闭,大 body 也原样存"""
        pool = TierUPool(config=TierUConfig(
            l1_capacity=2, body_crop_max_height=0, face_crop_max_height=0,
        ))
        crop = _make_crop("cam-a", 1, 0, 1.0)
        crop.body_crop = np.ones((800, 400, 3), dtype=np.uint8) * 200
        crop.face_crop = np.ones((300, 300, 3), dtype=np.uint8) * 100
        pool.push_crop(crop)
        stored_body = pool._entries[("cam-a", 1)].crops_l1[0].body_crop
        stored_face = pool._entries[("cam-a", 1)].crops_l1[0].face_crop
        assert stored_body.shape == (800, 400, 3)
        assert stored_face.shape == (300, 300, 3)

    def test_memory_bytes_reflects_shrunk_size(self):
        """resize 应体感到 memory_bytes — 缩前 ~1MB,缩后 ~100KB"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # 800×400×3 = 960000 字节 → resize 到 256×128×3 = 98304 字节
        crop = _make_crop("cam-a", 1, 0, 1.0)
        crop.body_crop = np.ones((800, 400, 3), dtype=np.uint8) * 200
        pool.push_crop(crop)
        bytes_after = pool._entries[("cam-a", 1)].memory_bytes()
        # 单 crop 应当 ~100K 而不是 ~960K
        assert bytes_after < 200_000
        assert bytes_after > 50_000


# =============================================================================
# quality_score: 综合质量评分 (det·0.5 + aspect·0.3 + sharp·0.2) × face_bonus
# =============================================================================


def _make_crop_with_aspect(aspect: float, *, sharpness: float = 100.0,
                            detector_conf: float = 0.8,
                            with_face: bool = False) -> CropEntry:
    """构造指定 aspect (w/h) 的 crop, 用于 quality_score 测试。

    h 固定 100, w = aspect × 100 (clamp 最小 1 防 0 维)。
    """
    h = 100
    w = max(1, int(round(aspect * h)))
    body = np.ones((h, w, 3), dtype=np.uint8) * 128
    face = np.ones((40, 40, 3), dtype=np.uint8) * 128 if with_face else None
    return CropEntry(
        cam_id="cam-x", track_id=1, frame_index=0, captured_at=1.0,
        body_crop=body, face_crop=face,
        sharpness=sharpness, bbox_xyxy=(0, 0, w, h), detector_conf=detector_conf,
    )


class TestAspectDistNormalized:
    def test_sweet_spot_returns_zero(self):
        """aspect=0.4 (1:2.5 站立人形) → dist=0"""
        assert _aspect_dist_normalized(0.4, 0.25, 2.0) == 0.0

    def test_narrow_boundary_returns_one(self):
        """aspect=aspect_min → dist=1 (满罚)"""
        assert _aspect_dist_normalized(0.25, 0.25, 2.0) == 1.0

    def test_wide_boundary_returns_one(self):
        """aspect=aspect_max → dist=1 (满罚)"""
        assert _aspect_dist_normalized(2.0, 0.25, 2.0) == 1.0

    def test_narrow_steeper_than_wide(self):
        """用户核心要求: aspect=0.2 系列应比 aspect=0.8 罚得更重。

        0.2 已超出 aspect_min=0.25 → 走 clamp 后是 1.0;
        0.8 在宽侧 [0.4, 2.0] 内 → dist = (0.8-0.4)/(2.0-0.4) = 0.25。
        """
        # aspect=0.2 < aspect_min → clamp 到 1.0
        narrow_dist = _aspect_dist_normalized(0.2, 0.25, 2.0)
        # aspect=0.8 → 宽侧, 小坡度
        wide_dist = _aspect_dist_normalized(0.8, 0.25, 2.0)
        assert narrow_dist == 1.0
        assert wide_dist == 0.25
        assert narrow_dist > wide_dist  # 0.2 罚得更重

    def test_narrow_side_linear(self):
        """窄侧 [0.25, 0.4] 线性: aspect=0.325 (中点) → dist≈0.5"""
        # (0.4 - 0.325) / (0.4 - 0.25) = 0.075 / 0.15 = 0.5
        d = _aspect_dist_normalized(0.325, 0.25, 2.0)
        assert abs(d - 0.5) < 1e-6

    def test_wide_side_linear(self):
        """宽侧 [0.4, 2.0] 线性: aspect=1.2 → dist=(1.2-0.4)/1.6 = 0.5"""
        d = _aspect_dist_normalized(1.2, 0.25, 2.0)
        assert abs(d - 0.5) < 1e-6


class TestQualityScore:
    def test_sweet_spot_max_score_no_face(self):
        """sweet spot aspect + 高 det + 高 sharp + no face → 接近 base 满分 1.0"""
        c = _make_crop_with_aspect(0.4, sharpness=300.0, detector_conf=1.0,
                                    with_face=False)
        s = quality_score(c, aspect_min=0.25, aspect_max=2.0)
        # base = 0.5×1.0 + 0.3×1.0 + 0.2×1.0 = 1.0
        assert abs(s - 1.0) < 1e-6

    def test_face_bonus_applied(self):
        """has_face 时 score × 1.5"""
        c_noface = _make_crop_with_aspect(0.4, sharpness=300.0, detector_conf=1.0,
                                           with_face=False)
        c_face = _make_crop_with_aspect(0.4, sharpness=300.0, detector_conf=1.0,
                                         with_face=True)
        s_noface = quality_score(c_noface, aspect_min=0.25, aspect_max=2.0)
        s_face = quality_score(c_face, aspect_min=0.25, aspect_max=2.0)
        assert abs(s_face - s_noface * 1.5) < 1e-6

    def test_high_quality_noface_beats_low_quality_face(self):
        """软判断核心: 高质 no_face 能超过 低质 has_face (no_face 比 has_face 高 50%+)。

        no_face quality 1.0 vs has_face quality 0.5 (×1.5=0.75) → no_face 胜。
        """
        c_high_noface = _make_crop_with_aspect(0.4, sharpness=300.0,
                                                detector_conf=1.0, with_face=False)
        c_low_face = _make_crop_with_aspect(0.4, sharpness=0.0,
                                              detector_conf=0.4, with_face=True)
        s_noface = quality_score(c_high_noface)
        s_face = quality_score(c_low_face)
        assert s_noface > s_face  # 1.0 > 0.4×0.5 + 0.3×1.0 + 0 = 0.5 × 1.5 = 0.75 ✓

    def test_same_quality_face_wins(self):
        """同 quality base 时, has_face 永远胜 no_face"""
        c_face = _make_crop_with_aspect(0.4, sharpness=200, detector_conf=0.7,
                                         with_face=True)
        c_noface = _make_crop_with_aspect(0.4, sharpness=200, detector_conf=0.7,
                                           with_face=False)
        assert quality_score(c_face) > quality_score(c_noface)

    def test_aspect_extreme_low_score(self):
        """极端 aspect (0.25 边界) 即使 det/sharp 高, aspect_score=0 → 总分低"""
        c_extreme = _make_crop_with_aspect(0.25, sharpness=300.0, detector_conf=1.0,
                                            with_face=False)
        c_normal = _make_crop_with_aspect(0.4, sharpness=300.0, detector_conf=1.0,
                                           with_face=False)
        # 边界 aspect:base = 0.5 + 0 + 0.2 = 0.7
        # 正常 aspect:base = 0.5 + 0.3 + 0.2 = 1.0
        assert quality_score(c_extreme) < quality_score(c_normal)
        assert abs(quality_score(c_extreme) - 0.7) < 1e-6


# =============================================================================
# 入池 quality_gate: aspect 收紧到 [0.25, 2.0]
# =============================================================================


class TestQualityGateNewBounds:
    def test_aspect_0_22_rejected(self):
        """aspect=0.22 (在老的 [0.20, 2.5] 内但已不在新 [0.25, 2.0] 内) → 被滤"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # h=128 w=28 → aspect ≈ 0.219
        c = CropEntry(
            cam_id="cam-a", track_id=1, frame_index=0, captured_at=1.0,
            body_crop=np.ones((128, 28, 3), dtype=np.uint8) * 100,
            face_crop=None, sharpness=100.0, bbox_xyxy=(0, 0, 28, 128),
            detector_conf=0.8,
        )
        assert not pool._pass_quality_gate(c)

    def test_aspect_2_3_rejected(self):
        """aspect=2.3 在老范围内但在新范围外 → 被滤"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # h=50 w=115 → aspect ≈ 2.3
        c = CropEntry(
            cam_id="cam-a", track_id=1, frame_index=0, captured_at=1.0,
            body_crop=np.ones((50, 115, 3), dtype=np.uint8) * 100,
            face_crop=None, sharpness=100.0, bbox_xyxy=(0, 0, 115, 50),
            detector_conf=0.8,
        )
        assert not pool._pass_quality_gate(c)

    def test_aspect_0_3_accepted(self):
        """aspect=0.3 在新范围 [0.25, 2.0] 内 → 通过"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # h=200 w=60 → aspect=0.3
        c = CropEntry(
            cam_id="cam-a", track_id=1, frame_index=0, captured_at=1.0,
            body_crop=np.ones((200, 60, 3), dtype=np.uint8) * 100,
            face_crop=None, sharpness=100.0, bbox_xyxy=(0, 0, 60, 200),
            detector_conf=0.8,
        )
        assert pool._pass_quality_gate(c)


# =============================================================================
# cluster 之间排序 post-sort: 提前最高分 has_face 到 #1
# =============================================================================


class TestClusterRankPostSort:
    def _push_with_face(self, pool: TierUPool, cam: str, tid: int, *,
                        with_face: bool, sharp: float = 100.0,
                        det: float = 0.8) -> None:
        """push 一张 crop, 控制 has_face / 质量参数"""
        face = np.ones((40, 40, 3), dtype=np.uint8) * 128 if with_face else None
        c = CropEntry(
            cam_id=cam, track_id=tid, frame_index=0, captured_at=1.0,
            body_crop=np.ones((100, 40, 3), dtype=np.uint8) * 128,  # aspect 0.4 sweet
            face_crop=face, sharpness=sharp, bbox_xyxy=(0, 0, 40, 100),
            detector_conf=det,
        )
        pool.push_crop(c)

    def test_face_promoted_to_first_when_noface_top(self):
        """软排序后 #1 是 no_face 高分, post-sort 把最高 has_face 提前到 #1"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # cluster A: no_face 极高质量 (det=1.0, sharp=300) → quality_score base 接近 1.0
        self._push_with_face(pool, "cam-a", 1, with_face=False, sharp=300.0, det=1.0)
        # cluster B: has_face 中等质量 (det=0.6, sharp=100) → base ~0.5×0.6+0.3+0.2×0.33 = 0.66
        #            × 1.5 = 0.99,仍微高于 A 的 1.0 base no_face... 调整 B 让它低于 A 即可
        # cluster B: has_face 低质量 (det=0.5, sharp=50) → base=0.5×0.5+0.3+0.2×0.17=0.583
        #            × 1.5 = 0.875
        self._push_with_face(pool, "cam-b", 2, with_face=True, sharp=50.0, det=0.5)
        cands = pool.fetch()
        # 软排序后 A (no_face 1.0) > B (face 0.875), 但 post-sort 把 B 提前
        assert len(cands) == 2
        assert cands[0].representative_crop.face_crop is not None  # #1 必含 face
        # B 的 cluster (cam-b track 2) 被提前
        assert ("cam-b", 2) in cands[0].members

    def test_all_noface_no_op(self):
        """全 cluster 都无 face → post-sort no-op, 维持软排序"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        self._push_with_face(pool, "cam-a", 1, with_face=False, sharp=300.0, det=1.0)
        self._push_with_face(pool, "cam-b", 2, with_face=False, sharp=100.0, det=0.5)
        cands = pool.fetch()
        assert len(cands) == 2
        # 全 no_face, #1 仍是 quality 最高的 (cam-a)
        assert ("cam-a", 1) in cands[0].members

    def test_face_already_first_no_change(self):
        """软排序后 #1 已是 has_face → post-sort 不变, 中后位顺序不动"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))
        # A: has_face 高质 (quality 大约 1.0 base × 1.5 = 1.5)
        self._push_with_face(pool, "cam-a", 1, with_face=True, sharp=300.0, det=1.0)
        # B: no_face 中等 (quality base 0.7)
        self._push_with_face(pool, "cam-b", 2, with_face=False, sharp=100.0, det=0.7)
        # C: has_face 低质 (quality 0.5 × 1.5 = 0.75)
        self._push_with_face(pool, "cam-c", 3, with_face=True, sharp=50.0, det=0.4)
        cands = pool.fetch()
        assert len(cands) == 3
        assert cands[0].representative_crop.face_crop is not None
        assert ("cam-a", 1) in cands[0].members  # 最高 has_face 保持 #1


# =============================================================================
# _cluster_pairwise_union: cluster-level 周期性 re-merge (v2: 含同 cam pair)
# =============================================================================
#
# 解决"online clustering 早期边界拒判 → 终身分裂"问题: dedup tick 是 dirty entry
# 驱动, entry 进 cluster 后 dirty=False 永不重评。但 cluster 演化后 centroid 可能
# 漂移到能合的程度。前身 _cross_cam_union 只跑跨 cam pair, 单 cam 内分裂永久化;
# v2 改名 _cluster_pairwise_union 并去掉 cam 限制, fetch 时 lazy 合并。


class TestClusterPairwiseUnion:
    def _push_with_emb(self, pool: TierUPool, provider: _MockReIDProvider,
                       cam: str, tid: int, emb: np.ndarray, n: int = 2) -> None:
        """push 一个 track n 张 crop 并设置 emb, 调 flush_if_due 让 cluster 化"""
        provider.set_embedding(cam, tid, emb)
        for i in range(n):
            pool.push_crop(_make_crop(cam, tid, i, 1.0 + i))
        pool.flush_if_due()

    def test_intra_cam_two_clusters_high_sim_merged_on_fetch(self):
        """v2 核心: 同 cam 内 2 个 cluster centroid 余弦超阈值 → fetch 时合并。

        复现 snapshot debug case: 早期边界拒判把同人切成两 cluster, 演化后 centroid
        高度相似但 dedup tick 不再重评。v2 在 fetch 时跑 _cluster_pairwise_union 收拾。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,   # 故意拉到极高让 dedup tick 不合
                reid_threshold_cross_cam=0.85,   # pairwise union 走这个阈值, 0.9 sim 应能合
            ),
            reid_provider=provider,
        )
        # 同 cam 两 track, emb 余弦 ~0.9 (远高于 0.85 但低于 0.99 dedup 阈值)
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.9, np.sqrt(1 - 0.9**2)] + [0.0] * 126, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        # dedup tick 0.99 阈值不合, fetch 前应是 2 cluster
        assert len({e.cluster_id for e in pool._entries.values()
                    if e.cluster_id is not None}) == 2
        # 全局 fetch → 触发 pairwise union, 0.9 ≥ 0.85 → 合并
        cands = pool.fetch()
        assert len(cands) == 1
        assert {("cam-a", 1), ("cam-a", 2)} == set(cands[0].members)

    def test_intra_cam_low_sim_clusters_stay_separate(self):
        """同 cam 两 cluster centroid 余弦 < 阈值 → 不合并, 各保独立"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        # 同 cam 两 track, emb 余弦 0.5 (远低于 0.85)
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.5, np.sqrt(0.75)] + [0.0] * 126, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        cands = pool.fetch()
        assert len(cands) == 2  # 没合, 各为 1 cluster

    def test_only_highest_pair_merged_per_call(self):
        """保守边界 case: 单次 fetch 只合最高分一对, 链式合并通过多次 fetch converge。

        构造 3 cluster A/B/C, 两两都超阈值但 A↔B 最高 → 第一次 fetch 只合 A-B。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        # 三 track: A=(1,0,...), B=(0.95, sqrt(1-0.95²), ...), C=(0.90, sqrt(1-0.90²), ...)
        # sim(A,B)=0.95 最高, sim(A,C)=0.90, sim(B,C)≈0.95·0.90+sqrt(...)·sqrt(...)
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.95, np.sqrt(1 - 0.95**2)] + [0.0] * 126, dtype=np.float32)
        emb_c = np.array([0.90, 0.0, np.sqrt(1 - 0.90**2)] + [0.0] * 125, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        self._push_with_emb(pool, provider, "cam-a", 3, emb_c)

        # 第一次 fetch: 只合最高分对 (A-B)
        cands1 = pool.fetch()
        assert len(cands1) == 2  # 3 → 2, 只合了一对

        # 第二次 fetch: 再合一对 (新 centroid 跟 C 比对)
        cands2 = pool.fetch()
        assert len(cands2) <= 2  # 1 或 2 都 OK (取决于新 centroid 跟 C 的余弦)

    def test_match_cache_invalidated_after_merge(self):
        """合并后涉及被弹 cluster_id 的 match_cache 项被清, 防 dangling reference"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.9, np.sqrt(0.19)] + [0.0] * 126, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        # 第一次 fetch 触发合并, match_cache 应该缓存 pair 且合并后清掉被弹方
        pool.fetch()
        # 合并后只剩 1 cluster, match_cache 不应留 stale entry
        all_cids = {e.cluster_id for e in pool._entries.values()
                    if e.cluster_id is not None}
        # 检查 match_cache 里的 key 只包含还活着的 cluster_id
        for key in pool._match_cache:
            for cid in key:
                assert cid in all_cids, f"stale cid {cid} in match_cache after merge"

    def test_match_cache_invalidated_for_merge_target(self):
        """合并后 cid_a (合并目标) 的 (cid_a, cid_x) 旧 sim 也必须清掉。

        回归保护: 历史代码只清被弹方 cid_b 的 cache 项, cid_a 的 centroid 已变但
        跟其他 cluster 的旧 sim 仍留在 cache; 下次 fetch 拿 cluster_reps[cid_a]
        新值跟 cache 老 sim 对账, 会误合 (cache 假高 → 把无关 cluster 拉进 cid_a,
        跨身份污染) 或漏合。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.9, np.sqrt(0.19)] + [0.0] * 126, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        pre_cids = {e.cluster_id for e in pool._entries.values()
                    if e.cluster_id is not None}
        assert len(pre_cids) == 2
        # 给每个 pre cid 都 inject 一条 (cid, "cid-fake-x") cache 项, 模拟"该 cid
        # 跟其他 cluster 的历史 sim 已落 cache"。fetch 后无论谁是合并目标 cid_a,
        # 涉及它的 (cid_a, cid-fake-x) 都应被清 (不再有任何 fake 项残留)。
        for cid in pre_cids:
            pool._match_cache[frozenset((cid, "cid-fake-x"))] = 0.99

        pool.fetch()
        for key in pool._match_cache:
            assert "cid-fake-x" not in key, (
                f"stale cache key {key} 含 cid-fake-x, 说明 cid_a/cid_b 一侧未清"
            )

    def test_dedup_tick_merge_invalidates_cache(self):
        """_intra_cam_dedup_tick 合并 cluster 后, _match_cache 涉及被弹/target 两侧
        的项都应被清。

        历史 bug: dedup_tick (60s 跑一次 by flush) 合并后**不清** cache;
        _cluster_pairwise_union (fetch 触发) 后续读 cache 命中 stale sim, 用旧
        centroid 算的 0.99 直接当 best_pair 误合到不同 cluster, 跨身份污染。
        改在 _merge_into_cluster 内统一 invalidate 后, 该路径自动获益。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                # intra_cam 阈值 0.85 让两个相似 emb track 在 dedup_tick 合并
                reid_threshold_intra_cam=0.85,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.95, np.sqrt(1.0 - 0.95 * 0.95)] + [0.0] * 126,
                         dtype=np.float32)
        # 第 1 个 track 先入池形成 cluster_X
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        cluster_x = pool._entries[("cam-a", 1)].cluster_id
        assert cluster_x is not None

        # 注入 stale cache: (cluster_x, "fake-other") sim=0.99
        # 模拟 cache 里"cluster_x 历史跟其他 cluster 算过 sim 留下"
        pool._match_cache[frozenset((cluster_x, "fake-other"))] = 0.99
        assert frozenset((cluster_x, "fake-other")) in pool._match_cache

        # 第 2 个 track 入池 + flush, 触发 dedup_tick → 跟 cluster_x 合并
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        # 确认合并发生 (两 track 同 cluster)
        assert pool._entries[("cam-a", 1)].cluster_id == pool._entries[("cam-a", 2)].cluster_id

        # 关键断言: 涉及 cluster_x 的 stale cache 项被清掉
        assert frozenset((cluster_x, "fake-other")) not in pool._match_cache, (
            "dedup_tick 合并后 cluster_x cache 未清, _cluster_pairwise_union 后续会"
            "读到 stale 0.99 用旧 centroid 算的值误合不同 cluster 造成跨身份污染"
        )

    def test_cross_cam_still_works(self):
        """v1 跨 cam 合并语义保留 (回归保护)"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(
                l1_capacity=2,
                reid_threshold_intra_cam=0.99,
                reid_threshold_cross_cam=0.85,
            ),
            reid_provider=provider,
        )
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        self._push_with_emb(pool, provider, "cam-b", 1, emb.copy())
        cands = pool.fetch()
        assert len(cands) == 1  # 跨 cam 同 emb → 合
        assert cands[0].span_cam_count == 2


# =============================================================================
# _filter_known_persons: fetch 末尾三层去重 (TierA mean / confirmed track / tier_c)
# =============================================================================
#
# 三层互补:
#   - TierA 层 (静态 0.85): 已注册人在 TierU 池子的残留 cluster
#   - confirmed track 层 (实时 0.85): 同人多 track 没合到一起, 此刻 active confirmed
#   - tier_c 层 (近期累积 0.90, 严一档): 用户换衣/换姿势 tier_c 已积累新外观, TierA mean 旧
# 命中阈值 → close_write_gate (物理清池) + 从返回过滤掉, 不进挑号拼图。


class TestFilterKnownPersons:
    def _push_with_emb(self, pool: TierUPool, provider: _MockReIDProvider,
                       cam: str, tid: int, emb: np.ndarray, n: int = 2) -> None:
        provider.set_embedding(cam, tid, emb)
        for i in range(n):
            pool.push_crop(_make_crop(cam, tid, i, 1.0 + i))
        pool.flush_if_due()

    def _make_emb_with_sim(self, base_dim: int, sim: float) -> np.ndarray:
        """构造一个跟 [1, 0, 0, ...] 余弦 = sim 的单位向量。"""
        e = np.zeros(128, dtype=np.float32)
        e[0] = sim
        e[1] = float(np.sqrt(max(0.0, 1.0 - sim * sim)))
        return e

    def test_tier_a_match_closes_cluster_and_filters_out(self):
        """cluster centroid 跟 tier_a mean emb sim >= 0.85 → close_write_gate
        物理清池 + 不在 fetch 返回里。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cands = pool.fetch(tier_a_emb_lookup={"person-1": emb.copy()})
        # 命中 tier_a → 不返回
        assert cands == []
        # 物理 close: write_open=False
        assert pool._entries[("cam-a", 1)].write_open is False

    def test_tier_a_no_match_keeps_cluster(self):
        """cluster centroid 跟所有 tier_a person sim < 0.85 → 不动, 正常返回。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb_track = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_other = self._make_emb_with_sim(128, 0.5)  # 余弦 0.5 << 0.85
        self._push_with_emb(pool, provider, "cam-a", 1, emb_track)
        cands = pool.fetch(tier_a_emb_lookup={"person-other": emb_other})
        assert len(cands) == 1
        assert pool._entries[("cam-a", 1)].write_open is True

    def test_target_track_id_bypasses_dedup(self):
        """target_track_id 明确锁定单 cluster 时跳过三层去重 — 调用方明确就要看
        这个 cluster, 误过滤体验差。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        # 即使 tier_a 完全命中, target_track_id 锁定也应返回
        cands = pool.fetch(
            target_track_id=1,
            cam_id="cam-a",
            tier_a_emb_lookup={"person-1": emb.copy()},
        )
        assert len(cands) == 1
        # 锁定路径不该 close write_gate (没走 _filter_known_persons)
        assert pool._entries[("cam-a", 1)].write_open is True

    def test_target_cluster_id_bypasses_dedup(self):
        """target_cluster_id 明确锁定 cluster 时跳过三层去重 (跟 target_track_id 对称)。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cluster_id = pool._entries[("cam-a", 1)].cluster_id
        assert cluster_id is not None
        cands = pool.fetch(
            target_cluster_id=cluster_id,
            tier_a_emb_lookup={"person-1": emb.copy()},
        )
        assert len(cands) == 1
        assert pool._entries[("cam-a", 1)].write_open is True

    def test_tier_c_threshold_stricter_than_tier_a(self):
        """sim=0.87 时 tier_a (0.85 阈值) 命中, tier_c (0.90) 不命中 — 验证 tier_c
        严一档不会让"看着像但不是同人"被误隐藏。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb_track = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_known = self._make_emb_with_sim(128, 0.87)  # 0.85 < 0.87 < 0.90

        # tier_c 单独命中阶段: 只传 tier_c, sim=0.87 不达 0.90 → 不过滤
        self._push_with_emb(pool, provider, "cam-a", 1, emb_track)
        cands = pool.fetch(tier_c_emb_lookup={"person-x": [emb_known]})
        assert len(cands) == 1  # tier_c 0.87 < 0.90 → 不命中, 保留
        assert pool._entries[("cam-a", 1)].write_open is True

        # 同一组数据用 tier_a 阈值 0.85 → 0.87 > 0.85 命中
        provider2 = _MockReIDProvider()
        pool2 = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider2)
        self._push_with_emb(pool2, provider2, "cam-a", 1, emb_track)
        cands2 = pool2.fetch(tier_a_emb_lookup={"person-x": emb_known})
        assert cands2 == []  # tier_a 0.87 > 0.85 → 命中, 过滤
        assert pool2._entries[("cam-a", 1)].write_open is False

    def test_no_centroid_emb_is_kept_conservative(self):
        """cluster centroid 算不出 emb (provider 没 emb + crops 也无 emb) → 保留,
        不冒险删 (保守: 留给用户自己判断)。
        """
        provider = _MockReIDProvider()
        # 故意不 set_embedding, provider 返 None
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        for i in range(2):
            pool.push_crop(_make_crop("cam-a", 1, i, 1.0 + i))
        pool.flush_if_due()
        # cluster 存在但所有 crop / entry emb=None → centroid 算不出
        emb_anything = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        cands = pool.fetch(tier_a_emb_lookup={"person-1": emb_anything})
        # 无 emb 的 cluster 保守保留 (不删)
        assert len(cands) == 1
        assert pool._entries[("cam-a", 1)].write_open is True


# =============================================================================
# close_same_person_clusters_by_track: confirmed 主动扫池清同人 residual cluster
# =============================================================================
#
# 触发时机: omni 把某 track commit 成 confirmed 时, engine 调本方法把池子里
# "同人 residual cluster" (case b: 同人多 track 没合到一起) 主动清掉,
# 物理释放内存 + 防止用户挑号时看到。


class TestCloseSamePersonClustersByTrack:
    def _push_with_emb(self, pool: TierUPool, provider: _MockReIDProvider,
                       cam: str, tid: int, emb: np.ndarray, n: int = 2) -> None:
        provider.set_embedding(cam, tid, emb)
        for i in range(n):
            pool.push_crop(_make_crop(cam, tid, i, 1.0 + i))
        pool.flush_if_due()

    def test_closes_match_cluster(self):
        """另一 cluster 跟 query track emb sim >= 0.85 → close_write_gate, 返回 1。

        构造: 两 track emb 余弦 0.95 (高于 close_same_person_clusters_by_track 用的
        0.85 cross_cam 阈值, 低于 intra_cam_dedup 0.99 阈值不被合) → 2 个独立
        cluster, close_same_person 时命中 close。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            # intra_cam 阈值拉高让 cam-a 内 2 track 不被 dedup 合, 形成 2 个独立 cluster
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        # cos = 0.95, 落在 [0.85 close 阈值, 0.99 intra_dedup 阈值) 区间
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.95
        emb_b[1] = float(np.sqrt(1.0 - 0.95 * 0.95))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        # 确认两 track 在不同 cluster (否则 close_same_person 全部 exclude_self 跳掉)
        assert pool._entries[("cam-a", 1)].cluster_id != pool._entries[("cam-a", 2)].cluster_id

        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        assert closed == 1  # cluster 2 被 close
        # track 1 自己的 cluster 不被影响 (exclude self)
        assert pool._entries[("cam-a", 1)].write_open is True
        # track 2 的 cluster 被 close
        assert pool._entries[("cam-a", 2)].write_open is False

    def test_excludes_self_cluster(self):
        """query track 自己所属 cluster 不被 close (caller close_write_gate 已处理)。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        # 唯一 cluster 就是 self, 被 exclude → 0 个 close
        assert closed == 0
        assert pool._entries[("cam-a", 1)].write_open is True

    def test_skips_already_closed_cluster(self):
        """所有成员 write_open=False 的 cluster 跳过, 不重复 close + 不重复算 centroid。
        防"close_write_gate 不 pop cluster, 每次 confirmed 又扫到"的无限重复 log。
        """
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        # cos = 0.95 同 test_closes_match_cluster, 让 2 cluster 不合
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.95
        emb_b[1] = float(np.sqrt(1.0 - 0.95 * 0.95))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        # 先手动 close track 2 的 cluster
        pool.close_write_gate("cam-a", 2)
        assert pool._entries[("cam-a", 2)].write_open is False
        # 再调 close_same_person_clusters_by_track → cluster 2 已 close 应跳过
        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        assert closed == 0  # 没人需要 close

    def test_no_match_does_not_close(self):
        """另一 cluster 跟 query emb sim < 0.85 → 不动。"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        # 构造跟 emb_a 余弦 0.3 的不同人 emb
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.3
        emb_b[1] = float(np.sqrt(1.0 - 0.3 * 0.3))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        assert closed == 0
        assert pool._entries[("cam-a", 2)].write_open is True

    def test_no_reid_provider_returns_zero(self):
        """provider 没注入时直接返 0, 不抛错。"""
        pool = TierUPool(config=TierUConfig(l1_capacity=2))  # 无 reid_provider
        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        assert closed == 0


# =============================================================================
# _fully_closed_clusters set 维护 (优化 confirmed 路径 close_same_person 扫池)
# =============================================================================


class TestFullyClosedClustersSet:
    """`_fully_closed_clusters` 是 close_same_person_clusters_by_track /
    _cluster_pairwise_union 跳过已 close cluster 用的快查 set。
    write_open True→False 不可逆, set 单调增长直到 cluster 接新成员或被弹。
    """

    def _push_with_emb(self, pool: TierUPool, provider: _MockReIDProvider,
                       cam: str, tid: int, emb: np.ndarray, n: int = 2) -> None:
        provider.set_embedding(cam, tid, emb)
        for i in range(n):
            pool.push_crop(_make_crop(cam, tid, i, 1.0 + i))
        pool.flush_if_due()

    def test_close_write_gate_adds_cluster_to_set(self):
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        assert cid not in pool._fully_closed_clusters
        pool.close_write_gate("cam-a", 1)
        assert cid in pool._fully_closed_clusters

    def test_close_write_gate_no_cluster_id_does_not_add(self):
        """entry.cluster_id is None 时 close_write_gate 走单 entry 路径, 不往
        set 里加 (set 只装 cluster_id)。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        e = pool._entries[("cam-a", 1)]
        e.cluster_id = None  # 强制走 None 分支
        before = set(pool._fully_closed_clusters)
        pool.close_write_gate("cam-a", 1)
        assert pool._fully_closed_clusters == before

    def test_merge_into_cluster_discards_target_when_new_member_joins(self):
        """已 close cluster 接新成员 (entry.cluster_id is None 分支) → discard。
        新 entry write_open 默认 True, cluster 不再 fully closed。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        pool.close_write_gate("cam-a", 1)
        assert cid in pool._fully_closed_clusters
        # 新 entry (cluster_id is None) 挂进这个已 close cluster
        self._push_with_emb(pool, provider, "cam-b", 2, emb)
        new_entry = pool._entries[("cam-b", 2)]
        new_entry.cluster_id = None  # 强制走 None 分支
        pool._merge_into_cluster(new_entry, cid)
        assert cid not in pool._fully_closed_clusters

    def test_merge_into_cluster_discards_both_sides_on_cross_merge(self):
        """跨 cluster 合并 → target 和 old 两侧都从 set 中移除。"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.95
        emb_b[1] = float(np.sqrt(1.0 - 0.95 * 0.95))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        cid_a = pool._entries[("cam-a", 1)].cluster_id
        cid_b = pool._entries[("cam-a", 2)].cluster_id
        assert cid_a != cid_b
        # 先把两边都 close
        pool.close_write_gate("cam-a", 1)
        pool.close_write_gate("cam-a", 2)
        assert cid_a in pool._fully_closed_clusters
        assert cid_b in pool._fully_closed_clusters
        # 触发跨 cluster 合并: 直接调 _merge_into_cluster 把 cid_b → cid_a
        pool._merge_into_cluster(pool._entries[("cam-a", 2)], cid_a)
        # 合并后两侧都该 discard (old 已被弹, target 接收新成员)
        assert cid_a not in pool._fully_closed_clusters
        assert cid_b not in pool._fully_closed_clusters

    def test_close_same_person_skips_via_set(self):
        """`_fully_closed_clusters` 命中的 cluster 直接跳过, 不算 centroid。
        本测试是 test_skips_already_closed_cluster 的 set 视角对照。"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.95
        emb_b[1] = float(np.sqrt(1.0 - 0.95 * 0.95))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        cid_b = pool._entries[("cam-a", 2)].cluster_id
        pool.close_write_gate("cam-a", 2)
        assert cid_b in pool._fully_closed_clusters
        # close_same_person 走 set 快查路径直接 skip cid_b, 不再算 centroid
        closed = pool.close_same_person_clusters_by_track("cam-a", 1)
        assert closed == 0

    def test_cluster_pairwise_union_skips_fully_closed(self):
        """_cluster_pairwise_union 不把 fully_closed cluster 拉进 pair 池 ——
        commit 后 close 的 cluster 没必要再合,合了也只是污染 close cluster 成员。"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.zeros(128, dtype=np.float32)
        emb_b[0] = 0.95
        emb_b[1] = float(np.sqrt(1.0 - 0.95 * 0.95))
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        self._push_with_emb(pool, provider, "cam-a", 2, emb_b)
        cid_a = pool._entries[("cam-a", 1)].cluster_id
        cid_b = pool._entries[("cam-a", 2)].cluster_id
        # 把 cid_b close 掉
        pool.close_write_gate("cam-a", 2)
        # 现在 pairwise union 应该不合 (cid_b 在 fully_closed 跳过, 池里只剩 cid_a)
        entries = list(pool._entries.values())
        pool._cluster_pairwise_union(entries)
        assert pool._entries[("cam-a", 1)].cluster_id == cid_a
        # cid_b 仍存在 (close_write_gate 不 pop cluster), 没被合进 cid_a
        assert pool._entries[("cam-a", 2)].cluster_id == cid_b

    def test_evict_entry_discards_cluster_from_set(self):
        """TTL/LRU 弹掉 cluster 最后一个成员时, set 同步清除, 防长跑泄漏。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        pool.close_write_gate("cam-a", 1)
        assert cid in pool._fully_closed_clusters
        # _evict_entry 弹掉唯一成员 → cluster 整个被弹 → set 应同步清
        pool._evict_entry(("cam-a", 1))
        assert cid not in pool._fully_closed_clusters
        assert cid not in pool._clusters

    def test_split_cluster_resets_set_state(self):
        """split_cluster: 原 cluster 成员组成变了 → discard 原 cluster_id, 然后按
        拆后两侧 write_open 实际情况重新 add (kept/to_remove 全 closed 才 add)。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        self._push_with_emb(pool, provider, "cam-b", 2, emb)
        # 手动合到同一 cluster (避开 intra/cross cam dedup 阈值与时序的不确定性)
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        pool._merge_into_cluster(pool._entries[("cam-b", 2)], cid)
        assert pool._entries[("cam-b", 2)].cluster_id == cid
        # 整 cluster close → cid 进 set
        pool.close_write_gate("cam-a", 1)
        assert cid in pool._fully_closed_clusters
        # split 出 cam-b → 原 cluster 留 cam-a, 新 cluster 含 cam-b
        result = pool.split_cluster(cid, remove_cams=["cam-b"])
        assert result is not None
        kept_cid, new_cid = result
        assert kept_cid == cid
        # 拆后两侧成员都 write_open=False (cluster close 时已置 False, split 不改)
        # → 两个 cid 都应该重新 add 回 set
        assert kept_cid in pool._fully_closed_clusters
        assert new_cid in pool._fully_closed_clusters

    def test_split_cluster_partial_open_not_in_set(self):
        """split 后 kept 含 write_open=True 的成员 → kept_cid 不进 set。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        self._push_with_emb(pool, provider, "cam-b", 2, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        pool._merge_into_cluster(pool._entries[("cam-b", 2)], cid)
        # 只 close cam-b, cam-a 仍 write_open=True
        pool._entries[("cam-b", 2)].write_open = False
        # 此时 cluster 还有 write_open 成员, set 里没有 cid
        assert cid not in pool._fully_closed_clusters
        # split: 把 cam-b (closed) 拆出去, kept=cam-a (open)
        result = pool.split_cluster(cid, remove_cams=["cam-b"])
        assert result is not None
        kept_cid, new_cid = result
        # kept 含 open 成员 → 不进 set; 拆出去的 new (含 closed 成员) → 进 set
        assert kept_cid not in pool._fully_closed_clusters
        assert new_cid in pool._fully_closed_clusters


# =============================================================================
# _centroid_cache (cluster mean emb 内容指纹式缓存)
# =============================================================================


class TestCentroidCache:
    """_cluster_mean_embedding 的内容指纹式缓存: 簇内容变 → 指纹变 → 自动重算
    (不靠写路径手动 invalidate, 规避漏删用 stale)。

    指纹里"emb 数"之外的每成员 (层级, 内容) 分量见 TestCentroidCacheFingerprintContent。
    """

    def _push_with_emb(self, pool: TierUPool, provider: _MockReIDProvider,
                       cam: str, tid: int, emb: np.ndarray, n: int = 2) -> None:
        provider.set_embedding(cam, tid, emb)
        for i in range(n):
            pool.push_crop(_make_crop(cam, tid, i, 1.0 + i))
        pool.flush_if_due()

    def test_cache_hit_returns_same_object(self):
        """内容不变时第二次调用命中缓存, 返回同一 centroid 对象 (没重算 np.mean)。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        c1 = pool._cluster_mean_embedding(cid)
        c2 = pool._cluster_mean_embedding(cid)
        assert c1 is not None
        assert c1 is c2  # 命中: 同一缓存对象, 未重算
        assert cid in pool._centroid_cache

    def test_centroid_recomputed_after_member_merge(self):
        """合并新 member (member frozenset 变 → 指纹变) → 重算, centroid 跟着变。
        这是 reviewer 要的 stale 回归: 簇内容变化后 centroid 不能还返旧值。"""
        provider = _MockReIDProvider()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, reid_threshold_intra_cam=0.99),
            reid_provider=provider,
        )
        emb_a = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        emb_b = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)  # 正交
        self._push_with_emb(pool, provider, "cam-a", 1, emb_a)
        cid = pool._entries[("cam-a", 1)].cluster_id
        c1 = pool._cluster_mean_embedding(cid)
        np.testing.assert_allclose(c1, emb_a, atol=1e-6)
        # 第二个 member (正交 emb) 合进同 cluster → 指纹变
        self._push_with_emb(pool, provider, "cam-b", 2, emb_b)
        pool._merge_into_cluster(pool._entries[("cam-b", 2)], cid)
        c2 = pool._cluster_mean_embedding(cid)
        assert c2 is not c1                         # 重算了 (没返 stale)
        assert not np.allclose(c2, emb_a, atol=1e-3)  # centroid 被 emb_b 拉偏
        # mean(emb_a, emb_b) 归一 ≈ [0.707, 0.707, 0...]
        np.testing.assert_allclose(c2[:2], [0.7071, 0.7071], atol=1e-3)

    def test_cache_cleared_when_cluster_gone(self):
        """cluster 被 evict 弹掉后, _cluster_mean_embedding 返 None 且缓存清除。"""
        provider = _MockReIDProvider()
        pool = TierUPool(config=TierUConfig(l1_capacity=2), reid_provider=provider)
        emb = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        self._push_with_emb(pool, provider, "cam-a", 1, emb)
        cid = pool._entries[("cam-a", 1)].cluster_id
        pool._cluster_mean_embedding(cid)
        assert cid in pool._centroid_cache
        pool._evict_entry(("cam-a", 1))  # 唯一 member 弹掉 → cluster pop
        assert pool._cluster_mean_embedding(cid) is None
        assert cid not in pool._centroid_cache


class TestTierUPoolThreadSafety:
    """§5: 跨线程访问的 RLock 保护(推理线程 push/flush/close vs API 主线程 fetch/status)。"""

    def test_lock_is_reentrant(self):
        """池锁可重入(RLock):同线程二次非阻塞 acquire 成功(普通 Lock 会返 False)。"""
        pool = TierUPool()
        assert pool._lock.acquire()
        try:
            assert pool._lock.acquire(blocking=False) is True
            pool._lock.release()
        finally:
            pool._lock.release()

    def test_decorated_method_reentrant_under_lock(self):
        """持锁时调装饰过的公开方法不死锁(RLock 重入;普通 Lock 会卡死)。"""
        pool = TierUPool()
        with pool._lock:
            pool.status()
            pool.tick_ttl()

    def test_concurrent_push_and_status_no_crash(self):
        """推理线程 push_crop 与 API 线程 status/tick 并发不抛 dict-changed-size。

        无锁时 status 遍历 _entries 同时 push 改 dict 可能 RuntimeError;加锁后恒安全。
        """
        import threading

        pool = TierUPool(config=TierUConfig(l1_capacity=50))
        errors: list[str] = []

        def producer() -> None:
            try:
                for i in range(400):
                    pool.push_crop(_make_crop("cam-a", i % 20, i, 1.0 + i))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        def reader() -> None:
            try:
                for _ in range(400):
                    pool.status()
                    pool.tick_ttl()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=producer)] + [
            threading.Thread(target=reader) for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"并发访问抛异常: {errors}"


# =============================================================================
# 回归:issue #429 —— cluster 消失后 _centroid_cache 必须同步清
# =============================================================================


class TestCentroidCacheNoOrphan:
    """cluster_id 被彻底弹掉后(evict 最后一个成员 / merge 旧簇被吸收),其
    _centroid_cache 项永不再被查、不会被指纹自愈 → 必须显式 pop,否则单调泄漏。
    不变量:``set(_centroid_cache) <= set(_clusters)``(不含任何已消失簇的孤儿)。
    """

    def _pool_one_cluster_with_cached_centroid(self):
        """push→flush 造一个单例簇,并把其质心算进 _centroid_cache。返回 (pool, clk, cid)。"""
        provider = _MockReIDProvider()
        provider.set_embedding("cam-a", 1, np.array([1.0] + [0.0] * 127, dtype=np.float32))
        clk = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, ttl_inactive_sec=100.0),
            reid_provider=provider,
            now_fn=clk,
        )
        for f in range(2):
            pool.push_crop(_make_crop("cam-a", 1, f, clk()))
        pool.flush_if_due()
        entry = pool._entries[("cam-a", 1)]
        cid = entry.cluster_id
        assert cid is not None, "flush 后单例应挂上 cluster_id"
        # 主动算一次质心塞进 cache(模拟 fetch / pairwise_union 命中路径)
        assert pool._cluster_mean_embedding(cid) is not None
        assert cid in pool._centroid_cache
        return pool, clk, cid

    def test_ttl_eviction_pops_centroid_cache(self):
        """TTL 过期 evict 最后一个成员 → cluster 消失 → centroid_cache 同步清。"""
        pool, clk, cid = self._pool_one_cluster_with_cached_centroid()
        clk.advance(1000)  # 远超 ttl_inactive_sec=100
        pool.tick_ttl()
        assert ("cam-a", 1) not in pool._entries
        assert cid not in pool._clusters
        assert cid not in pool._centroid_cache  # 修复前:残留 → 孤儿泄漏
        assert set(pool._centroid_cache) <= set(pool._clusters)

    def test_lru_eviction_pops_centroid_cache(self):
        """内存超预算 LRU 兜底 evict → 同样清 centroid_cache。"""
        pool, clk, cid = self._pool_one_cluster_with_cached_centroid()
        pool.config.memory_budget_mb = 0  # 逼 LRU 立即清空
        pool.gc_lru_if_over_budget()
        assert ("cam-a", 1) not in pool._entries
        assert cid not in pool._centroid_cache
        assert set(pool._centroid_cache) <= set(pool._clusters)

    def test_merge_pops_old_cluster_centroid_cache(self):
        """跨 cluster 合并:旧簇被吸收后其 centroid_cache 必须清(白盒直驱 _merge_into_cluster)。"""
        from miloco.perception.engine.identity.tier_u import EquivClass, TierUEntry

        pool = TierUPool()
        ka, kb = ("cam-a", 1), ("cam-a", 2)
        ea = TierUEntry(cam_id="cam-a", track_id=1)
        ea.cluster_id = "OLD"
        eb = TierUEntry(cam_id="cam-a", track_id=2)
        eb.cluster_id = "TGT"
        pool._entries[ka] = ea
        pool._entries[kb] = eb
        pool._clusters["OLD"] = EquivClass(cluster_id="OLD", members={ka})
        pool._clusters["TGT"] = EquivClass(cluster_id="TGT", members={kb})
        z = np.zeros(128, dtype=np.float32)
        pool._centroid_cache["OLD"] = ((frozenset({ka}), 1), z)
        pool._centroid_cache["TGT"] = ((frozenset({kb}), 1), z)

        pool._merge_into_cluster(ea, "TGT")

        assert "OLD" not in pool._clusters
        assert "OLD" not in pool._centroid_cache  # 修复前:残留 → 孤儿泄漏
        assert set(pool._centroid_cache) <= set(pool._clusters)


class TestMatchCacheStaleOnPartialEviction:
    """TTL/LRU 只淘汰 cluster 的**部分**成员时,_match_cache 里的旧 pair sim 必须失效。

    _match_cache 的键只有 frozenset({cid_a, cid_b}),不含成员指纹(不同于
    _centroid_cache 的内容指纹自愈)。成员被淘汰后 cluster 仍存在 → pair 键不变,
    _cluster_pairwise_union 会继续复用旧 sim:旧低分造成假阴性(漏 merge),旧高分
    造成假阳性(错合两个已不相近的簇,身份合并不可逆,危害更大)。
    """

    @staticmethod
    def _two_clusters(pool, sim_seed: float | None = None):
        """A 簇 2 成员(e1/e2)+ B 簇 1 成员(e1);可选预置 (A,B) 的 stale sim。

        返回 (ka1, ka2, kb):ka1 是把 A 的 centroid 拽向 B 的那个成员,淘汰它之后
        A 的真实 centroid(e2)与 B(e1)正交 → 真实 sim=0,远低于 0.85 阈值。
        """
        from miloco.perception.engine.identity.tier_u import EquivClass, TierUEntry

        e1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
        e2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
        ka1, ka2, kb = ("cam-a", 1), ("cam-a", 2), ("cam-b", 3)
        for key, cid, emb in ((ka1, "CID-A", e1), (ka2, "CID-A", e2), (kb, "CID-B", e1)):
            entry = TierUEntry(cam_id=key[0], track_id=key[1])
            entry.cluster_id = cid
            entry.reid_embedding = emb  # 让 _entry_dedup_embedding / centroid 有料
            entry.last_l1_push_ts = 1_700_000_000.0
            pool._entries[key] = entry
        pool._clusters["CID-A"] = EquivClass(cluster_id="CID-A", members={ka1, ka2})
        pool._clusters["CID-B"] = EquivClass(cluster_id="CID-B", members={kb})
        if sim_seed is not None:
            pool._match_cache[frozenset({"CID-A", "CID-B"})] = sim_seed
        return ka1, ka2, kb

    def test_partial_eviction_invalidates_match_cache(self):
        """淘汰 A 的一个成员(A 仍非空)→ 含 A 的 pair sim 必须清掉。"""
        pool = TierUPool()
        ka1, ka2, _kb = self._two_clusters(pool, sim_seed=0.99)

        pool._evict_entry(ka1)

        assert pool._clusters["CID-A"].members == {ka2}, "A 应仍存在、只少一个成员"
        # 修复前:cluster 非空 → 不 invalidate → stale 0.99 继续留在 cache 里
        assert frozenset({"CID-A", "CID-B"}) not in pool._match_cache

    def test_stale_high_sim_does_not_cause_false_merge(self):
        """行为断言:淘汰成员后真实 centroid 已正交,stale 高分不得再触发错误合并。"""
        pool = TierUPool()
        ka1, ka2, kb = self._two_clusters(pool, sim_seed=0.99)  # 0.99 >= 0.85 阈值

        pool._evict_entry(ka1)
        pool._cluster_pairwise_union(list(pool._entries.values()))

        # 修复前:命中 stale 0.99 → best_pair=(A,B) → _merge_into_cluster 把两簇错合
        assert pool._entries[ka2].cluster_id == "CID-A"
        assert pool._entries[kb].cluster_id == "CID-B"
        assert set(pool._clusters) == {"CID-A", "CID-B"}, "正交的两簇不该被合并"

    def test_recomputed_sim_replaces_stale_low_score(self):
        """反向:stale 低分不得挡掉本该发生的 merge —— 清缓存后按真实 sim 重算。"""
        pool = TierUPool()
        ka1, _ka2, kb = self._two_clusters(pool, sim_seed=0.0)  # stale 低分挡 merge

        # 淘汰 ka2(e2)→ A 只剩 ka1(e1),与 B(e1)真实 sim=1.0 > 0.85 应当合并
        pool._evict_entry(("cam-a", 2))
        pool._cluster_pairwise_union(list(pool._entries.values()))

        assert pool._entries[ka1].cluster_id == pool._entries[kb].cluster_id, (
            "真实 centroid 已同一身份,stale 低分不应继续阻止 merge"
        )
        assert len(pool._clusters) == 1


class TestMatchCacheStaleOnCropAccumulation:
    """成员集合**不变**、只是 crop 累积导致 centroid 漂移时,pair sim 也必须失效。

    _cluster_mean_embedding 收的是全体成员的**所有 crop emb**,所以"同一成员多攒一张
    crop"同样改 centroid。此前只有 L2 FIFO **弹出**那侧清了 cache,塞入侧没清 →
    L2 未满的填充期(每成员前 l2_capacity 次 flush)漂移对 _match_cache 完全不可见。
    """

    E1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
    E2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)

    def _pool_with_cluster(self, emb):
        """push→flush 造一个已挂 cluster 的单例 entry。返回 (pool, provider, clk, cid)。"""
        provider = _MockReIDProvider()
        provider.set_embedding("cam-a", 1, emb)
        clk = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2),
            reid_provider=provider,
            now_fn=clk,
        )
        for f in range(2):
            pool.push_crop(_make_crop("cam-a", 1, f, clk()))
        pool.flush_if_due()
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        return pool, provider, clk, cid

    def test_push_crop_invalidates_match_cache(self):
        """L1 累积(L2 为空时直接就是 centroid 来源)→ 清 pair sim。"""
        pool, _prov, clk, cid = self._pool_with_cluster(self.E1)
        pool._match_cache[frozenset({cid, "OTHER"})] = 0.42

        pool.push_crop(_make_crop("cam-a", 1, 99, clk()))

        assert frozenset({cid, "OTHER"}) not in pool._match_cache

    def test_l2_append_invalidates_match_cache_without_popleft(self):
        """L2 未满(走不到 popleft 分支)的塞入侧也要清 —— 修复前这里完全不可见。"""
        pool, _prov, clk, cid = self._pool_with_cluster(self.E1)
        entry = pool._entries[("cam-a", 1)]
        assert len(entry.crops_l2) < pool.config.l2_capacity, "须停在填充期"
        # 先把 L1 攒满,再 seed —— 避免 seed 被 push_crop 侧的清理提前拿走,
        # 这样断言只可能由 flush 的 L2 塞入侧满足。
        for f in range(2):
            pool.push_crop(_make_crop("cam-a", 1, 100 + f, clk()))
        pool._match_cache[frozenset({cid, "OTHER"})] = 0.42

        pool.flush_if_due()

        assert len(entry.crops_l2) == 2, "本轮应只 append、不 popleft"
        assert frozenset({cid, "OTHER"}) not in pool._match_cache

    def test_stale_low_sim_from_earlier_fetch_no_longer_blocks_merge(self):
        """行为断言:走 review 给的数值链路 —— fetch#1 算出低分入缓存,之后 crop 累积
        让真实 centroid 升到阈值之上,fetch#2 必须按新分数合并(而非复用旧低分)。

        缓存全程由真实路径 (_cluster_pairwise_union) 写入,不手工 seed。
        """
        provider = _MockReIDProvider()
        provider.set_embedding("cam-a", 1, self.E2)   # A:初始外观
        provider.set_embedding("cam-b", 2, self.E1)   # B:目标身份
        clk = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2), reid_provider=provider, now_fn=clk,
        )

        def _flush_round(cam: str, tid: int, base_frame: int) -> None:
            for f in range(2):
                pool.push_crop(_make_crop(cam, tid, base_frame + f, clk()))
            pool.flush_if_due()

        _flush_round("cam-a", 1, 0)     # A:L2 = [E2]
        _flush_round("cam-b", 2, 0)     # B:L2 = [E1]
        ka, kb = ("cam-a", 1), ("cam-b", 2)
        cid_a, cid_b = pool._entries[ka].cluster_id, pool._entries[kb].cluster_id
        assert cid_a != cid_b, "正交外观不该在此时合并"

        # fetch #1:真实 sim=0(正交)→ 不合并,低分写进 _match_cache
        pool._cluster_pairwise_union(list(pool._entries.values()))
        assert pool._match_cache.get(frozenset({cid_a, cid_b})) == pytest.approx(0.0)

        # A 的外观向 B 收敛:再攒 2 张 E1 crop → L2=[E2,E1,E1],与 E1 真实
        # cos = 2/sqrt(5) ≈ 0.894 > 0.85 阈值(L2 仍未满 → 走不到 popleft 分支)
        provider.set_embedding("cam-a", 1, self.E1)
        _flush_round("cam-a", 1, 10)
        _flush_round("cam-a", 1, 20)
        assert len(pool._entries[ka].crops_l2) == 3
        rep_a = pool._cluster_mean_embedding(pool._entries[ka].cluster_id)
        assert float(rep_a @ self.E1) > pool.config.reid_threshold_cross_cam

        # fetch #2:修复前命中 stale 0.0 → 永不合并(同人长期分裂)
        pool._cluster_pairwise_union(list(pool._entries.values()))

        assert pool._entries[ka].cluster_id == pool._entries[kb].cluster_id, (
            "crop 累积后真实 centroid 已过阈值,旧低分不应继续挡住 merge"
        )
        assert len(pool._clusters) == 1


class TestMatchCacheStaleOnFetchBackfill:
    """fetch **读取侧**的两处 emb 回填同样改 centroid,也必须清 pair sim。

    此前 4a/4b 只覆盖写入侧(攒 crop / 晋级 / 合并 / 淘汰)。fetch 路上还有两处会
    就地改写 CropEntry 的 emb 字段 —— "某 crop 从无 emb 变有 emb"会让参与 mean 的
    条数 +1,centroid 随之变:

    1. ``_cluster_candidate_for`` 给缺 emb 的 L1/L2 crop 回填 entry 级快照。它跑在
       ``_cluster_pairwise_union`` **之后**(fetch 步骤 3→4),所以本次 fetch 刚写入
       cache 的 sim 当场就与新 centroid 不一致,下次 fetch 命中即误判。号码图翻页
       靠 agent 拿 next_offset 重发 fetch 实现,用户回一句"更多"就是秒级内的第二次
       全局 fetch —— 不依赖任何竞态。
    2. fetch 第 4 道防线的现场抽 emb 回写。它后面只跟一次 intra-cam dedup,**只有
       真发生 merge** 才顺带清 cache;分数没过阈值就留下"质心已变、cache 未清"。
    """

    E1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
    E2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)
    E3 = np.array([0.0, 0.0, 1.0] + [0.0] * 125, dtype=np.float32)
    # 与 E1 夹角余弦恰好 0.9(> 0.85 阈值),作 A 簇回填前的 centroid
    A_REP = np.array([0.9, float(np.sqrt(1.0 - 0.81))] + [0.0] * 126, dtype=np.float32)

    @staticmethod
    def _add_entry(pool, key, cid, *, entry_emb, l2_embs=(), l1_embs=(), ts=1_700_000_000.0):
        """白盒造 entry + cluster。``*_embs`` 里的 None 表示"该 crop 没 emb"。"""
        from miloco.perception.engine.identity.tier_u import EquivClass, TierUEntry
        entry = TierUEntry(cam_id=key[0], track_id=key[1])
        entry.cluster_id = cid
        entry.reid_embedding = entry_emb
        entry.last_l1_push_ts = ts
        for i, emb in enumerate(l2_embs):
            crop = _make_crop(key[0], key[1], i, ts)
            crop.reid_embedding = emb
            entry.crops_l2.append(crop)
        for i, emb in enumerate(l1_embs):
            crop = _make_crop(key[0], key[1], 100 + i, ts)
            crop.reid_embedding = emb
            entry.crops_l1.append(crop)
        pool._entries[key] = entry
        cluster = pool._clusters.get(cid)
        if cluster is None:
            pool._clusters[cid] = EquivClass(cluster_id=cid, members={key})
        else:
            cluster.members.add(key)
        return entry

    def test_candidate_backfill_invalidates_before_next_page_fetch(self):
        """号码图翻页复现路径(假阳性,最危险):第 1 页把 0.9 写进 cache 但因"单次只合
        最高分一对"没合;打包候选时的回填把真实余弦压到 0.53;第 2 页必须按新分数不合。
        """
        pool = TierUPool(config=TierUConfig(), reid_provider=_MockReIDProvider())
        ka, kb = ("cam-a", 1), ("cam-b", 2)
        kc, kd = ("cam-c", 3), ("cam-d", 4)
        # A:一张有 emb(= A_REP)、一张缺 emb;entry 级快照是另一姿态(E2)→ 回填即漂移
        self._add_entry(pool, ka, "CID-A", entry_emb=self.E2, l2_embs=(self.A_REP, None))
        self._add_entry(pool, kb, "CID-B", entry_emb=self.E1, l2_embs=(self.E1,))
        # C/D 真实 sim=1.0 > (A,B) 的 0.9 → 本轮 best_pair 是 (C,D),(A,B) 只进 cache
        self._add_entry(pool, kc, "CID-C", entry_emb=self.E3, l2_embs=(self.E3,))
        self._add_entry(pool, kd, "CID-D", entry_emb=self.E3, l2_embs=(self.E3,))
        entries = list(pool._entries.values())
        pair_ab = frozenset({"CID-A", "CID-B"})

        # --- 第 1 页 fetch:步骤 3 两两比对 ---
        pool._cluster_pairwise_union(entries)
        assert pool._match_cache.get(pair_ab) == pytest.approx(0.9, abs=1e-4), (
            "(A,B)=0.9 应进 cache"
        )
        assert pool._entries[ka].cluster_id != pool._entries[kb].cluster_id, (
            "单次调用只合最高分一对 (C,D),(A,B) 本轮不该合"
        )
        assert pool._entries[kc].cluster_id == pool._entries[kd].cluster_id

        # --- 同一次 fetch 的步骤 4:打包候选 → 给缺 emb 的 crop 回填 entry 级快照 ---
        pool._build_cluster_candidates(entries)
        assert pool._entries[ka].crops_l2[1].reid_embedding is not None, "回填应已发生"
        rep_a = pool._cluster_mean_embedding(pool._entries[ka].cluster_id)
        real_sim = float(rep_a @ self.E1)
        assert real_sim < pool.config.reid_threshold_cross_cam, (
            f"回填后真实余弦应掉到阈值下,实际 {real_sim:.4f}"
        )

        # --- 用户回"更多"→ 第 2 页 fetch(秒级内,无新 crop 入池)---
        pool._cluster_pairwise_union(list(pool._entries.values()))

        assert pool._entries[ka].cluster_id != pool._entries[kb].cluster_id, (
            "回填已让真实质心不再相近,不得复用旧 0.9 把两个人错合(身份合并不可逆)"
        )

    def test_fourth_defense_backfill_invalidates_stale_high_sim(self):
        """第 4 道防线现场抽 emb 回写后,同一次 fetch 的两两比对不得再命中旧高分。

        走真实 ``fetch(reid_extractor=...)``。A 跨 cam 于 B,intra-cam dedup 不会
        merge → 修复前**没有任何**路径会清掉 seed 的 0.99。
        """
        clk = _clock()
        pool = TierUPool(config=TierUConfig(), reid_provider=_MockReIDProvider(), now_fn=clk)
        ka, kb = ("cam-a", 1), ("cam-b", 2)
        # A:entry 级 emb 仍为 None(provider 整条 track 返 None)、L1 crop 也没 emb
        # → 命中第 4 道防线;真实身份 E2 与 B 的 E1 正交
        self._add_entry(pool, ka, "CID-A", entry_emb=None, l1_embs=(None,), ts=clk())
        self._add_entry(pool, kb, "CID-B", entry_emb=self.E1, l2_embs=(self.E1,), ts=clk())
        pool._match_cache[frozenset({"CID-A", "CID-B"})] = 0.99

        class _FakeExtractor:
            calls = 0
            def extract_feature(self, body_crop):
                _FakeExtractor.calls += 1
                return TestMatchCacheStaleOnFetchBackfill.E2.copy()

        pool.fetch(reid_extractor=_FakeExtractor())

        assert _FakeExtractor.calls >= 1, "应命中第 4 道防线现场抽 emb"
        assert pool._entries[ka].reid_embedding is not None, "回写应已发生"
        assert pool._entries[ka].cluster_id != pool._entries[kb].cluster_id, (
            "现场抽出的 emb 与 B 正交,不得复用 stale 0.99 错合"
        )


class TestCentroidCacheFingerprintContent:
    """_centroid_cache 的内容指纹必须钉住"具体是哪一批 emb",而不只是 emb **条数**。

    历史指纹 ``(members, len(embs))`` 有三类"条数不变但换了 emb"的碰撞,命中即返回旧
    centroid。其中**层级切换**那类最毒:它让 ``_invalidate_match_cache_for_entry``
    退化成空操作 —— pair sim 清了,但这里返回旧 centroid,于是 ``_cluster_pairwise_union``
    把同一个旧分数原样写回,而且指纹在该成员下次晋级前不会再变(人走出画面就一直留到 TTL)。
    """

    E1 = np.array([1.0] + [0.0] * 127, dtype=np.float32)
    E2 = np.array([0.0, 1.0] + [0.0] * 126, dtype=np.float32)

    @staticmethod
    def _entry_in_cluster(pool, key, cid, *, entry_emb=None, l2=(), l1=(),
                          ts: float = 1_700_000_000.0):
        """白盒造 entry + cluster。``l2`` / ``l1`` 是 (frame_index, emb) 序列,emb 可为 None。"""
        from miloco.perception.engine.identity.tier_u import EquivClass, TierUEntry

        entry = TierUEntry(cam_id=key[0], track_id=key[1])
        entry.cluster_id = cid
        entry.reid_embedding = entry_emb
        entry.last_l1_push_ts = ts
        for frame, emb in l2:
            crop = _make_crop(key[0], key[1], frame, ts)
            crop.reid_embedding = emb
            entry.crops_l2.append(crop)
        for frame, emb in l1:
            crop = _make_crop(key[0], key[1], frame, ts)
            crop.reid_embedding = emb
            entry.crops_l1.append(crop)
        pool._entries[key] = entry
        pool._clusters[cid] = EquivClass(cluster_id=cid, members={key})
        return entry

    def test_tier_switch_with_unchanged_count_recomputes(self):
        """成员从 L1 换到 L2 收(条数恰好不变)→ 必须重算。

        修复前指纹 = (members, 1) 两次相同 → 返回旧 centroid,4c 刚清掉的 pair sim
        下一次就被同一个旧分数原样写回。
        """
        pool = TierUPool(config=TierUConfig())
        key = ("cam-a", 1)
        # L2 有一张但没 emb → 走 L1 fallback,centroid = E1
        entry = self._entry_in_cluster(
            pool, key, "CID-A", entry_emb=self.E1, l2=((0, None),), l1=((100, self.E1),),
        )
        np.testing.assert_allclose(pool._cluster_mean_embedding("CID-A"), self.E1, atol=1e-6)

        # 模拟 _cluster_candidate_for 的回填:L2 那张拿到 emb(正交的 E2)
        # → 该成员改从 L2 收,参与 mean 的条数仍是 1
        entry.crops_l2[0].reid_embedding = self.E2

        c2 = pool._cluster_mean_embedding("CID-A")
        np.testing.assert_allclose(c2, self.E2, atol=1e-6)

    def test_l2_fifo_replacement_recomputes(self):
        """L2 满时 flush 先 popleft 再 append:条数恒 = l2_capacity,但换了一张。

        走真实 push_crop + flush_if_due,不手工动 deque。旧注释断言的"flush 只移动
        不替换"漏了 popleft 这侧。
        """
        provider = _MockReIDProvider()
        provider.set_embedding("cam-a", 1, self.E1)
        clk = _clock()
        pool = TierUPool(
            config=TierUConfig(l1_capacity=2, l2_capacity=2),
            reid_provider=provider,
            now_fn=clk,
        )

        def _flush_round(base_frame: int) -> None:
            for f in range(2):
                pool.push_crop(_make_crop("cam-a", 1, base_frame + f, clk()))
            pool.flush_if_due()

        _flush_round(0)
        _flush_round(10)
        cid = pool._entries[("cam-a", 1)].cluster_id
        assert cid is not None
        assert len(pool._entries[("cam-a", 1)].crops_l2) == 2, "L2 应已填满"
        np.testing.assert_allclose(pool._cluster_mean_embedding(cid), self.E1, atol=1e-6)

        # 第 3 轮换成正交身份 → popleft 一张 E1、append 一张 E2,条数仍是 2
        provider.set_embedding("cam-a", 1, self.E2)
        _flush_round(20)
        l2 = pool._entries[("cam-a", 1)].crops_l2
        assert len(l2) == 2, "FIFO 应保持容量不变(这正是碰撞前提)"
        assert [bool(np.allclose(c.reid_embedding, self.E2)) for c in l2] == [False, True], (
            "应只换掉最旧那张(popleft + append)"
        )

        # mean(E1, E2) 归一 ≈ [0.707, 0.707, 0...]
        c2 = pool._cluster_mean_embedding(cid)
        assert c2 is not None
        np.testing.assert_allclose(c2[:2], [0.7071, 0.7071], atol=1e-3)

    def test_entry_snapshot_replacement_recomputes(self):
        """L1/L2 都没 emb 时靠 entry 级兜底,而它会被 embedding_dirty 重算替换。

        条数不变、值变 —— 旧注释把这条列为"唯一窄碰撞、可接受",现由
        embedding_snapshot_ts 进指纹覆盖。
        """
        pool = TierUPool(config=TierUConfig())
        key = ("cam-a", 1)
        entry = self._entry_in_cluster(
            pool, key, "CID-A", entry_emb=self.E1, l2=((0, None),), l1=((100, None),),
        )
        entry.embedding_snapshot_ts = 1.0
        np.testing.assert_allclose(pool._cluster_mean_embedding("CID-A"), self.E1, atol=1e-6)

        entry.reid_embedding = self.E2
        entry.embedding_snapshot_ts = 2.0

        np.testing.assert_allclose(pool._cluster_mean_embedding("CID-A"), self.E2, atol=1e-6)

    def test_unchanged_content_still_hits_cache(self):
        """内容没变时仍必须命中缓存(指纹变严不能把 np.mean 变成每次重算)。"""
        pool = TierUPool(config=TierUConfig())
        self._entry_in_cluster(
            pool, ("cam-a", 1), "CID-A", entry_emb=self.E1, l2=((0, self.E1), (1, self.E1)),
        )
        c1 = pool._cluster_mean_embedding("CID-A")
        c2 = pool._cluster_mean_embedding("CID-A")
        assert c1 is not None
        assert c1 is c2  # 同一缓存对象,未重算
