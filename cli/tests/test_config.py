"""config.py 测试（unify-config-management 后的嵌套结构）。"""

import json

import pytest

from miloco_cli.config import (
    atomic_write,
    get_value,
    known_paths,
    load_config,
    set_value,
    set_values,
    show_config,
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """通过 ``$MILOCO_HOME`` 将 config.json 重定向到临时目录。"""
    config_dir = tmp_path / "miloco"
    # 清空所有 MILOCO_* 环境变量避免污染测试
    import os as _os

    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))
    return config_dir / "config.json"


# ─── load_config / show_config ───────────────────────────────────────────────


def test_load_config_returns_defaults_when_no_file():
    cfg = load_config()
    assert cfg["server"]["url"] == "http://127.0.0.1:1810"
    assert cfg["server"]["token"] == ""
    assert cfg["server"]["tls_verify"] is False
    assert cfg["model"]["omni"]["model"] == "xiaomi/mimo-v2.5"
    assert cfg["debug"] is False
    # 扁平兼容字段已移除：调用方必须使用嵌套 cfg["server"]["url"] 等路径
    assert "server_url" not in cfg
    assert "token" not in cfg
    assert "tls_verify" not in cfg


def test_load_config_merges_file(isolated_config):
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(
        json.dumps({"server": {"url": "http://192.168.1.1:1810"}})
    )
    cfg = load_config()
    assert cfg["server"]["url"] == "http://192.168.1.1:1810"
    assert cfg["server"]["token"] == ""  # default preserved


def test_load_config_env_override(isolated_config, monkeypatch):
    monkeypatch.setenv("MILOCO_SERVER__URL", "http://env-url:9000")
    cfg = load_config()
    assert cfg["server"]["url"] == "http://env-url:9000"


def test_load_config_env_bool_override(monkeypatch):
    monkeypatch.setenv("MILOCO_DEBUG", "true")
    cfg = load_config()
    assert cfg["debug"] is True


def test_show_config_only_nested_keys():
    """show_config 只返回嵌套结构，不再注入扁平 server_url / token / tls_verify。"""
    data = show_config()
    assert "server_url" not in data
    assert "token" not in data
    assert "tls_verify" not in data
    assert "server" in data
    assert "model" in data


def test_load_config_handles_corrupt_json(isolated_config):
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("NOT JSON {{{{")
    cfg = load_config()
    assert cfg["server"]["url"] == "http://127.0.0.1:1810"


# ─── atomic_write ────────────────────────────────────────────────────────────


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "sub" / "config.json"
    data = {"key": "value"}
    atomic_write(target, data)
    assert target.exists()
    assert json.loads(target.read_text()) == data


def test_atomic_write_no_tmp_leftover(tmp_path):
    target = tmp_path / "config.json"
    atomic_write(target, {"x": 1})
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


# ─── set_value / get_value ───────────────────────────────────────────────────


def test_set_value_persists_nested(isolated_config):
    set_value("server.url", "http://new-url:1810")
    data = json.loads(isolated_config.read_text())
    assert data["server"]["url"] == "http://new-url:1810"


def test_set_value_coerces_bool(isolated_config):
    set_value("server.tls_verify", "true")
    data = json.loads(isolated_config.read_text())
    assert data["server"]["tls_verify"] is True


def test_set_value_rejects_unknown_path():
    with pytest.raises(ValueError):
        set_value("server.unknown", "value")


def test_set_value_rejects_bad_bool():
    with pytest.raises(ValueError):
        set_value("server.tls_verify", "maybe")


def test_min_suggestion_urgency_accepts_valid_values(isolated_config):
    """low / medium / high 三档均可写入(大小写不敏感由 _coerce 归一)。"""
    for v in ("low", "MEDIUM", "High"):
        set_value("perception.min_suggestion_urgency", v)
        data = json.loads(isolated_config.read_text())
        assert data["perception"]["min_suggestion_urgency"] == v.lower()


def test_min_suggestion_urgency_rejects_bogus():
    with pytest.raises(ValueError, match="min_suggestion_urgency"):
        set_value("perception.min_suggestion_urgency", "urgent")


def test_min_suggestion_urgency_default_is_low():
    """默认值 low = 不过滤,与 backend PerceptionSettings.Literal default 对齐。"""
    cfg = load_config()
    assert cfg["perception"]["min_suggestion_urgency"] == "low"


def test_get_value_reads_persisted(isolated_config):
    set_value("model.omni.api_key", "sk-xxx")
    assert get_value("model.omni.api_key") == "sk-xxx"


def test_get_value_missing_path_raises():
    with pytest.raises(KeyError):
        get_value("server.does_not_exist")


def test_known_paths_includes_all_scopes():
    paths = known_paths()
    assert "debug" in paths
    assert "server.url" in paths
    assert "server.python_bin" in paths
    assert "model.omni.api_key" in paths


def test_set_value_does_not_bake_env_var(isolated_config, monkeypatch):
    """set_value 只写入本次显式设置的 path，不把环境变量固化进文件。"""
    monkeypatch.setenv("MILOCO_SERVER__URL", "http://env-url:9000")
    set_value("server.token", "mytoken")
    data = json.loads(isolated_config.read_text())
    assert data["server"]["token"] == "mytoken"
    # env 的 url 不应写入文件（只有 set 显式设置的 key 被写）
    assert (
        "url" not in data.get("server", {})
        or data["server"]["url"] != "http://env-url:9000"
    )


# ─── 结构校验（load_config 对非法顶层结构的防御） ────────────────────────────


def test_load_config_rejects_nondict_server(isolated_config):
    """手改 config.json 把 server 设成字符串时，load_config 应抛 ValueError。"""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({"server": "not-a-dict"}))
    with pytest.raises(ValueError, match="server"):
        load_config()


def test_load_config_rejects_nondict_model_omni(isolated_config):
    """中间层 model.omni 也需校验：不是 dict 则报错，而非 TypeError 指向调用方。"""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({"model": {"omni": "oops"}}))
    with pytest.raises(ValueError, match="model.omni"):
        load_config()


def test_load_config_accepts_missing_subtree(isolated_config):
    """只出现部分键是允许的（缺失走默认值），不应误报结构错误。"""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({"debug": True}))
    cfg = load_config()
    assert cfg["debug"] is True
    assert cfg["server"]["url"] == "http://127.0.0.1:1810"


# ─── set_values 批量原子写入 ─────────────────────────────────────────────────


def test_set_values_writes_all_pairs(isolated_config):
    result = set_values(
        [
            ("model.omni.model", "xiaomi/mimo-v2.5"),
            ("model.omni.base_url", "https://api.xiaomimimo.com/v1"),
            ("model.omni.api_key", "sk-abc"),
        ]
    )
    assert result == {
        "model.omni.model": "xiaomi/mimo-v2.5",
        "model.omni.base_url": "https://api.xiaomimimo.com/v1",
        "model.omni.api_key": "sk-abc",
    }
    data = json.loads(isolated_config.read_text())
    assert data["model"]["omni"]["model"] == "xiaomi/mimo-v2.5"
    assert data["model"]["omni"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert data["model"]["omni"]["api_key"] == "sk-abc"


def test_set_values_empty_is_noop(isolated_config):
    assert set_values([]) == {}
    assert not isolated_config.exists()


def test_set_values_is_atomic_on_invalid_path(isolated_config):
    """若任一 pair 的 path 非法，整体不落盘——避免半更新的 config.json。"""
    with pytest.raises(ValueError):
        set_values(
            [
                ("server.url", "http://should-not-persist:1810"),
                ("server.bogus_unknown", "x"),
            ]
        )
    # 文件要么不存在，要么不包含本次尝试写入的任何值
    if isolated_config.exists():
        data = json.loads(isolated_config.read_text())
        assert data.get("server", {}).get("url") != "http://should-not-persist:1810"


def test_set_values_is_atomic_on_bad_coerce(isolated_config):
    """bool 强转失败时整体回滚，同一批的其它合法 pair 也不应被写入。"""
    with pytest.raises(ValueError):
        set_values(
            [
                ("server.url", "http://also-not-persisted:1810"),
                ("server.tls_verify", "maybe"),
            ]
        )
    if isolated_config.exists():
        data = json.loads(isolated_config.read_text())
        assert data.get("server", {}).get("url") != "http://also-not-persisted:1810"


def test_set_value_delegates_to_set_values(isolated_config):
    """set_value 现在是 set_values 的糖；保证返回单值语义不变。"""
    assert set_value("server.tls_verify", "true") is True
    assert set_value("server.url", "http://x:1810") == "http://x:1810"
