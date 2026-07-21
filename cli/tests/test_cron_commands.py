"""CLI cron 子命令测试: 边界透传 (校验在 backend router 侧, 不在 CLI 层)。"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.commands.cron import cron_group


@pytest.fixture
def runner():
    return CliRunner()


def test_at_iso_passthrough_to_body(runner):
    """--at-iso 原样透传到 body['at_iso'], 不做本地转换。

    naive/malformed/past/10y 上限 全在 backend router _validate_at_iso 校,
    这里只验 CLI 不擅自加工。
    """
    iso = "2026-06-11T09:00:00+08:00"
    with patch("miloco_cli.client.api_post") as mock_post:
        mock_post.return_value = {"code": 0, "data": {"id": "c1"}}
        result = runner.invoke(
            cron_group,
            [
                "add",
                "--name",
                "[t1] test",
                "--kind",
                "at",
                "--task-id",
                "t1",
                "--message",
                "m",
                "--at-iso",
                iso,
            ],
        )
        assert result.exit_code == 0, result.output
        _, body = mock_post.call_args.args
        assert body["at_iso"] == iso
        assert "at_ms" not in body


def test_cron_kind_omits_at_iso(runner):
    """kind=cron 不传 --at-iso; 传了但 kind=cron 时 CLI 也不加 at_iso 到 body。"""
    with patch("miloco_cli.client.api_post") as mock_post:
        mock_post.return_value = {"code": 0, "data": {"id": "c1"}}
        result = runner.invoke(
            cron_group,
            [
                "add",
                "--name",
                "[t1] daily",
                "--kind",
                "cron",
                "--task-id",
                "t1",
                "--message",
                "m",
                "--cron-expr",
                "0 9 * * *",
                "--tz",
                "Asia/Shanghai",
            ],
        )
        assert result.exit_code == 0, result.output
        _, body = mock_post.call_args.args
        assert body["kind"] == "cron"
        assert "at_iso" not in body
        assert "at_ms" not in body
        assert body["cron_expr"] == "0 9 * * *"
        assert body["tz"] == "Asia/Shanghai"
