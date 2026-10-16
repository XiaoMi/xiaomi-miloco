"""trace.py 单测：buffer 累积、reduce_meta、debug 门槛、daily cap 滚动淘汰、
僵尸 turn 清理、持久会话连续记账（本文件核心不变式）。"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest
from miloco_plugin_pkg import trace as tr


@pytest.fixture(autouse=True)
def _clean_state(tmp_path: Path, monkeypatch):
    """每个测试都用独立 miloco_home + 清空 _turns/_trace_links。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_TRACE_DEBUG", raising=False)
    with tr._lock:
        tr._turns.clear()
        tr._trace_links.clear()
    yield
    with tr._lock:
        tr._turns.clear()
        tr._trace_links.clear()


# ── 注册 ──────────────────────────────────────────────────────────────────

def test_register_trace_hooks_returns_count():
    """register_trace_hooks 用 mock ctx 返回成功数。"""
    class _MockCtx:
        def __init__(self):
            self.calls = []
        def register_hook(self, name, fn):
            self.calls.append((name, fn))

    ctx = _MockCtx()
    n = tr.register_trace_hooks(ctx)
    assert n == 6
    assert len(ctx.calls) == 6
    names = [c[0] for c in ctx.calls]
    assert "pre_llm_call" in names
    assert "post_llm_call" in names
    assert "pre_tool_call" in names
    assert "post_tool_call" in names
    assert "on_session_start" in names
    assert "on_session_end" in names


def test_register_trace_hooks_partial_failure():
    """单个 register 失败不影响其他。"""
    class _MockCtx:
        def __init__(self):
            self.fail = {"pre_tool_call"}
        def register_hook(self, name, fn):
            if name in self.fail:
                raise RuntimeError("simulated")
    ctx = _MockCtx()
    n = tr.register_trace_hooks(ctx)
    assert n == 5  # 5 succeeded


# ── run_id 推导 ───────────────────────────────────────────────────────────

def test_run_id_prefers_task_id():
    rid = tr._run_id_from_args(session_id="sess-abc", task_id="task-xyz")
    assert rid == "task-xyz"


def test_run_id_falls_back_to_session_id():
    rid = tr._run_id_from_args(session_id="sess-abc")
    assert rid == "sess-abc"


def test_run_id_unknown_when_both_missing():
    rid = tr._run_id_from_args()
    assert rid == "unknown"


# ── user query 提取 ───────────────────────────────────────────────────────

def test_extract_user_query_strips_date_prefix():
    raw = "[Mon Jun 18 14:32:11 2026] 你好世界"
    assert tr._extract_user_query(raw) == "你好世界"


def test_extract_user_query_keeps_plain():
    assert tr._extract_user_query("hello world") == "hello world"


def test_extract_user_query_empty():
    assert tr._extract_user_query("") == ""
    assert tr._extract_user_query(None) == ""


def test_sanitize_filename_safe_chars():
    s = tr._sanitize_filename('hello/world\\name:with*chars?')
    assert "/" not in s and "\\" not in s and ":" not in s and "*" not in s and "?" not in s


def test_sanitize_filename_truncates():
    s = tr._sanitize_filename("x" * 500)
    assert len(s) <= tr.QUERY_LEN_MAX


def test_sanitize_filename_empty_fallback():
    assert tr._sanitize_filename("") == "system"
    assert tr._sanitize_filename(None) == "system"


# ── record + reduce ───────────────────────────────────────────────────────

def test_pre_llm_call_records_event_and_query():
    tr._hk_pre_llm_call("sess-1", "[Mon Jun 18 14:32:11 2026] 你好", [], True, "claude-sonnet", "test")
    state = tr._turns["sess-1"]
    assert state.query == "你好"
    assert len(state.buffer) == 1
    assert state.buffer[0]["hook"] == "pre_llm_call"


def test_post_tool_call_extracts_error():
    """post_tool_call 能从 result JSON 提 error 字段。"""
    tr._hk_post_tool_call("sess-1", {"x": 1}, json.dumps({"error": "boom"}), "sess-1")
    state = tr._turns["sess-1"]
    assert state.buffer[-1]["payload"]["error"] == "boom"


def test_post_tool_call_no_error():
    tr._hk_post_tool_call("sess-1", {}, json.dumps({"ok": True}), "sess-1")
    state = tr._turns["sess-1"]
    assert state.buffer[-1]["payload"].get("error") is None


def test_reduce_meta_counts_llm_and_tools():
    """reduce_meta 聚合 llm_call_count / tool_call_count / 错误 / 最慢 tool。"""
    # pre_llm_call + post_llm_call x 2 + pre/post_tool_call x 2
    tr._hk_pre_llm_call("sess-1", "hi", [], True, "m", "p")
    tr._hk_post_llm_call("sess-1", "hi", "ans", [], "m", "p", duration_ms=1000)
    tr._hk_post_llm_call("sess-1", "hi2", "ans2", [], "m", "p", duration_ms=2000)
    tr._hk_pre_tool_call("miloco_im_push", {"m": "x"}, "sess-1")
    tr._hk_post_tool_call("miloco_im_push", {"m": "x"}, "ok", "sess-1", duration_ms=300)
    tr._hk_pre_tool_call("bad_tool", {}, "sess-1")
    tr._hk_post_tool_call("bad_tool", {}, json.dumps({"error": "fail"}), "sess-1", duration_ms=500)

    state = tr._turns["sess-1"]
    meta = tr._reduce_meta(state.buffer)
    assert meta["llm_call_count"] == 2
    assert meta["tool_call_count"] == 2
    assert meta["llm_total_ms"] == 3000
    assert meta["tool_total_ms"] == 800
    assert meta["tool_max_ms"] == 500
    assert meta["slowest_tool_name"] == "bad_tool"
    assert meta["error_count"] == 1
    assert "fail" in (meta["error_msg"] or "")


# ── traceLink ─────────────────────────────────────────────────────────────

def test_register_and_pop_trace_link():
    tr.register_trace_link("sess-1", "trace-abc")
    assert "sess-1" in tr._trace_links
    assert "sess-1" in tr._turns  # 同时 init turn entry
    v = tr.pop_trace_link("sess-1")
    assert v == "trace-abc"
    assert "sess-1" not in tr._trace_links


def test_pop_trace_link_missing_returns_none():
    assert tr.pop_trace_link("nonexistent") is None


# ── on_session_end finalize ───────────────────────────────────────────────

MILOCO_SESSION = "miloco:agent:main:miloco-suggest:miloco-suggest"


def _read_metas() -> list[dict]:
    """读当前 MILOCO_HOME 下落盘的全部 meta.json。"""
    root = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.meta.json"))]


def test_session_end_without_trace_id_drops(monkeypatch):
    """非 miloco: 前缀的 session 一个字都不落盘——那是车主自己的私聊，不该进 miloco trace。"""
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")
    tr._hk_pre_llm_call("sess-1", "我的私人问题", [], True, "m", "p")
    tr._hk_on_session_end("sess-1", True, False, "m", "p")
    assert "sess-1" not in tr._turns
    assert not _read_metas()
    assert not list((Path(os.environ["MILOCO_HOME"]) / "trace").rglob("*.jsonl.gz"))


def test_session_end_writes_meta_to_disk():
    """miloco: 前缀的 session → finalize 落盘 meta 给 backend 读。"""
    tr.register_trace_link(MILOCO_SESSION, "trace-abc")
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    tr._hk_post_llm_call(MILOCO_SESSION, "hi", "ans", [], "m", "p", duration_ms=500)
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    metas = _read_metas()
    assert len(metas) == 1
    assert metas[0]["trace_id"] == "trace-abc"
    assert metas[0]["success"] is True
    assert metas[0]["llm_call_count"] == 1
    assert MILOCO_SESSION not in tr._trace_links


def test_session_end_idempotent():
    """同一 turn 的 session_end 触发两次，第二次是 no-op（state 已释放）。"""
    tr.register_trace_link(MILOCO_SESSION, "trace-abc")
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    assert len(_read_metas()) == 1


# ── debug 落盘 ────────────────────────────────────────────────────────────

def test_meta_always_written_jsonl_needs_debug():
    """debug 关时只写 meta 不写 jsonl —— meta 是 backend 记账的唯一来源，不能被开关掐掉。"""
    sess = "miloco:test-flush"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")
    metas = _read_metas()
    assert len(metas) == 1
    assert metas[0]["jsonl_path"] is None
    assert not list((Path(os.environ["MILOCO_HOME"]) / "trace" / "agent").rglob("*.jsonl.gz"))


def test_meta_rolls_over_daily_cap(monkeypatch):
    """meta 数达上限时淘汰最老的再写，不是拒写——拒写会让当天记账整天静默停摆。"""
    monkeypatch.setattr(tr, "META_DAILY_MAX", 3)
    for i in range(6):
        _one_turn(MILOCO_SESSION, f"第 {i} 轮")

    queries = sorted(m["query"] for m in _read_metas())
    assert queries == ["第 3 轮", "第 4 轮", "第 5 轮"]


def test_flush_enabled_writes_jsonl_and_meta(monkeypatch):
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")
    sess = "miloco:test-flush-enabled"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "[Mon Jun 18 14:32:11 2026] 你好", [], True, "m", "p")
    tr._hk_post_tool_call("miloco_im_push", {}, "ok", sess, duration_ms=42)
    tr._hk_on_session_end(sess, True, False, "m", "p")

    today = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent"
    jsonl_files = list(today.rglob("*.jsonl.gz"))
    meta_files = list(today.rglob("*.meta.json"))
    assert len(jsonl_files) == 1
    assert len(meta_files) == 1

    # jsonl 能解开
    with gzip.open(jsonl_files[0], "rt", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert any(e["hook"] == "pre_llm_call" for e in lines)
    assert any(e["hook"] == "post_tool_call" for e in lines)
    assert any(e["hook"] == "on_session_end" for e in lines)

    # meta 内容齐
    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["trace_id"] == "trace-abc"
    assert meta["tool_call_count"] == 1
    assert meta["slowest_tool_name"] == "miloco_im_push"
    assert meta["jsonl_path"].endswith(".jsonl.gz")


def test_daily_cap_skips_dump(monkeypatch):
    """cap = 300，超出 warn 跳过（不抛错，jsonl_path=None）。"""
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")
    # 预先建 300 个 .gz 文件
    today = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent" / "20991231"
    today.mkdir(parents=True, exist_ok=True)
    for i in range(tr.DAILY_DUMP_MAX):
        (today / f"old_{i}.jsonl.gz").write_bytes(b"")

    # 把系统时间推到 2099-12-31 让 _today_dir() 用这个
    monkeypatch.setattr(tr, "_today_dir", lambda: today)

    sess = "miloco:test-cap"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")

    # jsonl 跳过，但 meta 必须照写——否则 cap 一满记账整天静默停摆
    assert not (today / f"{sess}__hi.jsonl.gz").exists()
    metas = _read_metas()
    assert len(metas) == 1
    assert metas[0]["jsonl_path"] is None


# ── sweeper ───────────────────────────────────────────────────────────────

def test_sweep_evicts_stuck_turn_keeps_fresh():
    """没等到 session_end 的僵尸 turn 被清，进行中的新 turn 留着。"""
    stuck, fresh = "miloco:stuck-1", "miloco:fresh-1"
    tr._hk_pre_llm_call(stuck, "hi", [], True, "m", "p")
    tr._hk_pre_llm_call(fresh, "hi", [], True, "m", "p")
    tr._turns[stuck].last_seen = 0  # epoch 之后再没有事件
    tr._sweep_stale_turns()
    assert stuck not in tr._turns
    assert fresh in tr._turns


def test_session_end_triggers_sweep():
    """sweeper 要真接在 turn 结束这条路径上，光有函数没人调等于没清。"""
    zombie = "miloco:zombie-1"
    tr._hk_pre_llm_call(zombie, "hi", [], True, "m", "p")
    tr._turns[zombie].last_seen = 0  # epoch 之后再没有事件
    _one_turn(MILOCO_SESSION, "正常一轮")
    assert zombie not in tr._turns


def test_sweep_keeps_slow_but_active_turn():
    """判据是「多久没动静」不是「开了多久」：慢 turn 只要还在出事件就不能被清。"""
    slow = "miloco:slow-1"
    tr._hk_pre_llm_call(slow, "hi", [], True, "m", "p")
    tr._turns[slow].started_at = 0  # epoch 就开始了
    tr._hk_post_llm_call(slow, "hi", "ans", [], "m", "p", duration_ms=1)  # 但刚刚还在出事件
    tr._sweep_stale_turns()
    assert slow in tr._turns


def test_sweep_hard_cap_keeps_active_over_idle():
    """撞硬上限时先淘汰最久没动静的，而不是开始得最早的。

    所有 turn 的 last_seen 都要留在 stuck 窗口内，否则先被僵尸清理带走，
    硬上限那段根本不会执行。
    """
    now = tr._now_ms()
    active = "miloco:long-running"
    tr._hk_pre_llm_call(active, "hi", [], True, "m", "p")
    tr._turns[active].started_at = now - 600_000  # 全场开始最早
    for i in range(tr.TURNS_HARD_CAP):
        sid = f"miloco:idle-{i}"
        tr._hk_pre_llm_call(sid, "hi", [], True, "m", "p")
        tr._turns[sid].started_at = now - 300_000 + i
        tr._turns[sid].last_seen = now - 300_000 + i  # 建完就没动静，但没到僵尸线
    tr._turns[active].last_seen = now  # 活跃的刚刚还在出事件

    tr._sweep_stale_turns()
    assert active in tr._turns
    assert "miloco:idle-0" not in tr._turns


def test_sweep_hard_cap_evicts_oldest():
    """并发 turn 数超硬上限时淘汰到上限为止，先淘汰最久没有动静的。

    判据本身由 test_sweep_hard_cap_keeps_active_over_idle 区分；这里只钉数量收敛。
    """
    for i in range(tr.TURNS_HARD_CAP + 5):
        sid = f"miloco:sess-{i}"
        # 插入顺序天然让 last_seen 单调不减：建得最早 = 最久没动静。
        # 僵尸判据同样看 last_seen，刚建的 turn 天然新鲜，不必覆写任何时间戳。
        tr._hk_pre_llm_call(sid, "hi", [], True, "m", "p")
    tr._sweep_stale_turns()
    assert len(tr._turns) == tr.TURNS_HARD_CAP
    assert "miloco:sess-0" not in tr._turns
    assert f"miloco:sess-{tr.TURNS_HARD_CAP + 4}" in tr._turns

# ── 持久会话连续记账（本文件核心不变式） ──────────────────────────────────

def _one_turn(session_id: str, query: str) -> None:
    tr._hk_pre_llm_call(session_id, query, [], True, "m", "p")
    tr._hk_post_llm_call(session_id, query, "ans", [], "m", "p", duration_ms=500)
    tr._hk_on_session_end(session_id, True, False, "m", "p")


def test_persistent_session_records_every_turn():
    """同一 session_id 连跑多轮，每轮都要落一份 meta —— 持久会话（rule/suggest）的记账前提。"""
    for i in range(3):
        _one_turn(MILOCO_SESSION, f"第 {i} 轮问题")

    metas = list((Path(os.environ["MILOCO_HOME"]) / "trace" / "agent").rglob("*.meta.json"))
    assert len(metas) == 3
    queries = sorted(json.loads(p.read_text(encoding="utf-8"))["query"] for p in metas)
    assert queries == ["第 0 轮问题", "第 1 轮问题", "第 2 轮问题"]


def test_turn_state_released_after_session_end():
    """turn 结束后内存里的 state 必须释放，否则下一轮复用到旧 buffer。"""
    _one_turn(MILOCO_SESSION, "问题")
    assert MILOCO_SESSION not in tr._turns


def test_second_turn_meta_excludes_first_turn_events():
    """第二轮的 meta 只统计第二轮的事件，不累加第一轮。"""
    tr._hk_pre_llm_call(MILOCO_SESSION, "第一轮", [], True, "m", "p")
    tr._hk_post_llm_call(MILOCO_SESSION, "第一轮", "ans", [], "m", "p", duration_ms=500)
    tr._hk_post_tool_call("tool_a", {}, "ok", MILOCO_SESSION, duration_ms=100)
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")

    tr._hk_pre_llm_call(MILOCO_SESSION, "第二轮", [], True, "m", "p")
    tr._hk_post_llm_call(MILOCO_SESSION, "第二轮", "ans", [], "m", "p", duration_ms=700)
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")

    metas = {
        json.loads(p.read_text(encoding="utf-8"))["query"]: json.loads(p.read_text(encoding="utf-8"))
        for p in (Path(os.environ["MILOCO_HOME"]) / "trace" / "agent").rglob("*.meta.json")
    }
    assert metas["第二轮"]["llm_call_count"] == 1
    assert metas["第二轮"]["tool_call_count"] == 0
    assert metas["第二轮"]["llm_total_ms"] == 700


def test_long_chinese_query_still_lands_on_disk():
    """全中文长 query + 长 session_id 不能撑爆文件名字节上限，否则整轮记账静默丢失。"""
    sess = "miloco:agent:main:miloco-suggest:miloco-suggest"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "客" * 200, [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")

    metas = _read_metas()
    assert len(metas) == 1
    for path in (Path(os.environ["MILOCO_HOME"]) / "trace" / "agent").rglob("*"):
        if path.is_file():
            assert len(path.name.encode("utf-8")) <= tr.NAME_MAX_BYTES


def test_state_released_even_if_flush_raises(monkeypatch):
    """落盘抛错也要释放 state，否则下一轮接着写这轮 buffer——正是本模块修掉的那个形态。"""
    def boom(*_a, **_kw):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(tr, "_flush_to_disk", boom)
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    with pytest.raises(RuntimeError):
        tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    assert MILOCO_SESSION not in tr._turns


def test_meta_written_even_when_evict_fails(monkeypatch):
    """淘汰只是尽力而为，它出错不能把这一轮的记账一起吃掉。"""
    real_glob = Path.glob

    def boom(self, pattern, **kwargs):
        if pattern == "*.meta.json":  # 只打掉淘汰那一次，别影响测试自己读盘
            raise OSError("permission denied")
        return real_glob(self, pattern, **kwargs)

    monkeypatch.setattr(Path, "glob", boom)
    _one_turn(MILOCO_SESSION, "问题")
    assert len(_read_metas()) == 1


def test_meta_written_even_when_jsonl_write_fails(monkeypatch):
    """明细写失败不能连累统计——磁盘写满时 meta 往往还写得下。"""
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")

    def boom(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tr.gzip, "open", boom)
    _one_turn(MILOCO_SESSION, "问题")

    metas = _read_metas()
    assert len(metas) == 1
    assert metas[0]["jsonl_path"] is None
