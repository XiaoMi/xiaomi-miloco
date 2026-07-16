"""跨端字段对齐（CLI 侧）：加载共享 fixture 后断言嵌套字段和 TS/backend 约定一致。

配合 backend 侧 `tests/test_cross_end_alignment.py` 与 openclaw 插件
`src/__tests__/cross_end.test.ts` 共同验证：同一份 `config.sample.json`
在三端加载后字段语义完全一致。
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from miloco_cli.config import load_config

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "miloco"
    / "tests"
    / "fixtures"
    / "config.sample.json"
)


@pytest.fixture
def fixture_home(tmp_path, monkeypatch):
    home = tmp_path / "miloco-home"
    home.mkdir()
    shutil.copy(_FIXTURE, home / "config.json")
    # 清空所有 MILOCO_* 环境变量后再 setenv，避免残留环境覆盖 fixture
    for key in list(os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(home))
    return home


def test_cli_load_matches_fixture(fixture_home):
    cfg = load_config()
    expected = json.loads(_FIXTURE.read_text())
    assert cfg["debug"] is expected["debug"]
    assert cfg["server"]["url"] == expected["server"]["url"]
    assert cfg["server"]["token"] == expected["server"]["token"]
    assert cfg["server"]["tls_verify"] is expected["server"]["tls_verify"]
    assert cfg["server"]["python_bin"] == expected["server"]["python_bin"]
    assert cfg["agent"]["webhook_url"] == expected["agent"]["webhook_url"]
    assert cfg["agent"]["auth_bearer"] == expected["agent"]["auth_bearer"]
    assert cfg["model"]["omni"]["model"] == expected["model"]["omni"]["model"]
    assert (
        cfg["model"]["omni"]["base_url"] == expected["model"]["omni"]["base_url"]
    )
    assert cfg["model"]["omni"]["api_key"] == expected["model"]["omni"]["api_key"]
    # scheduler / notify：openclaw 插件消费方，键名/形状必须与 fixture 一致
    assert cfg["scheduler"]["enabled"] is expected["scheduler"]["enabled"]
    assert (
        cfg["notify"]["dedup_window_sec"]
        == expected["notify"]["dedup_window_sec"]
    )
