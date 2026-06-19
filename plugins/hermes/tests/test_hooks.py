import pytest

from hermes import hooks
from hermes import trace


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    monkeypatch.setattr(hooks.catalog, "get_catalog", lambda: "")
    trace._turns.clear()
    trace._trace_links.clear()
    yield
    trace._turns.clear()
    trace._trace_links.clear()


# ------------------------------------------------------------- _resolve_profile


def test_resolve_profile_default_is_full():
    assert hooks._resolve_profile(session_id="agent:main:miloco") == "full"
    assert hooks._resolve_profile(session_id="user-im-123") == "full"


def test_resolve_profile_cron_session_prefix_is_minimal():
    assert hooks._resolve_profile(session_id="cron_habit_suggest") == "minimal"


def test_resolve_profile_cron_platform_is_minimal():
    assert hooks._resolve_profile(platform="cron") == "minimal"
    assert hooks._resolve_profile(session_id="anything", platform="cron") == "minimal"


def test_resolve_profile_rule():
    assert hooks._resolve_profile(session_id="ctx:miloco-rule:abc") == "rule"


def test_resolve_profile_suggestion():
    assert hooks._resolve_profile(session_id="ctx:miloco-suggest:abc") == "suggestion"


# ------------------------------------------------------------- _build_perception


def test_build_perception_full_has_three_formats():
    text = hooks._build_perception("full")
    assert "- 语音指令（header" in text
    assert "- 事件提醒（header" in text
    assert "- 规则触发（header" in text


def test_build_perception_suggestion_only():
    text = hooks._build_perception("suggestion")
    assert "- 事件提醒（header" in text
    assert "- 语音指令（header" not in text
    assert "- 规则触发（header" not in text


def test_build_perception_rule_only():
    text = hooks._build_perception("rule")
    assert "- 规则触发（header" in text
    assert "- 语音指令（header" not in text
    assert "- 事件提醒（header" not in text


# ----------------------------------------------------- _load_home_profile_block


def test_load_home_profile_block_missing_returns_empty():
    assert hooks._load_home_profile_block() == ""


def test_load_home_profile_block_demotes_headings():
    import os

    md = "# 家庭档案\n\n## 家庭成员\n\n王磊\n"
    path = os.environ["MILOCO_HOME"] + "/profile.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    out = hooks._load_home_profile_block()
    assert out.startswith("## 家庭档案")
    assert "### 家庭成员" in out


# ------------------------------------------------- _build_pending_suggestion_block


def test_build_pending_suggestion_block_empty_when_none(monkeypatch):
    monkeypatch.setattr(hooks, "load_open_questions", lambda: [])
    assert hooks._build_pending_suggestion_block() == ""


def test_build_pending_suggestion_block_formats_items(monkeypatch):
    monkeypatch.setattr(
        hooks,
        "load_open_questions",
        lambda: [
            {"key": "wl_water", "title": "下午喝水", "suggestion": "提醒喝水"},
        ],
    )
    out = hooks._build_pending_suggestion_block()
    assert "## 等用户回应的习惯建议" in out
    assert "- [wl_water] 下午喝水：提醒喝水" in out


# --------------------------------------------------------- _on_pre_llm_call


def test_pre_llm_call_full_contains_miloco_and_perception():
    res = hooks._on_pre_llm_call(session_id="agent:main:miloco")
    ctx = res["context"]
    assert "Miloco" in ctx
    assert "## 感知" in ctx
    assert "## 能力概览" in ctx


def test_pre_llm_call_minimal_omits_perception():
    res = hooks._on_pre_llm_call(session_id="cron_habit_suggest")
    ctx = res["context"]
    assert "Miloco" in ctx
    assert "## 感知" not in ctx
    assert "## 能力概览" not in ctx
    assert "## 设备目录" not in ctx


def test_pre_llm_call_includes_device_catalog(monkeypatch):
    monkeypatch.setattr(hooks.catalog, "get_catalog", lambda: "did-123\tspec_name")
    res = hooks._on_pre_llm_call(session_id="agent:main:miloco")
    ctx = res["context"]
    assert "## 设备目录" in ctx
    assert "did-123" in ctx
    assert "```text" in ctx


def test_pre_llm_call_returns_context_key():
    res = hooks._on_pre_llm_call(session_id="agent:main:miloco")
    assert set(res.keys()) == {"context"}
    assert isinstance(res["context"], str)


# --------------------------------------------------------- trace hook wrappers


def test_on_pre_tool_call_records_event():
    hooks._on_pre_tool_call(run_id="r1", tool_name="miloco_im_push")
    buf = trace._get_turn("r1")["buffer"]
    assert buf[0]["hook"] == "before_tool_call"


def test_on_post_tool_call_records_event():
    hooks._on_post_tool_call(run_id="r2", duration_ms=12, error=None)
    buf = trace._get_turn("r2")["buffer"]
    assert buf[0]["hook"] == "after_tool_call"


def test_on_post_llm_call_records_event():
    hooks._on_post_llm_call(run_id="r3", text="hi")
    buf = trace._get_turn("r3")["buffer"]
    assert buf[0]["hook"] == "llm_output"


def test_on_session_end_finalizes_turn():
    trace.register_trace_link("r4", "trace-X")
    hooks._on_session_end(run_id="r4", success=True, duration_ms=7)
    assert trace.get_turn_status("r4") == "done"


# ------------------------------------------------------------- register_hooks


def test_register_hooks_registers_all():
    class FakeCtx:
        def __init__(self):
            self.registered = []

        def on(self, name, fn):
            self.registered.append(name)

    ctx = FakeCtx()
    hooks.register_hooks(ctx)
    assert ctx.registered == [
        "pre_llm_call",
        "pre_tool_call",
        "post_tool_call",
        "post_llm_call",
        "on_session_end",
    ]
