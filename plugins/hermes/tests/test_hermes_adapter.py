"""HermesAdapter 单元测试(hermes-pr.md §五 #1+#4 配套)。

覆盖:
- URL 默认值(8642)+ env 覆盖
- API_SERVER_KEY 加载(env > ~/.hermes/.env > 空)
- _looks_like_overflow best-effort 关键词检测
- _extract_error_text 兼容 OpenAI-style envelope
- build_system(profile) 通过 _build_prepend/_build_append 拼装 system msg
- resolve_notify_target 3 级 fallback(state.json → scan ~/.hermes → needsBind)
- _map_session session_key → hermes session_id 映射

注: send_turn / read_trace_meta 端到端需要真 hermes 端 + 网络,不在单测里覆盖,
跑 diagnose 14 项 + 手动 hermes chat 验证链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# URL + API key 加载
# ---------------------------------------------------------------------------


def test_default_url_8642():
    """HERMES_API_URL 没设时默认 http://127.0.0.1:8642(hermes gateway 主端口)。

    注:之前默认 18100 是 v0.10.0 早期假设,实测 8642 才对。
    """
    from miloco_plugin_pkg.hermes_adapter.adapter import _DEFAULT_HERMES_URL

    assert _DEFAULT_HERMES_URL == "http://127.0.0.1:8642"


def test_default_api_key_from_env(monkeypatch):
    """API_SERVER_KEY env 优先(显式 set 时用 env)。"""
    monkeypatch.setenv("API_SERVER_KEY", "env-key-12345678")
    # _load_api_key() 是函数,每次调用读 env
    from miloco_plugin_pkg.hermes_adapter.adapter import _load_api_key

    assert _load_api_key() == "env-key-12345678"


def test_default_api_key_from_dotenv(monkeypatch, tmp_path: Path):
    """env 未设时 fallback 到 ~/.hermes/.env API_SERVER_KEY 行。

    重要:supervisor 由 launchd 起的 macOS 进程不继承用户 shell env,
    backend supervisor 重启时 MILOCO_HOME + API_SERVER_KEY 都从 ~/.hermes/.env 读。
    """
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    # fake ~/.hermes/.env
    fake_home = tmp_path
    env_file = fake_home / ".hermes" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "API_SERVER_KEY=dotenv-key-abc123\n"
        "OTHER_VAR=ignored\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    from miloco_plugin_pkg.hermes_adapter.adapter import _load_api_key

    assert _load_api_key() == "dotenv-key-abc123"


def test_default_api_key_empty_when_no_source(monkeypatch, tmp_path: Path):
    """env + ~/.hermes/.env 都没 API_SERVER_KEY 时,key 为空(不抛)。"""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # 不存在 .env

    from miloco_plugin_pkg.hermes_adapter.adapter import _load_api_key

    assert _load_api_key() == ""


# ---------------------------------------------------------------------------
# 溢出检测
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "err_text,expected",
    [
        ("context overflow at position 1000", True),
        ("Context Length Exceeded", True),
        ("maximum context length 8192 tokens", True),
        ("context window exceeded", True),
        ("prompt is too long", True),
        ("too many tokens", True),
        ("", False),
        (None, False),
        ("connection refused", False),
        ("rate limited", False),
    ],
)
def test_looks_like_overflow(err_text, expected):
    """best-effort 关键词识别:case-insensitive + 7 个 marker 都命中。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _looks_like_overflow

    assert _looks_like_overflow(err_text) is expected


# ---------------------------------------------------------------------------
# 错误文案抽取
# ---------------------------------------------------------------------------


def test_extract_error_text_json_envelope_dict():
    """OpenAI-style {error: {message: ...}} 抽取。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _extract_error_text

    class FakeResp:
        text = ""

        def json(self):
            return {"error": {"message": "rate limited"}}

    assert _extract_error_text(FakeResp()) == "rate limited"


def test_extract_error_text_top_level_message():
    """兜底:顶层 message / detail 字段。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _extract_error_text

    class FakeResp:
        text = ""

        def json(self):
            return {"message": "fallback msg"}

    assert _extract_error_text(FakeResp()) == "fallback msg"


def test_extract_error_text_non_json():
    """非 JSON 返回原文 text。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _extract_error_text

    class FakeResp:
        text = "raw html error"

        def json(self):
            raise json.JSONDecodeError("bad", "raw", 0)

    assert _extract_error_text(FakeResp()) == "raw html error"


def test_extract_error_text_empty():
    """空 dict 响应 → str({}) —— _extract_error_text 总会返 str(不返空)。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _extract_error_text

    class FakeResp:
        text = ""

        def json(self):
            return {}

    # dict 转 str 不为空,只测"不抛"
    out = _extract_error_text(FakeResp())
    assert isinstance(out, str)


def test_extract_error_text_returns_str():
    """任何响应都返 str(类型契约,便于拼接 error 消息)。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _extract_error_text

    class FakeResp:
        text = "raw"

        def json(self):
            return {}

    assert isinstance(_extract_error_text(FakeResp()), str)
    assert isinstance(_extract_error_text(FakeResp()), str)


# ---------------------------------------------------------------------------
# session mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_key,lane,expected",
    [
        ("agent:main:miloco", "miloco-interactive", "miloco:agent:main:miloco:miloco-interactive"),
        ("agent:main:miloco-rule", "miloco-rule", "miloco:agent:main:miloco-rule:miloco-rule"),
        ("agent:main:miloco-suggest", "miloco-suggest", "miloco:agent:main:miloco-suggest:miloco-suggest"),
    ],
)
def test_map_session_format(session_key, lane, expected):
    """_map_session(session_key, lane) 输出 miloco:{session_key}:{lane}。

    同 (session_key, lane) → 同一 hermes 会话,保证跨回合上下文连续。
    """
    from miloco_plugin_pkg.hermes_adapter.adapter import _map_session

    out = _map_session(session_key, lane)
    assert out == expected


def test_map_session_consistency():
    """同 (session_key, lane) → 同一 id(可作为 hermes X-Hermes-Session-Id 用)。"""
    from miloco_plugin_pkg.hermes_adapter.adapter import _map_session

    a = _map_session("agent:main:miloco", "miloco-interactive")
    b = _map_session("agent:main:miloco", "miloco-interactive")
    assert a == b


# ---------------------------------------------------------------------------
# resolve_notify_target 3 级 fallback(从 tools_notify.py 重新 import 测试)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_miloco_home(tmp_path: Path):
    """fake $MILOCO_HOME 给 tools_notify.resolve_notify_target 用。

    state.json 写 plugins/hermes 目录下(miloco-plugin/../state.json 或
    $HERMES_HOME/plugins/miloco/miloco-plugin/state.json)。
    """
    fake_home = tmp_path / "miloco"
    fake_home.mkdir()
    fake_hermes = tmp_path / "hermes"
    fake_hermes.mkdir()
    plugin_dir = fake_hermes / "plugins" / "miloco" / "miloco-plugin"
    plugin_dir.mkdir(parents=True)

    return {
        "MILOCO_HOME": fake_home,
        "HERMES_HOME": fake_hermes,
        "PLUGIN_DIR": plugin_dir,
        "STATE_FILE": plugin_dir / "state.json",
        "AUTH_FILE": fake_hermes / "auth.json",
    }


def test_resolve_notify_target_state_explicit(tmp_miloco_home):
    """state.json::deliver.target 显式 → 直接用,不扫 fallback。"""
    from miloco_plugin_pkg import tools_notify as tn

    paths = tmp_miloco_home
    paths["STATE_FILE"].write_text(
        json.dumps({"deliver": {"target": "feishu:oc_explicit"}}), encoding="utf-8"
    )

    with mock.patch.object(tn, "_state_path", return_value=paths["STATE_FILE"]):
        result = tn.resolve_notify_target(ctx=None)
    assert result["target"] == "feishu:oc_explicit"
    assert result["needsBind"] is False
    assert result["candidates"] == []  # 显式优先,不扫 fallback


def test_resolve_notify_target_fallback_to_home_channel(tmp_miloco_home):
    """state.json 无 target + plugin list 有 bot_token → 用 home channel。

    用 mock.patch 替换 _detect_im_platforms_simple(避免 monkeypatch Path.home
    在 classmethod 上不稳定)。
    """
    from miloco_plugin_pkg import tools_notify as tn

    paths = tmp_miloco_home
    paths["STATE_FILE"].write_text(json.dumps({}), encoding="utf-8")

    with mock.patch.object(tn, "_state_path", return_value=paths["STATE_FILE"]), \
         mock.patch.object(
             tn, "_detect_im_platforms_simple",
             return_value=["feishu", "telegram"],
         ):
        result = tn.resolve_notify_target(ctx=None)
    assert result["target"] in ("feishu", "telegram")
    assert result["needsBind"] is True  # 方案 A：有 fallback 时也走 needsBind，让第二回合带 bindHint 投递
    assert result["bindReason"] == "not_configured"
    assert "fallback" in result.get("hint", "").lower()
    assert "feishu" in result["candidates"]


def test_resolve_notify_target_needs_bind(tmp_miloco_home):
    """state.json 无 target + plugin list 空 → needsBind=true + hint。"""
    from miloco_plugin_pkg import tools_notify as tn

    paths = tmp_miloco_home
    paths["STATE_FILE"].write_text(json.dumps({}), encoding="utf-8")

    with mock.patch.object(tn, "_state_path", return_value=paths["STATE_FILE"]), \
         mock.patch.object(tn, "_detect_im_platforms_simple", return_value=[]):
        result = tn.resolve_notify_target(ctx=None)
    assert result["target"] is None
    assert result["needsBind"] is True
    assert "miloco_notify_bind" in result.get("hint", "")
    assert "action='switch'" in result.get("hint", "")


def test_resolve_notify_target_corrupt_state_json(tmp_miloco_home):
    """state.json 损坏 → 降级 fallback(不抛)。"""
    from miloco_plugin_pkg import tools_notify as tn

    paths = tmp_miloco_home
    paths["STATE_FILE"].write_text("{not valid json", encoding="utf-8")

    with mock.patch.object(tn, "_state_path", return_value=paths["STATE_FILE"]), \
         mock.patch.object(
             tn, "_detect_im_platforms_simple",
             return_value=["feishu"],
         ):
        result = tn.resolve_notify_target(ctx=None)
    # 损坏 → target 空 → fallback 到 home channel
    assert result["target"] == "feishu"


def test_resolve_notify_target_missing_state_file(tmp_miloco_home):
    """state.json 不存在 → fallback(不抛,load_state 返回 {})."""
    from miloco_plugin_pkg import tools_notify as tn

    paths = tmp_miloco_home
    # 不写 STATE_FILE(load_state 走 fallback 返回 {})

    with mock.patch.object(tn, "_state_path", return_value=paths["STATE_FILE"]), \
         mock.patch.object(
             tn, "_detect_im_platforms_simple",
             return_value=["telegram"],
         ):
        result = tn.resolve_notify_target(ctx=None)
    assert result["target"] == "telegram"


# ═══════════════════════════════════════════════════════════════════════════
# send_turn 传输层: 重试 / Idempotency-Key / client 生命周期
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_send_turn_connects_on_retry(monkeypatch):
    """ConnectError 前两次失败，第三次成功 → 调 3 次 client.post"""
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter, AdapterTransportError
    import httpx

    call_count = [0]
    async def fake_post(self, url, **kw):
        call_count[0] += 1
        if call_count[0] < 3:
            raise httpx.ConnectError("refused")
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", mock.AsyncMock())

    a = Adapter()
    ctx = _fake_ctx("test query")
    result = await a.send_turn(ctx)
    assert result.status == "ok"
    assert call_count[0] == 3


@pytest.mark.anyio
async def test_send_turn_exhausts_retries_raises(monkeypatch):
    """ConnectError 连抛 3 次 → AdapterTransportError"""
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter, AdapterTransportError
    import httpx

    async def fake_post(self, url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", mock.AsyncMock())

    a = Adapter()
    with pytest.raises(AdapterTransportError):
        await a.send_turn(_fake_ctx("test"))


@pytest.mark.anyio
async def test_send_turn_timeout_no_retry(monkeypatch):
    """TimeoutException → status="timeout", 只调 1 次"""
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter
    import httpx

    call_count = [0]
    async def fake_post(self, url, **kw):
        call_count[0] += 1
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", mock.AsyncMock())

    a = Adapter()
    result = await a.send_turn(_fake_ctx("timeout"))
    assert result.status == "timeout"
    assert call_count[0] == 1


@pytest.mark.anyio
async def test_send_turn_adds_idempotency_key(monkeypatch):
    """请求头包含 Idempotency-Key == ctx.trace_id"""
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter
    import httpx

    captured_headers = {}
    async def fake_post(self, url, headers=None, **kw):
        captured_headers.update(headers or {})
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", mock.AsyncMock())

    a = Adapter()
    ctx = _fake_ctx("ping", trace_id="tr-abc123")
    await a.send_turn(ctx)
    assert captured_headers.get("Idempotency-Key") == "tr-abc123"


@pytest.mark.anyio
async def test_send_turn_connect_timeout_raises(monkeypatch):
    """ConnectTimeout（TCP 握手超时，请求未达）→ AdapterTransportError，不是 timeout 状态。

    与 ReadTimeout（请求已到、turn 跑太久）区分：前者 dispatcher 该当传输错误丢 batch，
    不该误标 delivered=True 让 onboarding 漏发。
    """
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter, AdapterTransportError
    import httpx

    async def fake_post(self, url, **kw):
        raise httpx.ConnectTimeout("connect timeout")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", mock.AsyncMock())

    a = Adapter()
    with pytest.raises(AdapterTransportError):
        await a.send_turn(_fake_ctx("ping"))


@pytest.mark.anyio
async def test_send_turn_closes_client_on_every_exit(monkeypatch):
    """无论哪个出口，client.aclose 都被调 1 次"""
    from miloco_plugin_pkg.hermes_adapter.adapter import Adapter
    import httpx

    aclose_count = [0]
    async def fake_aclose(self):
        aclose_count[0] += 1

    async def fake_post(self, url, **kw):
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", fake_aclose)

    a = Adapter()
    await a.send_turn(_fake_ctx("ok"))
    assert aclose_count[0] == 1


def _fake_ctx(text: str, trace_id: str = "tr-test", lane: str = "miloco-interactive"):
    """构造 TurnContext duck-typed 对象"""
    ctx = mock.MagicMock()
    ctx.text = text
    ctx.trace_id = trace_id
    ctx.lane = lane
    ctx.session_key = "test:main:miloco"
    ctx.wait_timeout_ms = 1000
    ctx.profile = "minimal"
    ctx.extra = {}
    return ctx