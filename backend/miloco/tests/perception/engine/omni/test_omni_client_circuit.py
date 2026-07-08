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


async def test_call_omni_401_opens_config_immediately(monkeypatch):
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    with pytest.raises(omni_client.OmniError):
        await omni_client.call_omni(_payload(), _cfg())
    snap = get_omni_circuit_breaker().snapshot()
    assert snap.state == "error" and snap.code == "bad_key"


async def test_call_omni_404_opens_config(monkeypatch):
    monkeypatch.setattr(
        omni_client.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
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
    # 先让熔断打开
    cb = get_omni_circuit_breaker()
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
    from miloco.config import get_settings, reset_settings

    reset_settings()
    cb = get_omni_circuit_breaker()
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
    from miloco.config import get_settings, reset_settings

    reset_settings()
    cb = get_omni_circuit_breaker()
    omni_client._maybe_reset_breaker_on_config_change._last_triple = (
        "m1",
        "https://x/v1",
        "sk-OLD",
    )
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
