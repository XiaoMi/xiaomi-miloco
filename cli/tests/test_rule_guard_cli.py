"""前提方向在 CLI 上的透传。"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.main import cli

_SPEC = {
    "code": 0,
    "data": {"spec": {"prop.2.1": {"format": "bool", "description": "开关"}}},
}
_OK = {"code": 0, "message": "ok", "data": {"rule_id": "g1"}}


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    import os as _os

    for key in [k for k in _os.environ if k.startswith("MILOCO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco"))


def _create_guard(runner):
    """前提不配动作 —— 动作参数一个都不给, 走的是 CLI 那张 direction x action 表。"""
    with (
        patch("miloco_cli.client.api_get", return_value=_SPEC),
        patch("miloco_cli.client.api_post", return_value=_OK) as post,
    ):
        result = runner.invoke(
            cli,
            [
                "rule", "create",
                "--name", "空调开着",
                "--task-id", "ac_on_guard",
                "--direction", "guard",
                "--iot-did", "d1",
                "--iot-iid", "2.1",
                "--iot-op", "eq",
                "--iot-value", "true",
            ],
        )
    return result, post


def test_guard_without_actions_is_accepted(runner):
    result, post = _create_guard(runner)

    assert result.exit_code == 0, result.output
    assert post.called


def test_guard_direction_reaches_the_payload(runner):
    _result, post = _create_guard(runner)

    assert post.call_args[0][1]["direction"] == "guard"


def test_guard_stores_a_placeholder_mode(runner):
    """mode 列是 NOT NULL 而它表达不了 guard, 存一个自洽的占位值。"""
    _result, post = _create_guard(runner)

    assert post.call_args[0][1]["mode"] == "event"
