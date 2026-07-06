# Copyright (C) 2025 Xiaomi Corporation
"""AgentPlatformAdapter 框架测试(hermes-pr.md §五 #1 配套)。

覆盖:
- ABC: TurnContext / AgentTurnResult / TraceMeta 数据类
- duck-typed loader: 接受/拒绝 契约
- errors: AdapterTransportError / AdapterTransientError 分类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


def test_turn_context_minimal_required_fields():
    """TurnContext 必填字段:text / session_key / lane / trace_id / wait_timeout_ms。

    profile 默认 'full',extra 默认 {}。
    """
    from miloco.agent_platform.base import TurnContext

    ctx = TurnContext(
        text="hello",
        session_key="agent:main:miloco",
        lane="miloco-interactive",
        trace_id="t-001",
        wait_timeout_ms=30_000,
    )
    assert ctx.text == "hello"
    assert ctx.session_key == "agent:main:miloco"
    assert ctx.lane == "miloco-interactive"
    assert ctx.trace_id == "t-001"
    assert ctx.wait_timeout_ms == 30_000
    assert ctx.profile == "full"
    assert ctx.extra == {}


def test_turn_context_profile_choices():
    """profile 接受 full/suggestion/rule/minimal。"""
    from miloco.agent_platform.base import TurnContext

    for p in ("full", "suggestion", "rule", "minimal"):
        ctx = TurnContext(
            text="x", session_key="s", lane="l", trace_id="t",
            wait_timeout_ms=1000, profile=p,
        )
        assert ctx.profile == p


def test_turn_context_extra_dict():
    """extra 字段透传任意 dict。"""
    from miloco.agent_platform.base import TurnContext

    ctx = TurnContext(
        text="x", session_key="s", lane="l", trace_id="t",
        wait_timeout_ms=1000, extra={"event_type": "rule", "camera_did": "abc"},
    )
    assert ctx.extra == {"event_type": "rule", "camera_did": "abc"}


def test_agent_turn_result_ok_minimal():
    """AgentTurnResult ok 状态字段。"""
    from miloco.agent_platform.base import AgentTurnResult

    r = AgentTurnResult(run_id="r-001", status="ok", rtt_ms=1500.5)
    assert r.run_id == "r-001"
    assert r.status == "ok"
    assert r.rtt_ms == 1500.5
    assert r.recovered is None
    assert r.error is None


def test_agent_turn_result_with_recovered():
    """AgentTurnResult recovered/error 字段。"""
    from miloco.agent_platform.base import AgentTurnResult

    r = AgentTurnResult(
        run_id="r-002",
        status="ok",
        rtt_ms=2000.0,
        recovered=True,
        error=None,
    )
    assert r.recovered is True


def test_agent_turn_result_error_status():
    """error status 时 error 字段非空。"""
    from miloco.agent_platform.base import AgentTurnResult

    r = AgentTurnResult(run_id=None, status="error", error="some failure")
    assert r.run_id is None
    assert r.error == "some failure"


def test_trace_meta_required_fields():
    """TraceMeta 必填字段对齐 backend AgentRunRecord(observability/types.py)。

    backend AgentRunRecord.to_row 用这些字段写 SQLite,缺一列就崩。
    """
    from miloco.agent_platform.base import TraceMeta

    m = TraceMeta(
        run_id="r",
        query="q",
        duration_ms=100.0,
        llm_call_count=1,
        tool_call_count=2,
        llm_total_ms=50.0,
        tool_total_ms=40.0,
        tool_max_ms=30.0,
        slowest_tool_name="t1",
        success=True,
        error_count=0,
        error_msg=None,
        jsonl_path="/path/to/jsonl",
    )
    assert m.run_id == "r"
    assert m.duration_ms == 100.0
    assert m.slowest_tool_name == "t1"


def test_trace_meta_optional_fields_default():
    """TraceMeta slowest_tool_name / error_msg / jsonl_path 允许 None。"""
    from miloco.agent_platform.base import TraceMeta

    m = TraceMeta(
        run_id="r", query="q", duration_ms=0.0,
        llm_call_count=0, tool_call_count=0,
        llm_total_ms=0.0, tool_total_ms=0.0, tool_max_ms=0.0,
        slowest_tool_name=None, success=False,
        error_count=1, error_msg="tool failed", jsonl_path=None,
    )
    assert m.slowest_tool_name is None
    assert m.jsonl_path is None
    assert m.success is False


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


def test_adapter_transport_error_is_exception():
    """AdapterTransportError 是 Exception 子类,可正常 raise。"""
    from miloco.agent_platform.base import AdapterTransportError

    with pytest.raises(AdapterTransportError, match="connect refused"):
        raise AdapterTransportError("connect refused")


def test_adapter_transient_error_is_exception():
    from miloco.agent_platform.base import AdapterTransientError

    with pytest.raises(AdapterTransientError, match="timeout"):
        raise AdapterTransientError("timeout")


# ---------------------------------------------------------------------------
# Loader:duck-typed 接口契约
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter_dir(tmp_path: Path) -> Path:
    """写一个符合 duck-typed 契约的 adapter.py。"""
    d = tmp_path / "agent_platform" / "fake"
    d.mkdir(parents=True)
    (d / "adapter.py").write_text(
        """
class Adapter:
    name = "fake"

    def __init__(self):
        pass

    async def send_turn(self, ctx):
        from types import SimpleNamespace
        return SimpleNamespace(run_id="r1", status="ok", rtt_ms=100.0)

    async def read_trace_meta(self, run_id):
        return None

    def build_system(self, profile, extra):
        return f"<system profile={profile}>"
""",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def bad_adapter_dir(tmp_path: Path) -> Path:
    """缺 send_turn 方法的 adapter.py(应被 loader 拒绝)。"""
    d = tmp_path / "agent_platform" / "bad"
    d.mkdir(parents=True)
    (d / "adapter.py").write_text(
        """
class Adapter:
    name = "bad"
    def build_system(self, profile, extra):
        return ""
""",
        encoding="utf-8",
    )
    return d


def _patch_miloco_home(monkeypatch, tmp_path: Path):
    """loader 内部 `from miloco.utils.paths import miloco_home`,patch 不到 module attr。

    改 patch loader 模块里的本地绑定:monkeypatch ap_mod.loader.miloco_home。
    """
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "miloco_home", lambda: tmp_path)


def test_load_adapter_valid_duck_typed(tmp_path: Path, monkeypatch, adapter_dir: Path):
    """loader 接受符合 5 方法契约的 adapter(无需继承 ABC)。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.loader.load_adapter("fake")
    assert inst is not None
    assert inst.name == "fake"


def test_load_adapter_missing_dir(tmp_path: Path, monkeypatch):
    """agent_platform/<name>/ 不存在 → load_adapter 返回 None。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.loader.load_adapter("nonexistent")
    assert inst is None


def test_load_adapter_missing_adapter_py(tmp_path: Path, monkeypatch):
    """目录存在但 adapter.py 缺 → load_adapter 返回 None。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    d = tmp_path / "agent_platform" / "no_py"
    d.mkdir(parents=True)
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.loader.load_adapter("no_py")
    assert inst is None


def test_load_adapter_contract_violation(tmp_path: Path, monkeypatch, bad_adapter_dir: Path):
    """adapter.py 缺 send_turn → load_adapter 返回 None(don't raise)。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.loader.load_adapter("bad")
    assert inst is None  # 缺契约方法 → 拒绝, 不抛


def test_load_adapter_module_exception_caught(tmp_path: Path, monkeypatch):
    """adapter.py 顶层 import 抛错 → load_adapter 返回 None,不崩 backend。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    d = tmp_path / "agent_platform" / "broken"
    d.mkdir(parents=True)
    (d / "adapter.py").write_text(
        "raise RuntimeError('oops')\n",
        encoding="utf-8",
    )
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.loader.load_adapter("broken")
    assert inst is None  # 不抛,降级 None


def test_load_adapter_caches_singleton(tmp_path: Path, monkeypatch, adapter_dir: Path):
    """load_adapter 第二次调用返缓存,不重新 import。"""
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    _patch_miloco_home(monkeypatch, tmp_path)

    a = ap_mod.loader.load_adapter("fake")
    b = ap_mod.loader.load_adapter("fake")
    assert a is b  # same object — cached


def test_get_adapter_loads_when_none(tmp_path: Path, monkeypatch, adapter_dir: Path):
    """get_adapter 没缓存时主动 load_adapter。

    stub loader.get_settings 让 platform='fake',避免用全局的 'hermes'(找不到 fake)。
    loader 内部 `from miloco.config import get_settings`,patch 不到原 module attr,
    必须 patch loader 模块里的本地绑定。
    """
    from miloco import agent_platform as ap_mod

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    _patch_miloco_home(monkeypatch, tmp_path)

    @dataclass
    class FakeAgent:
        platform: str = "fake"

    @dataclass
    class FakeSettings:
        agent: Any = field(default_factory=FakeAgent)

    monkeypatch.setattr(ap_mod.loader, "get_settings", lambda: FakeSettings())

    inst = ap_mod.get_adapter()
    assert inst is not None
    assert inst.name == "fake"


def test_get_adapter_no_platform_configured(tmp_path: Path, monkeypatch):
    """settings.agent.platform 空时 get_adapter 返 None(不加载)。"""
    from miloco import agent_platform as ap_mod

    # stub settings.agent.platform = ""
    @dataclass
    class FakeAgent:
        platform: str = ""

    @dataclass
    class FakeSettings:
        agent: Any = field(default_factory=FakeAgent)

    monkeypatch.setattr(ap_mod.loader, "_cached_adapter", None)
    monkeypatch.setattr(ap_mod.loader, "get_settings", lambda: FakeSettings())
    _patch_miloco_home(monkeypatch, tmp_path)

    inst = ap_mod.get_adapter()
    assert inst is None


def test_reset_cache_clears_singleton():
    """reset_cache 把 _cached_adapter 置 None。"""
    from miloco import agent_platform as ap_mod

    ap_mod.loader._cached_adapter = "fake-obj"
    ap_mod.reset_cache()
    assert ap_mod.loader._cached_adapter is None


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api_exports():
    """agent_platform/__init__.py 导出正确的公共符号。"""
    from miloco import agent_platform

    expected = {
        "AgentPlatformAdapter",
        "AgentTurnResult",
        "SystemPromptBuilder",
        "TurnContext",
        "get_adapter",
        "load_adapter",
    }
    for name in expected:
        assert hasattr(agent_platform, name), f"missing {name}"