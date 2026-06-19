"""Miloco MCP Server — Async HTTP Client for Miloco Backend."""

from typing import Any

import httpx


class MilocoError(Exception):
    """Miloco API error."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Miloco API error {code}: {message}")


class MilocoClient:
    """Async HTTP client wrapping Miloco's REST API."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0, verify: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            verify=verify,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make a request and return the `data` field from the response."""
        resp = await self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        result = resp.json()
        code = result.get("code", 0)
        if code != 0:
            raise MilocoError(code, result.get("message", "Unknown error"), result.get("data"))
        return result.get("data")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, json=json)

    async def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self.request("PUT", path, json=json)

    async def patch(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self.request("PATCH", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    async def aclose(self) -> None:
        await self._client.aclose()
