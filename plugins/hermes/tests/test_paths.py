"""plugins/hermes/miloco-plugin/paths.py 默认值回归防护。

hermes plugin 的 paths.py 只在 hermes runtime 里被加载，跟 openclaw runtime
隔离；默认路径必须跟随 HERMES_HOME（HERMES_HOME/miloco），不能 fallback 到
openclaw 路径——否则 launchd 拉 gateway 时 .env 加载失败会 split-brain 到
openclaw 目录。

测试是守护主流程正确性的，不是反向约束主流程的：主流程对齐 install-hermes.sh
安装脚本中的 HERMES_HOME→MILOCO_HOME 推导，测试断言随之演化。
"""
from __future__ import annotations

from pathlib import Path


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


def test_miloco_home_follows_hermes_home_when_miloco_home_unset(monkeypatch, tmp_path):
    """MILOCO_HOME 未设时跟随 HERMES_HOME（与安装脚本中的推导镜像）。"""
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from miloco_plugin_pkg import paths
    assert paths.miloco_home() == tmp_path / "miloco"


def test_miloco_home_hermes_home_tilde_expands(monkeypatch):
    """HERMES_HOME 也支持 ~ 展开。"""
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", "~/my-hermes")
    from miloco_plugin_pkg import paths
    assert paths.miloco_home() == Path.home() / "my-hermes" / "miloco"


def test_miloco_home_falls_back_to_user_hermes_when_both_unset(monkeypatch):
    """MILOCO_HOME 和 HERMES_HOME 都没设时，落 ~/.hermes/miloco 作为最终默认。"""
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    from miloco_plugin_pkg import paths
    home = paths.miloco_home()
    assert home == Path.home() / ".hermes" / "miloco", (
        f"hermes plugin fallback 必须是 ~/.hermes/miloco，实际 {home}——"
        "不允许默认到 openclaw 路径，否则 env 传递失败时会 split-brain"
    )
