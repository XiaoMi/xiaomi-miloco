# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 源：把一条 MIoT 属性的当前值判成条件项的 bool。

设计见 docs/superpowers/specs/2026-09-08-iot-trigger-source-design.md。本文件先放
条件项的读取与求值这两件纯粹的事 —— 它们同时被手动触发、创建校验和源层用到。
"""

from __future__ import annotations

import asyncio
import logging
import operator
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from miloco.rule.schema import IOT_SOURCE_TYPE
from miloco.state.types import MISSING
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

_ORDERING_OPS = ("gt", "gte", "lt", "lte")

SUPPORTED_OPS: dict[str, Any] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}

# 数值 / 布尔 / 字符串三族，跨族即不兼容。
_NUMBER, _BOOL, _STR = "number", "bool", "str"


class EvalFailed(Exception):
    """这次求值做不了。**调用方要置未就绪，不能喂假** —— 假会驱动一次凭空的退出边沿。"""


@dataclass(frozen=True)
class IotRef:
    """一条 iot 条件项。一条 rule 一个 ``(did, iid, op, value)``。"""

    rule_id: str
    did: str
    iid: str
    op: str
    value: Any


def iot_ref_of(rule) -> IotRef | None:
    """rule 的条件项是不是 iot 源，是就返回它引用的那条属性。

    判源走 ``resolved_source_type``（唯一那份判据），本函数只负责取 spec。

    形状不认识时记日志返 None，不抛：建 rule 时已经校验过，跑到这里还不认识说明是
    库里的存量脏数据（迁移和代建那两条入口绕过创建校验），让这条不触发就行。
    """
    if rule.resolved_source_type != IOT_SOURCE_TYPE:
        return None
    dnf = getattr(rule, "condition_dnf", None)
    if dnf is None or not dnf.any_of:
        return None
    for conjunction in dnf.any_of:
        for item in conjunction:
            spec = item.spec or {}
            did, iid, op = spec.get("did"), spec.get("iid"), spec.get("op")
            if not did or not iid:
                logger.warning("rule %s 的 iot 条件项缺 did / iid, 不触发", rule.id)
                return None
            if op not in SUPPORTED_OPS:
                logger.warning("rule %s 的 iot 条件项不支持 op=%s, 不触发", rule.id, op)
                return None
            if "value" not in spec:
                logger.warning("rule %s 的 iot 条件项没写 value, 不触发", rule.id)
                return None
            return IotRef(rule_id=rule.id, did=did, iid=iid, op=op, value=spec["value"])
    return None


def value_family(value: Any) -> str | None:
    """值属于哪一族。认不出来（含元组）返回 None —— 本版没有需要比较序列的条件项。

    ``bool`` 单独一族：它是 ``int`` 的子类，``True == 1`` 为真 —— 不分开的话「开关
    属性配了一个数值阈值」这种配置错误会被静默当成合法比较。

    ``int`` 与 ``float`` 同族不再细分：云端对 spec 标 float 的属性会返回 int。
    """
    if isinstance(value, bool):
        return _BOOL
    if isinstance(value, (int, float)):
        return _NUMBER
    if isinstance(value, str):
        return _STR
    return None


def compare(current: Any, op: str, expected: Any) -> bool:
    """按谓词求值。类型不兼容抛 ``EvalFailed``。

    **比较之前先查类型兼容，不能靠「异类型会抛 TypeError」兜底** —— 那句话只对大小
    比较成立。Python 里 ``"on" == 1`` 返回 False、``"on" != 1`` 返回 True，都不抛，
    所以脏数据下 ``ne`` 会被静默判成真，产生一次凭空的进入边沿。

    比较本身仍然包在 try 里：类型检查按 spec 声明的 format 做，而容器里的值来自
    云端，可能与声明不一致。
    """
    func = SUPPORTED_OPS.get(op)
    if func is None:
        raise EvalFailed(f"不支持的运算符 {op!r}")
    left, right = value_family(current), value_family(expected)
    if left is None or right is None or left != right:
        raise EvalFailed(
            f"类型不兼容: 当前值 {current!r} 与阈值 {expected!r} 不属于同一族"
        )
    if left is _STR and op in _ORDERING_OPS:
        raise EvalFailed(f"字符串不支持大小比较 (op={op})")
    try:
        return bool(func(current, expected))
    except TypeError as e:
        raise EvalFailed(f"比较失败: {e}") from e


# ── 源本体 ────────────────────────────────────────────────────────────

_PROP_PATTERN = "iot/device/*/prop/*"
_ONLINE_PATTERN = "iot/device/*/status/online"

# 重连之后等多久再去拉属性。等一等有三个作用：网络抖动连着触发的多次重连合并成一
# 次、避开刚重连时订阅对账抢同一条连接的在飞额度、让断连期间积压的推送先落地（拉回
# 来的是云端缓存里的旧值，落地晚的会被「last_reported 更晚就不写」挡掉）。
RECONNECT_PULL_DELAY_SECONDS = 15.0


class DiagnosticReason(str, Enum):
    """一条 iot 条件项此刻为什么是这个值。**是枚举不是日志字符串** —— 日志能排障，
    但答不了「现在有几条规则因为设备离线而瞎着」，而那是上线后第一个会被问到的。"""

    OK = "ok"
    DEVICE_OFFLINE = "device_offline"
    PATH_MISSING = "path_missing"
    EVAL_FAILED = "eval_failed"
    NOT_SEEDED = "not_seeded"


@dataclass
class _RuleDiagnostic:
    value: bool | None = None
    reason: DiagnosticReason = DiagnosticReason.NOT_SEEDED
    at: int = 0
    last_change: str = ""


class IotSource:
    """iot 条件项的求值方。与 ``RecordSource`` 同生态位。

    **订容器，不订推送。** 三条写入通道（启动对齐、MQTT 推送、上线补拉）都往容器写，
    订容器一处等于三条都覆盖；订推送事件的话开机时门是开的、规则永远不知道。

    **变更只当提醒，值一律现读。** 同步回调只把「哪几条 rule 该重算」记进待算集合，
    消费协程取出集合、对每条 rule 从容器读现值求值。少三处要单独想的东西：运行中
    seed 与在途 change 撞上、配置替换后在途的旧 change、一批写入被拆成多次求值。

    **源层只读容器，不写。** 条件项的当前值、求值结果、未就绪都留在源层与条件层。
    写回去会破坏容器的 owner 单向依赖，而启动对齐正是靠整棵子树替换工作的。
    """

    def __init__(
        self,
        store,
        feed: Callable[[str, bool], Awaitable[None]],
        mark_unknown: Callable[[str, str], None],
        iot_refs: Callable[[], Iterable[IotRef]],
        pull_props: Callable[[str, list[str]], Awaitable[None]] | None = None,
        reconnect_pull_delay: float = RECONNECT_PULL_DELAY_SECONDS,
    ) -> None:
        self._store = store
        self._feed = feed
        self._mark_unknown = mark_unknown
        self._iot_refs = iot_refs
        self._pull_props = pull_props
        self._reconnect_pull_delay = reconnect_pull_delay

        # (did, iid) → rule_id 列表。全量重建，不做增量：漏重建的最坏后果是漏触发
        # （查得出来），增量残留是拿已删规则的条目触发（查不出来）。
        self._index: dict[tuple[str, str], list[str]] = {}
        # did → rule_id 列表。上下线转真时按 did 找回该重算的那几条。
        self._by_device: dict[str, list[str]] = {}

        self._pending: set[str] = set()
        self._wake = asyncio.Event()
        self._consumer: asyncio.Task | None = None
        self._consumer_exit: str = ""
        self._unsubscribes: list[Callable[[], None]] = []

        self._diagnostics: dict[str, _RuleDiagnostic] = {}
        self._last_change: dict[str, str] = {}

        # 重连拉属性：只保留一个在飞任务，窗口内的再次触发只置补一轮标志。形状照
        # MiotProxy._spawn_subscription_sync —— 不另造一个防抖机制。
        self._pull_running = False
        self._pull_rerun_requested = False
        self._pull_task: asyncio.Task | None = None

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """订阅 → 全部 rule 进待算集合 → 起消费协程。

        **subscribe 排在 seed 之前。** 容器只投递订阅之后提交的变更，反过来的话两者
        之间落地的变更既不在这一轮 seed 里、也不在订阅里，要等属性下一次变化才被看见。
        """
        self.rebuild_index()
        self._unsubscribes.append(
            self._store.subscribe(_PROP_PATTERN, self._on_prop_change)
        )
        self._unsubscribes.append(
            self._store.subscribe(_ONLINE_PATTERN, self._on_online_change)
        )
        self.seed_all()
        self._consumer = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()
        for task in (self._consumer, self._pull_task):
            if task is not None and not task.done():
                task.cancel()
        self._consumer = None
        self._pull_task = None

    # ── 索引与 seed ─────────────────────────────────────────────

    def rebuild_index(self) -> None:
        """全量重建反查索引。

        索引里不放谓词：求值从条件项现读，索引只回答「谁关心这条路径」。放进去就是
        同一份配置的第二个副本，条件项改了而索引没重建时它会拿旧谓词算出一个说得通
        但错的结果。
        """
        index: dict[tuple[str, str], list[str]] = {}
        by_device: dict[str, list[str]] = {}
        for ref in self._iot_refs():
            index.setdefault((ref.did, ref.iid), []).append(ref.rule_id)
            by_device.setdefault(ref.did, []).append(ref.rule_id)
        self._index = index
        self._by_device = by_device

    def seed(self, rule_ids: Iterable[str]) -> None:
        """把一批 rule 放进待算集合 —— seed 是一个动作，不是一个时刻。

        与一次普通的变更走同一条路（求值都从容器现读），区别只在谁把 rule 放进集合。
        所以不需要 barrier、不需要「seed 与 live 谁先 feed」的仲裁；同一条 rule 被
        两边同时放进来也只会算一次。
        """
        self._pending.update(rule_ids)
        self._wake.set()

    def seed_all(self) -> None:
        self.seed(ref.rule_id for ref in self._iot_refs())

    def seed_rule(self, rule_id: str) -> None:
        """一条 rule 的配置变了。**先重建索引，再放进集合。**

        反过来的话，seed 之后、索引重建之前到达的变更查不到这条 rule，被丢掉。这个
        顺序靠的是集合去重，不是时序精确。
        """
        self.rebuild_index()
        self.seed([rule_id])

    def seed_rules(self, rule_ids: Iterable[str]) -> None:
        self.rebuild_index()
        self.seed(rule_ids)

    # ── 容器订阅回调（跑在 event loop 线程）────────────────────

    def _on_prop_change(self, change) -> None:
        did, iid = _split_prop_path(change.path)
        if did is None:
            return
        rule_ids = self._index.get((did, iid))
        if not rule_ids:
            return
        for rule_id in rule_ids:
            self._last_change[rule_id] = (
                f"{change.path} {change.old!r} → {change.new!r}"
            )
        # 唤醒用 Event.set()，在同步回调里直接调：回调跑在 loop 线程（_dispatch 就是
        # loop 上的回调），不需要 call_soon_threadsafe。
        self.seed(rule_ids)

    def _on_online_change(self, change) -> None:
        """设备从离线转回在线时，没有任何东西会触发重新求值。

        离线期间不会有属性推送；上线补拉只补容器里**缺失**的叶子，而属性叶子还在
        （离线前写的旧值），所以不补、没有 change。

        触发条件是「online 转为真」，**含 old is MISSING**：切作用域后 clear 删掉了
        online 叶子，重新对齐写回来时是「从不存在变成真」，只判 False→True 会漏掉这
        一整类。
        """
        if change.new is not True:
            return
        did = _split_online_path(change.path)
        if did is None:
            return
        rule_ids = self._by_device.get(did)
        if rule_ids:
            self.seed(rule_ids)

    # ── 消费协程 ────────────────────────────────────────────────

    async def _consume(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                # 取出集合是 swap 不是遍历它本身：feed 是 await，每次 await 的那一刻
                # 同步回调都可能往里再放东西，直接迭代活集合会 RuntimeError。
                batch, self._pending = self._pending, set()
                for rule_id in batch:
                    await self._evaluate_one(rule_id)
        except asyncio.CancelledError:
            self._consumer_exit = "cancelled"
            raise
        except BaseException as e:  # noqa: BLE001 - 死因要留得下来
            self._consumer_exit = f"{type(e).__name__}: {e}"
            logger.exception("iot 源的消费协程退出, 全部 iot 条件从此不再更新")
            raise

    async def _evaluate_one(self, rule_id: str) -> None:
        """单条 rule 的失败不许带走循环。

        取不到 rule 就跳过（批次拿到手之后它可能已经被删了）；求值与 feed 各自兜住
        异常 —— 抛出去打掉的是消费协程，也就是整条 iot 链，而且是静默的。
        """
        try:
            ref = self._ref_of(rule_id)
            if ref is None:
                return
            value, reason = self._evaluate(ref)
            self._record(rule_id, value, reason)
            if value is None:
                self._mark_unknown(rule_id, ref.did)
                return
            await self._feed(rule_id, value)
        except Exception:
            logger.exception("iot 源求值 rule %s 失败, 本次不喂", rule_id)

    def _ref_of(self, rule_id: str) -> IotRef | None:
        for ref in self._iot_refs():
            if ref.rule_id == rule_id:
                return ref
        return None

    def _evaluate(self, ref: IotRef) -> tuple[bool | None, DiagnosticReason]:
        """求值。``None`` = 未就绪，调用方置未知而不是喂假。

        在线判据现读，不靠订阅 —— online 和 prop 是两条独立的 change，把 prop 也划
        成现读之后整条求值路径只有一个取值来源。
        """
        online = self._store.get(f"iot/device/{ref.did}/status/online", MISSING)
        if online is not True:
            return None, DiagnosticReason.DEVICE_OFFLINE
        current = self._store.get(f"iot/device/{ref.did}/prop/{ref.iid}", MISSING)
        if current is MISSING:
            return None, DiagnosticReason.PATH_MISSING
        try:
            return compare(current, ref.op, ref.value), DiagnosticReason.OK
        except EvalFailed as e:
            logger.warning("rule %s 的 iot 条件项求值失败: %s", ref.rule_id, e)
            return None, DiagnosticReason.EVAL_FAILED

    def _record(
        self, rule_id: str, value: bool | None, reason: DiagnosticReason
    ) -> None:
        self._diagnostics[rule_id] = _RuleDiagnostic(
            value=value,
            reason=reason,
            at=now_ms(),
            last_change=self._last_change.get(rule_id, ""),
        )

    # ── 观测 ────────────────────────────────────────────────────

    def diagnostics(self) -> dict:
        """答三个问题：每条条件项现在是什么、为什么、以及**消费协程还活着吗**。

        存活项是源级的，不是 per-rule —— 协程一死，每条 rule 的 reason 都停在最后一
        次求值时的 ok，而那正是最需要报警的时刻。
        """
        consumer = self._consumer
        return {
            "consumer_alive": consumer is not None and not consumer.done(),
            "consumer_exit": self._consumer_exit,
            "pending": len(self._pending),
            "indexed_rules": sum(len(v) for v in self._index.values()),
            "rules": {
                rule_id: {
                    "value": d.value,
                    "reason": d.reason.value,
                    "at": d.at,
                    "last_change": d.last_change,
                }
                for rule_id, d in sorted(self._diagnostics.items())
            },
        }

    # ── MQTT 重连：拉属性，不是 re-seed ─────────────────────────

    def on_mips_connect(self) -> None:
        """重连回调。**re-seed 在这里是空转** —— 断连期间丢掉的推送也没写进容器，
        读一遍旧值喂一遍什么都没恢复。

        能拉回来是因为断连和设备离线不是一回事：我们的 MQTT 断了，设备到云端那段没
        断，云端缓存是新的。
        """
        if self._pull_props is None:
            return
        if self._pull_running:
            self._pull_rerun_requested = True
            return
        self._pull_running = True
        self._pull_rerun_requested = False
        self._pull_task = asyncio.create_task(self._pull_loop())

    async def _pull_loop(self) -> None:
        try:
            while True:
                self._pull_rerun_requested = False
                await asyncio.sleep(self._reconnect_pull_delay)
                await self._pull_referenced_props()
                if not self._pull_rerun_requested:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("iot 源重连后拉属性失败")
        finally:
            self._pull_running = False

    async def _pull_referenced_props(self) -> None:
        """范围是反查索引的正向：全部 iot 条件项引用的那几条属性，通常几条。

        不是整台设备 —— 整台重拉会用云端缓存里的旧值盖掉刚推来的新值，而容器没有时
        间戳可仲裁。写进去产生变更，走正常路径求值，不需要单独的 seed 动作。
        """
        by_device: dict[str, list[str]] = {}
        for did, iid in self._index:
            by_device.setdefault(did, []).append(iid)
        for did, iids in by_device.items():
            try:
                await self._pull_props(did, iids)
            except Exception:
                logger.exception("iot 源拉 %s 的属性失败", did)


def _split_prop_path(path: str) -> tuple[str | None, str]:
    """``iot/device/<did>/prop/<iid>`` → ``(did, iid)``。形状不对给 ``(None, "")``。"""
    parts = path.split("/")
    if len(parts) != 5 or parts[0] != "iot" or parts[3] != "prop":
        return None, ""
    return parts[2], parts[4]


def _split_online_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) != 5 or parts[0] != "iot" or parts[3] != "status":
        return None
    return parts[2]
