"""Pending State —— 每 track 一份的身份判定状态机。

这不是独立组件——本文件仅含 ``TrackIdentityState`` dataclass 和几个纯函数；
``IdentityEngine`` 内部维护一个 ``dict[track_id, TrackIdentityState]``，
通过本模块的纯函数操作状态转移。

两个核心机制：
  - **置信度感知 commit**（``pick_commit_threshold`` / ``update_state``）：
    单次 omni 识别可能误判，按 ``best_conf`` 决定需要几次同答才 commit
  - **重审 hysteresis**（``apply_recheck_result``）：
    confirmed 状态周期性重审，连续 N 次不一致才退回 pending，防止单次抖动翻车
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

TrackStatus = Literal["none", "pending", "confirmed", "unknown", "no_person"]


# =============================================================================
# 配置（conf-aware commit 阈值）
# =============================================================================
# StabilityConfig 复用 config.py 里的 StabilityConfigDC，避免双份 dataclass 漂移。
# 模块级 re-export 保留 ``StabilityConfig`` 名字，给纯函数签名继续用。
from miloco.perception.engine.config import (  # noqa: E402
    StabilityConfigDC as StabilityConfig,  # noqa: F401
)

# =============================================================================
# 单 track 状态
# =============================================================================


@dataclass
class TrackIdentityState:
    """每个 track 的身份判定状态。"""

    track_id: int
    status: TrackStatus = "none"

    # pending 阶段：当前最有可能的候选
    candidate_person_id: str | None = None
    stability_count: int = 0
    best_conf: float = 0.0

    # confirmed 阶段：最终值
    committed_person_id: str | None = None
    unknown_index: int | None = None  # 仅 status=unknown 时有值（distinguish=true 时分配）

    # 派发节流相关
    last_omni_call_frame: int = 0
    last_omni_call_ts: float = 0.0
    pending_started_ts: float = 0.0
    inflight: bool = False
    unknown_recheck_count: int = 0   # commit unknown 后重审了几次(首次重审用 interval//2)

    # 重审 hysteresis (矛盾计数: 仅"判成另一个具体人"或 dup_id 才累加; 弃权 None 不累)
    consecutive_recheck_unmatched: int = 0

    # 时序一致性写库门 (tier_c 污染修复): **连续**命中同一 committed 身份的 confirmed
    # 重审次数。严格连续——None(弃权)/矛盾/coasting 任一打断即清 0; commit(pending→
    # confirmed) 时也清 0, 攒库完全发生在确认之后。仅 apply_recheck_result 维护(pending
    # 不累); _enqueue_tier_c_candidate 入口检查 ≥ stability.write_eligible_min_count 才入队。
    write_eligible_count: int = 0

    # tier_c 写库冷却截止帧(按窗口/帧计数, 非墙钟)。写一张后 engine 置为 当前窗帧 + cooldown_frames
    # (= 2N 次快重审);在此之前 needs_omni_call 走慢重审、tier_c 不晋升(process 冻结 write_eligible)。
    tier_c_cooldown_until_frame: int = 0

    # tier_c 写入"在途"标志(设计文档 E7 的 H1): 候选已入异步写库队列、worker 尚未出结果
    # 期间为 True, 防同一 track 在结果出来前重复入队(每次重复入队=多一次 omni 校验调用)。
    # worker 处理完(成功/否决/丢弃)清 False。与冷却一样, 期间 process() 冻结 write_eligible。
    in_flight_tier_c: bool = False

    # ----- 身份漂移自检(commit 后人物交叉/交互致 track 跟错人的纠正) -----
    # 累计 M 个低窗"track 当前外观质心 vs 近期同摄 TierC 质心" cos < 阈 的计数; sim 回升即清 0,
    # 无数据窗(emb 不足/无参考)不计不清。达 drift_check.consecutive_windows 入嫌疑集
    # (observe 只打日志; enforce 撤回重判)。
    drift_consec_low: int = 0
    # (enforce)采信复认护栏: 撤回时记下撤前 committed 身份; 若 omni 复认回同一 person,
    # 不再对该 (track, person) body 二次撤, 直到 committed 变成另一个新身份才重新武装。
    # 防 FP 震荡(撤→复认→又撤)。None = 未武装。
    drift_suppressed_pid: str | None = None

    # ----- 翻身份(A→B / A→陌生人)黏滞 + 加审 -----
    # 翻转场景标记: 由 confirmed 退回的 pending。True 时① commit 用 flip 阈值(更严防误翻)
    # ② 显示黏旧名(不闪 unknown)③ needs_omni_call 下窗即派。commit 成功 / 放手时清 False。
    reverted_from_confirmed: bool = False
    # 退回后经过的重审窗数; 达 flip_sticky_max_recheck 仍未 commit → engine 放手(置上面 False)。
    flip_recheck_count: int = 0

    # ----- no_person（非人误检抑制）-----
    # 连续命中 omni "no_person"（框内确无人）的票数; 达 vote_threshold 落定 status=no_person。
    no_person_vote_count: int = 0
    # 落定 no_person 时该 track 的锚点 bbox(xyxy); 后续帧据此判"明显移动"→回 pending 重新识别。
    no_person_anchor_bbox: tuple[int, int, int, int] | None = None


# =============================================================================
# 状态机操作（纯函数）
# =============================================================================


def pick_commit_threshold(
    best_conf: float, config: StabilityConfig, is_flip: bool = False
) -> int:
    """根据当前 candidate 的最高 conf 反查 commit 阈值（high/mid/low 之一，具体次数由配置定）。

    ``is_flip=True``（由 confirmed 退回的 pending 重确认）用 flip 阈值组（高2/中2/低3）：
    高置信比首次（1 票）更严、防单票误翻；中/低置信因翻转入口已积累矛盾证据（连续 2 次重审
    不一致才退回），再 settle 不必比首次更慢。首次识别仍保高置信单票快速上名。
    """
    if is_flip:
        hi, mid, lo = (
            config.flip_commit_threshold_high,
            config.flip_commit_threshold_mid,
            config.flip_commit_threshold_low,
        )
    else:
        hi, mid, lo = (
            config.commit_threshold_high,
            config.commit_threshold_mid,
            config.commit_threshold_low,
        )
    if best_conf >= config.high_conf_threshold:
        return hi
    if best_conf >= config.mid_conf_threshold:
        return mid
    return lo


def promote_to_pending(state: TrackIdentityState, now_ts: float | None = None) -> None:
    """SortTracker 首次 confirm 一个 track 时调用——状态从 none → pending。

    后续 IdentityEngine 检查 ``needs_omni_call`` 决定是否派发首次 omni 任务。
    """
    if state.status != "none":
        return
    state.status = "pending"
    state.pending_started_ts = now_ts if now_ts is not None else time.time()


def record_no_person(
    state: TrackIdentityState,
    *,
    anchor_bbox: tuple[int, int, int, int] | None,
    vote_threshold: int,
) -> bool:
    """omni 判该框"无人"（no_person）时累计票数；连续达 ``vote_threshold`` 则落定 no_person 状态。

    仅对**未确认成员**的 track 生效（none / pending / unknown）：confirmed 成员不因 no_person
    票翻转（视为弃权，防把背对 / 遮挡的真本人误掉），返回 False 不计票。已是 no_person 的
    track 不重复落定。落定时清空身份字段（candidate / committed / unknown_index）并记锚点 bbox。

    Returns:
        True 仅当本次调用使状态落定到 ``no_person``。
    """
    state.inflight = False  # 任务回流，清 inflight
    if state.status in ("confirmed", "no_person"):
        return False
    state.no_person_vote_count += 1
    if state.no_person_vote_count < vote_threshold:
        return False
    state.status = "no_person"
    state.no_person_anchor_bbox = anchor_bbox
    state.candidate_person_id = None
    state.committed_person_id = None
    state.unknown_index = None
    state.stability_count = 0
    state.best_conf = 0.0
    return True


def _bbox_moved(
    anchor: tuple[int, int, int, int],
    cur: tuple[int, int, int, int],
    displacement_ratio: float,
    min_abs_px: float,
) -> bool:
    """中心位移同时满足"≥ min_abs_px"且"≥ ratio×锚点对角线"才算明显移动。"""
    ax1, ay1, ax2, ay2 = anchor
    cx1, cy1, cx2, cy2 = cur
    disp = (((ax1 + ax2) - (cx1 + cx2)) ** 2 + ((ay1 + ay2) - (cy1 + cy2)) ** 2) ** 0.5 / 2.0
    diag = ((ax2 - ax1) ** 2 + (ay2 - ay1) ** 2) ** 0.5
    return disp >= min_abs_px and disp >= displacement_ratio * diag


def clear_no_person_on_motion(
    state: TrackIdentityState,
    current_bbox: tuple[int, int, int, int] | None,
    displacement_ratio: float,
    min_abs_px: float,
) -> bool:
    """no_person track 相对锚点 bbox 明显移动 → 回 pending 重新识别。

    处理"误检框被路过的真人 ID-switch 带走"：静态误检框不动、维持 no_person；一旦框开始
    随真人移动，立即解除抑制、重新走识别。返回 True 表示本次解除。
    """
    if (
        state.status != "no_person"
        or state.no_person_anchor_bbox is None
        or current_bbox is None
    ):
        return False
    if not _bbox_moved(state.no_person_anchor_bbox, current_bbox, displacement_ratio, min_abs_px):
        return False
    state.status = "pending"
    state.no_person_vote_count = 0
    state.no_person_anchor_bbox = None
    state.pending_started_ts = time.time()
    return True


def clear_no_person_to_pending(state: TrackIdentityState, now_ts: float | None = None) -> None:
    """no_person track 被慢重审判到"人"（收到非 no_person 的 omni 结果）→ 回 pending 重新识别。

    与 ``clear_no_person_on_motion`` 并列的第二条解除通道：那条靠"框移动"（会动的真人），这条
    靠"重审真看到人"（一动不动、只能靠 ``needs_omni_call`` 慢周期重审兜底的真人）。转 pending 后
    needs_omni_call 对 pending 走"下窗即派"，几秒内即可重新 commit 出去；真静止误检物体每次重审
    仍判 no_person、走不到这里，故不会因此复发"陌生人"幻觉。
    """
    state.status = "pending"
    state.no_person_vote_count = 0
    state.no_person_anchor_bbox = None
    state.pending_started_ts = now_ts if now_ts is not None else time.time()


def update_evidence(
    state: TrackIdentityState,
    candidate_person_id: str | None,
    confidence: float,
    config: StabilityConfig,
) -> bool:
    """新的 omni 识别证据到达时更新 state。

    Args:
        state:                被更新的 track state
        candidate_person_id:  omni 给出的 person_id；None 表示 omni 输出 unknown 类
        confidence:           omni 给出的 conf

    Returns:
        True 如果该次更新触发了 commit（status: pending → confirmed），否则 False。
    """
    state.inflight = False  # 任务回流，清 inflight 标记

    # 低于 cutoff 视为 unknown 候选
    if confidence < config.low_conf_threshold:
        candidate_person_id = None

    # candidate 切换 vs 累积
    # 注: write_eligible_count(写库资格)是 confirmed-only, pending 阶段不累积——
    # commit 时清 0, 之后仅在 apply_recheck_result 里随 confirmed 重审连续一致累加。
    if state.candidate_person_id == candidate_person_id:
        state.stability_count += 1
        state.best_conf = max(state.best_conf, confidence)
    else:
        state.candidate_person_id = candidate_person_id
        state.stability_count = 1
        state.best_conf = confidence

    # 检查 commit 阈值（conf-aware）；翻转重确认(reverted_from_confirmed)用更严的 flip 阈值
    threshold = pick_commit_threshold(
        state.best_conf, config, is_flip=state.reverted_from_confirmed
    )
    if state.stability_count >= threshold:
        # 快照 commit 前的 status, 用于区分"初次 commit-to-unknown"(从 pending/none
        # 切来) vs "已 unknown 时 recheck 又触发 commit branch"。两种情况都走
        # 同一段 commit 分支(stability_count 持续累积所以已 commit 后还会再次
        # 命中 threshold), 但语义不同。
        was_unknown_before = state.status == "unknown"
        # 触发 commit
        if candidate_person_id is None:
            # 三次同答 unknown → 落定 unknown 状态
            state.status = "unknown"
            # 已 unknown 时又一次 commit-to-unknown = recheck 到达, 计数 +1 让
            # 下次 needs_omni_call 走正常 interval。初次 commit (从 pending/none
            # 切到 unknown) 不增, 保留 count=0 让首次 recheck 拿到 interval//2
            # 抢翻转窗口 (commit 85fc76c 设计: "人刚入镜信息不全被误判, 几秒后
            # 全身入镜就该翻回")。
            # 注: 全程 unknown 的 stranger track 持续命中 commit branch (因
            # candidate 持续 None → stability_count 持续递增), 走不到下面"未达
            # 阈值"段的通用 +1 路径, 必须靠这里 was_unknown_before 分支保证递增。
            if was_unknown_before:
                state.unknown_recheck_count += 1
        else:
            state.status = "confirmed"
            # 写库资格从"确认之后"重新攒: commit 这一刻清 0, 之后靠 confirmed 重审
            # 连续一致累加到 N 才写 tier_c (杜绝 pending 期/确认前的样本进库)。
            state.write_eligible_count = 0
            # 离开 unknown → confirmed: 重置 count 防 stale 残留。否则同 track
            # 后续 confirmed→pending→unknown 来回切换时, 新一轮 commit-to-unknown
            # 是 was_unknown_before=False (从 pending 切来) 不动 count, 残留的
            # stale count != 0 会让 needs_omni_call 走 full interval (30 帧)
            # 而非 interval//2 (15 帧), 已注册人第二次"离开-回来被误判"时翻回
            # confirmed 比第一次慢 ~15s。
            if was_unknown_before:
                state.unknown_recheck_count = 0
        state.committed_person_id = candidate_person_id
        # 翻转结束(commit 成 confirmed 或落 unknown 都算): 清翻转态, 显示/阈值/派发回归正常。
        state.reverted_from_confirmed = False
        state.flip_recheck_count = 0
        return True

    # 未达阈值，处理状态切换：
    #   - none → pending（首次累积）
    #   - unknown 重审遇到具体候选 → pending（让"刚登记的人"或"角度误判的人"
    #     重新进入累积，不卡死在 unknown）
    # 注: unknown 状态下 candidate 持续 None 时 stability_count 持续累积, 必然
    # 命中**上面的 commit branch** (was_unknown_before=True → +1) 提前 return True,
    # 走不到这里的 "if status == 'unknown'" 自加路径。所以 unknown_recheck_count
    # 的递增统一在上面 commit branch 内做; 这里只处理 status 转移 + 切回 pending 归零。
    if state.status == "none":
        state.status = "pending"
    elif state.status == "unknown" and candidate_person_id is not None:
        state.status = "pending"
        # 重置 pending 起始时间——否则会沿用最初进入 pending 时的旧时间戳，
        # 下一窗口 check_pending_timeout 立即触发 timeout，把刚累积的证据冲掉。
        state.pending_started_ts = time.time()
        state.unknown_recheck_count = 0  # 切回 pending,重审计数归零
    return False


def apply_recheck_result(
    state: TrackIdentityState,
    candidate_person_id: str | None,
    confidence: float,
    config: StabilityConfig,
    is_dup_id: bool = False,
    face_visible: bool | None = None,
) -> bool:
    """confirmed 状态下的周期重审结果应用。结果分三类处理 (tier_c 污染修复)：

      - **一致** (== committed)：``write_eligible_count += 1`` (连续一致攒写库资格)，
        清矛盾计数 ``consecutive_recheck_unmatched``。
      - **弃权** (``candidate is None``、非 dup_id、且**当时没看到脸**)：omni 这窗没认出
        (背对/看不清/漏帧)，**不是反驳**——身份按兵不动 (不累 hysteresis、不退回 pending，
        真本人偶尔背对不掉身份)；但**写库连续性被打断 → ``write_eligible_count`` 清 0**。
      - **矛盾** (candidate 是另一个具体 person / dup_id 被掩成的 None / **看到脸却判
        None=对当前身份的否定**)：累 hysteresis + ``write_eligible_count`` 清 0；连续
        ≥ ``hysteresis_unmatched_count`` 退回 pending。

    Args:
        is_dup_id: 本次 None 是否来自 dup_id 跨身份冲突 (omni 实质判成了另一个已
                      被占位的人, 被 engine 掩成 None)。True 时按"矛盾"处理而非"弃权"。
        face_visible: 本次派发时该 track 是否几何上看到脸。``True`` 时一个 None 结果是
                      "看着脸不认 = 否定"(走矛盾分支可翻转)；``False``/``None`` 时是
                      "看不清 = 弃权"(身份按兵不动)。区分"陌生人正脸被否"与"真本人背对"。

    Returns:
        True 如果状态从 confirmed 退回 pending（重新累积），False 否则。
    """
    state.inflight = False

    if state.status != "confirmed":
        # 非 confirmed 状态不应当进重审，回退到普通 update_evidence
        update_evidence(state, candidate_person_id, confidence, config)
        return state.status != "confirmed"

    # 把低置信视为 unknown (弃权)
    if confidence < config.low_conf_threshold:
        candidate_person_id = None

    # —— 弃权: None、非 dup_id、且当时没看到脸 (背对/看不清, 没认出 ≠ 反驳) ——
    # 不动 hysteresis / stability (真本人背对不掉身份), 仅打断写库连续性。
    # 但"看到脸却判 None"(face_visible=True) 是对当前身份的否定 → 不早退, 落到下面矛盾分支。
    if candidate_person_id is None and not is_dup_id and not face_visible:
        state.write_eligible_count = 0
        return False

    # —— 一致 ——
    if candidate_person_id is not None and candidate_person_id == state.committed_person_id:
        state.stability_count = max(state.stability_count, 1)
        state.write_eligible_count += 1      # 连续一致 +1, 累计到 N 才允许写 tier_c
        state.best_conf = max(state.best_conf, confidence)
        state.consecutive_recheck_unmatched = 0
        return False

    # —— 矛盾: 另一个具体 person, 或 dup_id-None —— 累 hysteresis + 写库连续性清 0
    state.consecutive_recheck_unmatched += 1
    state.stability_count = max(1, state.stability_count // 2)
    state.write_eligible_count = 0

    if state.consecutive_recheck_unmatched >= config.hysteresis_unmatched_count:
        # 退回 pending
        state.status = "pending"
        # 重置 pending 起始时间——否则旧时间戳会让 next check_pending_timeout 立即击杀，
        # 与 update_evidence 的 unknown→pending pattern 同源。
        state.pending_started_ts = time.time()
        state.candidate_person_id = candidate_person_id
        state.stability_count = 1
        state.write_eligible_count = 0       # 退回 pending: 攒库从头 (重新 confirmed 后才再攒)
        state.best_conf = confidence
        state.consecutive_recheck_unmatched = 0
        # 进入翻转态: 显示黏旧名(committed 已保留)、commit 用 flip 阈值、下窗即派加审。
        # 这次矛盾票已被计为新身份"第 1 票"(上面 stability_count=1), 翻成员高/中只需再 1 票即切。
        state.reverted_from_confirmed = True
        state.flip_recheck_count = 0
        # 纵深防御: confirmed→pending hysteresis 路径也清 unknown_recheck_count,
        # 跟 update_evidence 的 unknown→confirmed reset 互为兜底。这条 confirmed
        # 是从早期 unknown 经 commit-to-confirmed 上来的, 理论上 count 已在那里
        # 归零, 这里冗余清一次保证 count 不会因任何遗漏路径残留 stale 值导致
        # 后续重进 unknown 时丢失首次 recheck 的 interval//2 优化。
        state.unknown_recheck_count = 0
        # committed_person_id 暂时保留（让 face_id 字段在重新稳定前保留旧姓名，避免下游闪烁）
        return True

    return False


def check_pending_timeout(
    state: TrackIdentityState,
    now_ts: float,
    config: StabilityConfig,
) -> bool:
    """pending 持续 ≥ pending_timeout_sec 未达稳定 → 强制 unknown 落定。

    本函数只做状态机转换；``unknown_index`` 的分配由调用方（``IdentityEngine``）
    通过单一 helper 完成，避免计数器读写散落在 state 机内、多分配点共用且无锁。

    Returns:
        True 如果触发了 unknown 落定（调用方据此分配 unknown_index）。
    """
    if state.status != "pending":
        return False
    # 翻转黏滞期不走 60s 超时(否则黏旧名期间被强制掉 unknown 闪陌生人); 放手由 engine 控
    # flip_recheck_count, 放手后此标记已清, 超时恢复正常。
    if state.reverted_from_confirmed:
        return False
    if (now_ts - state.pending_started_ts) < config.pending_timeout_sec:
        return False
    state.status = "unknown"
    state.committed_person_id = None
    return True


def needs_omni_call(
    state: TrackIdentityState,
    now_frame: int,
    now_ts: float,
    min_dispatch_interval_sec: float,
    config: StabilityConfig,
    engine_fps: float,
    no_person_recheck_sec: float = 1200.0,
    gallery_empty: bool = False,
) -> bool:
    """判断该 track 是否需要派发新的 omni 识别任务。

    决策矩阵（含 unknown 也走重审）：
        none, 刚 promoted to pending                              → True
        任意 status, inflight                                      → False
        pending, 非 inflight                                      → True   ← 下窗即派(不按 min_interval 节流)
        confirmed, 看脸否定累计中(consecutive_recheck_unmatched>0)   → True   ← 下窗即派抢翻转第 2 票
        confirmed, 攒库段距快间隔已到 / 否则距慢间隔已到             → True
        confirmed, 未达上述阈值                                    → False
        unknown 首次重审 (count==0) 距 ≥ recheck//2                 → True   ← 抢翻转窗口
        unknown 后续重审 (count>0)  距 ≥ recheck 周期                → True
        unknown,   未达上述阈值                                    → False

    ``gallery_empty=True``（身份库为空）时只改 unknown 分支：把重派周期从 recheck_interval_sec
    放慢到 config.gallery_empty_recheck_sec（且不走 count==0 的 interval//2 抢翻转——库空无从翻成
    成员）。库空下 unknown 常规重派唯一价值（等人转身/走近再匹配成员）恒为 0，放慢只留周期性
    翻 no_person 的机会。**pending 分支不受影响**：no_person 落定靠 pending 期连续投票在
    pending_timeout(60s) 内累计，放慢会破坏它。库非空时该参数无作用（等价旧行为）。

    重审周期(秒)在此入口按 ``engine_fps`` 换算成 frame_index 帧数(round(sec×fps)),
    与 max_age_sec 同款"秒标定、运行时换算", 改 fps 墙钟周期不漂移。
    注: ``min_dispatch_interval_sec`` 形参现仅为签名兼容保留——fused pending 已改"下窗即派"、
    函数体不再据它节流。
    """
    if state.status == "none":
        # 还没 promote 到 pending；等 SortTracker 把它 confirmed 上来才会 call promote_to_pending
        return False
    if state.status == "no_person":
        # 已落定非人误检：默认停派发（不重复问 omni 幻影框）。但每 no_person_recheck_sec 放行一次
        # 慢重审——"任何状态都不会被永久卡死"的最后兜底，兜极少数"未注册真人一动不动被连判两票
        # 误压、又不触发移动解除"的残留（会动的人由 engine 侧 motion check 即时解除；急性摔倒 /
        # 倒地动作由上游事件检测捕获、不依赖此状态，故周期放得很长）。重审判到人即回 pending
        # （见 _on_result → clear_no_person_to_pending）。
        if state.inflight:
            return False
        if state.last_omni_call_frame == 0:
            # reject_region 预标的 track（从未真正派过 omni）不参与时间重审：其解除靠移动 /
            # 真人覆盖 / 区域 TTL；否则流跑久后 now_frame 一上来就 ≥ 周期而立即误派。
            return False
        interval = max(1, round(no_person_recheck_sec * engine_fps))
        return (now_frame - state.last_omni_call_frame) >= interval
    if state.inflight:
        return False
    if state.status == "pending":
        # 首次识别与翻转重确认都**下窗即派**(inflight 已在上面挡 → 每 omni 窗最多一次):
        # 一个窗 ~4s 已是 omni 瓶颈, 再加 min_interval(5s) 节流只会拖慢中低置信的多票累积
        # (高置信本就 1 票即 commit、不受影响)。pending 必在几票内落定(commit / 60s 超时 /
        # 连续 None→unknown), 不会无限每窗派。fused 每窗本就跑, 多塞个 candidate 不增 omni
        # 调用次数。注: min_dispatch_interval_sec 形参保留(签名/separate 归档路径), fused
        # pending 不再据它节流。
        return True
    if state.status == "confirmed":
        # 收到"看脸否定"(consecutive_recheck_unmatched>0)后**下窗即派**抢翻转第 2 票:
        # 把摘错身份从 ~hysteresis×慢周期(~60s) 压到每 omni 窗(~4s, inflight 已挡限流);
        # 一旦翻转或重新认对, consecutive_recheck_unmatched 清 0, 自动回落正常间隔。
        if state.consecutive_recheck_unmatched > 0:
            return True
        # 快间隔只给"真正在连续攒库段"的 track(连续段已起步但未满 N): 更快凑齐"连续 N 次
        # 一致"。其余一律回落慢间隔——冷却期内(只验身份不攒库)、已攒够(>=N, 写被下游门挡住)、
        # 以及根本没在攒(连续段=0, 如持续背对/侧对时 omni 弃权, write_eligible 永远清 0)——
        # 不为"攒不动"的 track 白烧 3x omni 重审。冷却由 tier_c_cooldown_until_frame 标记。
        min_n = config.write_eligible_min_count
        accumulating = (
            now_frame >= state.tier_c_cooldown_until_frame
            and 0 < state.write_eligible_count < min_n
        )
        interval = (
            max(1, round(config.recheck_interval_accumulating_sec * engine_fps))
            if accumulating
            else max(1, round(config.recheck_interval_sec * engine_fps))
        )
        return (now_frame - state.last_omni_call_frame) >= interval
    if state.status == "unknown":
        # unknown 也走重审，让"刚登记的人"或"角度误判的人"能被自动识别上。
        # 自适应: 首次重审用 interval//2 (commit unknown 后最可能翻转的窗口——
        # 人刚入镜信息不全被误判, 几秒后全身入镜就该翻回),后续恢复正常间隔。
        if gallery_empty:
            # 库空: 没有任何成员可匹配, "等人转身/走近再匹配成员"的重派价值恒为 0 → 放慢到
            # 库空专用慢周期, 只保留周期性给 omni 翻 no_person 的机会(静物误检延迟纠正)。不走
            # count==0 的 interval//2 抢翻转(库空无从翻成成员)。库非空时不进此分支、回常规间隔。
            interval = max(1, round(config.gallery_empty_recheck_sec * engine_fps))
            return (now_frame - state.last_omni_call_frame) >= interval
        interval = max(1, round(config.recheck_interval_sec * engine_fps))
        if state.unknown_recheck_count == 0:
            interval = interval // 2
        return (now_frame - state.last_omni_call_frame) >= interval
    return False


def mark_dispatched(state: TrackIdentityState, now_frame: int, now_ts: float) -> None:
    """派发任务前在 state 上打 inflight 标记。"""
    state.inflight = True
    state.last_omni_call_frame = now_frame
    state.last_omni_call_ts = now_ts


def get_face_id_value(
    state: TrackIdentityState,
    distinguish: bool,
    scope_label: str = "",
) -> str:
    """渲染 prompt 用的 face_id 字符串值。

    Args:
        distinguish:  是否区分陌生人编号
        scope_label:  unknown id 的 scope 前缀（如 ``"客厅-dev0"``）。
                      非空时 unknown id 渲染为 ``unknown-{scope_label}-{idx}``；
                      空时走老格式 ``unknown_{idx}``，单 engine 部署向后兼容。

    Returns:
        - "none"                                          状态 none
        - "pending"                                       pending 无 candidate
        - "pending:<id>"                                  pending 有 candidate
        - "<person_id>"                                   confirmed
        - "unknown-<scope_label>-<n>" / "unknown_<n>"    unknown + distinguish=true
        - "unknown"                                       unknown + distinguish=false
    """
    if state.status == "none":
        return "none"
    if state.status == "no_person":
        # 非人误检：身份不导出（不进名册、不当待识别人物），等同"此处没人"。
        return "none"
    if state.status == "pending":
        # 翻转黏旧名: 由 confirmed 退回的 pending 显示保持旧成员名, 翻转期不闪 unknown。
        # 放手后 reverted_from_confirmed=False, 自动回落 "pending"/候选。
        # 去先验: 本窗被派发的翻转 track 靠 candidate_tids 剔出名册(见 prompt_builder), 不污染
        # 自身重审投票独立性; 但 coasting(本窗无检测、未派发)的翻转 track 不在 candidate_tids 内,
        # 仍可能以旧名进别的 track 名册当先验(已知小缺口, 同 inflight 窗, 设计内 defer——除非
        # 实测旧名先验导致翻不动再消除)。
        if state.reverted_from_confirmed and state.committed_person_id:
            return state.committed_person_id
        if state.candidate_person_id:
            return f"pending:{state.candidate_person_id}"
        return "pending"
    if state.status == "confirmed":
        return state.committed_person_id or "unknown"
    if state.status == "unknown":
        if distinguish and state.unknown_index is not None:
            if scope_label:
                return f"unknown-{scope_label}-{state.unknown_index}"
            return f"unknown_{state.unknown_index}"
        return "unknown"
    return "none"
