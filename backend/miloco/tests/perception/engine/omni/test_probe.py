"""probe.py 单元测试。用 monkeypatch 替换 httpx.AsyncClient 走 fake 响应。"""

from __future__ import annotations

import httpx
from miloco.perception.engine.omni import probe


class _FakeResp:
    def __init__(self, status_code: int, json_data: object | None = None, text: str = ""):
        self.status_code = status_code
        self._json: object = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _fake_async_client(
    resp: _FakeResp | None = None,
    *,
    exc: Exception | None = None,
    get_resp: _FakeResp | None = None,
    post_resp: _FakeResp | None = None,
):
    g = get_resp if get_resp is not None else resp
    p = post_resp if post_resp is not None else resp

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if exc:
                raise exc
            return g

        async def post(self, *a, **k):
            if exc:
                raise exc
            return p

    return _C


# ─── probe_reachable ────────────────────────────────────────────────────────


async def test_probe_reachable_returns_none_on_200(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": []})),
    )
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_returns_none_on_401(monkeypatch):
    """401 表示"地址对、只是需 key",不算 URL 错。"""
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_unreachable_on_connect_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(exc=httpx.ConnectError("dns fail")),
    )
    r = await probe.probe_reachable("https://nope.example/v1")
    assert r == {"code": "unreachable", "message": "无法连接 Base URL（ConnectError）"}


async def test_probe_reachable_http_error_on_404(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    r = await probe.probe_reachable("https://ok.example/v1")
    assert r == {"code": "http_error", "message": "服务返回异常（HTTP 404）"}


# ─── fetch_models ───────────────────────────────────────────────────────────


async def test_fetch_models_ok(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": [{"id": "m1"}, {"id": "m2"}]})),
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r == {"ok": True, "models": ["m1", "m2"]}


async def test_fetch_models_bad_key_on_401(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "bad_key"


async def test_fetch_models_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("nope"))
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable"


# ─── probe_chat ─────────────────────────────────────────────────────────────


async def test_probe_chat_ok(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(200))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is True and r["code"] == "ok" and r["status"] == 200
    assert "latency_ms" in r


async def test_probe_chat_bad_key(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "bad_key" and r["status"] == 401


async def test_probe_chat_not_found(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found" and r["status"] == 404


async def test_probe_chat_rejected_authed_on_400(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(400))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "rejected_authed"


async def test_probe_chat_rejected_authed_on_422(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(422))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "rejected_authed"


async def test_probe_chat_http_error_on_500(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(500))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "http_error"


async def test_probe_chat_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ReadTimeout("slow"))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "unreachable"


async def test_probe_chat_bad_response_on_json_decode_error(monkeypatch):
    """status=200 但 body 非 JSON → bad_response(而非误判 ok)。"""
    import json as _json

    class _Bad200:
        status_code = 200
        text = "not a json body"

        def json(self):
            raise _json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_Bad200())
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is False
    assert r["code"] == "bad_response"
    assert r["status"] == 200


async def test_probe_chat_bad_response_on_non_dict_body(monkeypatch):
    """status=200 但 body 是 list/非 dict → bad_response。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, json_data=["not", "a", "dict"])),
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is False
    assert r["code"] == "bad_response"
    assert r["status"] == 200


# ─── scheme 白名单(防 SSRF) ───────────────────────────────────────────────


async def test_probe_reachable_rejects_file_scheme():
    """file:// 被拒,不发 HTTP;不需要 mock httpx 因为压根不会调。"""
    r = await probe.probe_reachable("file:///etc/passwd")
    assert r == {
        "code": "unreachable",
        "message": "Base URL 协议非法（仅支持 http/https，实际: file）",
    }


async def test_probe_reachable_rejects_gopher_scheme():
    r = await probe.probe_reachable("gopher://evil/x")
    assert r["code"] == "unreachable" and "gopher" in r["message"]


async def test_probe_chat_rejects_file_scheme():
    r = await probe.probe_chat("m", "file:///etc/passwd", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_probe_omni_rejects_file_scheme():
    r = await probe.probe_omni("m", "file:///etc/passwd", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_fetch_models_rejects_ftp_scheme():
    r = await probe.fetch_models("ftp://x/y", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable" and r["models"] == []


async def test_probe_reachable_rejects_empty_host():
    r = await probe.probe_reachable("https:///")
    assert r["code"] == "unreachable" and "主机名" in r["message"]


# ─── probe_omni (两阶段) ────────────────────────────────────────────────────


async def test_probe_omni_get_401_short_circuits_to_bad_key(monkeypatch):
    """GET /models 401 立刻判 bad_key,不走 chat。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(401), post_resp=_FakeResp(200)),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "bad_key"


async def test_probe_omni_get_500_short_circuits_to_http_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(500), post_resp=_FakeResp(200)),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "http_error"


async def test_probe_omni_get_ok_then_chat_ok(monkeypatch):
    """GET /models 200 后调 chat,chat 200 → ok。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "m1"}]}), post_resp=_FakeResp(200)
        ),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is True and r["code"] == "ok"


async def test_probe_omni_get_ok_then_chat_not_found(monkeypatch):
    """模型不在列表但 GET 200:走 chat,chat 404 → not_found。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "other"}]}),
            post_resp=_FakeResp(404),
        ),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found"


async def test_probe_omni_connect_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("nope"))
    )
    r = await probe.probe_omni("m1", "https://nope/v1", "sk-x")
    assert r["code"] == "unreachable"


# ─── probe_chat × provider adapter (review #3 回归) ─────────────────────────


class _FakeStreamResp:
    """模拟 client.stream() 返回的 async context manager。"""

    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._lines = lines or []
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


def _fake_stream_client(get_resp, stream_resp):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return get_resp

        async def post(self, *a, **k):
            raise AssertionError("forced-stream should not call POST")

        def stream(self, *a, **k):
            return stream_resp

    return _C


async def test_probe_chat_uses_adapter_body_for_qwen(monkeypatch):
    """review #3 回归:Qwen adapter forced stream=True + modalities=["text"],
    probe_chat 必须走 SSE 流不是硬编码非流式 POST。原实现固定发非流式 body,合法
    Qwen 配置会被 400/422 判成 rejected_authed → OPEN_CONFIG,用户被卡死。"""
    stream_resp = _FakeStreamResp(
        200,
        lines=[
            'data: {"choices":[{"delta":{"content":"pong"}}]}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": [{"id": "qwen-omni"}]}), stream_resp),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
    assert r["ok"] is True
    assert r["code"] == "ok"


async def test_probe_chat_stream_401_maps_to_bad_key(monkeypatch):
    """forced-stream 路径撞 401 也要正常走 bad_key 分类,不能因为走了流式就丢掉状态码。"""
    stream_resp = _FakeStreamResp(401, lines=[])
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
    assert r["ok"] is False
    assert r["code"] == "bad_key"


async def test_probe_chat_non_qwen_still_uses_post(monkeypatch):
    """回归防护:非 Qwen 模型 (MiMo 默认) 仍走非流式 POST,行为未变。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "xiaomi/mimo-v2.5"}]}),
            post_resp=_FakeResp(200, {"choices": [{"message": {"content": "pong"}}]}),
        ),
    )
    r = await probe.probe_omni("xiaomi/mimo-v2.5", "https://mimo.example/v1", "sk-x")
    assert r["ok"] is True


async def test_probe_chat_stream_429_preserves_retry_after(monkeypatch):
    """review 🟡 回归:Qwen 撞 429 时 forced-stream 路径必须回传 Retry-After header,
    不然熔断退避走纯指数(early 12s vs server 说的 45s),对着限流的 Qwen 反复打 429、
    拖慢恢复。修复前 _probe_stream_chat 只返 (status, latency, ok),headers 恒空。"""
    stream_resp = _FakeStreamResp(429, lines=[], headers={"Retry-After": "45"})
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": [{"id": "qwen-omni"}]}), stream_resp),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
    assert r["ok"] is False
    assert r["code"] == "rate_limited"
    # 关键:Retry-After 被解析出来传给上层 _grow_backoff_locked
    assert r["retry_after_seconds"] == 45.0
