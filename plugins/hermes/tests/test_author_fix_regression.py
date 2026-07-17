"""针对 author 发现的 11 个 bug 的回归测试。

每个测试都是：用真实 Hermes 数据跑我们的代码 → 断言正确行为。
如果某条测试 FAIL，表示对应的 author bug 在我们代码里仍然存在。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from miloco_plugin_pkg import tools_notify as tn

# ═══════════════════════════════════════════════════════════════════════════
# Fixtures —— 真实路径的 Hermes 数据目录
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def real_hermes_home(tmp_path: Path, monkeypatch) -> Path:
    """模拟真实 Hermes home 目录含 channel_directory.json 和 cron 数据。"""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setattr(tn.Path, "home", lambda: tmp_path)
    # 也 patch _detect_im_platforms_simple 里的 Path.home()
    return h


def _write_channel_directory(home: Path):
    data = {
        "updated_at": "2026-07-09T00:00:00Z",
        "platforms": {
            "feishu": [{"id": "oc_806ed7124bae73745846704be33ae2b3", "name": "测试", "type": "dm", "thread_id": None}],
            "weixin": [{"id": "o9cq80y629QGu22aknaIChWNAxYI@im.wechat", "name": "某人", "type": "dm", "thread_id": None}],
            "telegram": [],
            "discord": [],
            "slack": [],
        },
    }
    (home / "channel_directory.json").write_text(json.dumps(data), encoding="utf-8")


def _write_auth(home: Path):
    """写 auth.json —— 按真实 Hermes 格式，没有 bot_token。"""
    data = {
        "providers": {
            "feishu": {"app_id": "cli_xxx", "app_secret": "yyy"},
            "telegram": {"enabled": False},
        },
    }
    (home / "auth.json").write_text(json.dumps(data), encoding="utf-8")


def _write_config_yaml(home: Path):
    """写 config.yaml —— 同真实格式，没有 bot_token。"""
    (home / "config.yaml").write_text(
        "model:\n  provider: xiaomi\nfeishu:\n  app_id: cli_xxx\n  app_secret: xxx\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bug 1+2: _detect_im_platforms_simple 和 _resolve_owner_session 读错结构
# ═══════════════════════════════════════════════════════════════════════════


class TestIMDetectionWithRealHermes:
    """验证 IM 探测使用正确数据源。"""

    def test_detect_with_channel_directory(self, real_hermes_home: Path, monkeypatch):
        """用真实 channel_directory.json 结构测试 IM 探测。

        正确行为：读到 feishu 和 weixin（两个平台有 channel）。
        """
        _write_channel_directory(real_hermes_home)
        _write_auth(real_hermes_home)
        _write_config_yaml(real_hermes_home)

        # 新 _detect_im_platforms_simple：读 channel_directory.json → 有 channel 的平台
        result = tn._detect_im_platforms_simple()

        # feishu 和 weixin 有非空 channel → 应该在列表中
        assert "feishu" in result, (
            f"_detect_im_platforms_simple 返回 {result}——"
            f"feishu 有 channel 但没被探测到"
        )
        assert "weixin" in result
        # telegram/discord/slack 是空列表 → 不在结果中
        assert "telegram" not in result
        assert "discord" not in result

    def test_resolve_notify_target_with_real_data(self, real_hermes_home: Path, monkeypatch):
        """在没有 state.json 时，fallback 应该能探测到有 channel 的平台。"""
        _write_channel_directory(real_hermes_home)
        _write_auth(real_hermes_home)

        # 模拟无 state.json 的 ctx
        class FakeCtx:
            class Manifest:
                path = str(real_hermes_home)
            manifest = Manifest()
        ctx = FakeCtx()

        result = tn.resolve_notify_target(ctx)
        # 方案 A：有 fallback 但未显式配时也返回 needsBind=True + target，让 M2 bindHint 协议可投递
        assert result.get("needsBind") is True
        assert result.get("target") == "feishu"
        assert result.get("bindReason") == "not_configured"
        assert result.get("target") is not None


class TestResolveOwnerSessionWithRealData:
    """验证 _resolve_owner_session 使用正确结构。"""

    def test_resolve_owner_session_finds_first_channel(self, tmp_path: Path, monkeypatch):
        """测试 adapter._resolve_owner_session 的正确行为。

        旧代码错误假设：channel_directory.json 顶层是 `{key: {session_id, platform}}`。
        真实结构：`{platforms: {name: [{id, name, type, thread_id}]}}`。
        """
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        data = {
            "platforms": {
                "feishu": [{"id": "oc_test123", "name": "测试群", "type": "dm", "thread_id": None}],
                "weixin": [{"id": "wx_test456", "name": "某人", "type": "dm", "thread_id": None}],
            },
        }
        (hermes / "channel_directory.json").write_text(json.dumps(data), encoding="utf-8")

        # 通过 monkeypatch 让 _resolve_owner_session 读我们的 tmp path
        monkeypatch.setattr("miloco_plugin_pkg.tools_notify.Path.home", lambda: tmp_path)

        # 导入 adapter 模块测试
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "miloco-plugin" / "hermes_adapter"))
        try:
            from adapter import _resolve_owner_session
            session, platform = _resolve_owner_session()
            # 旧代码：返回 (None, None)因为找错了字段
            # 新代码：应该返回第一个有 channel 的平台的 id 和平台名
            assert session is not None, (
                "_resolve_owner_session 在真实 channel_directory 上返回 None。"
                "旧代码找 `session_id` 字段——真实结构用的是 `id`。"
            )
            assert platform is not None
        finally:
            sys.path.pop(0)


# ═══════════════════════════════════════════════════════════════════════════
# Bug 3: trace 落盘字段名不一致
# ═══════════════════════════════════════════════════════════════════════════


class TestTraceFieldAlignment:
    """验证 trace.py 落盘与 adapter.read_trace_meta 读盘的字段名一致性。"""

    def test_trace_write_fields_are_readable(self, tmp_path: Path, monkeypatch):
        """端到端：写 trace → 读 trace，验证字段值不丢失。"""
        from miloco_plugin_pkg import trace as tr

        monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
        monkeypatch.setattr(tr, "_today_dir", lambda: tmp_path)
        t = [1000000]
        monkeypatch.setattr(tr, "_now_ms", lambda: t[0])
        monkeypatch.setattr(tr, "_now_iso", lambda: "2026-07-09T00:00:00Z")

        run_id = "end-to-end-test-run"
        tr.register_trace_link(run_id, "trace-e2e")

        state = tr._get_or_init(run_id)
        state.query = "端到端测试查询"
        tr._record(run_id, "pre_llm_call", {"model": "test", "platform": "test"})
        tr._record(run_id, "post_llm_call", {"model": "test", "platform": "test", "durationMs": 5000})
        tr._record(run_id, "post_tool_call", {"toolName": "test_tool", "durationMs": 1000})

        t[0] = 2000000  # advance time for done_at

        jsonl_path = tr._flush_to_disk(run_id, state, final_success=True)
        assert jsonl_path is not None, "trace 落盘失败——register_trace_link 可能没生效"

        # 现在读盘——模拟 adapter.read_trace_meta 逻辑
        from pathlib import Path
        candidates = sorted(
            Path(tmp_path).rglob(f"*{run_id}*.meta.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        assert candidates, "meta.json 没有生成"

        data = json.loads(candidates[0].read_text(encoding="utf-8"))

        # 关键断言：字段名对齐
        assert data.get("query") == "端到端测试查询"
        assert data.get("success") is True
        assert data.get("llm_call_count") == 1
        assert data.get("tool_call_count") == 1
        assert data.get("llm_total_ms") == 5000
        assert data.get("tool_total_ms") == 1000
        assert data.get("slowest_tool_name") == "test_tool"
        assert data.get("jsonl_path") is not None
        assert data.get("started_at") is not None
        assert data.get("done_at") is not None

        # duration = done_at - started_at (都是 ms 时间戳)
        dur = data["done_at"] - data["started_at"]
        assert dur > 0, f"duration 是 0——done_at={data['done_at']}, started_at={data['started_at']}"


# ═══════════════════════════════════════════════════════════════════════════
# Bug 5: register() 同步阻塞
# ═══════════════════════════════════════════════════════════════════════════


def test_register_uses_thread_not_blocking():
    """验证 register() 里 subprocess.run 在 daemon 线程中执行。

    Hermes register() 是同步调用——必须把 subprocess.run(timeout=30) 移到
    daemon 线程，不然会阻塞 Hermes 启动。
    """
    # 直接读源码文件（conftest 的 importlib 加载没有 __file__ 属性）
    init_path = Path(__file__).resolve().parent.parent / "miloco-plugin" / "__init__.py"
    init_src = init_path.read_text(encoding="utf-8")
    # 修复后：subprocess.run 必须在 threading.Thread 内
    assert "threading.Thread" in init_src, (
        "register() 里没有 threading.Thread——subprocess.run 会阻塞 Hermes 启动 30s"
    )
    assert "daemon=True" in init_src, (
        "threading.Thread 必须设 daemon=True，否则会阻止 Hermes 退出"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bug 9: tools_status 死代码（test_push 已被删）
# ═══════════════════════════════════════════════════════════════════════════


def test_make_test_push_handler_would_nameerror():
    """`make_test_push_handler` 引用 `test_push`——但 test_push 已被删，
    只要被调用就是 NameError。"""
    from miloco_plugin_pkg import tools_status as ts

    # make_test_push_handler 存在但内部引用 test_push
    assert hasattr(ts, "make_test_push_handler"), "确认函数存在"

    # 检查 register() 是否注册了这个 handler
    # 如果 __init__.py 的 register() 没注册它，那它是死代码但无害
    # 如果注册了——那调用就 NameError
    init_path = Path(__file__).resolve().parent.parent / "miloco-plugin" / "__init__.py"
    init_src = init_path.read_text(encoding="utf-8")
    assert "make_test_push_handler" not in init_src, (
        "make_test_push_handler 在 register() 里被注册了——"
        "一旦被调用就是 NameError（test_push 已删）。"
        "要么删掉这个函数，要么删掉注册调用。"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bug 10: max_send_turn_latency_s 已从契约中删除
# 原因: onboarding_trigger 硬引 WebhookAdapter 常量而非调 adapter 方法，
# 全仓无真实调用方——不是契约只是 adapter 内部预留。从 ABC 和 adapter 移除。
# ═══════════════════════════════════════════════════════════════════════════
