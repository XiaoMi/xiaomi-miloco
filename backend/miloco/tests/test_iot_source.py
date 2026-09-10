# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 源：订容器、现读求值、seed、重连拉属性。"""

from __future__ import annotations

import asyncio
import logging

import pytest
from miloco.rule.iot_source import DiagnosticReason, IotRef, IotSource
from miloco.state import StateStore


@pytest.fixture
async def store():
    s = StateStore()
    s.start()
    yield s
    s.stop()


class _Harness:
    """把源接在一个真容器上，记下它喂了什么、置了谁未知。"""

    def __init__(self, store, refs, pull=None):
        self.store = store
        self.refs = list(refs)
        self.fed: list[tuple[str, bool]] = []
        self.unknown: list[tuple[str, str]] = []
        self.pulls: list[tuple[str, list[str]]] = []
        self._pull_override = pull
        self.source = IotSource(
            store=store,
            feed=self._feed,
            mark_unknown=lambda rid, did: self.unknown.append((rid, did)),
            iot_refs=lambda: list(self.refs),
            pull_props=self._pull,
            reconnect_pull_delay=0.01,
        )

    async def _feed(self, rule_id: str, value: bool) -> None:
        self.fed.append((rule_id, value))

    async def _pull(self, did: str, iids: list[str]) -> None:
        self.pulls.append((did, sorted(iids)))
        if self._pull_override is not None:
            await self._pull_override(did, iids)

    async def settle(self):
        """等消费协程把待算集合跑空并稳定住。

        容器的投递是 call_soon_threadsafe 排的，「集合此刻是空的」不等于没有在途的
        变更 —— 要连着几轮都空才算完。
        """
        quiet = 0
        for _ in range(200):
            await asyncio.sleep(0)
            quiet = quiet + 1 if not self.source._pending else 0
            if quiet >= 5:
                return
        raise AssertionError("消费协程没把集合跑空")


def _ref(rule_id="r1", did="d1", iid="5.1", op="eq", value=1) -> IotRef:
    return IotRef(rule_id=rule_id, did=did, iid=iid, op=op, value=value)


def _online(store, did="d1", value=True):
    store.set(f"iot/device/{did}/status/online", value, source="test")


def _prop(store, value, did="d1", iid="5.1"):
    store.set(f"iot/device/{did}/prop/{iid}", value, source="test")


# ── 启动 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_seeds_the_current_value(store):
    """规则不能等属性下一次变化才第一次知道自己是真是假。

    「空调开着就怎样」这类规则在没有 seed 的情况下永远不触发 —— 空调一直开着、
    没有变化。
    """
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])

    h.source.start()
    await h.settle()

    assert h.fed == [("r1", True)]


@pytest.mark.asyncio
async def test_subscribe_happens_before_the_startup_seed(store):
    """两者之间落地的变更既不在这一轮 seed 里、也不在订阅里的话，要等属性下一次
    变化才被看见。

    **顺序要在 `start()` 内部观测。** 在它返回之后再写属性的话，两种顺序下订阅都已
    经装好了，那条变更必然被接住 —— 断言分不开对错。
    """
    _online(store)
    h = _Harness(store, [_ref()])
    order: list[str] = []
    real_subscribe = store.subscribe

    def spy_subscribe(pattern, callback):
        order.append("subscribe")
        return real_subscribe(pattern, callback)

    store.subscribe = spy_subscribe  # ty:ignore[invalid-assignment]
    real_seed_all = h.source.seed_all

    def spy_seed_all():
        order.append("seed")
        real_seed_all()

    h.source.seed_all = spy_seed_all  # ty:ignore[invalid-assignment]

    h.source.start()

    assert order.index("subscribe") < order.index("seed")


# ── 现读，不读 change.new ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluation_reads_the_container_not_the_change(store):
    """投一条 new=真 的变更，但在消费前把容器改成假 —— 喂进去的应当是假。

    用 change.new 的话这条会喂真。
    """
    _online(store)
    _prop(store, 2)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    h.fed.clear()

    _prop(store, 1)
    _prop(store, 2)
    await h.settle()

    # 恰好一次，值是容器里的现值。用 change.new 的话这两条变更各算一次，会先喂一次
    # 真 —— 断「最后一次是假」在两种实现下都成立，分不开对错。
    assert h.fed == [("r1", False)]


@pytest.mark.asyncio
async def test_one_batch_write_is_evaluated_once(store):
    """一次写整台设备的 prop 子树，该 rule 只求值一次。"""
    _online(store)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    h.fed.clear()

    store.set("iot/device/d1/prop", {"5.1": 1, "4.1": 80}, source="test")
    await h.settle()

    assert h.fed == [("r1", True)]


# ── 未就绪 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_offline_device_is_marked_unknown_not_false(store):
    """离线不能喂假 —— 假会驱动一次凭空的退出边沿。"""
    _online(store, value=False)
    _prop(store, 1)
    h = _Harness(store, [_ref()])

    h.source.start()
    await h.settle()

    assert h.fed == []
    assert h.unknown == [("r1", "d1")]
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == (
        DiagnosticReason.DEVICE_OFFLINE.value
    )


@pytest.mark.asyncio
async def test_missing_leaf_is_marked_unknown(store):
    _online(store)
    h = _Harness(store, [_ref()])

    h.source.start()
    await h.settle()

    assert h.fed == []
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == (
        DiagnosticReason.PATH_MISSING.value
    )


@pytest.mark.asyncio
async def test_type_mismatch_is_marked_unknown_not_true(store):
    """脏数据下 ne 会被静默判成真 —— "on" != 1 在 Python 里返回 True 且不抛。

    **这条必须用 ne**：eq 在有无类型检查时都返回假，永绿。
    """
    _online(store)
    _prop(store, "on")
    h = _Harness(store, [_ref(op="ne", value=1)])

    h.source.start()
    await h.settle()

    assert h.fed == []
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == (
        DiagnosticReason.EVAL_FAILED.value
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported, expected", [(1, True), (0, False)])
async def test_a_bool_property_reported_as_a_number_still_evaluates(
    store, reported, expected
):
    """设备把布尔属性报成 0 / 1 —— 同一个值的另一种上报形态，不是脏数据。

    判成不兼容的话这条规则永久停在 eval_failed、一次都不触发，而住户看到的是一条
    描述完全正常的规则。

    **两个取值都要测**：只测 1 的话「归一成 bool」被改成「恒为真」照样通过。
    """
    _online(store)
    _prop(store, reported)
    h = _Harness(store, [_ref(op="eq", value=True)])

    h.source.start()
    await h.settle()

    assert h.fed == [("r1", expected)]
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == DiagnosticReason.OK.value


@pytest.mark.asyncio
async def test_deleting_the_container_does_not_feed_false(store):
    """切家庭那一刻全屋条件项同时收到删除。喂假 = 一批退出边沿。"""
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    h.fed.clear()
    h.unknown.clear()

    store.clear(source="test")
    await h.settle()

    assert h.fed == []
    assert h.unknown == [("r1", "d1")]


# ── 上下线恢复 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_going_offline_triggers_a_re_evaluation(store):
    """设备转离线时没有别的东西会来告诉规则「这台设备现在瞎了」。

    离线期间不会有属性推送，所以只订阅「转真」的话，条件会一直停在离线前那个值、
    还报着 ok —— §4.7 承诺的「离线后不再拿过期的 True 拦住一次正常进入」就不成立。
    """
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    assert h.fed == [("r1", True)]

    _online(store, value=False)
    await h.settle()

    assert h.unknown == [("r1", "d1")]
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == (
        DiagnosticReason.DEVICE_OFFLINE.value
    )


@pytest.mark.asyncio
async def test_a_deleted_online_leaf_also_triggers_a_re_evaluation(store):
    """切作用域时 clear 删掉 online 叶子，也是「这台设备现在瞎了」。"""
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    h.unknown.clear()

    store.delete("iot/device/d1/status/online", source="test")
    await h.settle()

    assert h.unknown == [("r1", "d1")]


@pytest.mark.asyncio
async def test_coming_back_online_triggers_a_re_evaluation(store):
    """离线期间没有属性推送，上线补拉也只补缺失的叶子 —— 没有任何东西会触发重算。"""
    _online(store, value=False)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    assert h.fed == []

    _online(store, value=True)
    await h.settle()

    assert h.fed == [("r1", True)]


@pytest.mark.asyncio
async def test_online_appearing_from_nothing_also_triggers(store):
    """切作用域后 clear 删掉了 online 叶子，重新对齐写回来是「从不存在变成真」。

    只判 False→True 会漏掉这一整类。
    """
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    h.fed.clear()

    _online(store, value=True)
    await h.settle()

    assert h.fed == [("r1", True)]


# ── 运行中 seed ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seeding_a_new_rule_uses_the_value_already_in_the_tree(store):
    """运行中建一条「当前属性已经为真」的规则：容器里不会有新变更。

    只重建索引不 seed 的话它永远停在未就绪。
    """
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [])
    h.source.start()
    await h.settle()

    h.refs.append(_ref())
    h.source.seed_rule("r1")
    await h.settle()

    assert h.fed == [("r1", True)]


@pytest.mark.asyncio
async def test_seeding_a_rule_also_makes_future_changes_wake_it(store):
    """`seed_rule` 除了算一次现值，还要重建索引 —— 索引让**未来的**变更找得到它。

    只 seed 不重建索引的话，这条 rule 算完这一次就再也不会被唤醒了。断的是第二次
    写入（seed 之后的那次）有没有把它算到：只断第一次的话，seed 自己就够了，索引
    重建做没做都是绿的。

    两步的先后在这里不可观测（`seed_rule` 全同步，中间插不进东西），所以不断顺序。
    """
    _online(store)
    _prop(store, 2)
    h = _Harness(store, [])
    h.source.start()
    await h.settle()

    h.refs.append(_ref())
    h.source.seed_rule("r1")
    await h.settle()
    assert h.fed == [("r1", False)]

    _prop(store, 1)
    await h.settle()

    assert h.fed == [("r1", False), ("r1", True)]


# ── 消费协程的韧性 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_change_arriving_during_a_feed_does_not_kill_the_loop(store):
    """feed 是 await，每次 await 的那一刻同步回调都可能往待算集合里再放东西。

    直接迭代活集合会 RuntimeError: Set changed size during iteration。
    """
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref(), _ref("r2", iid="4.1", value=80)])
    _prop(store, 80, iid="4.1")

    injected = False

    async def feed(rule_id, value):
        nonlocal injected
        h.fed.append((rule_id, value))
        if not injected:
            injected = True
            _prop(store, 2)

    h.source._feed = feed
    h.source.start()
    await h.settle()

    # feed 途中投进去的那条变更要在下一批被算到，而不是把这一批的迭代打断。
    assert h.source.diagnostics()["consumer_alive"] is True
    assert ("r1", False) in h.fed


@pytest.mark.asyncio
async def test_one_rule_failing_does_not_stop_the_others(store):
    """断「后一条仍被 feed」，不断「没有异常逃出来」—— try 包在循环外面时后者仍绿。"""
    _online(store)
    _prop(store, 1)
    _prop(store, 80, iid="4.1")
    h = _Harness(store, [_ref(), _ref("r2", iid="4.1", value=80)])

    async def feed(rule_id, value):
        if rule_id == "r1":
            raise RuntimeError("boom")
        h.fed.append((rule_id, value))

    h.source._feed = feed
    h.source.start()
    await h.settle()

    assert h.fed == [("r2", True)]
    assert h.source.diagnostics()["consumer_alive"] is True


@pytest.mark.asyncio
async def test_a_rule_deleted_mid_batch_is_skipped(store, caplog):
    """批次拿到手之后那条 rule 可能已经被删了 —— 那是正常路径，不是异常。

    **断的是没有异常留痕。** `_evaluate_one` 整个包在 except 里，所以「干净跳过」和
    「抛了被吞掉」在 fed 和 consumer_alive 上给同样的结果，只有日志分得开。
    """
    _online(store)
    _prop(store, 1)
    _prop(store, 80, iid="4.1")
    h = _Harness(store, [_ref(), _ref("r2", iid="4.1", value=80)])
    h.source.start()
    h.refs = [r for r in h.refs if r.rule_id != "r1"]
    with caplog.at_level(logging.ERROR, logger="miloco.rule.iot_source"):
        await h.settle()

    assert h.fed == [("r2", True)]
    assert h.source.diagnostics()["consumer_alive"] is True
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_a_dead_consumer_shows_up_in_diagnostics(store):
    """协程一死，每条 rule 的 reason 都停在最后一次求值时的 ok —— 而那正是最需要
    报警的时刻。所以断的是源级的存活项，不是 per-rule 的 reason。"""
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    assert h.source.diagnostics()["rules"]["r1"]["reason"] == "ok"

    h.source._consumer.cancel()
    await asyncio.sleep(0)

    diag = h.source.diagnostics()
    assert diag["consumer_alive"] is False
    assert diag["rules"]["r1"]["reason"] == "ok"


# ── MQTT 重连 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_pulls_only_the_referenced_props(store):
    """断这条路径上有拉取、且范围只有 iot 条件项引用的那几条。

    只 re-seed 的话这条会红，而「条件恢复了」那种断言在两种实现下都可能绿 ——
    容器值没变时 seed 也能"恢复"。
    """
    _online(store)
    h = _Harness(store, [_ref(), _ref("r2", iid="4.1", value=80)])
    h.source.start()

    h.source.on_mips_connect()
    await asyncio.sleep(0.05)

    assert h.pulls == [("d1", ["4.1", "5.1"])]


@pytest.mark.asyncio
async def test_repeated_reconnects_inside_the_window_pull_once(store):
    """网络抖动会连着触发多次重连，每次都拉就是对同一批属性重复打云端。

    **断拉取次数，不断「拉到了」** —— 后者在拉三次时同样绿。
    """
    _online(store)
    h = _Harness(store, [_ref()])
    h.source.start()

    h.source.on_mips_connect()
    h.source.on_mips_connect()
    h.source.on_mips_connect()
    await asyncio.sleep(0.05)

    assert len(h.pulls) == 1


@pytest.mark.asyncio
async def test_the_reconnect_pull_is_delayed(store):
    """不能一收到重连回调就拉：此刻订阅对账正在跑，抢同一条连接的在飞额度。"""
    _online(store)
    h = _Harness(store, [_ref()])
    h.source.reconnect_pull_delay = 10
    h.source._reconnect_pull_delay = 10
    h.source.start()

    h.source.on_mips_connect()
    await asyncio.sleep(0.02)

    assert h.pulls == []


@pytest.mark.asyncio
async def test_diagnostics_summarises_by_reason(store):
    """逐条看答不了「现在有几条规则因为设备离线而瞎着」，而那是上线后第一个会被
    问到的。四种原因分开记，不合并成一句「未就绪」—— 排障方向完全不同。"""
    _online(store, did="d1")
    _prop(store, "on", did="d1")
    _online(store, did="d2", value=False)
    h = _Harness(
        store,
        [_ref("r1", did="d1", op="ne", value=1), _ref("r2", did="d2")],
    )

    h.source.start()
    await h.settle()

    assert h.source.diagnostics()["by_reason"] == {
        "eval_failed": 1,
        "device_offline": 1,
    }


@pytest.mark.asyncio
async def test_diagnostics_drop_rules_that_are_gone(store):
    """排障时看到一条已经不存在的规则会指错方向，按原因汇总的计数也被它污染。"""
    _online(store)
    _prop(store, 1)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.settle()
    assert "r1" in h.source.diagnostics()["rules"]

    h.refs.clear()
    h.source.rebuild_index()

    diag = h.source.diagnostics()
    assert diag["rules"] == {}
    assert diag["by_reason"] == {}


@pytest.mark.asyncio
async def test_a_reconnect_after_stop_does_not_pull(store):
    """关闭窗口里到达的重连: 15 秒后它会对着正在拆的 proxy 读云端、往已 stop 的容器写。"""
    _online(store)
    h = _Harness(store, [_ref()])
    h.source.start()
    await h.source.stop()

    h.source.on_mips_connect()
    await asyncio.sleep(0.05)

    assert h.pulls == []
