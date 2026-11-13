from unittest.mock import patch

from click.testing import CliRunner

from miloco_cli.commands.observe import observe_group
from miloco_cli.main import cli


def test_observe_batch():
    with patch("miloco_cli.client.api_post", return_value={"status": "empty"}) as post:
        result = CliRunner().invoke(observe_group, ["batch"])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/batch")
    assert result.output.strip() == '{"status":"empty"}'


def test_observe_command_runs_batch():
    with patch("miloco_cli.client.api_post", return_value={"status": "empty"}) as post:
        result = CliRunner().invoke(cli, ["observe", "batch"])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/batch")


def test_observe_command_runs_submit():
    with patch("miloco_cli.client.api_post", return_value={"status": "accepted"}) as post:
        result = CliRunner().invoke(cli, ["observe", "submit", "--data", '{"batch_id":"b","observations":[]}'])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/submit", {"batch_id": "b", "observations": []})


def test_observe_submit_empty_ack_and_invalid_json():
    with patch("miloco_cli.client.api_post", return_value={"status": "accepted"}) as post:
        result = CliRunner().invoke(observe_group, ["submit", "--data", '{"batch_id":"b","observations":[]}'])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/submit", {"batch_id": "b", "observations": []})
    with patch("miloco_cli.client.api_post") as post:
        result = CliRunner().invoke(observe_group, ["submit", "--data", "broken"])
    assert result.exit_code != 0
    post.assert_not_called()
