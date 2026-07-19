"""``miloco-cli task record`` 子组 + ``task delete --reason`` 测试。

mock 底层 ``api_*`` 调用，验证参数解析与 endpoint path 正确。
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "miloco"
    import os as _os

    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))
    return config_dir / "config.json"


_OK = {"code": 0, "message": "ok", "data": {"derived": {"remaining": 7}}}


def test_record_init_progress(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "init",
                "p1",
                "--kind",
                "progress",
                "--content",
                '{"target": 8, "unit": "杯", "window": "day", "recurring_pattern": {"window": "day"}}',
            ],
        )
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    path, body = m.call_args[0]
    assert path == "/api/tasks/p1/record"
    assert body == {
        "kind": "progress",
        "content": {
            "target": 8,
            "unit": "杯",
            "window": "day",
            "recurring_pattern": {"window": "day"},
        },
    }


def test_record_init_progress_rejected_without_pattern_or_expires(runner):
    result = runner.invoke(
        cli,
        [
            "task",
            "record",
            "init",
            "p1",
            "--kind",
            "progress",
            "--content",
            '{"target": 8, "unit": "杯", "window": "day"}',
        ],
    )
    assert result.exit_code == 1
    assert "必须明示 recurring_pattern 或 expires_at" in result.output


def test_record_init_duration_rejected_without_pattern_or_expires(runner):
    result = runner.invoke(
        cli,
        [
            "task",
            "record",
            "init",
            "d1",
            "--kind",
            "duration",
            "--content",
            '{"target_minutes": 5}',
        ],
    )
    assert result.exit_code == 1
    assert "必须明示 recurring_pattern 或 expires_at" in result.output


def test_record_init_event_not_blocked_by_pattern_check(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            ["task", "record", "init", "e1", "--kind", "event", "--content", "{}"],
        )
    assert result.exit_code == 0, result.output
    m.assert_called_once()


def test_record_init_progress_accepts_expires_at(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "init",
                "p2",
                "--kind",
                "progress",
                "--content",
                '{"target": 8, "unit": "杯", "window": "day", "expires_at": "2026-06-16T23:59:59+08:00"}',
            ],
        )
    assert result.exit_code == 0, result.output
    m.assert_called_once()


def test_record_init_invalid_json(runner):
    result = runner.invoke(
        cli,
        ["task", "record", "init", "p1", "--kind", "progress", "--content", "{bad"],
    )
    assert result.exit_code == 1
    assert "不是合法 JSON" in result.output or "不是合法 JSON" in (result.stderr or "")


def test_record_get(runner):
    with patch("miloco_cli.client.api_get", return_value=_OK) as m:
        result = runner.invoke(cli, ["task", "record", "get", "p1"])
    assert result.exit_code == 0
    m.assert_called_once_with("/api/tasks/p1/record")


def test_record_progress_inc_default_delta(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(cli, ["task", "record", "progress-inc", "p1"])
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert path == "/api/tasks/p1/record/progress/increment"
    assert body == {"delta": 1}


def test_record_progress_inc_negative_delta(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli, ["task", "record", "progress-inc", "p1", "--delta", "-2"]
        )
    assert result.exit_code == 0
    body = m.call_args[0][1]
    assert body == {"delta": -2}


def test_record_event_append_with_at(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "event-append",
                "e1",
                "--description",
                "喝水",
                "--at",
                "2026-06-10T09:00:00",
            ],
        )
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert path == "/api/tasks/e1/record/event/append"
    assert body == {"description": "喝水", "at": "2026-06-10T09:00:00"}


def test_record_session_start_no_at(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(cli, ["task", "record", "session-start", "d1"])
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert path == "/api/tasks/d1/record/session/start"
    assert body == {}


def test_record_session_end_at_strips_literal_quotes(runner):
    """OpenClaw exec tool 不解析 shell 引号，agent 传 --at "..." 时
    CLI 收到带字面双引号的字符串，需要剥一层。"""
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "session-end",
                "d1",
                "--at",
                '"2026-06-15T10:44:05+08:00"',
            ],
        )
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert path == "/api/tasks/d1/record/session/end"
    assert body == {"at": "2026-06-15T10:44:05+08:00"}


def test_record_session_start_at_strips_literal_single_quotes(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "session-start",
                "d1",
                "--at",
                "'2026-06-15T10:42:56+08:00'",
            ],
        )
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert body == {"at": "2026-06-15T10:42:56+08:00"}


def test_record_event_append_at_strips_literal_quotes(runner):
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "event-append",
                "e1",
                "--description",
                "喝水",
                "--at",
                '"2026-06-10T09:00:00"',
            ],
        )
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert body == {"description": "喝水", "at": "2026-06-10T09:00:00"}


def test_record_update_patch(runner):
    with patch("miloco_cli.client.api_patch", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            [
                "task",
                "record",
                "update",
                "p1",
                "--patch",
                '{"target": 10, "unit": "次"}',
            ],
        )
    assert result.exit_code == 0
    path, body = m.call_args[0]
    assert path == "/api/tasks/p1/record"
    assert body == {"target": 10, "unit": "次"}


def test_record_compute_with_date(runner):
    """--date 单独使用,不能同时传 --window(否则 backend 422 拒)。"""
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            ["task", "record", "compute", "p1", "--date", "2026-06-09"],
        )
    assert result.exit_code == 0
    path = m.call_args[0][0]
    assert path.startswith("/api/tasks/p1/record/compute?")
    assert "date=2026-06-09" in path
    assert "window=" not in path  # 不应透传 window


def test_record_compute_date_window_conflict_rejected(runner):
    """CLI 层先拒 --date + --window 共存(避免发到 backend 才报错)。"""
    result = runner.invoke(
        cli,
        [
            "task",
            "record",
            "compute",
            "p1",
            "--window",
            "day",
            "--date",
            "2026-06-09",
        ],
    )
    assert result.exit_code == 1
    assert "互斥" in result.output or "互斥" in (result.stderr or "")


def test_record_compute_date_alias_yesterday(runner):
    """F3 兜底：--date yesterday 由 CLI 端解析成具体 YYYY-MM-DD。"""
    import re

    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        result = runner.invoke(
            cli,
            ["task", "record", "compute", "p1", "--date", "yesterday"],
        )
    assert result.exit_code == 0
    path = m.call_args[0][0]
    # 不应该直接传 yesterday 字面量
    assert "date=yesterday" not in path
    # 应该是 YYYY-MM-DD
    assert re.search(r"date=\d{4}-\d{2}-\d{2}", path) is not None


def test_record_compute_range_from_to(runner):
    """G1：CLI --from/--to 拼 query string，--date 别名同样兜底解析。"""
    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        runner.invoke(
            cli,
            [
                "task",
                "record",
                "compute",
                "p1",
                "--from",
                "2026-06-01",
                "--to",
                "2026-06-07",
            ],
        )
    path = m.call_args[0][0]
    assert "from=2026-06-01" in path
    assert "to=2026-06-07" in path
    assert "window=" not in path  # 区间模式不传 window


def test_record_compute_from_without_to_errors(runner):
    """CLI 层先 reject 不 paired 用法（不发请求到 backend）。"""
    result = runner.invoke(
        cli,
        ["task", "record", "compute", "p1", "--from", "2026-06-01"],
    )
    assert result.exit_code == 1


def test_record_archive_list(runner):
    """G2：task record archive list <task_id>。"""
    archives_resp = {
        "code": 0,
        "message": "ok",
        "data": {"kind": "event", "archives": [{"date": "2026-06-09", "count": 3}]},
    }
    with patch("miloco_cli.client.api_get", return_value=archives_resp) as m:
        result = runner.invoke(
            cli, ["task", "record", "archive", "list", "p1"]
        )
    assert result.exit_code == 0, result.output
    m.assert_called_once_with("/api/tasks/p1/record/archives")


def test_record_compute_date_alias_today(runner):
    import re

    with patch("miloco_cli.client.api_post", return_value=_OK) as m:
        runner.invoke(
            cli,
            ["task", "record", "compute", "p1", "--date", "today"],
        )
    path = m.call_args[0][0]
    assert "date=today" not in path
    assert re.search(r"date=\d{4}-\d{2}-\d{2}", path) is not None


def test_task_delete_with_reason(runner):
    with patch("miloco_cli.client.api_delete", return_value=_OK) as m:
        result = runner.invoke(cli, ["task", "delete", "t1", "--reason", "abandoned"])
    assert result.exit_code == 0
    args, kwargs = m.call_args
    assert args[0] == "/api/tasks/t1"
    assert kwargs == {"params": {"reason": "abandoned"}}


def test_task_delete_default_reason(runner):
    with patch("miloco_cli.client.api_delete", return_value=_OK) as m:
        runner.invoke(cli, ["task", "delete", "t1"])
    kwargs = m.call_args.kwargs
    assert kwargs == {"params": {"reason": "completed"}}
