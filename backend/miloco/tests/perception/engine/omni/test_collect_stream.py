"""Tests for _collect_stream_response SSE parsing."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from miloco.perception.engine.omni.omni_client import _collect_stream_response


def _sse_body(*chunks: dict, done: bool = True) -> str:
    parts = []
    for c in chunks:
        parts.append(f"data: {json.dumps(c)}")
        parts.append("")
    if done:
        parts.append("data: [DONE]")
        parts.append("")
    return "\n".join(parts)


def _chunk(content: str | None = None, usage: dict | None = None) -> dict:
    c: dict = {}
    if content is not None:
        c["choices"] = [{"delta": {"content": content}}]
    if usage is not None:
        c["usage"] = usage
    return c


class _FakeStreamResponse:
    """Minimal async context manager mimicking httpx streaming response."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self._body = body
        self.text = body

    async def aiter_lines(self):
        for line in self._body.split("\n"):
            yield line

    async def aread(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=MagicMock(), response=MagicMock(status_code=self.status_code),
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _mock_client(status_code: int, body: str):
    client = MagicMock()
    client.stream = MagicMock(return_value=_FakeStreamResponse(status_code, body))
    return client


class TestCollectStreamResponse:
    @pytest.mark.asyncio
    async def test_basic_content(self):
        body = _sse_body(_chunk("Hello"), _chunk(" world"))
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["choices"][0]["message"]["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_usage_from_last_chunk(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        body = _sse_body(_chunk("Hi"), _chunk(usage=usage))
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["usage"] == usage

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        body = "data: [DONE]\n"
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["choices"][0]["message"]["content"] == ""
        assert result["usage"] == {}

    @pytest.mark.asyncio
    async def test_skips_malformed_json(self):
        body = "data: {bad}\n\n" + _sse_body(_chunk("ok"))
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["choices"][0]["message"]["content"] == "ok"

    @pytest.mark.asyncio
    async def test_skips_empty_and_non_data_lines(self):
        body = "\n  \nretry: 3000\n" + _sse_body(_chunk("a"))
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["choices"][0]["message"]["content"] == "a"

    @pytest.mark.asyncio
    async def test_multiple_content_chunks(self):
        body = _sse_body(_chunk("a"), _chunk("b"), _chunk("c"))
        client = _mock_client(200, body)
        result = await _collect_stream_response(client, "https://test", {}, {"messages": []})
        assert result["choices"][0]["message"]["content"] == "abc"

    @pytest.mark.asyncio
    async def test_400_raises(self):
        client = _mock_client(400, '{"error": "bad"}')
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_stream_response(client, "https://test", {}, {"messages": []})
