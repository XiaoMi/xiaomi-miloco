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
    seen: dict | None = None,
):
    """``seen`` 传入则记录 GET 实际用的 url / headers，供鉴权头断言。"""
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
            if seen is not None:
                seen["url"] = a[0] if a else k.get("url")
                seen["headers"] = k.get("headers") or {}
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
    """模拟 client.stream() 返回的 async context manager。

    headers 存 httpx.Headers 而非 plain dict,精确复现生产路径:
    _probe_stream_chat 里 dict(resp.headers) 会把 httpx.Headers 里的 key 全部小写化
    (httpx 0.28.1: dict(Headers({"Retry-After": "45"})) == {"retry-after": "45"});
    若测试 fake 用 plain dict 装原大小写 key,dict() 会保留原样,反而绕过生产路径的
    小写化,让「不区分大小写」相关的 bug 不能被回归测试守住。
    """

    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        headers: httpx.Headers | dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._lines = lines or []
        # 强制包成 httpx.Headers,即使传入 plain dict 也走真实 httpx 语义
        self.headers = httpx.Headers(headers or {})

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


# ─── Gemini 家族主机名判定（Gemini API / Vertex 两条接入路径）──────────────────


def test_is_gemini_native_endpoint_covers_both_access_paths():
    f = probe._is_gemini_native_endpoint
    assert f("https://generativelanguage.googleapis.com/v1beta")
    # Vertex 原生：express 形态与项目级形态，都经 /publishers/
    assert f("https://aiplatform.googleapis.com/v1/publishers/google")
    assert f(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1/projects/p/locations/us-central1/publishers/google"
    )
    # 按主机名判而非 URL 子串：把域名塞进路径不算命中
    assert not f("https://evil.com/aiplatform.googleapis.com")
    assert not f("https://evil.com/generativelanguage.googleapis.com")
    assert not f("https://api.xiaomimimo.com/v1")


def test_parse_model_ids_tries_all_shapes():
    """认识的几种形态都试：prefer_native 只决定先试哪一种，不是唯一依据。

    绑死单一形态时，端点形态一旦判错（经代理转发、或没验证过的路径）就解出空列表
    并被报成成功，下拉恒空且用户拿不到提示。
    """
    f = probe._parse_model_ids
    native = {"models": [{"name": "models/gemini-3.6-flash"}]}
    compat = {"data": [{"id": "m1"}]}
    # 判对时按各自形态解
    assert f(native, prefer_native=True) == ["gemini-3.6-flash"]
    assert f(compat, prefer_native=False) == ["m1"]
    # 判错时回退到另一种形态，而不是解出空列表
    assert f(compat, prefer_native=True) == ["m1"]
    assert f(native, prefer_native=False) == ["gemini-3.6-flash"]
    # Vertex 的 publisherModels 形态（name 带 publishers/…/models/ 前缀）也认
    assert f(
        {"publisherModels": [{"name": "publishers/google/models/gemini-3.6-flash"}]},
        prefer_native=True,
    ) == ["gemini-3.6-flash"]
    # 兼容形态的 id 原样透传：含 /models/ 是合法的（如 accounts/<org>/models/<name>），
    # 套用原生形态的剥前缀规则会把它截断成 provider 认不出的名字
    assert f(
        {"data": [{"id": "accounts/fireworks/models/llama-v3-70b"}]}, prefer_native=False
    ) == ["accounts/fireworks/models/llama-v3-70b"]
    # 三种都不认 / 非 dict → 空
    assert f({"someOtherShape": [{"name": "x"}]}, prefer_native=True) == []
    assert f(["not", "a", "dict"], prefer_native=True) == []
    # 条目不是 dict、列表位不是 list 都只跳过，不抛
    assert f({"models": ["bad", {"name": "models/ok"}]}, prefer_native=True) == ["ok"]
    assert f({"models": "notalist", "data": [{"id": "m1"}]}, prefer_native=True) == ["m1"]
    # 产出不含空串：剥完前缀为空的条目要被丢掉，且不能因此短路掉另一种形态的回退
    assert f({"models": [{"name": "models/"}]}, prefer_native=True) == []
    assert f(
        {"models": [{"name": "models/"}], "data": [{"id": "real"}]}, prefer_native=True
    ) == ["real"]
    # 标识是数字等标量时归一而非抛（旧实现会让调用方的 sorted 混排类型报 TypeError）
    assert f({"data": [{"id": "a"}, {"id": 1}]}, prefer_native=False) == ["a", "1"]
    # 但嵌套结构不是合法标识：跳过，而不是 str() 成一串垃圾文本混进下拉
    assert f({"data": [{"id": {"name": "x"}}, {"id": "ok"}]}, prefer_native=False) == ["ok"]
    assert f({"data": [{"id": ["a"]}]}, prefer_native=False) == []
    assert f({"models": [{"name": {"k": "v"}}], "data": [{"id": "fallback"}]},
             prefer_native=True) == ["fallback"]


async def test_fetch_models_mixed_id_types_do_not_500(monkeypatch):
    """条目 id 类型混杂时不能让异常逃出本接口。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "b"}, {"id": 1}]})),
    )
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k")
    assert r["ok"] is True and r["models"] == ["1", "b"]


async def test_fetch_models_recovers_when_shape_mismatches(monkeypatch):
    """原生端点回了兼容形态时仍解得出模型，而不是静默返回空列表。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "gemini-3.6-flash"}]})),
    )
    r = await probe.fetch_models(
        "https://aiplatform.googleapis.com/v1/publishers/google", "k"
    )
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}


def test_gemini_api_openai_compat_path_stays_on_openai_branch():
    """Gemini API 主机下的 OpenAI 兼容 base_url 必须留在 Bearer 分支。

    官方兼容 base_url 是 https://generativelanguage.googleapis.com/v1beta/openai/，
    走 Authorization: Bearer + {data:[{id}]}。判成原生有两种落点、都坏：发 x-goog-api-key
    被拒 → 401 误报 bad_key；或被收下 → 按 {models:[{name}]} 解析 → 空列表且 ok=true。
    """
    f = probe._is_gemini_native_endpoint
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai")
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai/")
    # 不少工具要求 base 以 /v1 结尾——尾锚定判定会漏掉这一形态
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai/v1")
    assert f("https://generativelanguage.googleapis.com/v1beta")
    # 按路径段比对，不误伤名字里恰好含 openai 的段
    assert f("https://generativelanguage.googleapis.com/v1beta/myopenai")
    # 路径段与主机名一样做大小写归一：URL 路径大小写敏感，但判定不该因此漏判
    assert not f("https://generativelanguage.googleapis.com/v1beta/OpenAI")
    assert not f("https://generativelanguage.googleapis.com/v1beta/OPENAI/v1")


async def test_fetch_models_gemini_openai_compat_uses_bearer(monkeypatch):
    """兼容路径走到发请求这一层也要是 Bearer，且按 {data:[{id}]} 解析出模型。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "gemini-3.6-flash"}]}), seen=seen),
    )
    r = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta/openai", "k"
    )
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}
    assert "Authorization" in seen["headers"]
    assert "x-goog-api-key" not in seen["headers"]


def test_vertex_openai_compat_path_stays_on_openai_branch():
    """Vertex 同主机下的 OpenAI 兼容路径必须留在 Bearer 分支。

    该端点鉴权走 ``Authorization: Bearer``；只按主机名判会给它发 x-goog-api-key
    → 401 → 误报 bad_key，正是本模块要消除的那类误报。
    """
    assert not probe._is_gemini_native_endpoint(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1beta1/projects/p/locations/us-central1/endpoints/openapi"
    )


def test_vertex_non_native_path_not_treated_as_native():
    """Vertex 主机上**任何**不经 publishers 段的路径都不按原生处理。

    这条锁的是「白名单」而非「黑名单」语义：**若改成**只排除已知的 OpenAI 兼容路径，
    该主机上其余非原生路径就会被误判成原生、发错鉴权头——本用例即为拦住那种改法。
    """
    assert not probe._is_gemini_native_endpoint(
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/p/locations/l/datasets"
    )
    assert not probe._is_gemini_native_endpoint(
        "https://aiplatform.googleapis.com/v1"
    )


async def test_fetch_models_vertex_openai_compat_uses_bearer(monkeypatch):
    """走到发请求这一层也要是 Bearer —— 判定改错时这条会连带变红。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(200, {"data": [{"id": "m1"}]}), seen=seen)
    )
    r = await probe.fetch_models(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1beta1/projects/p/locations/us-central1/endpoints/openapi",
        "k",
    )
    assert r == {"ok": True, "models": ["m1"]}
    assert "Authorization" in seen["headers"]
    assert "x-goog-api-key" not in seen["headers"]


async def test_fetch_models_vertex_uses_goog_key_header(monkeypatch):
    """Vertex 主机按 Gemini 家族处理：走 x-goog-api-key，不发 Bearer。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            _FakeResp(200, {"models": [{"name": "models/gemini-3.6-flash"}]}), seen=seen
        ),
    )
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k")
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}
    assert "x-goog-api-key" in seen["headers"]
    assert "Authorization" not in seen["headers"]


async def test_fetch_models_gemini_404_is_list_unsupported(monkeypatch):
    """该端点没有 models.list（Vertex 实测回 404）→ 提示手填模型名，不报地址错。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k")
    assert r["ok"] is False and r["code"] == "list_unsupported" and r["models"] == []


async def test_fetch_models_404_gate_is_endpoint_level_not_host_level(monkeypatch):
    """404 归「列不出来」的判据是**端点**级，不是主机级。

    同主机下的 OpenAI 兼容路径本身就有 /models 接口，那里的 404 更可能是路径写错，
    应落 http_error；把判据放宽到「同族主机」会让它被误报成「该端点没有列模型接口」。
    """
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    native = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta", "k"
    )
    assert native["code"] == "list_unsupported"

    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    compat = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta/openai", "k"
    )
    assert compat["code"] == "http_error", "同主机的兼容路径 404 不该归 list_unsupported"


async def test_fetch_models_gemini_401_403_still_bad_key(monkeypatch):
    """坏 key / 被限制的 key 正是回 401/403 —— 不能被「列不出来」吞掉。"""
    for status in (401, 403):
        monkeypatch.setattr(
            probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(status))
        )
        r = await probe.fetch_models("https://generativelanguage.googleapis.com/v1beta", "k")
        assert r["code"] == "bad_key", f"HTTP {status} 应仍判 bad_key"


async def test_fetch_models_non_gemini_unchanged(monkeypatch):
    """非 Gemini 主机行为不变：401 → bad_key，404 → http_error。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(401)))
    assert (await probe.fetch_models("https://api.example.com/v1", "k"))["code"] == "bad_key"
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (await probe.fetch_models("https://api.example.com/v1", "k"))["code"] == "http_error"


async def test_probe_reachable_404_gate_is_endpoint_level_not_host_level(monkeypatch):
    """预检侧的 404 判据同样是**端点**级，与拉模型列表那侧对称。

    同主机下的 OpenAI 兼容路径 404 仍应报出来；放宽到「同族主机」会让它被当作
    「该端点没有列模型接口」而放行，把地址错盖成「缺 key」。
    """
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (
        await probe.probe_reachable("https://generativelanguage.googleapis.com/v1beta")
        is None
    )
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    compat = await probe.probe_reachable(
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert compat is not None and compat["code"] == "http_error"


async def test_probe_reachable_gemini_404_not_reported_as_url_error(monkeypatch):
    """Vertex 没有 models.list，404 不该在「缺 key」之前先被报成地址错。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (
        await probe.probe_reachable("https://aiplatform.googleapis.com/v1/publishers/google")
        is None
    )
    # 非 Gemini 主机的 404 仍然要报出来
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    r = await probe.probe_reachable("https://api.example.com/v1")
    assert r is not None and r["code"] == "http_error"
