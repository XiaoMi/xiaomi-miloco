import json

import pytest

from hermes import tools


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    return home


# ------------------------------------------------------ _resolve_notify_target


def test_resolve_notify_target_not_configured():
    res = tools._resolve_notify_target()
    assert res["target"] is None
    assert res["needs_bind"] is True
    assert res["bind_reason"] == "not_configured"


def test_resolve_notify_target_configured():
    from hermes import config

    config.atomic_write_json({"notify_session_key": "sk-1"})
    res = tools._resolve_notify_target()
    assert res["needs_bind"] is False
    assert res["target"] == {"session_key": "sk-1"}


# ------------------------------------------------------ _miloco_im_push_handler


def test_im_push_no_channel_returns_needs_bind():
    raw = tools._miloco_im_push_handler({"message": "灯已开"})
    res = json.loads(raw)
    assert res["ok"] is False
    assert res["needsBind"] is True
    assert res["bindReason"] == "not_configured"
    assert res["bindHintExample"]


def test_im_push_with_target_succeeds():
    from hermes import config

    config.atomic_write_json({"notify_session_key": "sk-1"})
    raw = tools._miloco_im_push_handler({"message": "灯已开"})
    res = json.loads(raw)
    assert res["ok"] is True
    assert res["channel"] == "sk-1"


def test_im_push_needs_bind_with_hint_sends(monkeypatch):
    delivered = {}

    def fake_deliver(session_key, message):
        delivered["session_key"] = session_key
        delivered["message"] = message
        return {"ok": True}

    monkeypatch.setattr(tools, "_deliver_notification", fake_deliver)
    monkeypatch.setattr(
        tools,
        "_resolve_notify_target",
        lambda: {
            "target": {"session_key": "fallback-1"},
            "needs_bind": True,
            "bind_reason": "not_configured",
        },
    )
    raw = tools._miloco_im_push_handler(
        {"message": "灯已开", "bindHint": "请回复绑定通知频道"}
    )
    res = json.loads(raw)
    assert res["ok"] is True
    assert res["fallback"] is True
    assert "<miloco-notification>" in delivered["message"]
    assert "灯已开" in delivered["message"]
    assert "请回复绑定通知频道" in delivered["message"]


def test_im_push_wraps_body_in_notification_tag(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tools,
        "_deliver_notification",
        lambda sk, msg: captured.update(msg=msg) or {"ok": True},
    )
    from hermes import config

    config.atomic_write_json({"notify_session_key": "sk-x"})
    tools._miloco_im_push_handler({"message": "hello"})
    assert "<miloco-notification>hello</miloco-notification>" in captured["msg"]


# ----------------------------------------------------- _miloco_notify_bind_handler


def test_notify_bind_writes_config():
    raw = tools._miloco_notify_bind_handler({"sessionKey": "sk-xyz"})
    res = json.loads(raw)
    assert res["ok"] is True
    assert res["session_key"] == "sk-xyz"
    from hermes import config

    cfg = config.read_config_dict()
    assert cfg["notify_session_key"] == "sk-xyz"


def test_notify_bind_missing_session_key_fails():
    raw = tools._miloco_notify_bind_handler({})
    res = json.loads(raw)
    assert res["ok"] is False


# --------------------------------------------------------------- register_tools


def test_register_tools_registers_three():
    class FakeCtx:
        def __init__(self):
            self.tools = []

        def register_tool(self, name, handler):
            self.tools.append(name)

    ctx = FakeCtx()
    tools.register_tools(ctx)
    assert ctx.tools == ["miloco_im_push", "miloco_notify_bind", "miloco_habit_suggest"]


def test_habit_suggest_handler_delegates_to_suggestions():
    raw = tools._miloco_habit_suggest_handler({"action": "list"})
    res = json.loads(raw)
    assert res["ok"] is True
    assert "entries" in res
