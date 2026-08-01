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
   ``STATIC_RULE_EXECUTION`` 把这件事声明给 admin 接口与界面,而不是让规则悄悄失灵。

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
from miloco.perception.rule_scope import (
    camera_prompt_map,
    physical_did,
    rules_for_device,
)
from miloco.perception.types import (
    BatchedSnapshot,
    CaptionEntry,
    MatchedRule,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
)

logger = logging.getLogger(__name__)


def _as_float(v: object) -> float | None:
    """把边车回的 gate_p 归一成 float。契约对第三方实现开放,拿到字符串或
    null 都不该让整轮感知抛异常 —— 归一失败就当作"没有门控概率"。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class LocalVisionEngine(BasePerceptionEngine):
    #: 本通路不执行任何设备动作 —— 规则命中一律交 agent 决策执行,STATIC 规则的
    #: 感知层直连不生效。做成类属性是为了让 admin 接口能直接读它去告知用户,
    #: 而不是在别处再硬编码一份同样的事实(两处一旦漂移,用户看到的就是错的)。
    STATIC_RULE_EXECUTION = False

    def __init__(
        self,
        client: LocalVisionClient,
        *,
        fps: int = 4,
        crf: int = 28,
        max_new_tokens: int = 256,
        gate_threshold: float = 0.0,
        scene_ask: str | None = None,
        max_frames: int = 32,
        short_edge: int = 512,
    ) -> None:
        self._client = client
        self._fps = fps
        self._crf = crf
        self._max_frames = max_frames
        self._short_edge = short_edge
        self._max_new_tokens = max_new_tokens
        # 门控默认 0.0 = 从不据此跳过。参考实现的门控在体育解说数据上训练,
        # 家庭场景属分布外 —— 默认只观测(把 gate_p 记进 timing 供比对),
        # 不让它决定丢不丢事件。用户确认阈值在自家可靠后再调高。
        self._gate_threshold = gate_threshold
        self._scene_ask = scene_ask

    # ── 能力声明 ─────────────────────────────────────────────────────────

    # close / set_main_loop / set_tierc_frame_provider / apply_omni_fps /
    # get_input_config 全部继承 BasePerceptionEngine 的无害默认实现:本通路没有
    # 常驻资源、没有跨线程回调、没有身份识别、也不消费 omni 抽帧率。

    # ── 感知 ─────────────────────────────────────────────────────────────

    def _scene_ask_for(self, did: str, prompt_map: dict[str, str]) -> str | None:
        """拼出该摄像头本轮的场景提问 = 基础提问 + 用户给这台机位写的「感知须知」。

        这段须知是用户在面板上逐台相机填的机位说明(如"这台对着门口,忽略窗外行人"),
        云端通路一直会注入。本地通路必须同样注入 —— 否则同一台相机换条通路,用户
        写的指导就悄悄失效了,而界面上完全看不出来。
        """
        extra = (prompt_map.get(did) or prompt_map.get(physical_did(did)) or "").strip()
        base = self._scene_ask
        if not extra:
            return base
        return f"{base}\n\n本机位补充说明:{extra}" if base else f"感知须知:{extra}"

    async def _perceive_device(
        self, snapshot, rules: list[dict], prompt_map: dict[str, str]
    ) -> dict | None:
        """单设备一次感知。任一环节失败 → 返回 None(该设备本窗口跳过)。"""
        did = snapshot.device.did
        try:
            video = encode_snapshot_to_h264(
                snapshot, fps=self._fps, crf=self._crf,
                max_frames=self._max_frames, short_edge=self._short_edge,
            )
        except EncodeError as e:
            logger.warning("[local-vision] encode failed did=%s: %s", did, e)
            return None

        dispatched = rules_for_device(rules, did)
        payload_rules = [
            {"name": r.get("name", ""), "query": r.get("condition", {}).get("query", "")}
            for r in dispatched
        ]
        try:
            out = await self._client.perceive(
                video,
                rules=payload_rules,
                scene_ask=self._scene_ask_for(did, prompt_map),
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
        with_video = [s for s in batch.snapshots if s.has_video]
        if not with_video:
            # 本轮没有带画面的设备 —— 什么都没失败,只是没得看。标 skipped 但
            # **不填 error_code**,否则这一轮会被记成一次推理错误、污染错误率。
            return RealtimePerceptionResult(skipped=True)

        # 逐窗读一次「感知须知」表(实时,改动下一窗即生效),避免 per-device 重复读 KV。
        prompt_map = camera_prompt_map()
        results = await asyncio.gather(
            *[self._perceive_device(s, rules, prompt_map) for s in with_video]
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
        # did → 本轮实际判过的 rule_id。**必须**填:上层用它给"下发了但没命中"的
        # (rule_id, did) 喂 update_state(False),而规则状态机是边沿触发的 ——
        # 不喂 False,规则命中一次后 last_state 永远停在 True,同一条规则此后再也
        # 不会触发(state 模式也永远不 EXIT、duration 滑窗只进不出)。
        # 只登记**推理成功**的设备:边车失败的设备没有证据,登记了等于凭空推退。
        device_rule_map: dict[str, list[str]] = {}

        for item in ok:
            did, snapshot, dispatched, out = (
                item["did"], item["snapshot"], item["dispatched"], item["out"]
            )
            room = snapshot.room_name or ""
            device_name = getattr(snapshot.device, "name", "") or ""
            gate_p = _as_float(out.get("gate_p"))
            if gate_p is not None:
                gates[did] = gate_p
            if out.get("backend"):
                backends.add(str(out["backend"]))

            # 本设备的规则确实判过了(下面照常解析命中),登记进 map 让上层能把
            # 未命中的那些推退。**门控只压制叙述,不影响规则判定** —— 判定已经算
            # 出来了,丢掉它只会让状态机断供、规则卡死在上一态。
            device_rule_map[did] = [r["id"] for r in dispatched]

            # 规则命中:按名字回填 rule_id。名字在同设备内唯一(miloco 侧保证),
            # 顺序也一一对应,双保险按索引兜底。
            hits = out.get("rule_hits") or []
            for idx, hit in enumerate(hits):
                if not isinstance(hit, dict) or not hit.get("hit"):
                    continue
                rule = dispatched[idx] if idx < len(dispatched) else None
                if rule is None or (hit.get("name") and hit["name"] != rule.get("name")):
                    # 名字对不上就按名字找;**找不到就丢弃这条命中**,绝不回落到
                    # 索引位那条规则 —— 契约是对外开放的,一个只回命中项、且 name
                    # 留空的第三方边车会让"厨房明火"背上"有人跌倒"的判定,触发的
                    # 是完全无关的规则与动作。宁可漏报。
                    rule = next(
                        (r for r in dispatched if r.get("name") == hit.get("name")), None
                    )
                if rule is None:
                    logger.warning(
                        "[local-vision] dropping unmatched rule hit did=%s name=%r",
                        did, hit.get("name"),
                    )
                    continue
                matched.append(MatchedRule(
                    rule_id=rule["id"],
                    rule_name=rule.get("name", ""),
                    reason=hit.get("reason", "") or "本地视觉模型判定命中",
                    room_name=room,
                    source_device_ids=[did],
                    device_name=device_name,
                ))

            # 门控:阈值 > 0 时压制本设备的场景叙述(规则判定已在上面照常处理)。
            if self._gate_threshold > 0 and gate_p is not None and gate_p < self._gate_threshold:
                logger.info(
                    "[local-vision] gate suppressed caption did=%s p=%.2f < %.2f",
                    did, gate_p, self._gate_threshold,
                )
                continue

            caption = (out.get("caption") or "").strip()
            if caption:
                captions.append(CaptionEntry(
                    description=caption,
                    room_name=room,
                    source_device_ids=[did],
                    device_name=device_name,
                ))

        # '_' 前缀的键按约定不参与耗时统计,用来装 per-device 元数据。
        # devices / gate_p / backend 都不是毫秒数,必须走下划线区,否则会被
        # 当成阶段耗时混进面板的延迟明细里。
        timing = {
            "total": round((time.monotonic() - t0) * 1000, 1),
            "_devices": len(ok),
        }
        # 门控概率即使当前不据此决策也留痕,方便用户在自家数据上先观察再定阈值。
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
            # skipped 的语义是「本轮没有证据」,不是「没什么可叙述」—— 上层在
            # skipped 时直接 return,连 device_rule_map 都不看。只要有设备成功判过
            # 规则(map 非空),这一轮就有证据必须交上去,否则未命中的规则拿不到
            # False、状态机再次断供,规则又会卡死在上一态(这正是本文件要修的病)。
            # 典型触发:模型只回判定不写描述 → caption 空;或门控压制了叙述。
            # 注意是 any(values()) 而不是 map 本身非空:一台设备一条规则都没下发时
            # map 里是 {did: []},没有任何状态机需要供给,那才是真的"无事发生"。
            skipped=not captions and not matched and not any(device_rule_map.values()),
            device_rule_map=device_rule_map,
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

        async def _ask(snapshot) -> str | None:
            try:
                video = encode_snapshot_to_h264(
                    snapshot, fps=self._fps, crf=self._crf,
                    max_frames=self._max_frames, short_edge=self._short_edge,
                )
                out = await self._client.perceive(
                    video, rules=[], scene_ask=query,
                    max_new_tokens=self._max_new_tokens, want_gate=False,
                )
            except (EncodeError, LocalVisionError) as e:
                logger.warning(
                    "[local-vision] on-demand failed did=%s: %s", snapshot.device.did, e
                )
                return None
            text = (out.get("caption") or "").strip()
            if not text:
                return None
            room = snapshot.room_name or snapshot.device.did
            return f"{room}: {text}" if len(snaps) > 1 else text

        # 并发问,别串行 —— 串行时 N 台相机遇上卡住的边车,一次主动查询要等
        # N x timeout(默认 60s),agent 那头就是干等几分钟。
        answers = [a for a in await asyncio.gather(*[_ask(s) for s in snaps]) if a]

        if not answers:
            return None
        return OnDemandPerceptionResult(answer="\n".join(answers))
