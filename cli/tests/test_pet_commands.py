"""pet 命令组测试：CliRunner + mock 底层 API 调用。"""

from unittest.mock import patch

from click.testing import CliRunner

from miloco_cli.main import cli


def test_pet_list():
    with patch(
        "miloco_cli.client.api_get", return_value={"code": 0, "data": {"pets": []}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "list"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets")


def test_pet_add():
    with patch(
        "miloco_cli.client.api_post",
        return_value={"code": 0, "data": {"id": "pet_abc", "name": "小黑"}},
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "add", "--name", "小黑", "--species", "猫"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets", {"name": "小黑", "species": "猫"})


def test_pet_add_requires_name():
    r = CliRunner().invoke(cli, ["pet", "add", "--species", "猫"])
    assert r.exit_code != 0  # --name required


def test_pet_update():
    with patch(
        "miloco_cli.client.api_patch", return_value={"code": 0, "data": {}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "update", "pet_abc", "--name", "小白"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets/pet_abc", {"name": "小白"})


def test_pet_update_no_fields_errors():
    r = CliRunner().invoke(cli, ["pet", "update", "pet_abc"])
    assert r.exit_code != 0


def test_pet_delete():
    with patch(
        "miloco_cli.client.api_delete", return_value={"code": 0, "data": {}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "delete", "pet_abc"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets/pet_abc")
