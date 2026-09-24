from types import SimpleNamespace

import httpx
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import omni_client
from miloco.perception.engine.omni.circuit_breaker import CircuitOpenError
from miloco.perception.engine.omni.omni_client import (
    MalformedBodyError,
    OmniError,
    _is_fallback_eligible,
    resolve_fallback_omni_configs,
)


def _http_error(status: int) -> OmniError:
    req = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return OmniError(
        "failed", original=httpx.HTTPStatusError("failed", request=req, response=resp)
    )


def test_fallback_eligible_only_for_recoverable_failures():
    assert _is_fallback_eligible(OmniError("timeout", original=httpx.ReadTimeout("x")))
    assert _is_fallback_eligible(
        OmniError(
            "server disconnected",
            original=httpx.RemoteProtocolError("Server disconnected without response"),
        )
    )
    assert _is_fallback_eligible(_http_error(429))
    assert _is_fallback_eligible(_http_error(503))
    malformed = OmniError("bad", original=MalformedBodyError("list"))
    assert _is_fallback_eligible(malformed)
    assert malformed.code == "bad_response"
    assert _is_fallback_eligible(
        OmniError(
            "open",
            original=CircuitOpenError("skipped:cooling:rate_limited", "limited"),
        )
    )

    assert not _is_fallback_eligible(_http_error(400))
    assert not _is_fallback_eligible(_http_error(401))
    assert not _is_fallback_eligible(_http_error(404))
    assert not _is_fallback_eligible(
        OmniError(
            "open",
            original=CircuitOpenError("skipped:cooling:bad_key", "bad key"),
        )
    )
    assert not _is_fallback_eligible(OmniError("unknown"))


def test_resolve_fallback_configs_preserves_order_and_skips_invalid(monkeypatch):
    profiles = [
        SimpleNamespace(
            label="same", model="primary", base_url="https://p/v1", api_key="p"
        ),
        SimpleNamespace(
            label="backup-b", model="b", base_url="https://b/v1/", api_key="kb"
        ),
        SimpleNamespace(
            label="backup-a", model="a", base_url="https://a/v1", api_key="ka"
        ),
        SimpleNamespace(
            label="empty-key", model="x", base_url="https://x/v1", api_key=""
        ),
    ]
    settings = SimpleNamespace(
        model=SimpleNamespace(
            omni_profiles=profiles,
            omni_fallbacks=[
                "missing",
                "backup-b",
                "backup-b",
                "same",
                "empty-key",
                "backup-a",
            ],
        )
    )
    monkeypatch.setattr("miloco.config.get_settings", lambda: settings)
    base = OmniConfig(
        model="primary",
        base_url="https://p/v1",
        api_key="p",
        max_completion_tokens=321,
        timeout=12,
    )

    out = resolve_fallback_omni_configs(base)

    assert [(x.model, x.base_url, x.api_key) for x in out] == [
        ("b", "https://b/v1", "kb"),
        ("a", "https://a/v1", "ka"),
    ]
    assert all(x.max_completion_tokens == 321 for x in out)
    assert all(x.timeout == 12 for x in out)


@pytest.mark.asyncio
async def test_call_omni_uses_ordered_fallback_without_primary_breaker(monkeypatch):
    primary = OmniConfig(model="primary", base_url="https://p/v1", api_key="p")
    backup = OmniConfig(model="backup", base_url="https://b/v1", api_key="b")
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        omni_client, "resolve_fallback_omni_configs", lambda config: [backup]
    )

    async def fake_call(payload, config, type="realtime", *, use_circuit_breaker=True):
        calls.append((config.model, use_circuit_breaker))
        if config.model == "primary":
            raise _http_error(429)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(omni_client, "_call_omni_once", fake_call)

    result = await omni_client.call_omni({"messages": []}, primary)

    assert result["choices"][0]["message"]["content"] == "ok"
    assert calls == [("primary", True), ("backup", False)]


@pytest.mark.asyncio
async def test_call_omni_falls_back_for_malformed_body(monkeypatch):
    primary = OmniConfig(model="primary", base_url="https://p/v1", api_key="p")
    backup = OmniConfig(model="backup", base_url="https://b/v1", api_key="b")
    calls: list[str] = []

    monkeypatch.setattr(
        omni_client, "resolve_fallback_omni_configs", lambda config: [backup]
    )

    async def fake_call(payload, config, type="realtime", *, use_circuit_breaker=True):
        calls.append(config.model)
        if config.model == "primary":
            malformed = MalformedBodyError("list")
            raise OmniError(str(malformed), original=malformed)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(omni_client, "_call_omni_once", fake_call)

    result = await omni_client.call_omni({"messages": []}, primary)

    assert result["choices"][0]["message"]["content"] == "ok"
    assert calls == ["primary", "backup"]


@pytest.mark.asyncio
async def test_call_omni_does_not_fallback_for_bad_request(monkeypatch):
    primary = OmniConfig(model="primary", base_url="https://p/v1", api_key="p")
    backup = OmniConfig(model="backup", base_url="https://b/v1", api_key="b")
    calls: list[str] = []

    monkeypatch.setattr(
        omni_client, "resolve_fallback_omni_configs", lambda config: [backup]
    )

    async def fake_call(payload, config, type="realtime", *, use_circuit_breaker=True):
        calls.append(config.model)
        raise _http_error(400)

    monkeypatch.setattr(omni_client, "_call_omni_once", fake_call)

    with pytest.raises(OmniError):
        await omni_client.call_omni({"messages": []}, primary)

    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_stream_fallback_only_before_first_delta(monkeypatch):
    primary = OmniConfig(model="primary", base_url="https://p/v1", api_key="p")
    backup = OmniConfig(model="backup", base_url="https://b/v1", api_key="b")
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        omni_client, "resolve_fallback_omni_configs", lambda config: [backup]
    )

    async def fake_stream(
        payload,
        config,
        usage_out=None,
        type="realtime",
        *,
        use_circuit_breaker=True,
    ):
        calls.append((config.model, use_circuit_breaker))
        if config.model == "primary":
            raise _http_error(503)
        yield "backup"

    monkeypatch.setattr(omni_client, "_call_omni_stream_once", fake_stream)

    chunks = [
        chunk async for chunk in omni_client.call_omni_stream({"messages": []}, primary)
    ]

    assert chunks == ["backup"]
    assert calls == [("primary", True), ("backup", False)]


@pytest.mark.asyncio
async def test_stream_does_not_fallback_after_partial_output(monkeypatch):
    primary = OmniConfig(model="primary", base_url="https://p/v1", api_key="p")
    backup = OmniConfig(model="backup", base_url="https://b/v1", api_key="b")
    calls: list[str] = []

    monkeypatch.setattr(
        omni_client, "resolve_fallback_omni_configs", lambda config: [backup]
    )

    async def fake_stream(
        payload,
        config,
        usage_out=None,
        type="realtime",
        *,
        use_circuit_breaker=True,
    ):
        calls.append(config.model)
        yield "partial"
        raise _http_error(503)

    monkeypatch.setattr(omni_client, "_call_omni_stream_once", fake_stream)

    with pytest.raises(OmniError):
        async for _ in omni_client.call_omni_stream({"messages": []}, primary):
            pass

    assert calls == ["primary"]
