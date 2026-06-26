import os
import types


def test_miloco_home_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from hermes.config import miloco_home

    assert miloco_home() == tmp_path


def test_miloco_home_from_hermes_constants(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", fake)
    import importlib
    import hermes.config as mod

    importlib.reload(mod)
    assert mod.miloco_home() == tmp_path / "miloco"


def test_ensure_miloco_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from hermes.config import ensure_miloco_home_env

    result = ensure_miloco_home_env()
    assert os.environ["MILOCO_HOME"] == str(tmp_path)
    assert result == tmp_path


def test_get_plugin_config_returns_entry(monkeypatch):
    data = {"plugins": {"entries": {"miloco": {"debug": True}}}}
    cli = types.ModuleType("hermes_cli")
    cli.config = types.ModuleType("hermes_cli.config")
    cli.config.load_config = lambda: data

    def cfg_get(cfg, *keys, **kw):
        cur = cfg
        for k in keys:
            cur = cur.get(k) if isinstance(cur, dict) else kw.get("default")
            if cur is None:
                return kw.get("default")
        return cur

    cli.config.cfg_get = cfg_get
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli", cli)
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.config", cli.config)
    from hermes.config import get_plugin_config

    assert get_plugin_config(object()) == {"debug": True}
