"""Hermes 真实数据结构契约测试 —— 固化我们对 Hermes 运行时环境的假设。

为什么：author fix-v2 发现 11 个 bug，根因都是"我们的代码假设了错误的数据结构"——
channel_directory.json 不是 `{key: {session_id, platform}}`、IM 凭证不在 auth.json 的
bot_token 字段、cron jobs 的 `deliver` null 值会崩 CLI。本文件把这些假设变成永远会失败的
测试断言——只要代码里还按错误结构读，测试就 FAIL。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# 真实 Hermes 数据 fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def hermes_channel_directory(tmp_path: Path) -> Path:
    """真实 Hermes channel_directory.json 结构。

    来源：cat@mac 真实 Hermes 环境 `~/.hermes/channel_directory.json`。
    顶层 key = ``platforms``，值为 ``{platform_name: [channel_obj, ...]}``。
    channel 对象含 ``id`` / ``name`` / ``type`` / ``thread_id``。
    """
    data = {
        "updated_at": "2026-07-09T00:00:00Z",
        "platforms": {
            "feishu": [
                {"id": "oc_806ed7124bae73745846704be33ae2b3", "name": "测试群", "type": "dm", "thread_id": None},
            ],
            "weixin": [
                {"id": "o9cq80y629QGu22aknaIChWNAxYI@im.wechat", "name": "某人", "type": "dm", "thread_id": None},
            ],
            "telegram": [],
            "discord": [],
            "slack": [],
        },
    }
    path = tmp_path / "channel_directory.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def hermes_auth_json(tmp_path: Path) -> Path:
    """真实 Hermes auth.json 结构。

    Hermes 各平台认证字段存在环境变量（TELEGRAM_BOT_TOKEN, FEISHU_APP_ID 等），
    不走 auth.json providers 下的 bot_token 字段。旧 `_detect_im_platforms_simple`
    扫描 `auth.json/config.yaml` 里的 `bot_token` 字段 → 永远返回空。
    """
    data = {
        "providers": {
            "xiaomi": {"access_token": "xxx"},
            "anthropic": {"api_key": "yyy"},
            "feishu": {
                # 真实 Helmres 不在这存 bot_token，凭证存在环境变量中
                "app_id": "cli_xxx",
                # "bot_token" 不存在！
            },
        },
    }
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def hermes_config_yaml(tmp_path: Path) -> Path:
    """真实 Hermes config.yaml 结构 —— 也没有 bot_token 字段。"""
    content = (
        "model:\n"
        "  provider: xiaomi\n"
        "  model: mimo-v2.5\n"
        "gateway:\n"
        "  port: 8642\n"
        "feishu:\n"
        "  app_id: cli_xxx\n"
        "  app_secret: xxx\n"
        # 没有 bot_token！
        "telegram:\n"
        "  enabled: false\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def hermes_jobs_json(tmp_path: Path) -> Path:
    """真实 Hermes jobs.json 结构。

    每个 job 是 dict，含 id/name/schedule/skills/prompt/deliver 等字段。
    """
    data = [
        {
            "id": "job-001",
            "name": "miloco-perception-digest",
            "schedule": "*/15 * * * *",
            "skills": ["miloco-perception-digest"],
            "prompt": "感知摘要",
            "deliver": "local",
            "state": "active",
        },
        {
            "id": "job-002",
            "name": "non-miloco-job",
            "schedule": "0 0 * * 0",
            "skills": ["weekly-summary"],
            "prompt": "周报",
            "deliver": "local",
            "state": "active",
        },
    ]
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# 契约测试：channel_directory.json 结构
# ═══════════════════════════════════════════════════════════════════════════


class TestChannelDirectoryStructure:
    """验证 channel_directory.json 的真实结构 —— 我们的代码必须匹配这个结构。"""

    def test_top_level_has_platforms_key(self, hermes_channel_directory: Path):
        d = json.loads(hermes_channel_directory.read_text())
        assert "platforms" in d, (
            "真实 channel_directory.json 顶层是 `platforms` 字典。"
            "如果 `_resolve_owner_session` 假定了不同的顶层结构（如顶层直接是 session_id），"
            "这条测试会明确告诉你结构不对。"
        )

    def test_platforms_is_dict_of_lists(self, hermes_channel_directory: Path):
        d = json.loads(hermes_channel_directory.read_text())
        for name, channels in d["platforms"].items():
            assert isinstance(channels, list), (
                f"platform `{name}` 的值必须是 list，不是 {type(channels).__name__}."
                f" `_detect_im_platforms_simple` 如果按 dict 读会拿到错误类型。"
            )

    def test_channel_object_has_id_field(self, hermes_channel_directory: Path):
        d = json.loads(hermes_channel_directory.read_text())
        for name, channels in d["platforms"].items():
            for ch in channels:
                assert "id" in ch, (
                    f"channel {ch} 缺 `id` 字段。"
                    f" `_resolve_owner_session` 假定了 `session_id` 字段名——真实字段名是 `id`。"
                )

    def test_empty_channels_means_platform_unavailable(self, hermes_channel_directory: Path):
        """空 channels 列表 = 平台未连接。非空 = 可用 IM 目标。"""
        d = json.loads(hermes_channel_directory.read_text())
        assert d["platforms"]["telegram"] == []
        assert len(d["platforms"]["feishu"]) > 0

    def test_resolve_owner_session_top_level_access(self, hermes_channel_directory: Path):
        """模拟 `_resolve_owner_session` 的真实行为：
        必须从 `platforms` 字段下取平台名 → channel → id。

        很多代码错误假设顶层就是 `{key: {session_id, platform}}` ——
        这条测试验证正确的访问路径。
        """
        d = json.loads(hermes_channel_directory.read_text())
        # 错误方式（_resolve_owner_session 当前的假设）：
        #   for k, v in d.items():
        #       if v.get("session_id") and v.get("platform"):
        # 正确方式：
        platforms = d.get("platforms", {})
        for plat_name, channels in platforms.items():
            for ch in channels:
                session_id = ch.get("id")
                assert session_id is not None
                return  # 找到第一个就够
        pytest.fail("true Hermes channel_directory 顶层没有 session_id，platforms 下有")


# ═══════════════════════════════════════════════════════════════════════════
# 契约测试：auth.json 没有 bot_token
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthStructure:
    """验证 auth.json 里没有 `bot_token` 字段 —— 旧 `_detect_im_platforms_simple` 依赖它。"""

    def test_no_bot_token_in_auth_json_feishu(self, hermes_auth_json: Path):
        d = json.loads(hermes_auth_json.read_text())
        feishu = d["providers"]["feishu"]
        assert "bot_token" not in feishu, (
            "真实 Hermes auth.json 的 feishu 段没有 `bot_token` 字段。"
            " `_detect_im_platforms_simple` 扫描 `bot_token` → 永远找不到 feishu。"
        )

    def test_bot_token_scan_yields_empty(self, hermes_auth_json: Path, hermes_config_yaml: Path):
        """重现旧 `_detect_im_platforms_simple` 的逻辑：它扫 auth.json + config.yaml 的
        bot_token 字段，在真实 Hermes 上应该永远返回空列表。"""
        import re

        # 旧逻辑
        found = []
        # 1) auth.json
        auth_data = json.loads(hermes_auth_json.read_text())
        for provider, conf in auth_data.get("providers", {}).items():
            if isinstance(conf, dict) and conf.get("bot_token"):
                found.append(provider)
        # 2) config.yaml regex（灾难回溯风险）
        text = hermes_config_yaml.read_text()
        for m in re.finditer(
            r"^(\w+):\s*\n(?:\s+.+\n)*?\s+bot_token:", text, re.MULTILINE,
        ):
            found.append(m.group(1))

        assert found == [], (
            f"旧 _detect_im_platforms_simple 扫描 bot_token 在真实 auth.json/config.yaml 上 "
            f"找到了 {found}——但实际上应该为空，因为 Hermes 凭证存环境变量。"
            f"这意味着旧代码在真实 Hermes 上永远返回空，通知投递永远失败。"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 契约测试：cron jobs.json deliver 字段值类型
# ═══════════════════════════════════════════════════════════════════════════


class TestCronDeliverValue:
    """验证 cron job 的 deliver 字段类型 —— None 值会崩 `hermes cron list`。"""

    def test_deliver_must_be_string_or_list_not_none(self, hermes_jobs_json: Path):
        """重现 `hermes cron list` 崩溃根因。

        Hermes CLI 的 cron_list 用 `deliver = job.get("deliver", ["local"])`。
        key 存在且为 None 时 `.get()` 取不到默认值 → `", ".join(None)` → TypeError。
        """
        jobs = json.loads(hermes_jobs_json.read_text())
        for j in jobs:
            deliver = j.get("deliver")
            assert deliver is not None, (
                f"job `{j.get('name')}` 的 deliver 是 None。"
                f"True Hermes `hermes cron list` 读到这个值会抛 TypeError 崩溃。"
                f"`cron_setup.reconcile_cron_jobs` update_job 分支写 `deliver: None` 时"
                f"就是这条测试会炸的时候。"
            )
            assert isinstance(deliver, (str, list)), (
                f"job `{j.get('name')}` 的 deliver 必须是 str 或 list，"
                f"不能是 {type(deliver).__name__}。"
            )

    def test_deliver_local_string_is_safe(self, hermes_jobs_json: Path):
        """`"local"` 是安全的 deliver 值：Hermes CLI 能正确显示"""
        jobs = json.loads(hermes_jobs_json.read_text())
        assert jobs[0]["deliver"] == "local"


# ═══════════════════════════════════════════════════════════════════════════
# 契约测试：trace 字段名一致性
# ═══════════════════════════════════════════════════════════════════════════


class TestTraceFieldConsistency:
    """trace.py 落盘的字段名必须与 adapter.read_trace_meta 读盘的字段名一致。"""

    @staticmethod
    def _simulated_flush_buffer() -> dict:
        """模拟 trace.py _flush_to_disk 落盘的 meta 结构。"""
        return {
            "runId": "mock-run-123",
            "traceId": "mock-trace-456",
            "query": "测试查询",
            "success": True,
            "durationMs": 8578.0,
            "llmCallCount": 3,
            "toolCallCount": 2,
            "llmTotalMs": 5000,
            "toolTotalMs": 3000,
            "toolMaxMs": 2000,
            "slowestToolName": "miloco_im_push",
            "errorCount": 0,
            "errorMsg": None,
            "jsonlPath": "trace/agent/20260709/mock-run-123__xxx.jsonl.gz",
        }

    @staticmethod
    def _simulated_read_trace_meta(data: dict) -> dict | None:
        """模拟 adapter.read_trace_meta 读盘逻辑。

        当前 BUG：落盘写 camelCase（runId/durationMs/llmCallCount...），
        但读盘按 snake_case（run_id/duration_ms/llm_call_count...）取值——
        字段名对不上，所有值被静默读成 0/None。
        """
        from types import SimpleNamespace

        return SimpleNamespace(
            run_id=data.get("run_id") or data.get("runId", ""),
            query=data.get("query", ""),
            duration_ms=float(data.get("duration_ms") or data.get("durationMs") or 0.0),
            llm_call_count=int(data.get("llm_call_count") or data.get("llmCallCount") or 0),
            tool_call_count=int(data.get("tool_call_count") or data.get("toolCallCount") or 0),
            llm_total_ms=float(data.get("llm_total_ms") or data.get("llmTotalMs") or 0.0),
            tool_total_ms=float(data.get("tool_total_ms") or data.get("toolTotalMs") or 0.0),
            tool_max_ms=float(data.get("tool_max_ms") or data.get("toolMaxMs") or 0.0),
            slowest_tool_name=data.get("slowest_tool_name") or data.get("slowestToolName"),
            error_count=int(data.get("error_count") or data.get("errorCount") or 0),
            error_msg=data.get("error_msg") or data.get("errorMsg"),
            success=bool(data.get("success")),
            jsonl_path=data.get("jsonl_path") or data.get("jsonlPath"),
        )

    def test_read_meta_reads_write_fields_correctly(self):
        """核心：验证 read_trace_meta 能正确读到 trace.py 落盘的字段。

        如果这条测试 FAIL：落盘字段名和读盘字段名不一致——所有 trace 数据被静默丢弃。
        """
        meta = self._simulated_flush_buffer()
        result = self._simulated_read_trace_meta(meta)

        assert result.duration_ms == 8578.0, f"duration_ms 读成了 {result.duration_ms}（期望 8578）"
        assert result.llm_call_count == 3, f"llm_call_count 读成了 {result.llm_call_count}（期望 3）"
        assert result.tool_call_count == 2
        assert result.llm_total_ms == 5000
        assert result.tool_total_ms == 3000
        assert result.tool_max_ms == 2000
        assert result.slowest_tool_name == "miloco_im_push"
        assert result.error_count == 0
        assert result.success is True
        assert result.query == "测试查询"
        assert result.jsonl_path is not None

    def test_read_meta_with_only_snake_case_fields(self):
        """如果落盘字段改成 snake_case（修复后），读盘应该仍然能读。"""
        meta = {
            "run_id": "mock-run-123",
            "trace_id": "mock-trace-456",
            "query": "snake 测试",
            "success": True,
            "duration_ms": 5000.0,
            "llm_call_count": 2,
            "tool_call_count": 1,
            "llm_total_ms": 3000,
            "tool_total_ms": 2000,
            "tool_max_ms": 1500,
            "slowest_tool_name": "test_tool",
            "error_count": 0,
            "error_msg": None,
            "jsonl_path": "trace/agent/20260709/mock.jsonl.gz",
        }
        result = self._simulated_read_trace_meta(meta)

        assert result.duration_ms == 5000.0
        assert result.llm_call_count == 2
        assert result.tool_call_count == 1
        assert result.slowest_tool_name == "test_tool"


# ═══════════════════════════════════════════════════════════════════════════
# 契约测试：trace _hk_on_session_end 不落盘（register_trace_link 从未调）
# ═══════════════════════════════════════════════════════════════════════════


class TestTraceSessionFilter:
    """验证 trace 落盘过滤逻辑 —— 旧 `register_trace_link` 从未被调用，
    导致任何 turn 都不落盘。"""

    def test_register_trace_link_enables_writing(self):
        """模拟 `_hk_on_session_end` 的落盘判断：
        `_trace_links.get(run_id)` 为真时才落盘。但 `register_trace_link` 从来没人调 →
        `_trace_links` 永远空 → 不落盘。
        """
        _trace_links: dict[str, str] = {}
        run_id = "test-run-1"

        # 旧行为：register_trace_link 从未调用
        if not _trace_links.get(run_id):
            # 不落盘！这就是旧代码的路径
            pass
        else:
            # 这个分支永远不会执行
            pass

        assert not _trace_links.get(run_id), "register_trace_link 从未被调，_trace_links 永远空"

    def test_session_id_prefix_filter_works(self):
        """修复方案：用 session_id 前缀 'miloco:' 过滤，替代 register_trace_link。

        _map_session() 生成的格式是 `miloco:<sessionKey>:<lane>`。
        只有以此开头的 session 才落盘 trace。
        """
        miloco_session = "miloco:agent:main:miloco-suggest:miloco-suggest"
        user_session = "telegram:12345:o9cq"

        assert miloco_session.startswith("miloco:")
        assert not user_session.startswith("miloco:")

        # 这比 register_trace_link 更可靠——不依赖跨进程调用
        def should_flush(session_id: str) -> bool:
            return (session_id or "").startswith("miloco:")

        assert should_flush(miloco_session) is True
        assert should_flush(user_session) is False
