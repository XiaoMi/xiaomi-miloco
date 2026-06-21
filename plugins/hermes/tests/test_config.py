import json
import os
import sys
import types


from hermes import config


# ---------------------------------------------------------------- miloco_home


def test_miloco_home_env_override(tmp_miloco_home):
    assert config.miloco_home() == tmp_miloco_home


def test_miloco_home_default_derives_from_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    fake = types.ModuleType("hermes_constants")
    hermes_home = tmp_path / "hermes_root"
    fake.get_hermes_home = lambda: hermes_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    assert config.miloco_home() == hermes_home / "miloco"


def test_miloco_home_fallback_without_hermes(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    fake_home = tmp_path / "userhome"
    monkeypatch.setattr("hermes.config.Path.home", lambda: fake_home)
    assert config.miloco_home() == fake_home / ".hermes" / "miloco"


# ----------------------------------------------------------------- config_file


def test_config_file_path(tmp_miloco_home):
    assert config.config_file() == tmp_miloco_home / "config.json"


# ------------------------------------------------------------ read_config_dict


def test_read_config_dict_missing_file(tmp_miloco_home):
    assert config.read_config_dict() == {}


def test_read_config_dict_valid(tmp_miloco_home):
    data = {"omni_model": "gpt-4o", "debug": True}
    (tmp_miloco_home / "config.json").write_text(json.dumps(data))
    assert config.read_config_dict() == data


def test_read_config_dict_invalid_json(tmp_miloco_home):
    (tmp_miloco_home / "config.json").write_text("{not valid json")
    assert config.read_config_dict() == {}


# ----------------------------------------------------------- atomic_write_json


def test_atomic_write_json(tmp_miloco_home):
    data = {"a": 1, "b": [2, 3]}
    config.atomic_write_json(data)
    assert json.loads((tmp_miloco_home / "config.json").read_text()) == data


def test_atomic_write_json_replaces_existing(tmp_miloco_home):
    (tmp_miloco_home / "config.json").write_text('{"old": true}')
    config.atomic_write_json({"new": 1})
    assert json.loads((tmp_miloco_home / "config.json").read_text()) == {"new": 1}
    assert list(tmp_miloco_home.glob("*.tmp*")) == []


# ----------------------------------------------------------------- deep_merge


def test_deep_merge():
    target = {"a": 1, "nested": {"x": 1, "y": 2}}
    source = {"b": 2, "nested": {"y": 20, "z": 3}}
    config.deep_merge(target, source)
    assert target == {"a": 1, "b": 2, "nested": {"x": 1, "y": 20, "z": 3}}


def test_deep_merge_recursive():
    target = {"l1": {"l2": {"a": 1}}}
    source = {"l1": {"l2": {"b": 2}}}
    config.deep_merge(target, source)
    assert target == {"l1": {"l2": {"a": 1, "b": 2}}}


def test_deep_merge_does_not_mutate_source():
    source = {"nested": {"y": 20}}
    target = {"nested": {"x": 1}}
    config.deep_merge(target, source)
    assert source == {"nested": {"y": 20}}


# ----------------------------------------------------- ensure_miloco_home_env


def test_ensure_miloco_home_env_sets_envvar(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    fake = types.ModuleType("hermes_constants")
    hermes_home = tmp_path / "hermes"
    fake.get_hermes_home = lambda: hermes_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    result = config.ensure_miloco_home_env()
    assert os.environ["MILOCO_HOME"] == str(hermes_home / "miloco")
    assert result == hermes_home / "miloco"


def test_ensure_miloco_home_env_respects_existing(tmp_miloco_home):
    result = config.ensure_miloco_home_env()
    assert os.environ["MILOCO_HOME"] == str(tmp_miloco_home)
    assert result == tmp_miloco_home


# ------------------------------------------------------------- DEFAULT_CONFIG


def test_default_config_has_bridge_defaults():
    cfg = config.DEFAULT_CONFIG
    assert cfg["bridge_host"] == "127.0.0.1"
    assert cfg["bridge_port"] == 18789
    assert cfg["bridge_auth_token"] == ""


def test_default_config_has_omni_keys():
    cfg = config.DEFAULT_CONFIG
    for key in (
        "debug",
        "omni_model",
        "omni_base_url",
        "omni_api_key",
        "notify_session_key",
    ):
        assert key in cfg


# ----------------------------------------------------------- get_plugin_config


def _make_mock_cfg_get(data):
    def cfg_get(cfg, *keys, **kw):
        cur = cfg
        for k in keys:
            if isinstance(cur, dict):
                cur = cur.get(k)
            else:
                return kw.get("default")
            if cur is None:
                return kw.get("default")
        return cur

    return cfg_get


def test_get_plugin_config_returns_miloco_entry(monkeypatch):
    data = {"plugins": {"entries": {"miloco": {"debug": True}}}}
    cli = types.ModuleType("hermes_cli")
    cli.config = types.ModuleType("hermes_cli.config")
    cli.config.load_config = lambda: data
    cli.config.cfg_get = _make_mock_cfg_get(data)
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cli.config)
    assert config.get_plugin_config(object()) == {"debug": True}


def test_get_plugin_config_missing_entry_returns_empty(monkeypatch):
    data = {"plugins": {"entries": {}}}
    cli = types.ModuleType("hermes_cli")
    cli.config = types.ModuleType("hermes_cli.config")
    cli.config.load_config = lambda: data
    cli.config.cfg_get = _make_mock_cfg_get(data)
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cli.config)
    assert config.get_plugin_config(object()) == {}


def test_get_plugin_config_import_error_returns_empty(monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    assert config.get_plugin_config(object()) == {}


# -------------------------------------------------------- load_shared_config


def test_load_shared_config_merges_and_writes(tmp_miloco_home, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_plugin_config",
        lambda ctx: {"omni_model": "gpt-4o", "debug": True},
    )
    config.load_shared_config(object())
    written = config.read_config_dict()
    assert written["omni_model"] == "gpt-4o"
    assert written["debug"] is True
    assert written["bridge_host"] == "127.0.0.1"
    assert written["bridge_port"] == 18789
