"""client.py 测试：HTTP 请求封装、错误处理。"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from miloco_cli.client import api_delete, api_get, api_patch, api_post, api_put

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """隔离配置，使用 localhost 默认地址。"""
    config_dir = tmp_path / "miloco"
    # 清空所有 MILOCO_* 环境变量避免污染测试
    import os as _os
    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))


def _make_response(data: dict, status_code: int = 200) -> MagicMock:
    """构造 httpx.Response mock。"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _patch_client(resp: MagicMock):
    """patch httpx.Client，让所有 HTTP 方法返回指定 response。"""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = resp
    mock_client.post.return_value = resp
    mock_client.put.return_value = resp
    mock_client.patch.return_value = resp
    mock_client.delete.return_value = resp
    mock_client.request.return_value = resp
    return patch("miloco_cli.client.httpx.Client", return_value=mock_client), mock_client


# ─── api_get ──────────────────────────────────────────────────────────────────


def test_api_get_success():
    resp = _make_response({"code": 0, "data": {"items": []}})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        result = api_get("/api/test")
    mock_client.get.assert_called_once_with("/api/test", params=None)
    assert result["code"] == 0


def test_api_get_with_params():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_get("/api/test", params={"key": "val"})
    mock_client.get.assert_called_once_with("/api/test", params={"key": "val"})


def test_api_get_connection_error_exits_2():
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.ConnectError("refused")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


def test_api_get_timeout_error_exits_2():
    """C1 修复：超时等网络错误应退出码 2，而非抛出堆栈。"""
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.TimeoutException("timeout")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


def test_api_get_read_error_exits_2():
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.ReadError("read error")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


# ─── HTTP 4xx/5xx 错误处理 ────────────────────────────────────────────────────


def test_api_get_http_422_exits_3():
    """FastAPI 422 校验错误应退出码 3。"""
    resp = _make_response({"detail": [{"msg": "field required"}]}, status_code=422)
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


def test_api_get_http_500_exits_3():
    resp = _make_response({"detail": "Internal Server Error"}, status_code=500)
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


def test_api_get_business_error_exits_3():
    """业务 code != 0 应退出码 3。"""
    resp = _make_response({"code": 404, "message": "not found"})
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


# ─── api_post / api_put / api_patch ──────────────────────────────────────────


def test_api_post_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_post("/api/resource", {"name": "test"})
    mock_client.post.assert_called_once_with("/api/resource", json={"name": "test"})


def test_api_post_empty_body_sends_empty_dict():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_post("/api/resource", None)
    mock_client.post.assert_called_once_with("/api/resource", json={})


def test_api_put_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_put("/api/resource/1", {"name": "updated"})
    mock_client.put.assert_called_once_with("/api/resource/1", json={"name": "updated"})


def test_api_patch_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_patch("/api/resource/1", {"enabled": True})
    mock_client.patch.assert_called_once_with("/api/resource/1", json={"enabled": True})


# ─── api_delete ───────────────────────────────────────────────────────────────


def test_api_delete_success():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        result = api_delete("/api/resource/1")
    mock_client.request.assert_called_once_with(
        "DELETE", "/api/resource/1", params=None, json=None
    )
    assert result["code"] == 0


def test_api_delete_with_params():
    """M12 修复：api_delete 支持 params 参数。"""
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_delete("/api/rules/logs", params={"keep_days": 7})
    mock_client.request.assert_called_once_with(
        "DELETE", "/api/rules/logs", params={"keep_days": 7}, json=None
    )


def test_api_delete_with_body():
    """prompt-clear 等新端点：DELETE 带 JSON body。"""
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_delete("/api/miot/scope/cameras/prompt", body={"items": [{"did": "c1"}]})
    mock_client.request.assert_called_once_with(
        "DELETE",
        "/api/miot/scope/cameras/prompt",
        params=None,
        json={"items": [{"did": "c1"}]},
    )


# ─── tls_verify ───────────────────────────────────────────────────────────────


def test_tls_verify_false_by_default():
    """tls_verify 默认为 false，httpx.Client verify 应为 False。"""
    resp = _make_response({"code": 0})
    patcher, _ = _patch_client(resp)
    with patcher as MockClient:
        api_get("/api/test")
    assert MockClient.call_args[1]["verify"] is False


def test_tls_verify_true_when_configured():
    """tls_verify=true 时，httpx.Client verify 应为 True。"""
    from miloco_cli.config import set_value
    set_value("server.tls_verify", "true")
    resp = _make_response({"code": 0})
    patcher, _ = _patch_client(resp)
    with patcher as MockClient:
        api_get("/api/test")
    assert MockClient.call_args[1]["verify"] is True
