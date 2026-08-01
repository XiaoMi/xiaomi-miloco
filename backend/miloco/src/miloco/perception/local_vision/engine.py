"""LocalVisionEngine —— 走本地 GPU 视觉模型的感知引擎实现。

与云端 ``PerceptionEngine`` 并列,同实现 ``BasePerceptionEngine``,由
``PerceptionEngineProxy`` 按配置二选一。整条通路**不需要任何 API Key**。

与云端通路的三点本质差异:

1. **纯视觉,不产音频结论**。本地视觉模型没有音频输入,所以 ``speeches`` /
   ``env_sounds`` 恒为空 —— 不是"暂未实现",而是刻意不产:让一个看不见音频的
   模型去填这两个字段,只会得到凭画面脑补的人声与环境音(项目内已有实测结论,
   见 ``omni/field_registry.py`` 的 ``requires_audio``)。要音频能力就配云端通路。

2. **本地不执行任何设备动作**。规则命中只作为"观察结论"上报,**不含**规则里
   配置好的那些设备动作 —— 它们会被直接拒绝执行并记为一次 ``RULE_TRIGGER_FAILURE``,
   既不改写成 DYNAMIC、也不换条路替用户做掉(改写会丢掉 ``cooldown_minutes`` /
   ``idempotent`` 和台账里 ``source=rule`` 的归属)。要不要动设备、动哪个,由 agent
   自己判断。两道门:切换时拒绝(转移检查)+ 执行时拒绝(状态不变量)。
   人工 / agent 经 ``POST /api/rules/{id}/trigger`` 显式触发**不受此限**——那是
   被授权做决定的角色主动发起,不是感知通路自作主张。

3. **suggestions 不由本地模型产**。主动建议依赖跨模态与长上下文推理,4B 级
   视觉模型给不出可用质量;本通路的 ``suggestions`` 恒为空,不做替代实现。
"""

from __future__ import annotations

import asyncio
import logging
import time

from miloco.observability.context import (
    DeviceContext,
    reset_device_context,
    set_device_context,
)

# 复用云端那一份时间窗格式化:它走 deploy_timezone(),而不是进程时钟 —— 这条
# 差别有过实际事故(UTC 主机把北京 10:52 说成「凌晨02:52」,agent 据此判断作息)。
# 自己再写一份就等于把那个 bug 重新引进来一次。
from miloco.perception.engine.pipeline import _fmt_time_window
from miloco.perception.engine_base import BasePerceptionEngine
from miloco.perception.local_vision.client import LocalVisionClient, LocalVisionError
from miloco.perception.local_vision.encode import EncodeError, encode_snapshot_to_h264
from miloco.perception.rule_scope import (
    camera_prompt_map,
    rules_for_device,
)
from miloco.perception.snapshot_context import push_clip_bytes
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


#: 每条规则判定行的 token 开销(「规则N:是/否,依据…」),用于放大生成预算。
_TOKENS_PER_VERDICT = 24
#: 生成预算的上限。必须与边车 ``PerceiveRequest.max_new_tokens`` 的 ``le`` 一致 ——
#: 两者是分开部署的构件,这里放得比那边宽的话,每一窗都会 422,而在 miloco 的日志里
#: 与其它边车故障长得一模一样。有意保持这个字面值成对出现,改一处必须改另一处。
_MAX_NEW_TOKENS_CEILING = 1024
#: 主动查询这类自由问答用的复读硬禁 n —— 显著放宽,见调用处说明。
_NGRAM_GUARD_FREEFORM = 32


class _InputConfig:
    """面板上「各层帧率」那一行要读的东西。

    返回 None 会让 processor 把三层都显示成 0fps —— 本通路确实有一个真实的送检
    帧率(容器帧率),显示 0 是错的而不是"没有"。omni_fps 则如实为 0:本通路
    不做 omni 抽帧,那个旋钮在这里没有对应物。
    """

    __slots__ = ("fps", "omni_fps", "period_sec")

    def __init__(self, fps: int, period_sec: float) -> None:
        self.fps = fps
        self.omni_fps = 0
        self.period_sec = period_sec


class LocalVisionEngine(BasePerceptionEngine):
    #: 本通路不执行任何设备动作 —— 规则命中只作为观察结论上报,STATIC 规则的
    #: 感知层直连不生效。做成类属性是为了让 admin 接口能直接读它去告知用户,
    #: 而不是在别处再硬编码一份同样的事实(两处一旦漂移,用户看到的就是错的)。
    STATIC_RULE_EXECUTION = False

    def __init__(
        self,
        client: LocalVisionClient,
        *,
        container_fps: int = 4,
        crf: int = 28,
        max_new_tokens: int = 256,
        event_gate_threshold: float = 0.0,
        scene_ask: str | None = None,
        max_frames: int = 32,
        short_edge: int | None = None,
    ) -> None:
        self._client = client
        self._container_fps = container_fps
        self._crf = crf
        self._max_frames = max_frames
        # None = 每窗实时跟随共享的 perception.engine.input.video_short_edge。
        # 该设置的既有契约是「写盘后下一帧即生效、无需重启」(见 admin/router 的
        # perception-config),构造期定死会让面板上调分辨率对本通路无效。
        self._short_edge_override = short_edge
        self._max_new_tokens = max_new_tokens
        # 门控默认 0.0 = 从不据此跳过。参考实现的门控在体育解说数据上训练,
        # 家庭场景属分布外 —— 默认只观测(把 gate_p 记进 timing 供比对),
        # 不让它决定丢不丢事件。用户确认阈值在自家可靠后再调高。
        self._gate_threshold = event_gate_threshold
        self._scene_ask = scene_ask

    # ── 能力声明 ─────────────────────────────────────────────────────────

    # close / set_main_loop / set_tierc_frame_provider / apply_omni_fps /
    # get_input_config 全部继承 BasePerceptionEngine 的无害默认实现:本通路没有
    # 常驻资源、没有跨线程回调、没有身份识别、也不消费 omni 抽帧率。

    # ── 感知 ─────────────────────────────────────────────────────────────

    def get_input_config(self):
        """本通路的送检帧率。基类默认返回 None,会让面板把各层都显示成 0fps。"""
        from miloco.config import get_settings

        try:
            period = float(
                get_settings().perception.engine.get("input", {}).get("period_sec", 4)
            )
        except Exception:  # noqa: BLE001
            period = 4.0
        return _InputConfig(self._container_fps, period)

    def _resolve_short_edge(self) -> int:
        """每次取用时解析短边上限,而不是构造期定死。"""
        if self._short_edge_override is not None:
            return self._short_edge_override
        from miloco.config import get_settings

        try:
            return int(
                get_settings().perception.engine.get("input", {}).get(
                    "video_short_edge", 512
                )
            )
        except Exception:  # noqa: BLE001 —— 读配置失败不该中断感知
            return 512

    def _camera_note_for(self, did: str, prompt_map: dict[str, str]) -> str:
        """取该机位的「感知须知」。

        以**独立字段**送出,绝不拼进 scene_ask —— 拼进去会有两个后果:默认配置下
        scene_ask 为空,拼接结果里就只剩用户那句话,任务提问整个消失;而且它会落在
        输出格式约定之前,一句「只用一句话回答」就能让规则行不再出现,fail-closed
        随后把它变成"该相机所有规则静默失效"。云端通路的做法同样是把它作为一个
        独立小节附加在完整任务提示之上,而不是取代。
        """
        # 与云端一致地只按合成 did 查(prompt 就是按合成 did 存的)。此处曾多一层
        # 物理 did 兜底 —— 是死代码,但会让两条通路出现无人知晓的行为差异,而
        # rule_scope 的存在意义正是消灭这类差异。
        return (prompt_map.get(did) or "").strip()

    def _token_budget(self, rule_count: int) -> int:
        """描述预算之外,按规则条数追加判定行的开销。

        配置里的 max_new_tokens 是给**场景描述**定的。判定行是额外产出:每条约
        「规则N:是/否,依据…」二十来个 token。不追加的话,规则一多就必然截断在
        判定块中间 —— 而截断后的缺失判定会被 fail-closed 读成"未命中",于是
        「规则越多、越靠后的规则越容易静默失效」,且没有任何报错。
        """
        return min(
            _MAX_NEW_TOKENS_CEILING,
            self._max_new_tokens + _TOKENS_PER_VERDICT * max(0, rule_count),
        )

    async def _perceive_device(
        self, snapshot, rules: list[dict], prompt_map: dict[str, str]
    ) -> dict | None:
        """单设备一次感知。任一环节失败 → 返回 None(该设备本窗口跳过)。"""
        did = snapshot.device.did
        try:
            # libx264 是同步 CPU 活,必须丢线程 —— 直接在协程里编码会占住所在
            # 事件循环(主动查询走的就是主循环)。项目里 miot/transcoder.py 为同一
            # 个理由专门建了执行器,这里遵循同一条约定。
            video = await asyncio.to_thread(
                encode_snapshot_to_h264,
                snapshot, self._container_fps, self._crf,
                self._max_frames, self._resolve_short_edge(),
            )
        except EncodeError as e:
            logger.warning("[local-vision] encode failed did=%s: %s", did, e)
            return {"did": did, "error": "encode_failed"}

        # 把送去推理的那段字节挂到当前事件上。云端通路是在 omni 内部 push 的
        # (prompt_builder),本地这条路绕过了 omni,不补这一下的话
        # artifacts.clips 恒空 —— 日志页里每一条事件都只剩文字、没有视频,
        # 而这个缺失既不在能力声明里、也没有任何报错。
        token = set_device_context(
            DeviceContext(device_trace_id="", device_id=did,
                          room_name=snapshot.room_name or "")
        )
        try:
            push_clip_bytes(video, "mp4")
        finally:
            reset_device_context(token)

        dispatched = rules_for_device(rules, did)
        payload_rules = [
            {"name": r.get("name", ""), "query": r.get("condition", {}).get("query", "")}
            for r in dispatched
        ]
        try:
            out = await self._client.perceive(
                video,
                rules=payload_rules,
                scene_ask=self._scene_ask,
                camera_note=self._camera_note_for(did, prompt_map),
                max_new_tokens=self._token_budget(len(payload_rules)),
            )
        except LocalVisionError as e:
            logger.warning("[local-vision] sidecar failed did=%s: %s", did, e)
            return {"did": did, "error": type(e).__name__}

        if not isinstance(out, dict):
            # 契约对第三方实现开放。返回一个数组之类的东西会在下面 .get() 处抛
            # AttributeError,穿透"任一环节失败 → 该设备跳过"的降级设计,毁掉整窗
            # 所有设备。
            logger.warning(
                "[local-vision] sidecar returned %s, expected object did=%s",
                type(out).__name__, did,
            )
            return {"did": did, "error": "malformed_response"}

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
        # 失败的设备也带回来了(只带 did + error),分开:成功的进汇总,失败的
        # 进 timing。云端通路用 _omni_error_{did} 记录逐设备失败,观测面板据此
        # 统计 —— 本地这条路不写的话,一台相机每一窗都 503 也永远显示"零错误",
        # 而它上面的规则从此不再被评估。
        errors = {r["did"]: r["error"] for r in results if r and "error" in r}
        ok = [r for r in results if r and "error" not in r]
        if not ok:
            # 全设备失败:标 skipped 让上层按「本轮无结论」处理,不产空事件。
            # 逐设备原因照样带上 —— 全灭和"某一台一直灭"要能区分开。
            return RealtimePerceptionResult(
                skipped=True, error_code="local_vision_unavailable",
                timing={f"_omni_error_{d}": c for d, c in errors.items()},
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
            # 事件文本里的「时间」字段就取这个。不填的话每条本地事件都少一行时间,
            # 而 agent 判断作息、判断"刚才"指的是何时,全靠它。
            time_window = _fmt_time_window(
                snapshot.start_timestamp, snapshot.end_timestamp
            )
            gate_p = _as_float(out.get("gate_p"))
            if gate_p is not None:
                gates[did] = gate_p
            if out.get("backend"):
                backends.add(str(out["backend"]))

            # 本设备的规则确实判过了(下面照常解析命中),登记进 map 让上层能把
            # 未命中的那些推退。**门控只压制叙述,不影响规则判定** —— 判定已经算
            # 出来了,丢掉它只会让状态机断供、规则卡死在上一态。
            device_rule_map[did] = [r["id"] for r in dispatched]

            # fail-closed 把"没解析出判定"和"模型说不"变成同一个结果:规则未命中,
            # 状态规则还会因此走 on_exit。差别只在日志里 —— 不打出来的话,一台
            # 相机的规则可能整天都在被推退,而任何一层都看不出异常。
            unparsed = out.get("unparsed_rules") or 0
            if unparsed and dispatched:
                logger.warning(
                    "[local-vision] did=%s %s/%d 条规则未判出结论%s,本窗按未命中处理",
                    did, unparsed, len(dispatched),
                    "(生成被 token 上限截断)" if out.get("truncated") else "",
                )
            elif out.get("truncated"):
                # 没有规则时截断同样要报:描述会从句子中间断掉,然后原样交给 agent。
                # 只在有规则时才看这个标志的话,这种情况永远不会有人发现。
                budget = self._token_budget(len(dispatched))
                logger.warning(
                    "[local-vision] did=%s 描述被截断(本窗预算 %d token)%s",
                    did, budget,
                    "" if budget >= _MAX_NEW_TOKENS_CEILING
                    else ";可调高 perception.local_vision.max_new_tokens",
                )

            # 规则命中:按名字回填 rule_id。名字在同设备内唯一(miloco 侧保证),
            # 顺序也一一对应,双保险按索引兜底。
            hits = out.get("rule_hits") or []
            for idx, hit in enumerate(hits):
                if not isinstance(hit, dict) or not hit.get("hit"):
                    continue
                name = (hit.get("name") or "").strip()
                if not name:
                    # 空 name 不能落到索引兜底:一个只回命中项的第三方边车会让
                    # 「厨房明火」背上「有人跌倒」的判定 —— 正是下面那段注释警告的
                    # 情形。按既定的「宁可漏报」直接丢弃。
                    logger.warning(
                        "[local-vision] dropping rule hit with empty name did=%s", did
                    )
                    continue
                rule = dispatched[idx] if idx < len(dispatched) else None
                if rule is None or name != rule.get("name"):
                    # 名字对不上就按名字找;**找不到就丢弃这条命中**,绝不回落到
                    # 索引位那条规则 —— 契约是对外开放的,一个只回命中项、且 name
                    # 留空的第三方边车会让"厨房明火"背上"有人跌倒"的判定,触发的
                    # 是完全无关的规则与动作。宁可漏报。
                    rule = next(
                        (r for r in dispatched if r.get("name") == name), None
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
                    time_window=time_window,
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
                    time_window=time_window,
                ))

        # '_' 前缀的键按约定不参与耗时统计,用来装 per-device 元数据。
        # devices / gate_p / backend 都不是毫秒数,必须走下划线区,否则会被
        # 当成阶段耗时混进面板的延迟明细里。
        timing = {
            **{f"_omni_error_{d}": code for d, code in errors.items()},
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
                video = await asyncio.to_thread(
                    encode_snapshot_to_h264,
                    snapshot, self._container_fps, self._crf,
                    self._max_frames, self._resolve_short_edge(),
                )
                out = await self._client.perceive(
                    video, rules=[], scene_ask=query,
                    max_new_tokens=self._max_new_tokens, want_gate=False,
                    # 主动查询的提问是 agent 现编的自由文本,答案里合理重复一个
                    # 短句的可能性远高于定时描述(「是的,门是关着的;门是关着的
                    # 状态已持续…」)。默认那个为定时描述调出来的 12-gram 硬禁
                    # 会把这类回答掐断,故这里放宽到与带规则时同档。
                    ngram_guard=_NGRAM_GUARD_FREEFORM,
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
