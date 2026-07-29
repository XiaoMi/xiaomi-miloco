"""HTTP 客户端，封装对 Miloco 后端的请求。

退出码：
  2 — 网络错误（连接失败、超时）
  3 — 业务错误（后端返回非零 code）
"""

import json
import os
import sys
import urllib.request
from typing import NoReturn

import httpx

from miloco_cli.config import load_config

# httpx 的 get_environment_proxies 遍历的三个键(all = 全协议出口,常见于 SOCKS)。
_PROXY_SCHEMES = ("http", "https", "all")


def _system_proxies() -> dict[str, str]:
    """只读系统代理设置,绕开 getproxies() 的 env 短路(裸 NO_PROXY 也会短路它)。"""
    for name in ("getproxies_macosx_sysconf", "getproxies_registry"):
        fn = getattr(urllib.request, name, None)
        if fn is None:
            continue
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return {}
    return {}


def ensure_no_proxy_for_local() -> None:
    """把回环并入 NO_PROXY,防系统代理劫持 CLI→后端(127.0.0.1:1810)的调用。

    与 backend 的 ``main._ensure_no_proxy_for_local`` 同口径(先快照、快照本身防
    env 短路、守门看三个 scheme 且判**存在性**而非真值、两个大小写写同值、
    不列 CIDR),详见那边 docstring 的完整推理。
    两个包互不依赖故各留一份;本函数刻意包成函数而非模块级裸语句——模块级
    ``for`` 在 Python 里不是块作用域,循环变量会挂在模块命名空间上,且无法在
    测试里重复调用。
    """
    try:
        snapshot = urllib.request.getproxies()
        if not any(snapshot.get(k) for k in _PROXY_SCHEMES):
            snapshot = {**_system_proxies(), **snapshot}
    except Exception:  # noqa: BLE001
        snapshot = {}
    user_configured = any(
        f"{s}_proxy" in os.environ or f"{s.upper()}_PROXY" in os.environ
        for s in _PROXY_SCHEMES
    )
    if not user_configured:
        # 导出只做 http/https:平台函数的键集里没有 all(macOS _scproxy 给
        # http/https/ftp/gopher/socks,Windows 注册表给协议名),唯一能产出 all 的是
        # getproxies_environment(),而那种情况 user_configured 已为真、走不到这里。
        for scheme in ("http", "https"):
            proxied = snapshot.get(scheme)
            if proxied:
                os.environ[f"{scheme}_proxy"] = proxied
                os.environ[f"{scheme.upper()}_PROXY"] = proxied

    merged: list[str] = []
    for var in ("NO_PROXY", "no_proxy"):
        for entry in os.environ.get(var, "").split(","):
            entry = entry.strip()
            if entry and entry not in merged:
                merged.append(entry)
    merged += [e for e in ("localhost", "127.0.0.1", "::1") if e not in merged]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(merged)


# 调用点在 miloco_cli.main(声明的入口点),不在这里自动执行——依赖 import
# 副作用会让「谁 import 了本模块」变成隐式契约。


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


def _handle_response(resp: httpx.Response) -> dict | list:
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

    # observability 系列 endpoint（/api/actions、/api/traces 等）直接返回裸 list,
    # 无 NormalResponse 信封;2xx 已判过,list 恒为成功,原样透传。
    if isinstance(data, dict) and data.get("code", 0) != 0:
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
) -> dict | list:
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


def api_post_multipart(
    path: str,
    files: list[tuple[str, tuple[str, bytes, str]]],
    data: dict | None = None,
) -> dict:
    """POST multipart/form-data（上传文件 + 表单字段）。

    ``files``：``[("字段名", ("文件名", 字节, content_type)), ...]``；同名字段可重复
    （如 medias / crops 多文件）。``data``：普通表单字段，list 值会展开成重复字段
    （如 scores=[...]）。不设 Content-Type，交 httpx 按 multipart 自动生成 boundary。
    """
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.post(path, files=files, data=data or {})
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
