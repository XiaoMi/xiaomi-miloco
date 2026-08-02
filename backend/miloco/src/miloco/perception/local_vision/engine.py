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


#: 边车整体不可达后,连续多少窗才开始退避。1~2 窗的抖动很常见(边车正在重启),
#: 不该一有失败就停摆。
_BACKOFF_AFTER_FAILURES = 3
#: 退避时长上限(秒)。到点后放一窗过去试探,成功即清零。
_BACKOFF_MAX_SEC = 30.0
#: 哪些逐设备失败该算进"边车不可达"。encode_failed 是本地编码问题(PyAV 缺
#: libx264、帧不是三通道),一次网络都没发过 —— 把它算进去会让用户被指向网络,
#: 而拆掉引擎对它毫无帮助。malformed_response 保留:边车确实答了,但答得不对。
_SIDECAR_ATTRIBUTABLE = frozenset({"LocalVisionError", "malformed_response"})
#: 连续多少窗完全不可达后,把引擎降回「等前置条件」态。比退避阈值大一档:短暂抖动
#: 由退避吸收,真的挂了才降级(降级会让界面显示"感知不可用",不该被一次抖动触发)。
_DEMOTE_AFTER_FAILURES = 5
#: 每条规则判定行的 token 开销(「规则N:是/否,依据…」),用于放大生成预算。
_TOKENS_PER_VERDICT = 24
#: 生成预算的上限。必须与边车 ``PerceiveRequest.max_new_tokens`` 的 ``le`` 一致 ——
#: 两者是分开部署的构件,这里放得比那边宽的话,每一窗都会 422,而在 miloco 的日志里
#: 与其它边车故障长得一模一样。有意保持这个字面值成对出现,改一处必须改另一处。
_MAX_NEW_TOKENS_CEILING = 1024
#: 主动查询这类自由问答用的复读硬禁 n —— 显著放宽,见调用处说明。
#: 这个值是边车侧 ``_NO_REPEAT_NGRAM_WITH_RULES`` 的镜像(同为 32):两边分属独立
#: 部署的构件,改一边而不改另一边,主动查询的回答就会被按定时描述的力度掐断。
#: 与 ``_MAX_NEW_TOKENS_CEILING`` 同一类跨仓库配对,两侧各有一条测试钉住字面值。
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
        codec_target_canvas: int = 8,
    ) -> None:
        self._client = client
        self._container_fps = container_fps
        self._crf = crf
        self._max_frames = max_frames
        self._short_edge = short_edge
        self._max_new_tokens = max_new_tokens
        # codec 的 token 预算 —— 本通路真正的成本旋钮(prompt token ≈ 223×它)。
        # 帧数与分辨率对成本影响都很小,所以这个值单独配、单独送给边车。
        self._codec_target_canvas = codec_target_canvas
        # 门控默认 0.0 = 从不据此跳过。参考实现的门控在体育解说数据上训练,
        # 家庭场景属分布外 —— 默认只观测(把 gate_p 记进 timing 供比对),
        # 不让它决定丢不丢事件。用户确认阈值在自家可靠后再调高。
        self._gate_threshold = event_gate_threshold
        self._scene_ask = scene_ask
        # 边车整体不可达时的退避。没有它的话:引擎已经建好,tick 的探活会 no-op,
        # 于是每个窗口仍然照常给每台相机做一次 libx264 编码(同步 CPU 活),再去连
        # 一个死掉的端口 —— 4 台相机就是每 4 秒 4 次白烧,而且日志被刷满。云端通路
        # 对同类情形有熔断器,这条通路此前什么都没有。
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        #: 已经就哪些「相机台数」提示过容量不足。按台数记而不是记一个布尔:实测
        #: 发现冷启动时单相机偶尔会超一次,把唯一那次额度用掉,之后用户**加了一台
        #: 相机**——一个全新的、稳定的容量问题——反而不再提示了。
        self._over_budget_warned_for: set[int] = set()

    @property
    def sustained_failure(self) -> bool:
        """边车已经连续多窗完全不可达。

        上层据此把引擎降回「等前置条件」态,好让既有那套(状态条提示 + tick 自愈)
        接手 —— 否则引擎会永远停在 ready,而用户在界面上看不到任何异常:云端通路
        挂掉有全局红条,本地通路此前什么都没有。
        """
        return self._consecutive_failures >= _DEMOTE_AFTER_FAILURES

    # ── 能力声明 ─────────────────────────────────────────────────────────

    # proxy / processor 会**无条件**调用下面这几个方法。刻意在这里各写一遍,而不是
    # 去给 ABC 加无害默认值 —— 那样云端引擎哪天把 close() 改了名,proxy 会静默地
    # 什么都不做(身份引擎的线程于是泄漏),而不是报错。契约留在 ABC 上是严格的,
    # 这条通路"不需要"什么,由这条通路自己声明。

    async def close(self) -> None:
        """释放常驻 HTTP 连接池。除此之外本通路没有需要收的资源。"""
        # getattr 而不是直接调:契约允许注入任何满足 perceive 的客户端(测试替身、
        # 第三方实现),它们不一定持有连接池。有就关,没有就不关。
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()

    def set_main_loop(self, loop) -> None:  # noqa: ANN001
        """本通路没有跨线程回调,不需要主循环句柄。"""
        return None

    def set_tierc_frame_provider(self, provider) -> None:  # noqa: ANN001
        """tier_c 是身份识别的清理钩子;本通路不做身份识别。"""
        return None

    def apply_omni_fps(self, omni_fps: int) -> None:
        """omni 抽帧率是云端通路的概念,本通路不消费它(见 get_input_config)。"""
        return None

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

    def _resolve_short_edge(self) -> int | None:
        """本通路自己的短边上限。

        **刻意不再复用 perception.engine.input.video_short_edge**:两条通路的成本
        结构是相反的。云端按 token 计费,分辨率和帧数都得省;本通路的成本由 codec
        的 token 预算封顶,分辨率给高了只让每个 canvas 更贵而收益递减(论文:超过
        384 像素只有边际收益),不如把预算换成更高帧率。共用一个旋钮会逼两条通路
        往相反方向调同一个值。
        """
        return self._short_edge  # None = 不缩放,原始画面直接交给模型

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
                codec_target_canvas=self._codec_target_canvas,
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

        return {"did": did, "snapshot": snapshot, "dispatched": dispatched, "out": out,
                # 本窗实际拿到多少源帧 —— 判断"画布喂饱了没有"的唯一依据:
                # 画布数 = 源帧数 / 8(group_size=32 产出 4 张),喂不够就只能拿到更少。
                "src_frames": len(snapshot.video.frames) if snapshot.video else 0}

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

        if time.monotonic() < self._backoff_until:
            # 退避窗口内:直接判本窗无结论,连编码都不做。与"边车失败"落在同一档
            # 语义上(上层按无结论跳过),区别只是不再白烧 CPU。
            return RealtimePerceptionResult(
                skipped=True, error_code="local_vision_unavailable"
            )

        # 逐窗读一次「感知须知」表(实时,改动下一窗即生效),避免 per-device 重复读 KV。
        prompt_map = camera_prompt_map()
        # 与下面 timing["total"] 用的 t0 分开量。t_start 从这里起算,覆盖编码 +
        # 并发等待 + 推理,也就是这一窗真正占掉的墙钟 —— "追不追得上感知周期"
        # 比的正是它。t0 起算得更晚、只覆盖汇总阶段,用于和云端的耗时口径对齐。
        t_start = time.monotonic()
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
            #
            # 但只有**边车归因**的失败才算进"边车不可达"。本地编码失败(PyAV 没编进
            # libx264、帧不是三通道)一次网络都没发,拆掉引擎既救不了它,还会把用户
            # 指向网络和边车 —— 真正的原因只在另一行 WARNING 里。
            if any(c in _SIDECAR_ATTRIBUTABLE for c in errors.values()):
                self._consecutive_failures += 1
            if (
                self._consecutive_failures
                and self._consecutive_failures >= _BACKOFF_AFTER_FAILURES
            ):
                # 指数退避,封顶 30s:边车重启一般十几秒就好,退避太久会让恢复
                # 明显变慢;而封顶之后每 30s 试探一次,代价可以忽略。
                step = 2 ** (self._consecutive_failures - _BACKOFF_AFTER_FAILURES)
                wait = min(_BACKOFF_MAX_SEC, 4.0 * step)
                self._backoff_until = time.monotonic() + wait
                logger.warning(
                    "[local-vision] 边车连续 %d 窗不可达,退避 %.0fs 后再试"
                    "(期间不再编码,本轮按无结论处理)",
                    self._consecutive_failures, wait,
                )
            return RealtimePerceptionResult(
                skipped=True, error_code="local_vision_unavailable",
                timing={f"_omni_error_{d}": c for d, c in errors.items()},
            )

        # 有任何一台成功 = 边车活着,清掉退避。
        self._consecutive_failures = 0
        self._backoff_until = 0.0

        # 容量提示:边车用一把锁串行推理(单卡单模型),所以一个窗口的耗时约等于
        # 各相机推理耗时之和 —— 相机数一多就会追不上感知周期,而症状是"事件变稀疏"
        # 这种很难联想到原因的表现。用仓库既有的 RTF 口径(耗时/窗口时长)判,
        # 并且**只提示一次**,免得每窗刷屏。
        # 除数必须是用户**真正配的**感知周期。此前写的是
        # getattr(batch, "window_duration_ms", …) —— BatchedSnapshot 上根本没有这个
        # 字段(只有 snapshots / captured_at),于是永远落到硬编码的 4000:period_sec
        # 调到 8 就每窗误报,调到 2 就该报的时候不报。get_input_config 早就会读它。
        window_ms = self.get_input_config().period_sec * 1000.0
        elapsed_ms = (time.monotonic() - t_start) * 1000
        if elapsed_ms > window_ms and len(ok) not in self._over_budget_warned_for:
            self._over_budget_warned_for.add(len(ok))
            logger.warning(
                "[local-vision] 一个窗口耗时 %.0fms 已超过感知周期 %.0fms"
                "(%d 台相机;边车串行推理,耗时随相机数累加)。"
                "可减少同时启用的相机、调大 perception.engine.input.period_sec,"
                "或把 max_new_tokens / video_short_edge 调小。",
                elapsed_ms, window_ms, len(ok),
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
            **{f"_src_frames_{r['did']}": r["src_frames"] for r in ok},
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
