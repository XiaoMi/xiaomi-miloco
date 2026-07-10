"""TrackIdentityState 状态机纯函数单测。

重点覆盖 ``update_evidence`` 在 commit 到 unknown 时 ``unknown_recheck_count``
的递增行为, 保证 needs_omni_call 拿到的计数能让"首次重审 interval//2 抢翻转
窗口, 后续恢复正常"语义生效 (commit 85fc76c)。
"""

from __future__ import annotations

from miloco.perception.engine.config import StabilityConfigDC
from miloco.perception.engine.identity.state import (
    TrackIdentityState,
    apply_recheck_result,
    check_pending_timeout,
    get_face_id_value,
    needs_omni_call,
    pick_commit_threshold,
    update_evidence,
)


def _config() -> StabilityConfigDC:
    # 显式 pin 历史阶梯 1/2/3：这些状态机单测按此设计（高置信单票即 commit、
    # 中 2 次、低 3 次）。生产默认已调为单调的 2/3/3（挡误判），单测不继承该
    # 策略默认、只验状态机逻辑本身。
    return StabilityConfigDC(
        commit_threshold_high=1,
        commit_threshold_mid=2,
        commit_threshold_low=3,
    )


# 测试用 engine_fps（needs_omni_call 入口按它把秒间隔换算成帧）。
_FPS = 3


def _frames(sec: float) -> int:
    """秒间隔 → 帧数, 与 needs_omni_call 内部换算同口径。"""
    return max(1, round(sec * _FPS))


class TestUpdateEvidenceUnknownRecheckCount:
    def test_initial_commit_to_unknown_keeps_count_zero(self):
        """初次 commit-to-unknown (从 pending 切来) 不动 count, 保留首次 recheck
        拿 interval//2 抢翻转窗口的语义 (commit 85fc76c "人刚入镜被误判, 几秒后
        全身入镜就该翻回")。
        """
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        # commit_threshold_low (3) 次同答 unknown (conf < low_conf_threshold)
        # → 初次 commit, 从 pending → unknown
        for _ in range(cfg.commit_threshold_low):
            committed = update_evidence(state, None, confidence=0.1, config=cfg)
        assert committed is True
        assert state.status == "unknown"
        # 初次 commit 不动 count, 让下次 needs_omni_call 走 interval//2
        assert state.unknown_recheck_count == 0

    def test_recheck_while_unknown_increments_count(self):
        """已 commit 到 unknown 后, 又一次 unknown recheck 走 commit branch
        → count += 1, 让后续 needs_omni_call 走正常 interval。

        关键观察: candidate_person_id 持续 None 时 stability_count 持续递增,
        每次重审都会再次命中 ``stability_count >= threshold`` → 走 commit branch
        (而非走 line 148-149 的通用 +1 路径), 所以靠 was_unknown_before 分支
        保证递增。
        """
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        # 初次 commit
        for _ in range(cfg.commit_threshold_low):
            update_evidence(state, None, confidence=0.1, config=cfg)
        assert state.unknown_recheck_count == 0
        # 第 1 个 post-commit recheck → count=1
        update_evidence(state, None, confidence=0.1, config=cfg)
        assert state.unknown_recheck_count == 1
        # 第 2 个 post-commit recheck → count=2
        update_evidence(state, None, confidence=0.1, config=cfg)
        assert state.unknown_recheck_count == 2

    def test_commit_to_confirmed_does_not_increment_recheck(self):
        """commit 到 confirmed 时不该动 unknown_recheck_count (它只跟 unknown 相关)。"""
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        # 高 conf 命中具体 person_id → commit 到 confirmed
        for _ in range(cfg.commit_threshold_high):
            update_evidence(state, "person-a", confidence=0.95, config=cfg)
        assert state.status == "confirmed"
        assert state.unknown_recheck_count == 0

    def test_unknown_back_to_pending_resets_count(self):
        """unknown 状态下重审遇到具体 candidate → 切回 pending, count 应重置。

        构造: 先 commit unknown (count=0) → 再来 1 次 unknown recheck (count=1)
        → 再来 mid conf 具体 candidate → 切 pending, count 归零。
        """
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        for _ in range(cfg.commit_threshold_low):
            update_evidence(state, None, confidence=0.1, config=cfg)
        # 再来 1 次 unknown recheck 让 count 涨到 1, 这样下面归零测试才有意义
        update_evidence(state, None, confidence=0.1, config=cfg)
        assert state.status == "unknown"
        assert state.unknown_recheck_count == 1
        # 来一次具体 candidate, 用 mid conf 避免单次直接 commit 到 confirmed
        # (commit_threshold_high=1 时高 conf 第一次就 commit, 不会走 unknown→pending
        # 切换路径)。mid conf 让 stability_count=1 < commit_threshold_mid=2 不
        # commit, 落到 line 141-146 切换路径, count 归零。
        update_evidence(state, "person-a", confidence=0.70, config=cfg)
        assert state.status == "pending"
        assert state.unknown_recheck_count == 0

    def test_unknown_to_confirmed_resets_count(self):
        """unknown 被高 conf candidate commit 到 confirmed 时, count 应归零。
        否则后续 hysteresis 退 pending → 再次进 unknown 时 was_unknown_before=False
        不动 count, stale 残留让首次 recheck 拿不到 interval//2 抢翻转窗口。
        """
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        # 进 unknown, 让 count 涨到 1
        for _ in range(cfg.commit_threshold_low):
            update_evidence(state, None, confidence=0.1, config=cfg)
        update_evidence(state, None, confidence=0.1, config=cfg)
        assert state.status == "unknown"
        assert state.unknown_recheck_count == 1
        # 高 conf candidate 直接 commit 到 confirmed (commit_threshold_high=1)
        update_evidence(state, "person-a", confidence=0.95, config=cfg)
        assert state.status == "confirmed"
        # 离开 unknown 必须归零, 防 stale 残留
        assert state.unknown_recheck_count == 0

    def test_confirmed_to_pending_hysteresis_resets_count(self):
        """apply_recheck_result 触发 confirmed→pending 时 count 应归零 (纵深防御)。

        理论上 count 已在 unknown→confirmed 那里归零, 但这条路径冗余清一次,
        防任何遗漏让 stale 值穿到后续 unknown 进入路径。
        """
        state = TrackIdentityState(track_id=1)
        cfg = _config()
        # 先进 confirmed (用具体 candidate + 高 conf)
        update_evidence(state, "person-a", confidence=0.95, config=cfg)
        assert state.status == "confirmed"
        # 人工塞 stale count 模拟 "early-bird 路径漏 reset" 的边界
        state.unknown_recheck_count = 3
        # 连续 hysteresis_unmatched_count 次重审不一致 → confirmed→pending
        for _ in range(cfg.hysteresis_unmatched_count):
            apply_recheck_result(state, "person-b", confidence=0.95, config=cfg)
        assert state.status == "pending"
        # hysteresis reset 应清零 stale count
        assert state.unknown_recheck_count == 0


class TestNeedsOmniCallUnknownInterval:
    """needs_omni_call 在 unknown 状态下根据 unknown_recheck_count 切 interval//2 vs full。

    单测覆盖消费侧, 防 count 字段被 lockin 错值或 needs_omni_call 比较运算符
    被改反 (e.g. count > 0 改成 count >= 0) 导致 commit 85fc76c 的"首次 recheck
    抢翻转窗口"语义静默失效。
    """

    def test_unknown_first_recheck_uses_half_interval(self):
        """unknown_recheck_count == 0 (初次 commit-to-unknown 后未 recheck 过)
        → 用 interval//2, 实现 commit 85fc76c "首次 recheck 抢翻转窗口" 语义。
        """
        state = TrackIdentityState(track_id=1, status="unknown")
        cfg = _config()
        half = _frames(cfg.recheck_interval_sec) // 2  # 秒→帧再取半, 不写死
        state.last_omni_call_frame = 0
        state.unknown_recheck_count = 0
        assert needs_omni_call(state, half - 1, 0.0, 0.0, cfg, _FPS) is False  # < interval//2
        assert needs_omni_call(state, half, 0.0, 0.0, cfg, _FPS) is True       # == interval//2

    def test_unknown_subsequent_recheck_uses_full_interval(self):
        """unknown_recheck_count > 0 (已 recheck 过至少 1 次) → 走正常 interval。"""
        state = TrackIdentityState(track_id=1, status="unknown")
        cfg = _config()
        full = _frames(cfg.recheck_interval_sec)
        state.last_omni_call_frame = 0
        state.unknown_recheck_count = 1
        assert needs_omni_call(state, full - 1, 0.0, 0.0, cfg, _FPS) is False
        assert needs_omni_call(state, full, 0.0, 0.0, cfg, _FPS) is True

    def test_unknown_count_2_still_full_interval(self):
        """count > 1 也走正常 interval (跟 count==1 行为一致, 防比较运算符被改成 == 1)。"""
        state = TrackIdentityState(track_id=1, status="unknown")
        cfg = _config()
        full = _frames(cfg.recheck_interval_sec)
        state.last_omni_call_frame = 0
        state.unknown_recheck_count = 5
        assert needs_omni_call(state, full - 1, 0.0, 0.0, cfg, _FPS) is False
        assert needs_omni_call(state, full, 0.0, 0.0, cfg, _FPS) is True


# =============================================================================
# tier_c 写库状态机回归 (review: 承重墙逻辑补测, 防新分支被现有用例绕过/假绿灯)
# =============================================================================


class TestApplyRecheckWriteEligible:
    """apply_recheck_result 三路分流 + write_eligible_count 时序门 (tier_c 写库前置)。"""

    @staticmethod
    def _confirmed() -> TrackIdentityState:
        return TrackIdentityState(
            track_id=1,
            status="confirmed",
            committed_person_id="person-a",
            candidate_person_id="person-a",
        )

    def test_consistent_accumulates_write_eligible(self):
        """连续一致 → write_eligible_count 逐次 +1, 不退回 pending, 清矛盾计数。"""
        st = self._confirmed()
        cfg = _config()
        for i in range(1, 4):
            fell_back = apply_recheck_result(st, "person-a", confidence=0.95, config=cfg)
            assert fell_back is False
            assert st.write_eligible_count == i
            assert st.status == "confirmed"
            assert st.consecutive_recheck_unmatched == 0

    def test_abstain_holds_identity_but_breaks_streak(self):
        """弃权 (None 且非 dup_id): 不掉身份 / 不累 hysteresis, 但写库连续性清 0。"""
        st = self._confirmed()
        st.write_eligible_count = 4
        cfg = _config()
        fell_back = apply_recheck_result(st, None, confidence=0.0, config=cfg)
        assert fell_back is False
        assert st.status == "confirmed"
        assert st.write_eligible_count == 0
        assert st.consecutive_recheck_unmatched == 0

    def test_dup_id_none_treated_as_contradiction(self):
        """dup_id-None: 按"矛盾"处理 → 累 hysteresis + 清写库连续性 (而非弃权)。"""
        st = self._confirmed()
        st.write_eligible_count = 4
        cfg = _config()
        fell_back = apply_recheck_result(
            st, None, confidence=0.95, config=cfg, is_dup_id=True
        )
        assert fell_back is False              # 1 次 < hysteresis(2), 尚未退回
        assert st.write_eligible_count == 0
        assert st.consecutive_recheck_unmatched == 1
        assert st.status == "confirmed"

    def test_face_visible_none_counts_as_contradiction(self):
        """看到脸却判 None (face_visible=True): 当"否定/矛盾"处理 → 累 hysteresis, 非弃权。

        修复死局: 库中无同性别第二人时, 被误锁身份的陌生人纠正票永远是 None,
        若一律当弃权则 confirmed 永不翻转。看脸否定计入矛盾才能翻回。
        """
        st = self._confirmed()
        st.write_eligible_count = 4
        cfg = _config()
        fell_back = apply_recheck_result(
            st, None, confidence=0.10, config=cfg, face_visible=True
        )
        assert fell_back is False              # 1 次 < hysteresis(2), 尚未退回
        assert st.write_eligible_count == 0
        assert st.consecutive_recheck_unmatched == 1
        assert st.status == "confirmed"

    def test_face_visible_none_reverts_after_hysteresis(self):
        """连续 hysteresis 次"看脸否定" → confirmed 退回 pending (摘掉误锁身份)。"""
        st = self._confirmed()
        cfg = _config()
        fell_back = False
        for _ in range(cfg.hysteresis_unmatched_count):
            fell_back = apply_recheck_result(
                st, None, confidence=0.10, config=cfg, face_visible=True
            )
        assert fell_back is True
        assert st.status == "pending"
        assert st.consecutive_recheck_unmatched == 0

    def test_no_face_none_still_abstains(self):
        """没看到脸 (face_visible=False) 的 None 仍是弃权: 不掉身份、不累 hysteresis。

        防误退: 真本人背对/侧脸时 omni 回 None, 不应摘掉其已确认身份。
        """
        st = self._confirmed()
        cfg = _config()
        fell_back = apply_recheck_result(
            st, None, confidence=0.10, config=cfg, face_visible=False
        )
        assert fell_back is False
        assert st.status == "confirmed"
        assert st.consecutive_recheck_unmatched == 0


class TestConfirmedRecheckInterval:
    """needs_omni_call confirmed 分支快/慢间隔切换 (review R2 回归保护)。

    快间隔(accumulating)只给"真正在连续攒库段"(0<count<N 且非冷却);
    冷却期 / 已攒够(>=N) / 根本没在攒(count=0, 如持续背对弃权) 一律回落慢间隔,
    不为攒不动的 track 白烧 3x omni 重审。间隔阈值从 cfg 取, 不写死。
    """

    @staticmethod
    def _confirmed() -> TrackIdentityState:
        return TrackIdentityState(
            track_id=1,
            status="confirmed",
            committed_person_id="person-a",
            last_omni_call_frame=100,
        )

    def test_accumulating_uses_fast_interval(self):
        st = self._confirmed()
        st.write_eligible_count = 2            # 0 < 2 < 6 → 攒库段
        st.tier_c_cooldown_until_frame = 0
        cfg = _config()
        fast = _frames(cfg.recheck_interval_accumulating_sec)  # base=last_omni_call_frame=100
        assert needs_omni_call(st, 100 + fast, 0.0, 0.0, cfg, _FPS) is True       # 快间隔达阈
        assert needs_omni_call(st, 100 + fast - 1, 0.0, 0.0, cfg, _FPS) is False

    def test_idle_streak_zero_uses_slow_interval(self):
        """背对/侧对: confirmed 但 write_eligible_count=0 从未攒起 → 慢间隔, 不被 3x 重审。"""
        st = self._confirmed()
        st.write_eligible_count = 0
        st.tier_c_cooldown_until_frame = 0
        cfg = _config()
        fast, slow = _frames(cfg.recheck_interval_accumulating_sec), _frames(cfg.recheck_interval_sec)
        assert needs_omni_call(st, 100 + fast, 0.0, 0.0, cfg, _FPS) is False  # 到快间隔也不触发(走慢)
        assert needs_omni_call(st, 100 + slow, 0.0, 0.0, cfg, _FPS) is True   # 慢间隔达阈

    def test_already_eligible_uses_slow_interval(self):
        st = self._confirmed()
        st.write_eligible_count = _config().write_eligible_min_count   # >= N
        st.tier_c_cooldown_until_frame = 0
        cfg = _config()
        fast, slow = _frames(cfg.recheck_interval_accumulating_sec), _frames(cfg.recheck_interval_sec)
        assert needs_omni_call(st, 100 + fast, 0.0, 0.0, cfg, _FPS) is False
        assert needs_omni_call(st, 100 + slow, 0.0, 0.0, cfg, _FPS) is True

    def test_cooldown_uses_slow_interval(self):
        st = self._confirmed()
        st.write_eligible_count = 3            # 即便值落在攒库区间
        st.tier_c_cooldown_until_frame = 100000  # now_frame < this → 冷却中
        cfg = _config()
        fast, slow = _frames(cfg.recheck_interval_accumulating_sec), _frames(cfg.recheck_interval_sec)
        assert needs_omni_call(st, 100 + fast, 0.0, 0.0, cfg, _FPS) is False  # 到快间隔也不触发(走慢)
        assert needs_omni_call(st, 100 + slow, 0.0, 0.0, cfg, _FPS) is True   # 慢间隔达阈

    def test_suspected_unmatched_dispatches_next_window(self):
        """收到看脸否定 (consecutive_recheck_unmatched>0) → **下窗即派**抢翻转第 2 票
        (翻身份重构: 从旧"快档 10s"提速到每 omni 窗 ~4s, inflight 已挡限流)。

        对比 test_idle_streak_zero: 同样没在攒库(count=0), 但有一次否定即下窗即派。
        """
        st = self._confirmed()
        st.write_eligible_count = 0              # 没在攒库
        st.tier_c_cooldown_until_frame = 0
        st.consecutive_recheck_unmatched = 1     # 已有一次看脸否定 → 存疑
        cfg = _config()
        fast = _frames(cfg.recheck_interval_accumulating_sec)
        # 下窗即派: 距上次派发仅 +1 帧、远未到旧快档间隔也立即派
        assert needs_omni_call(st, 101, 0.0, 0.0, cfg, _FPS) is True
        assert needs_omni_call(st, 100 + fast - 1, 0.0, 0.0, cfg, _FPS) is True


# =============================================================================
# 翻身份(A→B / A→陌生人)黏滞 + 加审
# =============================================================================


class TestFlipIdentity:
    """翻转重构: flip 阈值 / 黏旧名 / 翻转期下窗即派 / 黏滞超时豁免。

    _config() 只 pin 首次阈值(高1/中2/低3); flip 阈值走 StabilityConfigDC 默认
    (高2/中2/低3, flip_sticky_max_recheck=2)。
    """

    def test_pick_commit_threshold_flip_vs_first(self):
        cfg = _config()
        # 高置信: 首次 1 票, 翻转 2 票
        assert pick_commit_threshold(0.9, cfg, is_flip=False) == 1
        assert pick_commit_threshold(0.9, cfg, is_flip=True) == 2
        # 低置信(>=low cutoff、<mid): 翻转 3 票
        assert pick_commit_threshold(0.55, cfg, is_flip=True) == 3

    def test_update_evidence_flip_needs_two_votes(self):
        """翻转态高置信需 2 票才 commit(对照首次 1 票); commit 后清翻转态。"""
        cfg = _config()
        st = TrackIdentityState(
            track_id=1, status="pending",
            reverted_from_confirmed=True, committed_person_id="A",
        )
        assert update_evidence(st, "B", 0.9, cfg) is False   # 第 1 票不够(flip 高门=2)
        assert st.status == "pending"
        assert st.reverted_from_confirmed is True            # 仍翻转态, 黏旧名
        assert update_evidence(st, "B", 0.9, cfg) is True    # 第 2 票 commit
        assert st.status == "confirmed" and st.committed_person_id == "B"
        assert st.reverted_from_confirmed is False           # 翻转结束清态
        assert st.flip_recheck_count == 0

    def test_update_evidence_first_time_unaffected(self):
        """回归: 非翻转态高置信仍 1 票 commit(首次识别体验不变)。"""
        cfg = _config()
        st = TrackIdentityState(track_id=1, status="pending")
        assert update_evidence(st, "B", 0.9, cfg) is True
        assert st.status == "confirmed"

    def test_apply_recheck_revert_enters_flip(self):
        """连续 2 次矛盾退回 pending → 进翻转态: reverted=True/count=0/黏旧名(committed 保留)/
        预存新身份第 1 票。"""
        cfg = _config()  # hysteresis_unmatched_count 默认 2
        st = TrackIdentityState(
            track_id=1, status="confirmed", committed_person_id="A",
            stability_count=5, best_conf=0.9,
        )
        assert apply_recheck_result(st, "B", 0.9, cfg) is False   # 矛盾#1, 未退回
        assert st.status == "confirmed" and st.reverted_from_confirmed is False
        assert apply_recheck_result(st, "B", 0.9, cfg) is True    # 矛盾#2 → 退回
        assert st.status == "pending"
        assert st.reverted_from_confirmed is True
        assert st.flip_recheck_count == 0
        assert st.committed_person_id == "A"   # 黏旧名依赖, 保留
        assert st.candidate_person_id == "B"
        assert st.stability_count == 1         # 退回即预存新身份第 1 票

    def test_check_pending_timeout_flip_exempt(self):
        cfg = _config()
        st = TrackIdentityState(
            track_id=1, status="pending",
            reverted_from_confirmed=True, committed_person_id="A", pending_started_ts=0.0,
        )
        # 远超 60s 但翻转黏滞期豁免
        assert check_pending_timeout(st, now_ts=10_000.0, config=cfg) is False
        assert st.status == "pending"
        # 回归: 非翻转 pending 正常超时掉 unknown
        st2 = TrackIdentityState(track_id=2, status="pending", pending_started_ts=0.0)
        assert check_pending_timeout(st2, now_ts=10_000.0, config=cfg) is True
        assert st2.status == "unknown"

    def test_needs_omni_call_pending_reverted_next_window(self):
        cfg = _config()
        st = TrackIdentityState(
            track_id=1, status="pending", reverted_from_confirmed=True,
            last_omni_call_ts=999.0, last_omni_call_frame=100,
        )
        # 翻转期下窗即派: 即使刚派过也立即派
        assert needs_omni_call(st, 101, 1000.0, 5.0, cfg, _FPS) is True
        # inflight 仍挡
        st.inflight = True
        assert needs_omni_call(st, 101, 1000.0, 5.0, cfg, _FPS) is False

    def test_get_face_id_value_flip_sticky(self):
        # 翻转黏旧名
        st = TrackIdentityState(
            track_id=1, status="pending",
            reverted_from_confirmed=True, committed_person_id="A-uuid",
        )
        assert get_face_id_value(st, distinguish=False) == "A-uuid"
        # 回归: 非翻转 pending 不黏(即便有 committed 残留)
        st2 = TrackIdentityState(track_id=2, status="pending", committed_person_id="A-uuid")
        assert get_face_id_value(st2, distinguish=False) == "pending"
        # 翻转但无 committed → 回落候选
        st3 = TrackIdentityState(
            track_id=3, status="pending",
            reverted_from_confirmed=True, candidate_person_id="B",
        )
        assert get_face_id_value(st3, distinguish=False) == "pending:B"


class TestNeedsOmniCallPendingNextWindow:
    """普通(非翻转)pending 也下窗即派: 不再受 min_dispatch_interval 节流, 加速中低置信多票累积
    (一个窗 ~4s 已是瓶颈)。高置信仍 1 票即 commit、不受影响; inflight 仍挡。"""

    def test_normal_pending_dispatches_every_window(self):
        cfg = _config()
        # 非翻转 pending, 距上次派发仅 1s(远 < min_interval=5s)——改前会被节流返 False, 现下窗即派
        st = TrackIdentityState(track_id=1, status="pending", last_omni_call_ts=100.0)
        assert needs_omni_call(st, 2, 101.0, 5.0, cfg, _FPS) is True
        # inflight 仍挡(每 omni 窗最多一次)
        st.inflight = True
        assert needs_omni_call(st, 2, 101.0, 5.0, cfg, _FPS) is False


class TestNeedsOmniCallGalleryEmpty:
    """身份库为空时的重派节流（Part 2）：只放慢 unknown 到 gallery_empty_recheck_sec，
    pending 不动（no_person 落定靠 pending 期连续投票，放慢会破坏它）。库非空行为不变。
    """

    def test_unknown_gallery_empty_uses_slow_recheck(self):
        cfg = _config()
        state = TrackIdentityState(track_id=1, status="unknown")
        slow = _frames(cfg.gallery_empty_recheck_sec)   # 600s
        normal = _frames(cfg.recheck_interval_sec)      # 30s
        # 常规 30s 到点也不派（要等库空慢周期）
        assert needs_omni_call(state, normal, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is False
        assert needs_omni_call(state, slow - 1, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is False
        assert needs_omni_call(state, slow, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is True

    def test_unknown_gallery_empty_ignores_half_interval(self):
        """库空不走 count==0 的 interval//2 抢翻转（库空无从翻成成员）。"""
        cfg = _config()
        state = TrackIdentityState(track_id=1, status="unknown")
        assert state.unknown_recheck_count == 0
        slow = _frames(cfg.gallery_empty_recheck_sec)
        assert needs_omni_call(state, slow // 2, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is False

    def test_pending_gallery_empty_still_immediate(self):
        """pending 不受库空影响，仍下窗即派——保住 no_person 在 pending_timeout 内攒够票。"""
        cfg = _config()
        state = TrackIdentityState(track_id=1, status="pending")
        assert needs_omni_call(state, 1, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is True
        assert needs_omni_call(state, 10_000, 0.0, 0.0, cfg, _FPS, gallery_empty=True) is True

    def test_unknown_gallery_nonempty_unchanged(self):
        """gallery_empty=False（库非空/默认）→ unknown 仍走 30s（首次 //2），行为不变。"""
        cfg = _config()
        state = TrackIdentityState(track_id=1, status="unknown")
        half = _frames(cfg.recheck_interval_sec) // 2
        assert needs_omni_call(state, half - 1, 0.0, 0.0, cfg, _FPS, gallery_empty=False) is False
        assert needs_omni_call(state, half, 0.0, 0.0, cfg, _FPS, gallery_empty=False) is True
