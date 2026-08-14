# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""分层 KV 状态容器。

多个异构来源（iot / omni / tracker）往里写，消费方按路径模式订阅变化、按路径模式取
一致快照。只装状态（能问出「现在是什么」），事件走 `dispatch/dispatcher.py`。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from miloco.state.path import (
    MAX_DEPTH,
    match_pattern,
    parse_pattern,
    split_path,
    validate_segment,
)
from miloco.state.types import MISSING, Change, Entry, Node, StateValue
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

MAX_LEAVES = 100_000
MAX_CASCADE_DEPTH = 10
PENDING_WARN_THRESHOLD = 1000

# 回调里再写容器时，那次 set 与触发它的 _dispatch 不在同一个调用栈，深度只能显式传递。
# 用 ContextVar 而不是实例属性：set 可以从任意线程调用，非 loop 线程读到默认值 0，
# 正是想要的语义——外部来源的写入不算级联。
_cascade_depth: ContextVar[int] = ContextVar("miloco_state_cascade_depth", default=0)

Subscriber = tuple[tuple[str, ...], Callable[[Change], None]]


@dataclass(slots=True)
class _Batch:
    """一次写入产生的变更。

    删除必须排在新增和修改之前：叶子换成子树时，订阅方若先加了子叶子、再收到父路径的删除，
    会把刚加的一起抹掉。
    """

    source: str
    now: int
    deletes: list[Change] = field(default_factory=list)
    upserts: list[Change] = field(default_factory=list)

    def changes(self) -> list[Change]:
        return self.deletes + self.upserts


def _same(left: Any, right: Any) -> bool:
    """带类型且逐元素的判等。

    裸 `==` 会把 `True` 和 `1` 判成同值，于是不产事件，但存进去的已经是新值——值变了却没有
    任何事件。设备把 bool 属性时而报数字时而报布尔很常见。
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same(x, y) for x, y in zip(left, right))
    return left == right


def _normalize_scalar(value: Any) -> StateValue:
    # 用精确类型而不是 isinstance：numpy 的 float64 是 float 的子类，收下来之后判等那步
    # (`_same` 比精确类型) 会认定它和原生 float 永远不相等，同一个数值来回写就产一串假事件。
    # 感知链路全是 numpy，宁可在入口报错让写入方显式转换。
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        # NaN != NaN，一个报 NaN 的传感器每写一次就产一次变更事件，稳定输出会变成事件风暴
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"值不能是 NaN 或 ±Inf：{value!r}")
        return value
    raise TypeError(f"值必须是 JSON 标量，收到 {type(value).__name__}")


def _normalize(value: Any, depth: int) -> Any:
    """校验并规范化写入值：dict 保持子树，list 转 tuple，其余必须是 JSON 标量。

    `depth` 是 `value` 所在的层数。校验全部集中在这里，`_build` 之后不会再抛异常。
    """
    if isinstance(value, dict):
        if depth + 1 > MAX_DEPTH:
            raise ValueError(f"展开后深度超过 {MAX_DEPTH} 层")
        normalized: dict[str, Any] = {}
        for key, sub in value.items():
            validate_segment(key)
            normalized[key] = _normalize(sub, depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        # 元素只许是标量：`[{"r": 255}]` 能绕过 dict 展开、把一块结构塞进叶子
        return tuple(_normalize_scalar(element) for element in value)
    return _normalize_scalar(value)


def _join(segments: tuple[str, ...]) -> str:
    return "/".join(segments)


def _deleted(segments: tuple[str, ...], entry: Entry, batch: _Batch) -> Change:
    return Change(
        path=_join(segments),
        old=entry.value,
        new=MISSING,
        source=batch.source,
        at=batch.now,
    )


def _collect_deletes(node: Node, segments: tuple[str, ...], batch: _Batch) -> None:
    """为 node 下的每个叶子产一个删除事件。"""
    if isinstance(node, Entry):
        batch.deletes.append(_deleted(segments, node, batch))
        return
    for key, child in node.items():
        _collect_deletes(child, segments + (key,), batch)


def _build(
    value: Any, old: Node | None, segments: tuple[str, ...], batch: _Batch
) -> Node:
    """构造替换 old 之后的新节点，同时记录变更。只读 old，不修改它。"""
    if isinstance(value, dict):
        if isinstance(old, Entry):
            batch.deletes.append(_deleted(segments, old, batch))
            old = None
        old_children: dict[str, Node] = old if isinstance(old, dict) else {}
        new_node: dict[str, Node] = {}
        for key, sub in value.items():
            child = _build(sub, old_children.get(key), segments + (key,), batch)
            # 展开出零个叶子就不建这个节点，否则会留下一个谁都看不见、也不占叶子名额的空节点
            if isinstance(child, dict) and not child:
                continue
            new_node[key] = child
        # set 恒替换：原子树下未被新值覆盖的叶子会被删除
        for key, child in old_children.items():
            if key not in value:
                _collect_deletes(child, segments + (key,), batch)
        return new_node

    if isinstance(old, dict):
        _collect_deletes(old, segments, batch)
        old = None
    if old is None:
        batch.upserts.append(
            Change(_join(segments), MISSING, value, batch.source, batch.now)
        )
        return Entry(value, batch.now, batch.now, batch.source)
    if _same(old.value, value):
        return Entry(value, old.last_changed, batch.now, batch.source)
    batch.upserts.append(
        Change(_join(segments), old.value, value, batch.source, batch.now)
    )
    return Entry(value, batch.now, batch.now, batch.source)


def _prune(parents: list[dict[str, Node]], segments: tuple[str, ...]) -> None:
    """摘掉这条路径上因删除而变空的中间节点。

    留着的话，拿无界标识符当路径段的来源会让空节点无限堆积——它们不算叶子，绕开了叶子总数
    这道闸，而 `get` 和 `snapshot` 又都看不见，撑爆内存也查不出来源。
    """
    for index in range(len(parents) - 1, 0, -1):
        if parents[index]:
            return
        del parents[index - 1][segments[index - 1]]


def _plain(node: dict[str, Node]) -> dict[str, Any]:
    """剥掉元数据的嵌套 dict。"""
    plain: dict[str, Any] = {}
    for key, child in node.items():
        plain[key] = child.value if isinstance(child, Entry) else _plain(child)
    return plain


def _leaf_view(entry: Entry, with_meta: bool) -> Any:
    if not with_meta:
        return entry.value
    return {
        "value": entry.value,
        "last_changed": entry.last_changed,
        "last_reported": entry.last_reported,
        "source": entry.source,
    }


def _copy_leaves(node: Node, key: str, out: dict, with_meta: bool) -> None:
    """把 node 下的所有叶子按原结构写进 out[key]。"""
    if isinstance(node, Entry):
        out[key] = _leaf_view(node, with_meta)
        return
    sub: dict[str, Any] = {}
    for child_key, child in node.items():
        _copy_leaves(child, child_key, sub, with_meta)
    out[key] = sub


def _collect_matches(
    node: dict[str, Node],
    pattern: tuple[str, ...],
    index: int,
    out: dict,
    with_meta: bool,
) -> None:
    """把 node 下匹配 pattern[index:] 的叶子按原路径写进 out。

    不匹配的顶层段直接剪掉，不遍历。
    """
    segment = pattern[index]
    if segment == "**":
        for key, child in node.items():
            _copy_leaves(child, key, out, with_meta)
        return

    if segment == "*":
        children = list(node.items())
    elif segment in node:
        children = [(segment, node[segment])]
    else:
        return

    is_last = index == len(pattern) - 1
    for key, child in children:
        if is_last:
            if isinstance(child, Entry):
                out[key] = _leaf_view(child, with_meta)
        elif isinstance(child, dict):
            sub: dict[str, Any] = {}
            _collect_matches(child, pattern, index + 1, sub, with_meta)
            if sub:
                out[key] = sub


class StateStore:
    """分层 KV 状态容器。线程安全，任意线程可写，回调统一投到主 event loop。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._root: dict[str, Node] = {}
        self._subs: tuple[Subscriber, ...] = ()
        self._loop: asyncio.AbstractEventLoop | None = None
        # 每次真实的启停切换加一，幂等调用不加。停止前排队的 _dispatch 靠它作废。
        self._generation = 0
        self._leaf_count = 0
        self._pending = 0
        self._dropped = 0
        self._discarded = 0
        self._warned_not_started = False
        self._warned_leaf_limit = False

    # ---- 写 ----

    def set(self, path: str, value: Any, *, source: str) -> None:
        """写入。恒为替换：原子树下未被新值覆盖的叶子会被删除，想局部更新就写更深的路径。"""
        depth = self._next_cascade_depth("set", path)
        if depth is None:
            return
        with self._lock:
            # 取时间必须在锁内：放锁外时先取到时间的线程可能后提交，树里最终那个值会带上比
            # 前一个值更旧的时间戳，按时间戳判新鲜度的消费方会把最新值当过期的丢掉
            self._enqueue(self._apply(path, value, source, now_ms()), depth)

    def delete(self, path: str, *, source: str) -> None:
        """删除路径下的所有叶子。路径不存在时是 no-op：不抛异常、零事件、树不动。"""
        depth = self._next_cascade_depth("delete", path)
        if depth is None:
            return
        with self._lock:
            self._enqueue(self._remove(path, source, now_ms()), depth)

    def _next_cascade_depth(self, operation: str, path: str) -> int | None:
        depth = _cascade_depth.get() + 1
        if depth > MAX_CASCADE_DEPTH:
            logger.error(
                "cascade depth %s exceeds limit %s; rejecting %s %s",
                depth,
                MAX_CASCADE_DEPTH,
                operation,
                path,
            )
            return None
        return depth

    def _commit(self, path: str, value: Any, *, source: str) -> list[Change]:
        """写入并返回本次变更，不投递。数据结构层单测的入口。"""
        with self._lock:
            return self._apply(path, value, source, now_ms())

    def _commit_delete(self, path: str, *, source: str) -> list[Change]:
        """删除并返回本次变更，不投递。数据结构层单测的入口。"""
        with self._lock:
            return self._remove(path, source, now_ms())

    def _apply(self, path: str, value: Any, source: str, now: int) -> list[Change]:
        """校验 → diff → 提交。必须在锁内调用。

        校验全部前置在 `_normalize`，构造新节点这一段不会再抛异常；最后一步的挂载是单次
        赋值，不可能改到一半。所以校验失败时旧树一个字节没动。
        """
        segments = split_path(path)
        normalized = _normalize(value, len(segments))
        batch = _Batch(source=source, now=now)
        old_node = self._locate_for_write(segments, batch)
        new_node = _build(normalized, old_node, segments, batch)

        added = sum(1 for change in batch.upserts if change.old is MISSING)
        total = self._leaf_count + added - len(batch.deletes)
        if total > MAX_LEAVES:
            # 只报一次：撑爆树的来源通常是高频的，而这条日志在锁内，按写入频率打会把所有
            # 写入方一起堵在锁上。第一条已经够定位，后面的没有新信息
            if not self._warned_leaf_limit:
                self._warned_leaf_limit = True
                logger.error(
                    "leaf count would reach %s over limit %s; rejecting set %s",
                    total,
                    MAX_LEAVES,
                    path,
                )
            return []

        self._attach(segments, new_node)
        self._leaf_count = total
        return batch.changes()

    def _locate_for_write(
        self, segments: tuple[str, ...], batch: _Batch
    ) -> Node | None:
        """只读走到目标节点。路径中途撞上叶子时，那个叶子会被换成子树，先产它的删除事件。"""
        node: Node | None = self._root
        for depth, segment in enumerate(segments):
            if isinstance(node, Entry):
                batch.deletes.append(_deleted(segments[:depth], node, batch))
                return None
            if node is None:
                return None
            node = node.get(segment)
        return node

    def _attach(self, segments: tuple[str, ...], node: Node) -> None:
        """挂载新节点。这是整次写入唯一修改旧树的动作。"""
        parents = [self._root]
        for segment in segments[:-1]:
            child = parents[-1].get(segment)
            if not isinstance(child, dict):
                child = {}
                parents[-1][segment] = child
            parents.append(child)
        if isinstance(node, dict) and not node:
            # 空 dict 展开出零个叶子，等于把这个 key 从父节点摘掉
            parents[-1].pop(segments[-1], None)
            _prune(parents, segments)
        else:
            parents[-1][segments[-1]] = node

    def _remove(self, path: str, source: str, now: int) -> list[Change]:
        segments = split_path(path)
        parents = [self._root]
        for segment in segments[:-1]:
            child = parents[-1].get(segment)
            if not isinstance(child, dict):
                return []
            parents.append(child)
        node = parents[-1].get(segments[-1])
        if node is None:
            return []
        batch = _Batch(source=source, now=now)
        _collect_deletes(node, segments, batch)
        del parents[-1][segments[-1]]
        _prune(parents, segments)
        self._leaf_count -= len(batch.deletes)
        return batch.changes()

    # ---- 读 ----

    def get(self, path: str, default: Any = MISSING) -> Any:
        """叶子返回值，子树返回剥掉元数据的嵌套 dict。

        缺省返回 `MISSING` 而不是 `None`——`None` 是合法值，用它表示「不存在」会让调用方
        分不出「这个设备没出错」和「这个设备根本没跑过」。
        """
        segments = split_path(path)
        with self._lock:
            node = self._locate(segments)
            if isinstance(node, Entry):
                return node.value
            if isinstance(node, dict):
                return _plain(node)
        return default

    def get_entry(self, path: str) -> Entry | None:
        """仅对叶子有意义。中间节点和不存在的路径都返回 `None`。"""
        segments = split_path(path)
        with self._lock:
            node = self._locate(segments)
        return node if isinstance(node, Entry) else None

    def snapshot(self, pattern: str, *, with_meta: bool = False) -> dict:
        """按 pattern 取一致快照，保留完整路径。一个都没匹配上时返回 `{}`。

        全程在锁内，成本是 O(匹配到的叶子数)。热路径上别用 `**` 取全量——期间所有写入方
        都阻塞在锁上。
        """
        segments = parse_pattern(pattern)
        result: dict[str, Any] = {}
        with self._lock:
            _collect_matches(self._root, segments, 0, result, with_meta)
        return result

    def _locate(self, segments: tuple[str, ...]) -> Node | None:
        node: Node = self._root
        for segment in segments:
            if not isinstance(node, dict):
                return None
            child = node.get(segment)
            if child is None:
                return None
            node = child
        return node

    # ---- 订阅 ----

    def subscribe(
        self, pattern: str, callback: Callable[[Change], None]
    ) -> Callable[[], None]:
        """订阅匹配 pattern 的叶子变更，返回退订函数。可以在 `start()` 之前调用。"""
        subscriber: Subscriber = (parse_pattern(pattern), callback)
        with self._lock:
            self._subs = self._subs + (subscriber,)

        def unsubscribe() -> None:
            with self._lock:
                self._subs = tuple(
                    item for item in self._subs if item is not subscriber
                )

        return unsubscribe

    # ---- 生命周期 ----

    def start(self) -> None:
        """必须在主 event loop 线程调用——从别的线程调会抛 `RuntimeError`。"""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None:
                logger.warning("StateStore already started")
                return
            self._loop = loop
            self._generation += 1

    def stop(self) -> None:
        """停止投递。已排队的 `_dispatch` 靠 generation 作废，不必逐个取消。"""
        with self._lock:
            if self._loop is None:
                logger.warning("StateStore already stopped")
                return
            self._loop = None
            self._generation += 1

    # ---- 投递 ----

    def _enqueue(self, changes: list[Change], depth: int) -> None:
        """排队投递。必须在锁内调用。

        排队与提交要是同一个原子步骤：挪到锁外时两个线程写同一路径会让投递顺序与树的提交
        顺序相反，订阅方的镜像状态就与容器对不上了。
        """
        if not changes:
            return
        if self._loop is None:
            self._drop(changes)
            return
        try:
            self._loop.call_soon_threadsafe(
                self._dispatch, changes, self._generation, depth
            )
        except RuntimeError:
            self._drop(changes)
            return
        previous = self._pending
        self._pending += len(changes)
        # 只在越过水位那一次报，不然积压期间每次写入都打一条，日志自己变成新的负担
        if previous <= PENDING_WARN_THRESHOLD < self._pending:
            logger.warning(
                "pending state changes %s exceed %s; a subscriber is likely blocking the loop",
                self._pending,
                PENDING_WARN_THRESHOLD,
            )

    def _drop(self, changes: list[Change]) -> None:
        self._dropped += len(changes)
        if not self._warned_not_started:
            self._warned_not_started = True
            logger.warning("StateStore is not started; dropping state changes")

    def _dispatch(self, changes: list[Change], generation: int, depth: int) -> None:
        with self._lock:
            self._pending -= len(changes)
            if generation != self._generation:
                self._discarded += len(changes)
                return
            subs = self._subs

        paths = [tuple(change.path.split("/")) for change in changes]
        token = _cascade_depth.set(depth)
        try:
            for pattern, callback in subs:
                for segments, change in zip(paths, changes):
                    if not match_pattern(pattern, segments):
                        continue
                    try:
                        callback(change)
                    except Exception:
                        logger.exception("state subscriber failed on %s", change.path)
        finally:
            _cascade_depth.reset(token)
