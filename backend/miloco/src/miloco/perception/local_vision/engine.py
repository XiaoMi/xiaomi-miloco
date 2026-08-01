"""LocalVisionEngine —— 走本地 GPU 视觉模型的感知引擎实现。

与云端 ``PerceptionEngine`` 并列,同实现 ``BasePerceptionEngine``,由
``PerceptionEngineProxy`` 按配置二选一。整条通路**不需要任何 API Key**。

与云端通路的三点本质差异:

1. **纯视觉,不产音频结论**。本地视觉模型没有音频输入,所以 ``speeches`` /
   ``env_sounds`` 恒为空 —— 不是"暂未实现",而是刻意不产:让一个看不见音频的
   模型去填这两个字段,只会得到凭画面脑补的人声与环境音(项目内已有实测结论,
   见 ``omni/field_registry.py`` 的 ``requires_audio``)。要音频能力就配云端通路。

2. **本地不执行任何设备动作**。规则命中只作为"观察结论"上报,一律经
   AgentDispatcher 交给 agent 决策与执行 —— 即本地通路下所有规则都按 DYNAMIC
   语义处理。STATIC 规则的"感知层直连设备"低延迟路径在本通路下不启用,
   ``static_rules_disabled`` 会把这件事显式告诉调用方,而不是让规则悄悄失灵。

3. **suggestions 不由本地模型产**。主动建议依赖跨模态与长上下文推理,4B 级
   视觉模型给不出可用质量;这部分能力上移给 agent。
"""

from __future__ import annotations

import asyncio
import logging
import time

from miloco.perception.engine_base import BasePerceptionEngine
from miloco.perception.local_vision.client import LocalVisionClient, LocalVisionError
from miloco.perception.local_vision.encode import EncodeError, encode_snapshot_to_h264
from miloco.perception.types import (
    BatchedSnapshot,
    CaptionEntry,
    MatchedRule,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
)

logger = logging.getLogger(__name__)


def _physical_did(did: str) -> str:
    """合成通道 did → 物理 did(``cam1:ch0`` → ``cam1``)。

    与云端引擎同语义:规则可绑到整台相机的物理 did,匹配时两种粒度都要命中。
    """
    return did.rsplit(":ch", 1)[0] if ":ch" in did else did


def _rules_for_device(rules: list[dict], did: str) -> list[dict]:
    """按 ``condition.perceive_device_ids`` 筛出该设备该判的规则(空 = 广播)。"""
    return [
        r for r in rules
        if not r.get("condition", {}).get("perceive_device_ids")
        or did in r["condition"]["perceive_device_ids"]
        or _physical_did(did) in r["condition"]["perceive_device_ids"]
    ]


class LocalVisionEngine(BasePerceptionEngine):
    def __init__(
        self,
        client: LocalVisionClient,
        *,
        fps: int = 4,
        crf: int = 28,
        max_new_tokens: int = 256,
        gate_threshold: float = 0.0,
        scene_ask: str | None = None,
    ) -> None:
        self._client = client
        self._fps = fps
        self._crf = crf
        self._max_new_tokens = max_new_tokens
        # 门控默认 0.0 = 从不据此跳过。参考实现的门控在体育解说数据上训练,
        # 家庭场景属分布外 —— 默认只观测(把 gate_p 记进 timing 供比对),
        # 不让它决定丢不丢事件。用户确认阈值在自家可靠后再调高。
        self._gate_threshold = gate_threshold
        self._scene_ask = scene_ask

    # ── 能力声明 ─────────────────────────────────────────────────────────

    @property
    def static_rules_disabled(self) -> bool:
        """本通路不执行设备动作,STATIC 规则的直连执行不生效。

        供上层在日志与界面上显式告知用户 —— 已配了 STATIC 规则的人必须知道
        切到本地通路后它们不再直接控设备,而是改由 agent 决策。
        """
        return True

    async def close(self) -> None:
        return None

    # ── 感知 ─────────────────────────────────────────────────────────────

    async def _perceive_device(self, snapshot, rules: list[dict]) -> dict | None:
        """单设备一次感知。任一环节失败 → 返回 None(该设备本窗口跳过)。"""
        did = snapshot.device.did
        try:
            video = encode_snapshot_to_h264(snapshot, fps=self._fps, crf=self._crf)
        except EncodeError as e:
            logger.warning("[local-vision] encode failed did=%s: %s", did, e)
            return None

        dispatched = _rules_for_device(rules, did)
        payload_rules = [
            {"name": r.get("name", ""), "query": r.get("condition", {}).get("query", "")}
            for r in dispatched
        ]
        try:
            out = await self._client.perceive(
                video,
                rules=payload_rules,
                scene_ask=self._scene_ask,
                max_new_tokens=self._max_new_tokens,
            )
        except LocalVisionError as e:
            logger.warning("[local-vision] sidecar failed did=%s: %s", did, e)
            return None

        return {"did": did, "snapshot": snapshot, "dispatched": dispatched, "out": out}

    async def realtime_perceive(
        self,
        batch: BatchedSnapshot,
        rules: list[dict] | None = None,
        on_early_speeches=None,
        on_early_matched_rules=None,
        on_early_suggestions=None,
    ) -> RealtimePerceptionResult | None:
        """逐设备并发感知,汇总成一份结果。

        ``on_early_*`` 是云端通路的流式早送钩子。本地单次推理只有 1~2 秒,
        没有"边生成边送"的中间态可利用,故不触发 —— 结果一次性返回即可。
        """
        rules = rules or []
        if batch.empty:
            return None

        t0 = time.monotonic()
        results = await asyncio.gather(
            *[self._perceive_device(s, rules) for s in batch.snapshots if s.has_video]
        )
        ok = [r for r in results if r]
        if not ok:
            # 全设备失败:标 skipped 让上层按「本轮无结论」处理,不产空事件。
            return RealtimePerceptionResult(
                skipped=True, error_code="local_vision_unavailable"
            )

        captions: list[CaptionEntry] = []
        matched: list[MatchedRule] = []
        gates: dict[str, float] = {}
        backends: set[str] = set()

        for item in ok:
            did, snapshot, dispatched, out = (
                item["did"], item["snapshot"], item["dispatched"], item["out"]
            )
            room = snapshot.room_name or ""
            gate_p = out.get("gate_p")
            if gate_p is not None:
                gates[did] = gate_p
            if out.get("backend"):
                backends.add(out["backend"])

            # 门控:阈值 > 0 时才据此跳过;默认只观测不决策(见 __init__ 注释)。
            if self._gate_threshold > 0 and gate_p is not None and gate_p < self._gate_threshold:
                logger.info(
                    "[local-vision] gate skip did=%s p=%.2f < %.2f",
                    did, gate_p, self._gate_threshold,
                )
                continue

            caption = (out.get("caption") or "").strip()
            if caption:
                captions.append(CaptionEntry(
                    description=caption,
                    room_name=room,
                    source_device_ids=[did],
                    device_name=getattr(snapshot.device, "name", "") or "",
                ))

            # 规则命中:按名字回填 rule_id。名字在同设备内唯一(miloco 侧保证),
            # 顺序也一一对应,双保险按索引兜底。
            hits = out.get("rule_hits") or []
            for idx, hit in enumerate(hits):
                if not hit.get("hit"):
                    continue
                rule = None
                if idx < len(dispatched):
                    rule = dispatched[idx]
                if rule is None or (hit.get("name") and hit["name"] != rule.get("name")):
                    rule = next(
                        (r for r in dispatched if r.get("name") == hit.get("name")), rule
                    )
                if rule is None:
                    continue
                matched.append(MatchedRule(
                    rule_id=rule["id"],
                    rule_name=rule.get("name", ""),
                    reason=hit.get("reason", "") or "本地视觉模型判定命中",
                    room_name=room,
                    source_device_ids=[did],
                ))

        timing = {
            "total": round((time.monotonic() - t0) * 1000, 1),
            "devices": len(ok),
        }
        # 门控概率一律记进 timing 的下划线区(约定:'_' 前缀不参与耗时统计),
        # 即使当前不据此决策也留痕,方便用户在自家数据上先观察再决定阈值。
        for did, p in gates.items():
            timing[f"_gate_p_{did}"] = round(p, 4)
        if backends:
            timing["_video_backend"] = ",".join(sorted(backends))

        return RealtimePerceptionResult(
            caption=captions,
            matched_rules=matched,
            speeches=[],      # 纯视觉:恒空,不给模型脑补音频的机会
            env_sounds=[],    # 同上
            suggestions=[],   # 主动建议交给 agent
            skipped=not captions and not matched,
            timing=timing,
        )

    async def on_demand_perceive(
        self, batch: BatchedSnapshot, query: str
    ) -> OnDemandPerceptionResult | None:
        """主动查询:把问题直接当场景提问送给本地模型。"""
        if batch.empty:
            return None
        snaps = [s for s in batch.snapshots if s.has_video]
        if not snaps:
            return None

        answers: list[str] = []
        for snapshot in snaps:
            try:
                video = encode_snapshot_to_h264(snapshot, fps=self._fps, crf=self._crf)
                out = await self._client.perceive(
                    video, rules=[], scene_ask=query,
                    max_new_tokens=self._max_new_tokens, want_gate=False,
                )
            except (EncodeError, LocalVisionError) as e:
                logger.warning(
                    "[local-vision] on-demand failed did=%s: %s", snapshot.device.did, e
                )
                continue
            text = (out.get("caption") or "").strip()
            if text:
                room = snapshot.room_name or snapshot.device.did
                answers.append(f"{room}: {text}" if len(snaps) > 1 else text)

        if not answers:
            return None
        return OnDemandPerceptionResult(answer="\n".join(answers))
