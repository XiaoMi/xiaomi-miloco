# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 源：订容器、现读求值、seed、重连拉属性。"""

from __future__ import annotations

import asyncio

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
    变化才被看见。"""
    _online(store)
    h = _Harness(store, [_ref()])
    h.source.start()
    _prop(store, 1)

    await h.settle()

    assert ("r1", True) in h.fed


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
async def test_seeding_rebuilds_the_index_first(store):
    """先重建索引再 seed：反过来的话两步之间到达的变更查不到这条 rule，被丢掉。"""
    _online(store)
    h = _Harness(store, [])
    h.source.start()
    await h.settle()

    h.refs.append(_ref())
    h.source.rebuild_index()
    _prop(store, 1)
    h.source.seed(["r1"])
    await h.settle()

    assert ("r1", True) in h.fed


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
async def test_a_rule_deleted_mid_batch_is_skipped(store):
    """批次拿到手之后那条 rule 可能已经被删了。取不到就 KeyError 的话这条会红。"""
    _online(store)
    _prop(store, 1)
    _prop(store, 80, iid="4.1")
    h = _Harness(store, [_ref(), _ref("r2", iid="4.1", value=80)])
    h.source.start()
    h.refs = [r for r in h.refs if r.rule_id != "r1"]
    await h.settle()

    assert h.fed == [("r2", True)]
    assert h.source.diagnostics()["consumer_alive"] is True


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
