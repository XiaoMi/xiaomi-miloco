"""plugins/hermes/miloco-plugin/paths.py 默认值回归防护。

hermes plugin 的 paths.py 只在 hermes runtime 里被加载，跟 openclaw runtime
隔离；默认路径必须对齐自己（~/.hermes/miloco），不能 fallback 到 openclaw
——否则 launchd 场景下 .env 加载失败会 split-brain 到 openclaw 目录。
"""
from __future__ import annotations

from pathlib import Path


def test_miloco_home_defaults_to_hermes_when_env_missing(monkeypatch):
    """MILOCO_HOME 未设置时默认走 ~/.hermes/miloco，绝不 fallback openclaw。"""
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    from miloco_plugin_pkg import paths  # 通过 conftest 装载的别名
    home = paths.miloco_home()
    assert home == Path.home() / ".hermes" / "miloco", (
        f"hermes plugin fallback 必须是 ~/.hermes/miloco，实际 {home}——"
        "不允许默认到 openclaw 路径，否则 env 传递失败时会 split-brain"
    )


def test_miloco_home_uses_env_override(monkeypatch, tmp_path):
    """MILOCO_HOME 设了就用它，不看默认值。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco_plugin_pkg import paths
    assert paths.miloco_home() == tmp_path


def test_miloco_home_expands_tilde(monkeypatch):
    """~ 前缀展开到 $HOME（与 TS 端 env.startsWith('~') 分支一致）。"""
    monkeypatch.setenv("MILOCO_HOME", "~/custom-miloco")
    from miloco_plugin_pkg import paths
    assert paths.miloco_home() == Path.home() / "custom-miloco"
