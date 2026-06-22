import json

import pytest

from hermes import tools


class _FakeSendResult:
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error


class _FakeAdapter:
    def __init__(self, success=True, error=None):
        self._success = success
        self._error = error
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return _FakeSendResult(success=self._success, error=self._error)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))


def test_im_push_delivers_via_adapter(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        tools, "_resolve_deliver_target", lambda cfg: (adapter, "oc_xxx", "")
    )
    plugin_cfg = {"deliver": "feishu", "deliver_extra": {"chat_id": "oc_xxx"}}
    raw = tools._miloco_im_push_handler({"message": "灯已开"}, plugin_cfg=plugin_cfg)
    res = json.loads(raw)
    assert res["ok"] is True
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "oc_xxx"
    assert (
        "<miloco-notification>灯已开</miloco-notification>"
        == adapter.sent[0]["content"]
    )


def test_im_push_with_thread_id(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        tools, "_resolve_deliver_target", lambda cfg: (adapter, "oc_xxx", "t_123")
    )
    plugin_cfg = {
        "deliver": "feishu",
        "deliver_extra": {"chat_id": "oc_xxx", "message_thread_id": "t_123"},
    }
    raw = tools._miloco_im_push_handler({"message": "hello"}, plugin_cfg=plugin_cfg)
    res = json.loads(raw)
    assert res["ok"] is True
    assert adapter.sent[0]["metadata"] == {"thread_id": "t_123"}


def test_im_push_no_target_returns_error(monkeypatch):
    monkeypatch.setattr(
        tools, "_resolve_deliver_target", lambda cfg: (None, None, None)
    )
    raw = tools._miloco_im_push_handler({"message": "灯已开"}, plugin_cfg={})
    res = json.loads(raw)
    assert res["ok"] is False
    assert "no deliver target" in res["error"]


def test_im_push_adapter_send_failure(monkeypatch):
    adapter = _FakeAdapter(success=False, error="rate limited")
    monkeypatch.setattr(
        tools, "_resolve_deliver_target", lambda cfg: (adapter, "oc_xxx", "")
    )
    plugin_cfg = {"deliver": "feishu", "deliver_extra": {"chat_id": "oc_xxx"}}
    raw = tools._miloco_im_push_handler({"message": "test"}, plugin_cfg=plugin_cfg)
    res = json.loads(raw)
    assert res["ok"] is False
    assert "rate limited" in res["error"]


def test_resolve_deliver_target_explicit_chat_id(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        tools, "_resolve_platform_adapter", lambda name: (adapter, None)
    )
    cfg = {
        "deliver": "feishu",
        "deliver_extra": {"chat_id": "oc_abc", "message_thread_id": "t_1"},
    }
    a, cid, tid = tools._resolve_deliver_target(cfg)
    assert a is adapter
    assert cid == "oc_abc"
    assert tid == "t_1"


def test_resolve_deliver_target_fallback_home_channel(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        tools, "_resolve_platform_adapter", lambda name: (adapter, None)
    )
    monkeypatch.setattr(
        tools, "_resolve_home_channel", lambda name: ("oc_home", "t_home")
    )
    cfg = {"deliver": "feishu", "deliver_extra": {}}
    a, cid, tid = tools._resolve_deliver_target(cfg)
    assert cid == "oc_home"
    assert tid == "t_home"


def test_resolve_deliver_target_no_deliver_returns_none(monkeypatch):
    cfg = {"deliver": "", "deliver_extra": {}}
    a, cid, tid = tools._resolve_deliver_target(cfg)
    assert a is None
    assert cid is None
    assert tid is None


def test_resolve_deliver_target_no_adapter_returns_none(monkeypatch):
    monkeypatch.setattr(tools, "_resolve_platform_adapter", lambda name: (None, None))
    cfg = {"deliver": "feishu", "deliver_extra": {"chat_id": "oc_xxx"}}
    a, cid, tid = tools._resolve_deliver_target(cfg)
    assert a is None


def test_resolve_deliver_target_no_chat_id_no_home_channel(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        tools, "_resolve_platform_adapter", lambda name: (adapter, None)
    )
    monkeypatch.setattr(tools, "_resolve_home_channel", lambda name: (None, None))
    cfg = {"deliver": "feishu", "deliver_extra": {}}
    a, cid, tid = tools._resolve_deliver_target(cfg)
    assert a is None


def test_register_tools_registers_two():
    class FakeCtx:
        def __init__(self):
            self.tools = []

        def register_tool(self, *, name, toolset, schema, handler):
            self.tools.append(name)

    ctx = FakeCtx()
    tools.register_tools(ctx)
    assert ctx.tools == ["miloco_im_push", "miloco_habit_suggest"]


def test_habit_suggest_handler_delegates_to_suggestions():
    raw = tools._miloco_habit_suggest_handler({"action": "list"})
    res = json.loads(raw)
    assert res["ok"] is True
    assert "entries" in res
