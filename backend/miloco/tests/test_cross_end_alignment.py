"""跨端字段对齐（backend 侧）：加载共享 fixture 后断言 pydantic 模型字段一致。

配合 CLI 侧 `cli/tests/test_cross_end_alignment.py` 与 openclaw 插件
`src/__tests__/cross_end.test.ts` 共同验证：同一份 `config.sample.json`
在三端加载后字段语义完全一致。
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from miloco.config.settings import get_settings, reset_settings

_FIXTURE = (
    Path(__file__).resolve().parents[0]
    / "fixtures"
    / "config.sample.json"
)


@pytest.fixture
def fixture_home(tmp_path, monkeypatch):
    home = tmp_path / "miloco-home"
    home.mkdir()
    shutil.copy(_FIXTURE, home / "config.json")
    monkeypatch.setenv("MILOCO_HOME", str(home))
    # 清空其他 MILOCO_* 环境变量避免污染
    for key in list(os.environ):
        if key.startswith("MILOCO_") and key != "MILOCO_HOME":
            monkeypatch.delenv(key, raising=False)
    reset_settings()
    yield home
    reset_settings()


def test_backend_load_matches_fixture(fixture_home):
    settings = get_settings()
    expected = json.loads(_FIXTURE.read_text())
    assert settings.debug is expected["debug"]
    assert settings.server.url == expected["server"]["url"]
    assert settings.server.token == expected["server"]["token"]
    assert settings.server.tls_verify is expected["server"]["tls_verify"]
    assert settings.server.python_bin == expected["server"]["python_bin"]
    assert settings.agent.webhook_url == expected["agent"]["webhook_url"]
    assert settings.agent.auth_bearer == expected["agent"]["auth_bearer"]
    assert settings.model.omni.model == expected["model"]["omni"]["model"]
    assert settings.model.omni.base_url == expected["model"]["omni"]["base_url"]
    assert settings.model.omni.api_key == expected["model"]["omni"]["api_key"]
    # scheduler / notify：openclaw 插件消费方，键名/形状必须与 fixture 一致
    assert settings.scheduler.enabled is expected["scheduler"]["enabled"]
    assert (
        settings.notify.dedup_window_sec
        == expected["notify"]["dedup_window_sec"]
    )
