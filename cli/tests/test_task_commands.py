"""CLI task 子命令测试:args 校验 + HTTP body 构造。"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.commands.task import task_group


@pytest.fixture
def runner():
    return CliRunner()


def test_create_posts_minimal_body(runner):
    """方案 P：task create body 收窄为 ``{task_id, description}``。"""
    with patch("miloco_cli.client.api_post") as mock_post:
        mock_post.return_value = {
            "code": 0,
            "message": "ok",
            "data": {"task_id": "t1"},
        }
        result = runner.invoke(
            task_group,
            ["create", "--task-id", "t1", "--description", "d"],
        )
        assert result.exit_code == 0, result.output
        path, body = mock_post.call_args.args
        assert path == "/api/tasks"
        assert body == {"task_id": "t1", "description": "d"}


def test_create_no_longer_accepts_refs(runner):
    """方案 P：``--rule`` / ``--cron`` / ``--memory`` 选项已移除。"""
    result = runner.invoke(
        task_group,
        ["create", "--task-id", "t1", "--description", "d", "--rule", "r1"],
    )
    assert result.exit_code != 0
    assert "No such option" in result.output or "no such option" in result.output.lower()


def test_update_uses_patch(runner):
    with patch("miloco_cli.client.api_patch") as mock_patch:
        mock_patch.return_value = {"code": 0}
        result = runner.invoke(
            task_group, ["update", "t1", "--description", "new desc"]
        )
        assert result.exit_code == 0
        path, body = mock_patch.call_args.args
        assert path == "/api/tasks/t1"
        assert body == {"description": "new desc"}


def test_list_calls_get(runner):
    with patch("miloco_cli.client.api_get") as mock_get:
        mock_get.return_value = {"code": 0, "data": []}
        result = runner.invoke(task_group, ["list"])
        assert result.exit_code == 0
        (path,) = mock_get.call_args.args
        assert path == "/api/tasks"


def test_get_calls_correct_path(runner):
    with patch("miloco_cli.client.api_get") as mock_get:
        mock_get.return_value = {"code": 0, "data": {}}
        runner.invoke(task_group, ["get", "t1"])
        (path,) = mock_get.call_args.args
        assert path == "/api/tasks/t1"


def test_disable_posts(runner):
    with patch("miloco_cli.client.api_post") as mock_post:
        mock_post.return_value = {"code": 0}
        runner.invoke(task_group, ["disable", "t1"])
        path = mock_post.call_args.args[0]
        assert path == "/api/tasks/t1/disable"


def test_enable_posts(runner):
    with patch("miloco_cli.client.api_post") as mock_post:
        mock_post.return_value = {"code": 0}
        runner.invoke(task_group, ["enable", "t1"])
        path = mock_post.call_args.args[0]
        assert path == "/api/tasks/t1/enable"


def test_delete_uses_delete_verb(runner):
    with patch("miloco_cli.client.api_delete") as mock_del:
        mock_del.return_value = {"code": 0}
        runner.invoke(task_group, ["delete", "t1"])
        path = mock_del.call_args.args[0]
        assert path == "/api/tasks/t1"


def test_summary_default_window(runner):
    """task summary 默认 --window day,走 GET /api/tasks/summary?window=day。"""
    with patch("miloco_cli.client.api_get") as mock_get:
        mock_get.return_value = {"code": 0, "message": "ok", "data": []}
        result = runner.invoke(task_group, ["summary"])
        assert result.exit_code == 0, result.output
        path = mock_get.call_args.args[0]
        assert path == "/api/tasks/summary?window=day"


def test_summary_window_all_flag(runner):
    """--window all 透传到 query string。"""
    with patch("miloco_cli.client.api_get") as mock_get:
        mock_get.return_value = {"code": 0, "data": []}
        result = runner.invoke(task_group, ["summary", "--window", "all"])
        assert result.exit_code == 0
        path = mock_get.call_args.args[0]
        assert path == "/api/tasks/summary?window=all"


def test_summary_rejects_unsupported_window(runner):
    """--window week/month 被 Click Choice 拒绝(后端也不支持)。"""
    result = runner.invoke(task_group, ["summary", "--window", "week"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid" in result.output.lower()


def test_summary_pretty_flag(runner):
    """--pretty 输出 JSON pretty print(包含换行和缩进)。"""
    with patch("miloco_cli.client.api_get") as mock_get:
        mock_get.return_value = {"code": 0, "data": [{"task_id": "t1"}]}
        result = runner.invoke(task_group, ["summary", "--pretty"])
        assert result.exit_code == 0
        assert '"task_id"' in result.output
