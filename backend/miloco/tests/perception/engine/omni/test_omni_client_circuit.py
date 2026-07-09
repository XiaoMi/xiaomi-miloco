"""omni_client 三个 HTTP 出口 × 熔断器 集成测试。"""

from __future__ import annotations

import httpx
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import omni_client
from miloco.perception.engine.omni.circuit_breaker import (
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
)


class _FakeResp:
    def __init__(
        self,
        status_code: int,
        json_data: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(self.status_code),
            )


def _fake_async_client(resp: _FakeResp | None = None, *, exc: Exception | None = None):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            if exc:
                raise exc
            return resp

    return _C


@pytest.fixture(autouse=True)
def _reset_cb():
    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


def _cfg() -> OmniConfig:
    return OmniConfig(
        model="m",
        base_url="https://x/v1",
        api_key="sk-1",
        temperature=0,
        top_p=1,
        max_completion_tokens=1,
        timeout=1.0,
        stream=False,
    )


def _payload() -> dict:
    return {"system_prompt": "sys", "user_content": "u"}


# ─── call_omni × 熔断 ───────────────────────────────────────────────────────


async def test_call_omni_success_records_success(monkeypatch):
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"choices": [], "usage": {}})),
    )
    await omni_client.call_omni(_payload(), _cfg())
    assert get_omni_circuit_breaker().snapshot().state == "ok"


async def test_call_omni_single_401_does_not_open(monkeypatch):
    """瞬时 401 不该一击停感知——运行时 CONFIG 走窗口阈值(consecutive=3),
    连续 3 次才开断路,单次视为噪声(provider 侧鉴权抖动 / 换 key 中转)。"""
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    with pytest.raises(omni_client.OmniError):
        await omni_client.call_omni(_payload(), _cfg())
    assert get_omni_circuit_breaker().snapshot().state == "ok"


async def test_call_omni_three_consecutive_401_open_config(monkeypatch):
    """连续 3 次 401 稳定复现才 OPEN_CONFIG。"""
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    for _ in range(3):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(_payload(), _cfg())
    snap = get_omni_circuit_breaker().snapshot()
    assert snap.state == "error" and snap.code == "bad_key"


async def test_call_omni_three_consecutive_404_open_config(monkeypatch):
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    for _ in range(3):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(_payload(), _cfg())
    assert get_omni_circuit_breaker().snapshot().code == "not_found"


async def test_call_omni_three_connect_errors_open_recoverable(monkeypatch):
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        _fake_async_client(exc=httpx.ConnectError("nope")),
    )
    for _ in range(3):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(_payload(), _cfg())
    snap = get_omni_circuit_breaker().snapshot()
    assert snap.state == "warn" and snap.code == "unreachable"


async def test_call_omni_open_short_circuits_no_http(monkeypatch):
    """熔断 OPEN 时不再发 HTTP,直接抛。"""
    # 先让熔断打开(CONFIG 走窗口阈值,连打 3 次 bad_key 才 OPEN_CONFIG)
    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(ClassifiedError("bad_key", "m", ErrorCategory.CONFIG))
    assert cb.snapshot().state == "error"

    # 下一次 call 不应发 HTTP;用 exc 兜底如果被调到会抛这个可辨识 exception
    call_count = {"n": 0}

    def bomb_client(*a, **k):
        call_count["n"] += 1

        class C:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a_):
                return False

            async def post(self_, *a_, **k_):
                raise AssertionError("should not reach HTTP")

        return C()

    monkeypatch.setattr(omni_client.httpx, "AsyncClient", bomb_client)
    with pytest.raises(omni_client.OmniError) as ei:
        await omni_client.call_omni(_payload(), _cfg())
    assert call_count["n"] == 0  # AsyncClient() 都没被调
    assert "short-circuited" in str(ei.value)


async def test_call_omni_bad_response_non_dict(monkeypatch):
    """非 dict 响应算 recoverable,单次未到阈值不熔断;3 次后熔断为 bad_response。"""
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(200, []))
    )  # list 不是 dict
    for _ in range(3):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(_payload(), _cfg())
    snap = get_omni_circuit_breaker().snapshot()
    assert snap.state == "warn" and snap.code == "bad_response"


# ─── resolve_live_omni_config × 三元组变化 ──────────────────────────────────


async def test_resolve_live_config_no_change_keeps_state(monkeypatch):
    """三元组不变时不动熔断。"""
    from miloco.config import reset_settings

    reset_settings()
    cb = get_omni_circuit_breaker()
    for _ in range(3):
        await cb.record_failure(ClassifiedError("bad_key", "m", ErrorCategory.CONFIG))
    assert cb.snapshot().state == "error"

    # 第一次调用建立 cache
    if hasattr(omni_client._maybe_reset_breaker_on_config_change, "_last_triple"):
        del omni_client._maybe_reset_breaker_on_config_change._last_triple
    base = OmniConfig(model="m1", base_url="https://x/v1", api_key="sk-1")
    omni_client.resolve_live_omni_config(base)
    # 第二次调用同样值:不 reset
    omni_client.resolve_live_omni_config(base)
    # settings 里的 api_key 和 base.api_key 一样(sk-1),triple 不变
    assert cb.snapshot().state == "error"


async def test_resolve_live_config_change_resets_breaker(monkeypatch):
    """settings.model.omni 三元组变化时清熔断。"""
    from miloco.config import reset_settings

    reset_settings()
    cb = get_omni_circuit_breaker()
    omni_client._maybe_reset_breaker_on_config_change._last_triple = (
        "m1",
        "https://x/v1",
        "sk-OLD",
    )
    for _ in range(3):
        await cb.record_failure(ClassifiedError("bad_key", "m", ErrorCategory.CONFIG))
    assert cb.snapshot().state == "error"

    # settings 返回不同的 api_key
    class _Mo:
        model = "m1"
        base_url = "https://x/v1"
        api_key = "sk-NEW"

    class _M:
        omni = _Mo()

    class _S:
        model = _M()

    monkeypatch.setattr(omni_client, "get_settings", lambda: _S(), raising=False)
    # 兼容 dataclasses.replace 需要新 api_key 字段:直接调 resolve
    base = OmniConfig(model="m1", base_url="https://x/v1", api_key="sk-OLD")
    # patch get_settings 引用点
    import miloco.perception.engine.omni.omni_client as oc

    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _S(),
        raising=True,
    )
    oc.resolve_live_omni_config(base)
    # 等待 create_task 完成
    import asyncio

    await asyncio.sleep(0)
    assert cb.snapshot().state == "ok"


# ─── forced-stream 路径熔断器记录(review #1 回归防护) ──────────────────────


def _forced_stream_client_ok():
    """post 返 200 (让 body["stream"]=True 前的构造走通),真正 forced_stream 分支
    走 _collect_stream_response —— 由测试自己 monkeypatch 抛 HTTPStatusError。"""

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp(200, {"choices": [], "usage": {}})

    return _C


async def test_call_omni_forced_stream_401_records_failure(monkeypatch):
    """review #1 回归:forced-stream 路径遇 401 时熔断器必须能看到。之前
    _collect_stream_response 直接 raise_for_status 抛 HTTPStatusError,上层
    `not isinstance(e, HTTPStatusError)` 守卫会跳过 record_failure,导致
    Qwen 等强制 stream adapter 的 4xx/5xx 熔断器根本感知不到。"""
    # 强制 forced_stream=True:让 adapter 生成 body["stream"]=True
    from miloco.perception.engine.omni import provider

    orig_adapter = provider.get_adapter("m")

    class _StreamAdapter:
        def build_request_body(self, messages, **kw):
            kw["stream"] = True  # 关键:忽略调用方传的 stream=False
            return orig_adapter.build_request_body(messages, **kw)

    monkeypatch.setattr(
        omni_client, "get_adapter", lambda model: _StreamAdapter()
    )

    # 让 _collect_stream_response 抛 401 的 HTTPStatusError,模拟真 SSE 401 场景
    async def _raise_401(*a, **k):
        raise httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("POST", "https://x/v1/chat/completions"),
            response=httpx.Response(401),
        )

    monkeypatch.setattr(omni_client, "_collect_stream_response", _raise_401)
    monkeypatch.setattr(omni_client.httpx, "AsyncClient", _forced_stream_client_ok())

    with pytest.raises(omni_client.OmniError):
        await omni_client.call_omni(_payload(), _cfg())
    # 关键断言:熔断器看到了 401 → consecutive_failures = 1(修复前会是 0)
    assert get_omni_circuit_breaker().snapshot().consecutive_failures == 1


async def test_call_omni_forced_stream_500_records_failure(monkeypatch):
    """forced-stream 遇 5xx (recoverable) 同样要 record_failure 累计到熔断阈值。"""
    from miloco.perception.engine.omni import provider

    orig_adapter = provider.get_adapter("m")

    class _StreamAdapter:
        def build_request_body(self, messages, **kw):
            kw["stream"] = True
            return orig_adapter.build_request_body(messages, **kw)

    monkeypatch.setattr(
        omni_client, "get_adapter", lambda model: _StreamAdapter()
    )

    async def _raise_500(*a, **k):
        raise httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("POST", "https://x/v1/chat/completions"),
            response=httpx.Response(500),
        )

    monkeypatch.setattr(omni_client, "_collect_stream_response", _raise_500)
    monkeypatch.setattr(omni_client.httpx, "AsyncClient", _forced_stream_client_ok())

    # 连打 3 次触发 OPEN_RECOVERABLE (consecutive_threshold=3)
    for _ in range(3):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(_payload(), _cfg())
    snap = get_omni_circuit_breaker().snapshot()
    assert snap.state == "warn"
    assert snap.code == "http_error"
