"""EvidenceDispatcher —— 派发器抽象。

把"如何派发识别 + 如何写回 state"统一到一个 Protocol，``IdentityEngine`` 按配置
注入具体实现。当前已实现：

  - ``FusedDispatcher`` —— 缓存候选，等 omni 主调用 response 回流后触发 on_result

``TrackFreeFusedDispatcher`` 保留占位（构造抛 NotImplementedError），等 track-free
策略落地时再实施。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Protocol

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from miloco.perception.engine.identity.library import GallerySamples

logger = logging.getLogger(__name__)


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class IdentityQueryItem:
    """单个待识别 query。

    fused 模式下携带 prompt 渲染信息（track 列表段的 bbox + face_visible 展示）：
      - bbox_xyxy_norm: 归一化到 0-1000 区间的整数坐标 (x1, y1, x2, y2)。
        与 mimo 系列预处理标准一致；与发给 omni 的视频在分辨率/宽高比上解耦
        （视频被 ``_encode_video_mp4`` 按短边 512 等比缩放，原始像素
        坐标在 prompt 里没有意义——必须归一化）。
    这些字段由 ``IdentityEngine.process`` 填充，``prompt_builder._build_fused_user_content`` 直接读。

    注：不再向 prompt 注入该 track 的当前/疑似**身份**（原 status / candidate_name /
    committed_name 字段已移除）——身份先验会锚定 omni 复读旧答案，破坏"连续 N 次
    独立同答才 commit"计数器赖以成立的投票独立性。omni 每窗只凭 bbox 定位 +
    gallery 视觉独立识别；当前身份与确认全由 engine 侧状态机管理。``is_recheck`` 是 engine
    内部对"是否重审"的标记，当前**不进 prompt**——首次出现与重审在 prompt 里不作区分（去先验更彻底）。
    """

    track_id: int
    body_crop: Optional[NDArray[np.uint8]] = None  # fused 模式可不送 query crop
    bbox_xyxy_norm: Optional[tuple[int, int, int, int]] = None
    # prompt 判置信引导 (tier_c 污染修复): 本窗 FacePersonMatcher 是否把这个
    # track 关联到某个 face_detection。确定性事实, 由 IdentityEngine.process
    # 在派发前算好。prompt 据此引导 omni 在 face_visible=false 时压低置信。
    # None = 未知 (face_detections 未传 / engine 未填), prompt_builder 不渲染该字段。
    face_visible: Optional[bool] = None
    # 该 track 是否为"重新核实"(status ∈ {confirmed, unknown}: 已落定身份, 正被周期性
    # 重审)。当前 prompt 不区分首次/重审(去先验更彻底, 不再渲染任何标记), 该字段已无消费方,
    # 仅由 engine 填充、保留供将来可能的差异化处理。pending(首次识别中、尚未落定) 为 False。
    is_recheck: bool = False


@dataclass
class OmniIdentityResult:
    """omni 识别调用的输出。"""

    track_id: int
    person_id: str | None      # None = unknown
    confidence: float
    reason: str = ""
    # 本次 fused 派发的候选总数。>=2 时 omni 把 video 里看到的某个目标挂到哪个
    # track_id 存在误判风险（哪怕身份识别本身是对的），上层应跳过 tier_c 累积，
    # 避免把别人的体型样本沉淀进目标人物的画像库。
    batch_size: int = 0
    # 跨身份冲突标记：本条 assignment 的 person_id 已经被另一个 confirmed track
    # 占用了。"画面里同一身份只能对应一个 track"这条物理约束意味着此 person_id 不可信，
    # 上层应当将其视为 unknown 处理（state 走 None 路径，不 commit / 不写 tier_c）。
    dup_id: bool = False
    # 本次派发时该 track 是否几何上看到脸（IdentityQueryItem.face_visible 透传）。
    # confirmed 重审时用于区分 None 结果：看到脸仍判 unknown = 对当前身份的"否定"
    # （累 hysteresis、可翻转）；没看到脸 = "弃权"（背对/看不清，身份按兵不动）。
    # 仅在 omni 明确返回了该 track 的 assignment 时才透传真值；omni 漏报该 track 或
    # 整体调用失败时保持 None（非否定，按弃权处理）。
    face_visible: Optional[bool] = None
    # omni 在 identities 把该框判为 "no_person"（框内确无人 / 非人误检），区别于 unknown
    # （有人但认不出）。上层 _on_result 据此累计 no_person 票、落定后停派发 + 身份不导出。
    no_person: bool = False
    # omni 是否对该 track 真正给出了判定。True = 本条来自 omni response 的实际 assignment
    # （成员 / unknown / no_person 都算"答了"）；False = 合成的"非答复"——omni 漏报该 track 或
    # 整体调用失败时兜底补投（person_id=None、no_person=False，但并非"判到人"）。上层据此避免把
    # "没答/失败"误当"判到人"（尤其别拿它把已落定的 no_person 解除、复发 caption 陌生人幻觉）。
    omni_answered: bool = True


# =============================================================================
# 配置
# =============================================================================
# DispatchConfig 复用 config.py 里的 DispatchConfigDC，避免双份 dataclass 漂移。
from miloco.perception.engine.config import (  # noqa: E402
    DispatchConfigDC as DispatchConfig,  # noqa: F401
)

# =============================================================================
# Protocol
# =============================================================================


class EvidenceDispatcher(Protocol):
    """派发器统一接口。"""

    async def dispatch(
        self,
        candidates: list[IdentityQueryItem],
        gallery_snapshot: dict[str, "GallerySamples"],
        on_result: Callable[[OmniIdentityResult], Awaitable[None]],
    ) -> None:
        """派发识别任务（separate: 入 worker queue / fused: 缓存到本窗口）。"""
        ...

    async def close(self) -> None:
        """关闭资源（worker queue 等）。``IdentityEngine.close`` 时调用。"""
        ...


# =============================================================================
# Fused 模式
# =============================================================================


@dataclass
class _FusedPending:
    """fused 模式本窗口缓存的待派发任务。"""

    candidates: list[IdentityQueryItem]
    gallery_snapshot: dict[str, "GallerySamples"]
    on_result: Callable[[OmniIdentityResult], Awaitable[None]]
    # 已 confirmed 的 person_id → 占用的 track_id；deliver_response 据此识别"omni 把
    # 已锁定身份挂到另一个 candidate"的物理冲突，给冲突的 assignment 打 dup_id。
    confirmed_pid_owner: dict[str, int] = field(default_factory=dict)


class FusedDispatcher:
    """fused 模式同步派发——把候选缓存到本窗口，等主调用 response 回流后触发回调。

    与 SeparateDispatcher 不同，FusedDispatcher 没有 worker queue。它的工作流：

    1. ``IdentityEngine.process`` 调 ``dispatch(candidates, gallery, on_result)`` 缓存候选
    2. ``omni.py`` 在构建主调用 prompt 前调 ``take_pending()`` 取候选
    3. ``prompt_builder.build_fused_payload`` 用候选 + gallery 渲染 prompt（含 identity_assignments schema）
    4. ``omni.py`` 调主调用 omni → 拿到 response（含 identity_assignments）
    5. ``omni.py`` 调 ``deliver_response(response_assignments)`` 触发 on_result 写回 state

    注：on_result 在第 5 步被同步触发，即"本窗口主调用 response 同步写回 state"——
    state 的更新在本窗口主调用结束时完成，但**只能影响下一窗口的 prompt 渲染**
    （本窗口 prompt 早已发出）。
    """

    def __init__(self, config: DispatchConfig | None = None) -> None:
        self.config = config or DispatchConfig()
        self._pending: _FusedPending | None = None

    async def dispatch(
        self,
        candidates: list[IdentityQueryItem],
        gallery_snapshot: dict[str, "GallerySamples"],
        on_result: Callable[[OmniIdentityResult], Awaitable[None]],
        confirmed_pid_owner: dict[str, int] | None = None,
    ) -> None:
        """缓存候选到本窗口（累积式）。

        多设备同房间场景下，同一窗口会从 ``IdentityEngine.process`` 触发多次 dispatch
        （每个设备一次）。这里**累积**而非覆盖：否则后到的 dispatch 会丢掉先前设备的
        候选，先前候选已被 ``mark_dispatched`` 置 inflight=True，但 on_result 永远不会
        被 deliver_response 触发 —— 这些 track 会卡死直到 dead-track GC。

        累积语义：
          - candidates 按 track_id 去重，同 track 后到覆盖（最新 bbox / status）
          - gallery_snapshot 合并 dict（同 person_id 后到覆盖）
          - on_result 取最新（不同设备闭包仅 ``now_ts`` 略有差异，影响可忽略）
          - confirmed_pid_owner 取最新（每窗口算出来的"已 confirmed 占位"集合，
            多设备 dispatch 内容应一致，最后一次写入即可）
        """
        owner = dict(confirmed_pid_owner) if confirmed_pid_owner else {}
        if self._pending is None:
            self._pending = _FusedPending(
                candidates=list(candidates),
                gallery_snapshot=dict(gallery_snapshot),
                on_result=on_result,
                confirmed_pid_owner=owner,
            )
        else:
            existing_idx = {c.track_id: i for i, c in enumerate(self._pending.candidates)}
            for c in candidates:
                idx = existing_idx.get(c.track_id)
                if idx is None:
                    self._pending.candidates.append(c)
                else:
                    self._pending.candidates[idx] = c
            self._pending.gallery_snapshot.update(gallery_snapshot)
            self._pending.on_result = on_result
            self._pending.confirmed_pid_owner = owner

        # 累积总量上限（fused 单次主调用就要含全部 candidates；这里仅 warn 不截断 —
        # 截断会让被丢弃的 track 永远 inflight=True，重新引入本次修复的死锁。
        # 实际容量由 prompt 渲染层把控，配置侧应保证 max_queries_per_call ≥ 房间设备数 × 单设备活跃 track 数。）
        if len(self._pending.candidates) > self.config.max_queries_per_call:
            logger.warning(
                "fused dispatch 累积候选数 %d > max_queries_per_call=%d（不截断，避免 inflight 死锁）",
                len(self._pending.candidates), self.config.max_queries_per_call,
            )

    async def close(self) -> None:
        self._pending = None

    # ----- Fused-specific API -----

    def take_pending(self) -> _FusedPending | None:
        """取出本窗口缓存的待派发数据。给 omni.py 构建主调用 prompt 时调用。

        取出后内部 buffer 不立即清空——deliver_response 触发回调后再清。
        允许多次 take（幂等读取）。
        """
        return self._pending

    async def deliver_response(
        self,
        assignments: list[dict],
    ) -> None:
        """主调用 response 解析完成后调用。

        Args:
            assignments: response 里 ``identity_assignments`` 字段，已解析为 dict 列表。
                每项含 ``track_id`` / ``person_id`` / ``confidence`` / ``reason`` 字段。
        """
        if self._pending is None:
            logger.debug("fused deliver_response 时无 pending（跳过）")
            return

        on_result = self._pending.on_result
        candidate_track_ids = {c.track_id for c in self._pending.candidates}
        batch_size = len(self._pending.candidates)
        confirmed_pid_owner = self._pending.confirmed_pid_owner
        # 透传 face_visible 给 on_result：仅用于 confirmed 重审区分"看脸否定"vs"背对弃权"。
        face_visible_by_tid = {c.track_id: c.face_visible for c in self._pending.candidates}

        # 处理 response 中每条 assignment
        seen: set[int] = set()
        for a in assignments:
            try:
                tid = int(a.get("track_id"))
                pid = a.get("person_id")
                if pid in ("unknown", "", None):
                    pid = None
                conf = float(a.get("confidence", 0.0))
                reason = str(a.get("reason", ""))
                is_no_person = bool(a.get("no_person", False))
            except (TypeError, ValueError) as e:
                logger.warning("fused response 解析单条 assignment 失败 a=%s err=%s", a, e)
                continue
            if tid not in candidate_track_ids:
                # response 里出现了不在本窗口候选列表的 track_id，忽略
                continue
            seen.add(tid)

            # 跨身份冲突判定：omni 把 pid 挂到了 tid 上，但 pid 已经被另一个 confirmed
            # track（owner_tid != tid）锁定 —— 物理上不可能两个 track 都是同一人。
            # 此处只打标记，上层 _on_result 据此把 person_id 视为 unknown 处理。
            dup_id = False
            if pid is not None:
                owner_tid = confirmed_pid_owner.get(pid)
                if owner_tid is not None and owner_tid != tid:
                    dup_id = True

            try:
                await on_result(OmniIdentityResult(
                    track_id=tid,
                    person_id=pid,
                    confidence=conf,
                    reason=reason,
                    batch_size=batch_size,
                    dup_id=dup_id,
                    face_visible=face_visible_by_tid.get(tid),
                    no_person=is_no_person,
                ))
            except Exception:  # noqa: BLE001
                # 单条 on_result 抛出不连累整批,也不向上传播(否则 run_omni_fused 的 finally
                # 会误判"未 deliver"、对已处理候选重复投递)。inflight 由 on_result 内部 early-clear。
                logger.warning("fused on_result 异常 track_id=%d", tid, exc_info=True)

        # 候选列表中没在 response 里出现的 track_id：视为 unknown / 失败
        for cand in self._pending.candidates:
            if cand.track_id not in seen:
                try:
                    await on_result(OmniIdentityResult(
                        track_id=cand.track_id,
                        person_id=None,
                        confidence=0.0,
                        reason="fused_response_missing_track",
                        batch_size=batch_size,
                        omni_answered=False,   # 漏报 = 未判定，非"判到人"
                    ))
                except Exception:  # noqa: BLE001
                    logger.warning("fused on_result(missing) 异常 track_id=%d", cand.track_id, exc_info=True)

        # 清 pending
        self._pending = None

    async def deliver_failure(self, reason: str) -> None:
        """主调用整体失败时（HTTP error / parse fail）触发——给所有候选标失败。"""
        if self._pending is None:
            return
        on_result = self._pending.on_result
        batch_size = len(self._pending.candidates)
        for cand in self._pending.candidates:
            try:
                await on_result(OmniIdentityResult(
                    track_id=cand.track_id,
                    person_id=None,
                    confidence=0.0,
                    reason=f"fused_main_call_failed: {reason}",
                    batch_size=batch_size,
                    omni_answered=False,   # 整体失败 = 未判定，非"判到人"
                ))
            except Exception:  # noqa: BLE001
                logger.warning("fused on_result(failure) 异常 track_id=%d", cand.track_id, exc_info=True)
        self._pending = None


# =============================================================================
# track-free 模式占位（暂未实施）
# =============================================================================


class TrackFreeFusedDispatcher:
    """track-free + fused 模式的 dispatcher。当前未实施。"""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("TrackFreeFusedDispatcher 暂未实施")
