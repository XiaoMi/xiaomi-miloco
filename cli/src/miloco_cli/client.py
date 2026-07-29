"""HTTP 客户端，封装对 Miloco 后端的请求。

退出码：
  2 — 网络错误（连接失败、超时）
  3 — 业务错误（后端返回非零 code）
"""

import json
import os
import sys
from typing import NoReturn

import httpx

from miloco_cli.config import load_config

# 后端在 127.0.0.1:1810——macOS 上 httpx 会读**系统级代理设置**(urllib.getproxies),
# Clash 等开系统代理后 CLI 打本机后端也被送进代理返 502。回环/私网并入 NO_PROXY
# (httpx 支持 CIDR),与 backend main._ensure_no_proxy_for_local 同口径。
for _var in ("NO_PROXY", "no_proxy"):
    _existing = [e.strip() for e in os.environ.get(_var, "").split(",") if e.strip()]
    _local = ["localhost", "127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    os.environ[_var] = ",".join(_existing + [e for e in _local if e not in _existing])


def _get_client(cfg: dict) -> httpx.Client:
    server = cfg["server"]
    headers = {}
    if token := server.get("token"):
        headers["Authorization"] = f"Bearer {token}"
    tls = server.get("tls_verify", False)
    verify = tls if isinstance(tls, bool) else str(tls).lower() == "true"
    return httpx.Client(
        base_url=server["url"],
        headers=headers,
        verify=verify,
        timeout=30,
    )


def _handle_response(resp: httpx.Response) -> dict:
    """统一处理响应，业务错误 sys.exit(3)。"""
    try:
        data = resp.json()
    except Exception:
        print(
            json.dumps(
                {
                    "error": f"invalid JSON response: {resp.status_code} {resp.text[:200]}"
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(3)

    # FastAPI 4xx/5xx 返回的错误体（如 422 {"detail": [...]}）无 code 字段
    if not resp.is_success:
        print(json.dumps({"error": data}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    if data.get("code", 0) != 0:
        print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    return data


def _connect_error(url: str) -> NoReturn:
    print(
        json.dumps(
            {"error": f"cannot connect to Miloco backend at {url}"},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    sys.exit(2)


def api_get(
    path: str,
    params: dict | list[tuple[str, str | int | float | None]] | None = None,
    *,
    timeout: float | None = None,
) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            kw = {"timeout": timeout} if timeout is not None else {}
            resp = client.get(path, params=params, **kw)
            return _handle_response(resp)
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_post(path: str, body: dict | None = None) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.post(path, json=body or {})
            return _handle_response(resp)
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_put(path: str, body: dict | None = None) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.put(path, json=body or {})
            return _handle_response(resp)
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_patch(path: str, body: dict | None = None) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.patch(path, json=body or {})
            return _handle_response(resp)
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_delete(path: str, params: dict | None = None) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.delete(path, params=params)
            return _handle_response(resp)
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])
