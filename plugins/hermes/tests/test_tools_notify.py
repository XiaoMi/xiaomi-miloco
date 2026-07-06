"""miloco_im_push 工具：state.json 投递 + 无 target 时的明确错误。

新流程（v0.3.0）：不用 ``ctx.dispatch_tool("send_message", ...)``（Hermes
故意把 send_message 从 model tools 移除），改用 ``subprocess.run(["hermes",
"send", "--to", target, "--json", "-q", body])`` 走 Hermes 官方 CLI 入口。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from miloco_plugin_pkg import tools_notify as tn


# ---------- fake ctx ----------

class _FakeCtx:
    """最小 Hermes ctx 替身：只暴露 manifest.path（用来定位 state.json）。"""

    def __init__(self, plugin_dir: Path) -> None:
        self.manifest = type("M", (), {"path": str(plugin_dir)})()


# ---------- fake subprocess ----------

class _FakeCompleted:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeProcess:
    """模拟 subprocess.run 返回值 + 记录最近一次的 args。"""

    last_call: Optional[Dict[str, Any]] = None

    def __init__(self, *, returncode: int = 0, json_payload: Optional[Dict[str, Any]] = None,
                 stderr: str = "") -> None:
        self._returncode = returncode
        self._payload = json_payload
        self._stderr = stderr

    def __call__(self, cmd, **kwargs):
        type(self).last_call = {"cmd": cmd, "kwargs": kwargs}
        stdout = json.dumps(self._payload) if self._payload is not None else ""
        return _FakeCompleted(returncode=self._returncode, stdout=stdout, stderr=self._stderr)


# ---------- state.json 读写 ----------

def test_state_roundtrip(tmp_path: Path):
    state_file = tmp_path / "state.json"
    ctx = _FakeCtx(tmp_path)
    assert tn.get_deliver_target(ctx) is None

    tn.set_deliver_target(ctx, "telegram:-1001234567890:17585")
    assert state_file.is_file()
    assert tn.get_deliver_target(ctx) == "telegram:-1001234567890:17585"

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["deliver"]["target"] == "telegram:-1001234567890:17585"
    assert payload["deliver"]["auto_configured"] is False


def test_state_corrupt_returns_empty(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json {", encoding="utf-8")
    ctx = _FakeCtx(tmp_path)
    assert tn.get_deliver_target(ctx) is None


def test_state_missing_returns_empty(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    assert tn.get_deliver_target(ctx) is None


def test_state_with_deliver_key_but_no_target(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"auto_configured": True}}), encoding="utf-8")
    ctx = _FakeCtx(tmp_path)
    assert tn.get_deliver_target(ctx) is None


# ---------- notify_owner 投递（mock subprocess.run） ----------

@pytest.fixture
def fake_hermes(monkeypatch):
    """把 subprocess.run 替换成 _FakeProcess，并返回可配置函数。

    Usage:
        fake_hermes(returncode=0, payload={"success": True, "platform": "telegram"})
        # 或：
        fake_hermes.fail("Unknown tool")
    """
    def _setter(*, returncode: int = 0, payload: Optional[Dict[str, Any]] = None,
                stderr: str = "") -> _FakeProcess:
        proc = _FakeProcess(returncode=returncode, json_payload=payload, stderr=stderr)
        # subprocess.run 是 tools_notify 在 module 顶层 import 时拿的引用，
        # monkeypatch 替换它
        monkeypatch.setattr(tn.subprocess, "run", proc)
        # hermes CLI 在 PATH 也要找得到
        monkeypatch.setattr(tn.shutil, "which", lambda _: "/usr/local/bin/hermes")
        _FakeProcess.last_call = None
        return proc
    return _setter


def test_notify_no_target_returns_clear_error(tmp_path: Path, monkeypatch):
    ctx = _FakeCtx(tmp_path)
    # 071931f 改 + hermes-pr.md §五 #4 完整迁移: notify_owner 改走
    # resolve_notify_target 3 级 fallback。stale state.json + ~/.hermes/auth.json
    # 真实配置会让 fallback 找到 feishu → 调 hermes send 卡住测试。
    # 显式把 fallback 列表设空,确认 needsBind=true 路径。
    from miloco_plugin_pkg import tools_notify as tn_local
    monkeypatch.setattr(tn_local, "_detect_im_platforms_simple", lambda: [])
    result = tn.notify_owner(ctx, "hello")
    assert result["ok"] is False
    assert result.get("needsBind") is True
    assert "miloco_notify_bind" in result.get("hint", "")


def test_notify_success(fake_hermes, tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    fake_hermes(returncode=0, payload={"success": True, "platform": "telegram", "chat_id": "-100xxx"})
    ctx = _FakeCtx(tmp_path)

    result = tn.notify_owner(ctx, "喝水提醒")

    assert result["ok"] is True
    assert result["platform"] == "telegram"
    assert result["chat_id"] == "-100xxx"
    # 验证 hermes send 命令参数
    call = _FakeProcess.last_call
    assert call is not None
    cmd = call["cmd"]
    assert cmd[0].endswith("hermes")
    assert cmd[1:3] == ["send", "--to"]
    assert cmd[3] == "telegram"
    assert cmd[4] == "--json"
    assert cmd[5] == "-q"
    assert "<miloco-notification>" in cmd[6]
    assert "喝水提醒" in cmd[6]


def test_notify_with_chat_id_target(fake_hermes, tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"deliver": {"target": "telegram:-1001234567890:17585"}}),
        encoding="utf-8",
    )
    fake_hermes(returncode=0, payload={"success": True, "platform": "telegram"})
    ctx = _FakeCtx(tmp_path)
    tn.notify_owner(ctx, "x")
    assert _FakeProcess.last_call["cmd"][3] == "telegram:-1001234567890:17585"


def test_notify_hermes_send_error_propagates(fake_hermes, tmp_path: Path):
    """hermes send exit=1 + payload.error='no route' → notify_owner 透传。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    fake_hermes(returncode=1, payload={"success": False, "error": "no route"})
    ctx = _FakeCtx(tmp_path)
    result = tn.notify_owner(ctx, "x")
    assert result["ok"] is False
    assert "no route" in result["error"]


def test_notify_hermes_send_runtime_error(fake_hermes, tmp_path: Path):
    """subprocess.run 抛 RuntimeError → notify_owner 返回明确错误，不裸抛。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    monkeypatch = fake_hermes.__self__ if hasattr(fake_hermes, "__self__") else None  # noqa
    # 直接 set 一个抛异常的函数
    def boom(cmd, **kwargs):
        raise RuntimeError("dispatch blew up")
    tn.subprocess.run = boom  # type: ignore[assignment]
    ctx = _FakeCtx(tmp_path)
    result = tn.notify_owner(ctx, "x")
    assert result["ok"] is False
    assert "dispatch blew up" in result["error"]


def test_notify_hermes_binary_missing(tmp_path: Path, monkeypatch):
    """hermes 不在 PATH → 明确错误，不抛。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    monkeypatch.setattr(tn.shutil, "which", lambda _: None)
    ctx = _FakeCtx(tmp_path)
    result = tn.notify_owner(ctx, "x")
    assert result["ok"] is False
    assert "找不到 hermes CLI" in result["error"]


def test_notify_hermes_send_timeout(tmp_path: Path, monkeypatch):
    """subprocess.run 抛 TimeoutExpired → 明确超时错误。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    monkeypatch.setattr(tn.shutil, "which", lambda _: "/usr/local/bin/hermes")

    def timeout_boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(tn.subprocess, "run", timeout_boom)
    ctx = _FakeCtx(tmp_path)
    result = tn.notify_owner(ctx, "x")
    assert result["ok"] is False
    assert "超时" in result["error"]


def test_notify_hermes_skipped(fake_hermes, tmp_path: Path):
    """hermes send 返 skipped=true（cron 重复发被去重）→ ok=True + skipped=True。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    fake_hermes(
        returncode=0,
        payload={"skipped": True, "reason": "cron_auto_delivery_duplicate_target"},
    )
    ctx = _FakeCtx(tmp_path)
    result = tn.notify_owner(ctx, "x")
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "cron_auto_delivery_duplicate_target"


# ---------- handler 包装 ----------

def test_handler_empty_message(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    handler = tn.make_im_push_handler(ctx)
    out = json.loads(handler({"message": ""}))
    assert out["ok"] is False
    assert "message" in out["error"]


def test_handler_no_state_json(tmp_path: Path, monkeypatch):
    """handler 包装的 notify_owner 在 needsBind 路径下返 ok=false。

    同 test_notify_no_target:monkeypatch 关掉 fallback,只走 needsBind 路径。
    """
    from miloco_plugin_pkg import tools_notify as tn_local

    ctx = _FakeCtx(tmp_path)
    monkeypatch.setattr(tn_local, "_detect_im_platforms_simple", lambda: [])
    handler = tn.make_im_push_handler(ctx)
    out = json.loads(handler({"message": "hi"}))
    assert out["ok"] is False
    assert out.get("needsBind") is True


def test_handler_delivers(fake_hermes, tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"deliver": {"target": "telegram"}}), encoding="utf-8")
    fake_hermes(returncode=0, payload={"success": True, "platform": "telegram"})
    ctx = _FakeCtx(tmp_path)
    handler = tn.make_im_push_handler(ctx)
    out = json.loads(handler({"message": "fire alarm"}))
    assert out["ok"] is True
    assert out["platform"] == "telegram"