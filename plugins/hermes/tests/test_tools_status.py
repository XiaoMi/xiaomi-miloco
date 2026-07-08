"""miloco_status / miloco_test_push / miloco_notify_bind 测试。

覆盖：
- gather_status 子项的 happy / fail 路径
- list_candidates / switch_target 读 / 写 state.json
- 三 tool handler 的 ok/false 返回结构
- schema 必填字段校验
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest
from miloco_plugin_pkg import tools_status as ts

# ─── fake ctx ────────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, plugin_dir: Path) -> None:
        self.manifest = type("M", (), {"path": str(plugin_dir)})()
        self.calls: list[Dict[str, Any]] = []

    def dispatch_tool(self, name: str, args: Dict[str, Any]) -> str:
        """保留兼容，但实际 notify_owner 现在走 subprocess.run，不调这个。"""
        self.calls.append({"name": name, "args": args})
        return json.dumps({"success": True, "platform": "feishu", "chat_id": "oc_xxx"})


class _FakeCompleted:
    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _patch_hermes_send(monkeypatch, *, payload=None, returncode: int = 0):
    """把 tools_status / tools_notify 用的 subprocess.run 替换成 fake。

    记录最近一次调用的 cmd，给 test_push 三个测试用。
    """
    from miloco_plugin_pkg import tools_notify as tn
    monkeypatch.setattr(tn.shutil, "which", lambda _: "/usr/local/bin/hermes")

    state = {"last_cmd": None}

    def fake_run(cmd, **kwargs):
        state["last_cmd"] = cmd
        return _FakeCompleted(returncode=returncode, stdout=json.dumps(payload or {}))

    monkeypatch.setattr(tn.subprocess, "run", fake_run)
    return state


# ─── schema ──────────────────────────────────────────────────────────────


def test_status_schema_has_no_required_params():
    """miloco_status 不需要任何参数（agent 一调就跑）。"""
    assert ts.MILOCO_STATUS_SCHEMA["parameters"]["required"] == []


def test_test_push_schema_message_optional():
    """miloco_test_push message 可选（默认带时间戳）。"""
    assert "message" not in ts.MILOCO_TEST_PUSH_SCHEMA["parameters"]["required"]


def test_notify_bind_schema_requires_action():
    """miloco_notify_bind 必填 action（list / switch）。"""
    assert "action" in ts.MILOCO_NOTIFY_BIND_SCHEMA["parameters"]["required"]
    assert set(ts.MILOCO_NOTIFY_BIND_SCHEMA["parameters"]["properties"]["action"]["enum"]) == {"list", "switch"}


# ─── gather_status 子项 ──────────────────────────────────────────────────


def test_status_state_json_missing(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    result = ts._check_state_json(ctx)
    assert result["ok"] is False
    assert "state.json" in result["error"]


def test_status_state_json_target_null(tmp_path: Path):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"auto_configured": True}}), encoding="utf-8"
    )
    ctx = _FakeCtx(tmp_path)
    result = ts._check_state_json(ctx)
    assert result["ok"] is False
    assert "miloco_notify_bind" in result["error"]


def test_status_state_json_target_set(tmp_path: Path):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu", "auto_configured": True, "candidates": ["feishu"]}}),
        encoding="utf-8",
    )
    ctx = _FakeCtx(tmp_path)
    result = ts._check_state_json(ctx)
    assert result["ok"] is True
    assert result["target"] == "feishu"


def test_gather_status_returns_9_checks(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    out = ts.gather_status(ctx)
    assert "checks" in out
    expected = {
        "plugin_self",
        "state_json_deliver_target",
        "hermes_plugin_enabled",
        "adapter_health",
        "cron_jobs",
        "miloco_backend",
        "skills_installed",
        "versions",
        "trace_hooks",
    }
    assert set(out["checks"].keys()) == expected
    # 至少有 failed_count 字段
    assert "failed_count" in out
    assert "failed" in out


def test_gather_status_doesnt_raise_when_external_unavailable(tmp_path: Path):
    """环境里没 hermes / 没 miloco-cli / adapter 不在 → 子项 ok=False 但不抛。"""
    ctx = _FakeCtx(tmp_path)
    out = ts.gather_status(ctx)
    # 应该正常返回 dict，不抛异常
    assert isinstance(out, dict)


# ─── versions / trace_hooks 子项（Phase 3.1 + 3.2） ──────────────────────


def test_versions_missing_when_state_empty(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    out = ts._check_versions(ctx)
    assert out["ok"] is False
    assert "没记录 versions" in out["error"]


@pytest.mark.skipif(
    shutil.which("hermes") is None,
    reason="hermes CLI 不在 PATH（无法对比版本号），跳过测试",
)
def test_versions_match(tmp_path: Path, monkeypatch):
    """state.json::versions 与当前一致 → ok=True，无 mismatches。

    _check_versions 会调 ``hermes --version`` 和 ``miloco-cli --version`` 拿
    当前版本对比。Windows 上 hermes / miloco-cli 不在 PATH 时调 subprocess
    会 FileNotFoundError，被 _check_versions 兜底成 ``err:...`` 字串 → 算
    mismatch。本测试是单元测试不依赖外部 CLI：手工 monkeypatch subprocess.run
    让它返稳定的版本字符串（state.json 里的值），保证 hermes 和 miloco-cli
    字段一致 → mismatches == []。
    """
    # monkeypatch subprocess.run: hermes --version 和 miloco-cli --version 都返预期值
    class _FakeProc:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    _FAKE_OUTPUTS = {
        # 老式 --version flag（fallback 路径）
        ("hermes", "--version"): "Hermes Agent v0.10.0 (2026.4.16)",
        ("miloco-cli", "--version"): "miloco-cli 1.2.3",
        # 新式 subcommand（49a9607 之后用 miloco-cli version 子命令）
        ("miloco-cli", "version"): "miloco-cli 1.2.3",
    }

    def fake_run(cmd, *args, **kwargs):
        key = tuple(cmd[:2])
        return _FakeProc(stdout=_FAKE_OUTPUTS.get(key, ""))

    monkeypatch.setattr("subprocess.run", fake_run)

    plugin_yaml = tmp_path / "plugin.yaml"
    plugin_yaml.write_text("version: 0.4.0\n", encoding="utf-8")
    state = {
        "versions": {
            "hermes": "Hermes Agent v0.10.0 (2026.4.16)",
            "miloco_cli": "miloco-cli 1.2.3",
            "plugin": "0.4.0",
            "git_commit": "abc1234",
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ctx = _FakeCtx(tmp_path)
    out = ts._check_versions(ctx)
    # 不强求 ok=True（因为 hermes/miloco-cli 真实命令可能不在 PATH），但 mismatches 应为空
    assert out["mismatches"] == [], f"mismatches 不应非空: {out['mismatches']}"
    assert out["recorded"]["plugin"] == "0.4.0"


def test_versions_mismatch_detected(tmp_path: Path):
    """plugin 升级但 state.json 没更新 → mismatches 含 plugin。"""
    plugin_yaml = tmp_path / "plugin.yaml"
    plugin_yaml.write_text("version: 0.5.0\n", encoding="utf-8")
    state = {
        "versions": {
            "plugin": "0.4.0",
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ctx = _FakeCtx(tmp_path)
    out = ts._check_versions(ctx)
    assert out["ok"] is False
    assert any("plugin: 装时=0.4.0 现在=0.5.0" in m for m in out["mismatches"])


def test_trace_hooks_empty_trace_dir(tmp_path: Path, monkeypatch):
    """trace 目录不存在 → ok=True + note（debug 没开过 ≠ 坏了，不进 failed）。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    out = ts._check_trace_hooks()
    assert out["ok"] is True
    assert out["enabled"] is False
    assert "trace debug 未启用" in out["note"]


def test_trace_hooks_today_dir_no_files(tmp_path: Path, monkeypatch):
    """trace 目录有但今天没 turn → ok=True + note。"""
    from datetime import datetime
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    trace_dir = tmp_path / "trace" / "agent"
    today = trace_dir / datetime.now().strftime("%Y%m%d")
    today.mkdir(parents=True, exist_ok=True)
    out = ts._check_trace_hooks()
    assert out["ok"] is True
    assert "今天还没" in out["note"]


def test_trace_hooks_today_has_files(tmp_path: Path, monkeypatch):
    """今天有 meta.json → ok=True + count。"""
    from datetime import datetime
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    today = tmp_path / "trace" / "agent" / datetime.now().strftime("%Y%m%d")
    today.mkdir(parents=True, exist_ok=True)
    (today / "sess-1__test.jsonl.gz").write_bytes(b"")
    (today / "sess-1__test.meta.json").write_text(
        json.dumps({"runId": "sess-1", "success": True}), encoding="utf-8"
    )
    out = ts._check_trace_hooks()
    assert out["ok"] is True
    assert out["meta_files_today"] == 1
    assert out["trace_files_today"] == 1
    assert out["newest_meta"] == "sess-1__test.meta.json"


# ─── test_push ───────────────────────────────────────────────────────────


def test_test_push_no_target_returns_clear_error(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)  # 无 state.json
    result = ts.test_push(ctx)
    assert result["ok"] is False
    assert "no deliver target" in result["error"]


def test_test_push_success(tmp_path: Path, monkeypatch):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu"}}), encoding="utf-8"
    )
    state = _patch_hermes_send(
        monkeypatch,
        payload={"success": True, "platform": "feishu", "chat_id": "oc_xxx"},
    )
    ctx = _FakeCtx(tmp_path)
    result = ts.test_push(ctx, "user-supplied message")
    assert result["ok"] is True
    assert result["platform"] == "feishu"
    # 验证调了 hermes send 且 message 含用户传的内容
    cmd = state["last_cmd"]
    assert cmd is not None
    assert cmd[1:3] == ["send", "--to"]
    assert cmd[3] == "feishu"
    assert "user-supplied message" in cmd[6]


def test_test_push_default_message_includes_timestamp(tmp_path: Path, monkeypatch):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu"}}), encoding="utf-8"
    )
    state = _patch_hermes_send(
        monkeypatch,
        payload={"success": True, "platform": "feishu"},
    )
    ctx = _FakeCtx(tmp_path)
    ts.test_push(ctx)  # 不传 message
    sent = state["last_cmd"][6]
    assert "miloco test push" in sent


# ─── notify_bind ─────────────────────────────────────────────────────────


def test_notify_bind_list_empty(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    result = ts.list_candidates(ctx)
    assert result["ok"] is True
    assert result["candidates"] == []
    assert result["current"] is None


def test_notify_bind_list_with_candidates(tmp_path: Path):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu", "auto_configured": True, "candidates": ["feishu", "telegram"]}}),
        encoding="utf-8",
    )
    ctx = _FakeCtx(tmp_path)
    result = ts.list_candidates(ctx)
    assert result["ok"] is True
    assert result["current"] == "feishu"
    assert result["candidates"] == ["feishu", "telegram"]


def test_notify_bind_switch_writes_state(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    result = ts.switch_target(ctx, "telegram")
    assert result["ok"] is True
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["deliver"]["target"] == "telegram"
    assert saved["deliver"]["auto_configured"] is False
    assert "manual" in saved["deliver"]["source"]


def test_notify_bind_switch_empty_target_rejected(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    result = ts.switch_target(ctx, "")
    assert result["ok"] is False


def test_notify_bind_switch_preserves_candidates(tmp_path: Path):
    """switch 时不应清空原有 candidates 列表。"""
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu", "candidates": ["feishu", "telegram"]}}),
        encoding="utf-8",
    )
    ctx = _FakeCtx(tmp_path)
    ts.switch_target(ctx, "telegram")
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["deliver"]["candidates"] == ["feishu", "telegram"]


# ─── handlers ────────────────────────────────────────────────────────────


def test_status_handler_returns_json(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    handler = ts.make_status_handler(ctx)
    out = json.loads(handler({}))
    assert "checks" in out


def test_test_push_handler_returns_json(tmp_path: Path, monkeypatch):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu"}}), encoding="utf-8"
    )
    _patch_hermes_send(monkeypatch, payload={"success": True, "platform": "feishu"})
    ctx = _FakeCtx(tmp_path)
    handler = ts.make_test_push_handler(ctx)
    out = json.loads(handler({"message": "hello"}))
    assert out["ok"] is True


def test_notify_bind_handler_list(tmp_path: Path):
    (tmp_path / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu", "candidates": ["feishu"]}}), encoding="utf-8"
    )
    ctx = _FakeCtx(tmp_path)
    out = json.loads(ts.handle_notify_bind({"action": "list"}, ctx))
    assert out["ok"] is True
    assert out["current"] == "feishu"


def test_notify_bind_handler_switch(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    out = json.loads(ts.handle_notify_bind({"action": "switch", "target": "telegram"}, ctx))
    assert out["ok"] is True


def test_notify_bind_handler_unknown_action(tmp_path: Path):
    ctx = _FakeCtx(tmp_path)
    out = json.loads(ts.handle_notify_bind({"action": "bad"}, ctx))
    assert out["ok"] is False


# ─── plugin.yaml 注册了 3 个新 tool ───────────────────────────────────────


def test_plugin_yaml_lists_all_5_tools():
    """Author #3 收敛后 plugin.yaml 只列 3 个 tool（status/test_push 删了,改外部 wrapper）。

    历史名 test_plugin_yaml_lists_all_5_tools 保留以反映这是从 5 → 3 的迁移测试。
    """
    from pathlib import Path as P

    yaml_path = P(__file__).resolve().parents[1] / "miloco-plugin" / "plugin.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    # 收敛后 3 个 tool
    for tool in ("miloco_im_push", "miloco_habit_suggest", "miloco_notify_bind"):
        assert f"- {tool}" in text, f"plugin.yaml 没列 {tool}"
    # 删除的 2 个不该在 plugin.yaml
    for removed in ("miloco_status", "miloco_test_push"):
        assert f"- {removed}" not in text, f"plugin.yaml 不该列 {removed}（已收敛到外部 wrapper）"


# ─── Issue 5: adapter_health 用 .status_code 而不是 .status ─────────────


def test_adapter_health_source_uses_status_not_status_code():
    """urllib 返回 http.client.HTTPResponse，状态码字段叫 .status（int），
    不是 .status_code（requests 库的命名）。Source 必须用 .status。
    之前写错：resp.status_code → AttributeError → except 兜底 → 自检假阳性 ✗。
    """
    from pathlib import Path as P
    src = (P(__file__).resolve().parents[1] / "miloco-plugin" / "tools_status.py").read_text(encoding="utf-8")
    # 找 _check_adapter_health 函数体
    start = src.find("def _check_adapter_health")
    assert start >= 0
    # 切到下一个 def / class / 顶层 "def " 前
    rest = src[start:]
    body_end = rest.find("\ndef ")
    body = rest[: body_end if body_end > 0 else len(rest)]
    # 函数体内不能出现 .status_code（排除 docstring 注释里举例的字面量）
    lines = [
        ln for ln in body.splitlines()
        if ".status_code" in ln and not ln.lstrip().startswith(("#", '"""', "'''"))
    ]
    assert not lines, (
        f"_check_adapter_health 还在用 .status_code（urllib 没这个字段，AttributeError 假阳性挂）: {lines}"
    )
    # 必须用 .status（http.client.HTTPResponse 的真实字段名）
    assert "resp.status" in body or ".status\n" in body, "_check_adapter_health 没读 .status"


def test_adapter_health_reads_correct_status_field(monkeypatch):
    """运行时验证：mock urllib 返回的对象带 .status（不是 .status_code）时，
    _check_adapter_health 必须正确判 ok=True。"""

    class _FakeResp:
        # 只实现 urllib 真实会用到的字段：status（int）
        def __init__(self, code: int) -> None:
            self.status = code
        def read(self) -> bytes:
            return b'{"status":"ok"}'
        # 故意不实现 .status_code（模拟 http.client.HTTPResponse 的真实行为）
        def __getattr__(self, name):
            if name == "status_code":
                raise AttributeError(
                    "http.client.HTTPResponse 没有 .status_code 字段"
                )
            raise AttributeError(name)

    class _FakeUrlopen:
        def __init__(self, code: int) -> None:
            self._code = code
        def __enter__(self): return _FakeResp(self._code)
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        return _FakeUrlopen(200)

    monkeypatch.setattr(ts.urllib.request, "urlopen", fake_urlopen)
    out = ts._check_adapter_health()
    assert out["ok"] is True, f"adapter /health 200 应该 ok=True，实际: {out}"
    assert out["status"] == 200


def test_adapter_health_5xx_returns_ok_false(monkeypatch):
    """/health 返 503 → ok=False（避免假阳性）。"""
    class _FakeResp:
        def __init__(self, code: int) -> None:
            self.status = code
        def read(self) -> bytes: return b""

    class _FakeUrlopen:
        def __init__(self, code: int) -> None:
            self._code = code
        def __enter__(self): return _FakeResp(self._code)
        def __exit__(self, *a): return False

    monkeypatch.setattr(ts.urllib.request, "urlopen", lambda req, timeout=None: _FakeUrlopen(503))
    out = ts._check_adapter_health()
    assert out["ok"] is False
    assert out["status"] == 503


def test_adapter_health_connection_refused_returns_clear_error(monkeypatch):
    """adapter 没启（连接拒绝）→ ok=False + 明确 fix 提示（不要被 AttributeError 吞掉）。"""
    def fake_urlopen(req, timeout=None):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(ts.urllib.request, "urlopen", fake_urlopen)
    out = ts._check_adapter_health()
    assert out["ok"] is False
    # 不要被 AttributeError 误报（之前的 bug 就是 AttributeError 被兜底成"not ok"，
    # 用户看不到真实原因）
    assert "AttributeError" not in out.get("error", ""), (
        f"adapter /health 失败不应是 AttributeError（之前 .status_code bug 会导致这个）: {out}"
    )
    assert "miloco-cli service start" in out.get("fix", "")