from unittest.mock import patch

from click.testing import CliRunner

from miloco_cli.commands.observation import observation_group


def test_observation_forwards_correlation_id():
    with patch("miloco_cli.client.api_post", return_value={"status": "disabled"}) as post:
        result = CliRunner().invoke(observation_group, ["run", "--run-id", "patrol-run"])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/run", {"run_id": "patrol-run"})


def test_observation_batch():
    with patch("miloco_cli.client.api_post", return_value={"status": "empty"}) as post:
        result = CliRunner().invoke(observation_group, ["batch"])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/batch")
    assert result.output.strip() == '{"status":"empty"}'


def test_observation_submit_empty_ack_and_invalid_json():
    with patch("miloco_cli.client.api_post", return_value={"status": "accepted"}) as post:
        result = CliRunner().invoke(observation_group, ["submit", "--data", '{"batch_id":"b","observations":[]}'])
    assert result.exit_code == 0, result.output
    post.assert_called_once_with("/api/iot-observation/submit", {"batch_id": "b", "observations": []})
    with patch("miloco_cli.client.api_post") as post:
        result = CliRunner().invoke(observation_group, ["submit", "--data", "broken"])
    assert result.exit_code != 0
    post.assert_not_called()
