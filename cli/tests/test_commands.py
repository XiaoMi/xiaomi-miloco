"""CLI 命令测试：使用 Click CliRunner，mock 底层 API 调用。"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.main import cli

# ─── Fixtures ─────────────────────────────────────────────────────────────────

_SUCCESS = {"code": 0, "message": "ok", "data": None}


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "miloco"
    # 清空所有 MILOCO_* 环境变量避免污染测试
    import os as _os

    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))
    return config_dir / "config.json"


@pytest.fixture()
def fake_home_info(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    info = {
        "updated_at": datetime.now(UTC).isoformat(),
        "home_name": "我的家",
        "devices": [
            {
                "did": "lamp_001",
                "name": "台灯",
                "room": "客厅",
                "category": "light",
                "online": True,
                "spec": {
                    "prop.2.1": {"type_name": "on", "type": "bool"},
                    "prop.2.2": {
                        "type_name": "brightness",
                        "type": "int",
                        "value_range": [0, 100],
                    },
                },
            },
        ],
        "scenes": [{"id": "s1", "name": "回家"}],
        "persons": [{"id": "p1", "name": "爸爸"}],
    }
    monkeypatch.setattr(
        "miloco_cli.home_info._fetch",
        lambda **kwargs: info,
    )
    return info


# ─── version ──────────────────────────────────────────────────────────────────


def test_version(runner):
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "version" in data


def test_version_pretty(runner):
    result = runner.invoke(cli, ["version", "--pretty"])
    assert result.exit_code == 0
    assert "\n" in result.output


# ─── config show / get / set ──────────────────────────────────────────────────


def test_config_show(runner):
    result = runner.invoke(cli, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "server" in data and "url" in data["server"]


def test_config_show_masks_token(runner, isolated_config):
    from miloco_cli.config import set_value

    set_value("server.token", "secret-token")
    result = runner.invoke(cli, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["server"]["token"] == "***"


def test_config_show_unmasked(runner, isolated_config):
    from miloco_cli.config import set_value

    set_value("server.token", "secret-token")
    result = runner.invoke(cli, ["config", "show", "--unmasked"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["server"]["token"] == "secret-token"


def test_config_get_existing(runner):
    result = runner.invoke(cli, ["config", "get", "server.url"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "server.url"
    assert data["value"] == "http://127.0.0.1:1810"


def test_config_get_missing_exits(runner):
    result = runner.invoke(cli, ["config", "get", "server.does_not_exist"])
    assert result.exit_code != 0


def test_config_set_server_url_no_restart(runner, isolated_config, monkeypatch):
    result = runner.invoke(
        cli, ["config", "set", "server.url", "http://10.0.0.1:1810", "--no-restart"]
    )
    assert result.exit_code == 0
    from miloco_cli.config import load_config

    cfg = load_config()
    assert cfg["server"]["url"] == "http://10.0.0.1:1810"


def test_config_set_bool_coerces(runner, isolated_config):
    result = runner.invoke(
        cli, ["config", "set", "server.tls_verify", "true", "--no-restart"]
    )
    assert result.exit_code == 0
    from miloco_cli.config import load_config

    cfg = load_config()
    assert cfg["server"]["tls_verify"] is True


def test_config_set_unknown_path_errors(runner):
    result = runner.invoke(
        cli, ["config", "set", "server.nonsense", "x", "--no-restart"]
    )
    assert result.exit_code != 0


def test_config_set_triggers_restart_when_running(runner, isolated_config, monkeypatch):
    """后端运行态下，``config set`` 默认自动触发 service restart。"""
    import miloco_cli.commands.config as cfg_cmd

    called = {}

    def fake_restart_if_running(pretty):
        called["pretty"] = pretty
        return {"triggered": True}

    monkeypatch.setattr(cfg_cmd, "_restart_if_running", fake_restart_if_running)
    result = runner.invoke(cli, ["config", "set", "server.token", "abc"])
    assert result.exit_code == 0
    assert called == {"pretty": False}
    data = json.loads(result.output)
    assert data["restart"] == {"triggered": True}


def test_config_list_paths(runner):
    result = runner.invoke(cli, ["config", "list-paths"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    paths = {item["path"] for item in data}
    assert "server.url" in paths
    assert "model.omni.api_key" in paths


def test_config_get_value_only_outputs_bare_value(runner, isolated_config):
    """``config get --value-only`` 输出裸值, 便于 shell 脚本免 JSON 解析。"""
    from miloco_cli.config import set_value

    set_value("server.url", "http://10.0.0.1:1810")
    result = runner.invoke(cli, ["config", "get", "server.url", "--value-only"])
    assert result.exit_code == 0
    # 裸输出: 不是 JSON, 末尾 print 会追加换行
    assert result.output.rstrip("\n") == "http://10.0.0.1:1810"


def test_config_get_value_only_empty_for_unset_string(runner, isolated_config):
    """未配置的 api_key 返回空串, 而非报错——install.sh cfg_get 依赖此行为。"""
    result = runner.invoke(cli, ["config", "get", "model.omni.api_key", "--value-only"])
    assert result.exit_code == 0
    assert result.output.rstrip("\n") == ""


def test_config_set_multi_pair_atomic(runner, isolated_config):
    """``config set`` 支持一次提交多组 (path, value), 避免中途被 Ctrl+C 留下半更新。"""
    result = runner.invoke(
        cli,
        [
            "config",
            "set",
            "model.omni.model",
            "xiaomi/mimo-v2.5",
            "model.omni.base_url",
            "https://api.xiaomimimo.com/v1",
            "model.omni.api_key",
            "sk-xxxxx",
            "--no-restart",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["code"] == 0
    assert {u["path"] for u in data["updated"]} == {
        "model.omni.model",
        "model.omni.base_url",
        "model.omni.api_key",
    }

    from miloco_cli.config import load_config

    cfg = load_config()
    assert cfg["model"]["omni"]["model"] == "xiaomi/mimo-v2.5"
    assert cfg["model"]["omni"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert cfg["model"]["omni"]["api_key"] == "sk-xxxxx"


def test_config_set_single_pair_preserves_legacy_output_shape(runner, isolated_config):
    """单 pair 时仍使用 {path, value} 形状, 与旧脚本/文档兼容。"""
    result = runner.invoke(
        cli, ["config", "set", "server.url", "http://10.0.0.1:1810", "--no-restart"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "server.url"
    assert data["value"] == "http://10.0.0.1:1810"
    assert "updated" not in data


def test_config_set_odd_args_rejected(runner):
    """奇数个位置参数应报错, 不得写入任何键。"""
    result = runner.invoke(
        cli,
        [
            "config",
            "set",
            "server.url",
            "http://10.0.0.1:1810",
            "model.omni.model",  # 缺对应 value
            "--no-restart",
        ],
    )
    assert result.exit_code != 0


def test_config_set_multi_pair_unknown_path_is_atomic(runner, isolated_config):
    """多 pair 中任一 path 非法时整体失败, 合法 pair 也不得落盘。"""
    result = runner.invoke(
        cli,
        [
            "config",
            "set",
            "server.url",
            "http://should-not-persist:1810",
            "server.bogus_unknown",
            "x",
            "--no-restart",
        ],
    )
    assert result.exit_code != 0

    from miloco_cli.config import load_config

    cfg = load_config()
    # 未被污染: server.url 仍是默认值
    assert cfg["server"]["url"] != "http://should-not-persist:1810"


# ─── person ───────────────────────────────────────────────────────────────────


def test_person_list(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": [{"id": "p1", "name": "爸爸"}]}
        result = runner.invoke(cli, ["person", "list"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/identity/persons")


def test_person_add(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"person_id": "p-new"}}
        result = runner.invoke(cli, ["person", "add", "--name", "妈妈"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/identity/persons", {"name": "妈妈"})


def test_person_add_with_role(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"person_id": "p-new"}}
        result = runner.invoke(
            cli, ["person", "add", "--name", "王伟", "--role", "爸爸"]
        )
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/identity/persons", {"name": "王伟", "role": "爸爸"}
    )


def test_person_add_missing_name(runner):
    result = runner.invoke(cli, ["person", "add"])
    assert result.exit_code != 0


def test_person_update(runner):
    with patch("miloco_cli.client.api_put") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["person", "update", "p-1", "--name", "新名字"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/identity/persons/p-1", {"name": "新名字"})


def test_person_update_no_fields_errors(runner):
    result = runner.invoke(cli, ["person", "update", "p-1"])
    assert result.exit_code != 0


def test_person_delete(runner):
    with patch("miloco_cli.client.api_delete") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["person", "delete", "p-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/identity/persons/p-1")


# ─── identity register preview (多图 / 单图 / 视频 三选一) ───────────────────


def _make_tmp_jpg(tmp_path, name: str) -> str:
    """造一个 .jpg 文件(CLI 不验证内容,只看后缀),返回路径字符串。
    backend 才会真解码——CLI 单测里只验路径透传 / payload 组装,不依赖图像数据。
    """
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # 假 JFIF 头,够过路径校验
    return str(p)


def test_register_preview_images_builds_media_b64_list(runner, tmp_path):
    """--images a.jpg --images b.jpg → body 里 media_b64_list 是 2 串 base64。"""
    a = _make_tmp_jpg(tmp_path, "a.jpg")
    b = _make_tmp_jpg(tmp_path, "b.jpg")
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {}}
        result = runner.invoke(
            cli,
            [
                "identity",
                "register",
                "preview",
                "--images",
                a,
                "--images",
                b,
                "--topk",
                "5",
            ],
        )
    assert result.exit_code == 0, result.output
    call_args = mock.call_args
    assert call_args[0][0] == "/api/identity/register/preview"
    sent = call_args[0][1]
    assert "media_b64_list" in sent
    assert len(sent["media_b64_list"]) == 2
    # 不应误带 media_b64 / media_kind
    assert "media_b64" not in sent
    assert "media_kind" not in sent
    assert sent["topk"] == 5


def test_register_preview_image_video_images_mutex(runner, tmp_path):
    """--image + --images 同时给 → 报错退出。"""
    a = _make_tmp_jpg(tmp_path, "a.jpg")
    b = _make_tmp_jpg(tmp_path, "b.jpg")
    result = runner.invoke(
        cli,
        ["identity", "register", "preview", "--image", a, "--images", b],
    )
    assert result.exit_code != 0
    # error 是 JSON,中文被 escape 成 unicode,parse 后再比
    err_out = result.output + (result.stderr or "")
    parsed = json.loads(err_out.strip().splitlines()[-1])
    assert "三选一" in parsed["error"]


def test_register_preview_single_image_unchanged(runner, tmp_path):
    """旧 --image 单图行为不变:走 media_b64 + media_kind='image'。"""
    a = _make_tmp_jpg(tmp_path, "a.jpg")
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {}}
        result = runner.invoke(
            cli,
            ["identity", "register", "preview", "--image", a, "--topk", "3"],
        )
    assert result.exit_code == 0, result.output
    sent = mock.call_args[0][1]
    assert "media_b64" in sent
    assert sent["media_kind"] == "image"
    assert "media_b64_list" not in sent


# ─── device ───────────────────────────────────────────────────────────────────


def test_device_list_default_tsv(runner, fake_home_info):
    result = runner.invoke(cli, ["device", "list"])
    assert result.exit_code == 0
    lines = [r for r in result.output.splitlines() if r]
    # home banner
    assert lines[0] == "# home=我的家"
    # 表头
    assert lines[1] == "# did|device_name|room|category|online"
    rows = [r for r in lines if not r.startswith("#")]
    assert len(rows) == 1
    parts = rows[0].split("|")
    assert len(parts) == 5
    assert parts[0] == "lamp_001"
    assert parts[1] == "台灯"
    assert parts[2] == "客厅"
    assert parts[3] == "light"
    assert parts[4] in ("online", "offline")


def test_device_list_home_banner_from_top_level(runner, fake_home_info):
    """设备 dict 无 home 字段时，banner 仍取顶层 home_name。"""
    result = runner.invoke(cli, ["device", "list"])
    assert result.exit_code == 0
    lines = [r for r in result.output.splitlines() if r]
    assert lines[0] == "# home=我的家"


def test_device_list_filter_room(runner, fake_home_info):
    result = runner.invoke(cli, ["device", "list", "--room", "客厅"])
    assert result.exit_code == 0
    rows = [r for r in result.output.splitlines() if r and not r.startswith("#")]
    assert rows  # 至少匹配一台
    for row in rows:
        # did|device_name|room|category|online
        assert row.split("|")[2] == "客厅"


def test_device_control_single(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "prop.2.1", "true"]
        )
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/miot/devices/lamp_001/control",
        {"type": "set_property", "iid": "prop.2.1", "value": True},
    )


def test_device_control_batch_set(runner, fake_home_info):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli,
            [
                "device",
                "control",
                "lamp_001",
                "--set",
                "prop.2.1",
                "true",
                "--set",
                "prop.2.2",
                "80",
            ],
        )
    assert result.exit_code == 0
    mock.assert_called_once()
    body = mock.call_args.args[1]
    assert body["type"] == "set_properties"
    assert {"iid": "prop.2.2", "value": 80} in body["properties"]


def test_device_control_annotates_did(runner, fake_home_info):
    """control 返回体补 did，让并发批量控制（&+wait）的多行输出可归属到设备。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Device control executed successfully",
            "data": {"results": [{"code": 0}]},
        }
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "prop.2.1", "false"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["did"] == "lamp_001"
    # code=0 成功项不应被补 code_msg
    assert "code_msg" not in data["data"]["results"][0]


def test_device_control_annotates_error_code(runner, fake_home_info):
    """results[].code 为设备侧失败码 → 补 code_msg 中文释义（-704042011 = 设备离线）。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Device control executed successfully",
            "data": {"results": [{"did": "lamp_001", "iid": "prop.2.1", "code": -704042011}]},
        }
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "prop.2.1", "true"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["results"][0]["code_msg"] == "设备离线"
    # 外层信封对齐真实结果，不再 code=0 + "successfully"
    assert data["code"] == -704042011
    assert data["message"] == "失败：设备离线"


def test_device_control_annotates_unknown_error_code(runner, fake_home_info):
    """未知失败码 → 补默认释义，不丢失"这是设备侧失败"的信号。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"results": [{"code": -999999999}]},
        }
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "prop.2.1", "true"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "未知错误码" in data["data"]["results"][0]["code_msg"]
    assert data["code"] == -999999999


def test_device_control_partial_failure_envelope(runner, fake_home_info):
    """多设备部分失败 → 外层 message 标"部分失败（n/total）"。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"results": [{"code": 0}, {"code": -704042011}]},
        }
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "--set", "prop.2.1", "true"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["message"] == "部分失败（1/2）：设备离线"


def test_device_control_positional_and_set_conflict(runner, fake_home_info):
    """M13 修复：同时使用位置参数和 --set 应报错。"""
    result = runner.invoke(
        cli,
        [
            "device",
            "control",
            "lamp_001",
            "prop.2.1",
            "true",
            "--set",
            "prop.2.2",
            "50",
        ],
    )
    assert result.exit_code != 0


def test_device_control_no_args_errors(runner):
    result = runner.invoke(cli, ["device", "control", "lamp_001"])
    assert result.exit_code != 0


def test_device_control_action_rejected(runner, fake_home_info):
    """用 control 调 action（解析出 action.s.p）→ 报错并导向 device action，不发后端。"""
    with patch("miloco_cli.client.api_post") as mock:
        result = runner.invoke(cli, ["device", "control", "lamp_001", "action.5.3", "1"])
    assert result.exit_code == 1
    mock.assert_not_called()


def test_device_action_infers_types(runner, fake_home_info):
    """M14 修复：action params 应推断类型。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["device", "action", "lamp_001", "action.7.3", "100", "true"]
        )
    assert result.exit_code == 0
    mock.assert_called_once()
    assert mock.call_args.args[1]["params"] == [100, True]


def test_device_action_annotates_error_code(runner, fake_home_info):
    """call_action 返回单数 result（非 results 数组）；失败码同样要补 code_msg + 改写外层信封。

    这是音箱 TTS 的执行路径（play-text / execute-text-directive）——设备离线时
    若外层仍 code=0，agent 会误报"已播报"。
    """
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "Device control executed successfully",
            "data": {"result": {"did": "lamp_001", "aiid": "action.7.3", "code": -704042011}},
        }
        result = runner.invoke(
            cli, ["device", "action", "lamp_001", "action.7.3", "晚安"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["did"] == "lamp_001"
    assert data["data"]["result"]["code_msg"] == "设备离线"
    # 外层信封对齐真实结果，不再 code=0 + "successfully"
    assert data["code"] == -704042011
    assert data["message"] == "失败：设备离线"


def test_device_spec_default_table(runner, fake_home_info):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "did": "lamp_001",
                "name": "台灯",
                "home": "我的家",
                "room": "客厅",
                "online": True,
                "category": "light",
                "spec": {"prop.2.1": {"type": "bool"}},
            },
        }
        result = runner.invoke(cli, ["device", "spec", "lamp_001"])
    assert result.exit_code == 0
    text = result.output
    assert "did=lamp_001" in text
    assert "home=我的家" in text
    assert "device_name=台灯" in text
    assert "room=客厅" in text
    assert "properties" in text
    assert "prop.2.1" in text
    mock.assert_called_once_with("/api/miot/devices/lamp_001/spec")


def test_device_spec_multiple_dids(runner, fake_home_info):
    """device spec 支持多 did：依次输出各设备规格，设备之间空两行分隔。"""
    def fake_get(path, *args, **kwargs):
        did = path.split("/")[-2]  # /api/miot/devices/<did>/spec
        return {"code": 0, "data": {
            "did": did, "name": f"dev-{did}", "online": True,
            "category": "light", "spec": {"prop.2.1": {"type": "bool"}},
        }}

    with patch("miloco_cli.client.api_get", side_effect=fake_get) as mock:
        result = runner.invoke(cli, ["device", "spec", "lamp_001", "lamp_002"])
    assert result.exit_code == 0
    assert "did=lamp_001" in result.output and "did=lamp_002" in result.output
    assert "\n\n\n" in result.output  # 设备之间空两行（连续三个换行）
    assert mock.call_count == 2


def test_device_spec_multiple_partial_failure(runner, fake_home_info):
    """多 did 中某台 spec 为空 → 该 did 报错到 stderr，其余正常输出，exit 0。"""
    def fake_get(path, *args, **kwargs):
        did = path.split("/")[-2]
        if did == "bad":
            return {"code": 0, "data": {}}  # 空 spec
        return {"code": 0, "data": {
            "did": did, "online": True, "category": "light",
            "spec": {"prop.2.1": {"type": "bool"}},
        }}

    with patch("miloco_cli.client.api_get", side_effect=fake_get):
        result = runner.invoke(cli, ["device", "spec", "good", "bad"])
    assert result.exit_code == 0
    assert "did=good" in result.output


def test_device_props_annotates_spec_name(runner, fake_home_info):
    """props 返回按 iid 归集，补 spec_name（= 属性 key）让外部能把值关联到属性。"""
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "properties": [
                    {"iid": "prop.2.1", "value": True, "code": 0},
                    {"iid": "prop.2.2", "value": 80, "code": 0},
                ],
            },
        }
        result = runner.invoke(cli, ["device", "props", "lamp_001"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    props = data["data"]["properties"]
    assert {p["iid"]: p["spec_name"] for p in props} == {
        "prop.2.1": "on",
        "prop.2.2": "brightness",
    }
    assert data["data"]["did"] == "lamp_001"


def test_device_props_spec_name_falls_back_to_iid(runner, fake_home_info):
    """spec 查不到该 iid（如未知属性）→ spec_name 回落为 iid，不丢字段。"""
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"properties": [{"iid": "prop.9.9", "value": 1, "code": 0}]},
        }
        result = runner.invoke(cli, ["device", "props", "lamp_001"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["properties"][0]["spec_name"] == "prop.9.9"


# ─── scene ────────────────────────────────────────────────────────────────────


def test_scene_list(runner, fake_home_info):
    result = runner.invoke(cli, ["scene", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["scenes"][0]["name"] == "回家"


def test_scene_trigger(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["scene", "trigger", "s1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/scenes/s1/trigger", None)


def test_scene_create(runner):
    action = '{"did":"lamp_001","iid":"prop.2.1","value":true,"idempotent":true}'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"scene_id": "s-new"}}
        result = runner.invoke(
            cli, ["scene", "create", "--name", "睡前", "--action", action]
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body["name"] == "睡前"
    assert len(body["actions"]) == 1


def test_scene_create_invalid_action_json(runner):
    result = runner.invoke(
        cli, ["scene", "create", "--name", "测试", "--action", "INVALID"]
    )
    assert result.exit_code != 0


# ─── rule ─────────────────────────────────────────────────────────────────────


def test_rule_list(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"rules": []}}
        result = runner.invoke(cli, ["rule", "list"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules", None)


def test_rule_create_static(runner):
    action = '{"did":"lamp_001","iid":"prop.2.1","value":true,"idempotent":true}'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"rule_id": "r-1"}}
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--task-id",
                "light_on",
                "--name",
                "[light_on] 开灯规则",
                "--source",
                "cam_001",
                "--condition",
                "有人在看书",
                "--action",
                action,
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("actions")  # STATIC: 写入 actions 字段
    assert "type" not in body


def test_rule_create_action_rejects_json_array_with_hint(runner):
    """传 JSON 数组给 --action 时错误信息镜像 flag 名 + 引导重复写法。"""
    array_payload = '[{"did":"a","iid":"prop.2.1","value":true,"idempotent":true}]'
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
            "--action",
            array_payload,
        ],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "--action expects a single JSON object" in combined
    assert "--action '{...}' --action '{...}'" in combined


def test_rule_create_on_enter_action_array_mirrors_flag_name(runner):
    """传 JSON 数组给 --on-enter-action 时错误信息直接说 --on-enter-action，不要让 agent 二次映射。"""
    array_payload = '[{"did":"a","iid":"prop.2.1","value":true,"idempotent":true}]'
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
            "--mode",
            "state",
            "--on-enter-action",
            array_payload,
        ],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "--on-enter-action expects a single JSON object" in combined
    assert "--on-enter-action '{...}' --on-enter-action '{...}'" in combined


def test_rule_create_static_without_action_errors(runner):
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
        ],
    )
    assert result.exit_code != 0


def test_rule_create_dynamic(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"rule_id": "r-2"}}
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--task-id",
                "warm_light",
                "--name",
                "[warm_light] 调灯色",
                "--source",
                "cam_001",
                "--condition",
                "有人在读书",
                "--action-desc",
                "调成温暖色",
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("action_descriptions")  # DYNAMIC: 写入 action_descriptions
    assert "type" not in body


def test_rule_create_with_duration_payload(runner):
    """event + duration_seconds + duration_ratio 透传到 payload."""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"rule_id": "r-dur"}}
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--task-id",
                "sit_too_long",
                "--name",
                "[sit_too_long] 久坐",
                "--source",
                "cam_study",
                "--condition",
                "用户坐在书桌前",
                "--action-desc",
                "播报起来活动",
                "--duration-seconds",
                "60",
                "--duration-ratio",
                "0.5",
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body["duration_seconds"] == 60
    assert body["duration_ratio"] == 0.5


def test_rule_create_duration_state_mode_payload(runner):
    """STATE mode + --duration-seconds 也合法（ENTERED 前置确认门槛），payload 含两字段."""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"rule_id": "r-state-dur"}}
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--task-id",
                "reading",
                "--name",
                "[reading] 看书前置确认",
                "--source",
                "cam_study",
                "--condition",
                "用户在书桌前阅读",
                "--mode",
                "state",
                "--on-enter-desc",
                "进入看书状态",
                "--duration-seconds",
                "180",
                "--duration-ratio",
                "0.8",
            ],
        )
    assert result.exit_code == 0, result.output
    body = mock.call_args[0][1]
    assert body["mode"] == "state"
    assert body["duration_seconds"] == 180
    assert body["duration_ratio"] == 0.8


def test_rule_create_duration_ratio_without_seconds_rejected(runner):
    """只传 --duration-ratio 无 --duration-seconds → 报错."""
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
            "--action-desc",
            "z",
            "--duration-ratio",
            "0.5",
        ],
    )
    assert result.exit_code != 0
    assert "duration-ratio requires --duration-seconds" in (
        result.output + (result.stderr or "")
    )


def test_rule_create_duration_seconds_zero_rejected(runner):
    """--duration-seconds=0 命中下界校验，被 CLI 拒。"""
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
            "--action-desc",
            "z",
            "--duration-seconds",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "duration-seconds out of range" in (result.output + (result.stderr or ""))


def test_rule_create_duration_seconds_over_86400_rejected(runner):
    """--duration-seconds=86401 命中上界校验，被 CLI 拒。"""
    result = runner.invoke(
        cli,
        [
            "rule", "create",
            "--task-id", "x",
            "--name", "[x] x",
            "--source", "cam",
            "--condition", "y",
            "--action-desc", "z",
            "--duration-seconds", "86401",
        ],
    )
    assert result.exit_code != 0
    assert "duration-seconds out of range" in (result.output + (result.stderr or ""))


def test_rule_create_duration_ratio_zero_rejected(runner):
    """--duration-ratio=0.0 命中下界校验，被 CLI 拒。"""
    result = runner.invoke(
        cli,
        [
            "rule",
            "create",
            "--task-id",
            "x",
            "--name",
            "[x] x",
            "--source",
            "cam",
            "--condition",
            "y",
            "--action-desc",
            "z",
            "--duration-seconds",
            "60",
            "--duration-ratio",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "duration-ratio must be in (0, 1]" in (result.output + (result.stderr or ""))


def test_rule_update_duration_ratio_zero_rejected(runner):
    """update 路径 --duration-ratio=0 也被拒。"""
    result = runner.invoke(
        cli,
        ["rule", "update", "r-1", "--duration-ratio", "0"],
    )
    assert result.exit_code != 0
    assert "duration-ratio must be in (0, 1]" in (result.output + (result.stderr or ""))


def test_rule_update_duration_seconds_over_86400_rejected(runner):
    """update 路径 --duration-seconds=86401 命中上界校验，被 CLI 拒。"""
    result = runner.invoke(
        cli,
        ["rule", "update", "r-1", "--duration-seconds", "86401"],
    )
    assert result.exit_code != 0
    assert "duration-seconds out of range" in (
        result.output + (result.stderr or "")
    )


def test_rule_logs_cleanup_uses_params(runner):
    """M12 修复：logs-cleanup 应通过 params 传 keep_days，不拼在 URL 里。"""
    with patch("miloco_cli.client.api_delete") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "logs-cleanup", "--keep-days", "14"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/logs", params={"keep_days": 14})


def test_rule_delete(runner):
    with patch("miloco_cli.client.api_delete") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "delete", "r-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1")


def test_rule_enable(runner):
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "enable", "r-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1", {"enabled": True})


def test_rule_disable(runner):
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "disable", "r-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1", {"enabled": False})


def test_rule_trigger(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "trigger", "r-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1/trigger", None)


def test_rule_trigger_with_context(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["rule", "trigger", "r-1", "--context", "画面显示张三在看书"]
        )
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/rules/r-1/trigger", {"context": "画面显示张三在看书"}
    )


# ─── perceive ─────────────────────────────────────────────────────────────────


def test_perceive_devices(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": [{"did": "cam_001", "name": "客厅摄像头"}],
        }
        result = runner.invoke(cli, ["perceive", "devices"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/perception/devices")


def test_perceive_query(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"results": [{"source": "cam_001", "answer": "有人"}]},
        }
        result = runner.invoke(
            cli,
            [
                "perceive",
                "query",
                "--source",
                "cam_001",
                "--query",
                "有没有人",
            ],
        )
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/perception/perceive",
        {"sources": ["cam_001"], "query": "有没有人"},
    )


def test_perceive_query_multiple_sources(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {"results": []}}
        result = runner.invoke(
            cli,
            [
                "perceive",
                "query",
                "--source",
                "cam_001",
                "--source",
                "cam_002",
                "--query",
                "有没有人",
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body["sources"] == ["cam_001", "cam_002"]


def test_perceive_query_requires_source(runner):
    result = runner.invoke(cli, ["perceive", "query", "--query", "有没有人"])
    assert result.exit_code != 0


def test_perceive_query_requires_query(runner):
    result = runner.invoke(cli, ["perceive", "query", "--source", "cam_001"])
    assert result.exit_code != 0


def test_perceive_logs_agent_mode_no_cursor(runner, monkeypatch):
    """无 cursor 文件时，agent 模式不传 after 参数。"""
    import miloco_cli.commands.perceive as p_mod

    monkeypatch.setattr(p_mod, "_load_cursor", lambda: None)
    monkeypatch.setattr(p_mod, "_save_cursor", lambda ms: None)
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"logs": [], "count": 0}}
        result = runner.invoke(cli, ["perceive", "logs"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/perception/logs", None)


def test_perceive_logs_agent_mode_with_cursor(runner, monkeypatch):
    """有 cursor 时，agent 模式自动传 after 参数，查完后 cursor 推进到新日志的时间戳。"""
    import miloco_cli.commands.perceive as p_mod

    existing_cursor_ms = 100  # 上次拉取停在这里
    new_log_ms = 200  # 本次返回的日志时间戳，比 cursor 新
    saved = {}
    monkeypatch.setattr(p_mod, "_load_cursor", lambda: existing_cursor_ms)
    monkeypatch.setattr(p_mod, "_save_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"logs": [{"t": new_log_ms, "d": {}}], "count": 1},
        }
        result = runner.invoke(cli, ["perceive", "logs"])
    assert result.exit_code == 0
    assert "after" in mock.call_args[0][1]
    assert saved["ms"] == new_log_ms  # cursor 推进到新日志的时间戳


def test_perceive_logs_agent_mode_updates_cursor(runner, monkeypatch):
    """返回多条日志时，cursor 更新为最后一条（最新）的 t 值。"""
    import miloco_cli.commands.perceive as p_mod

    first_log_ms = 100  # 较早的日志
    last_log_ms = 200  # 最新的日志，cursor 应推进到这里
    saved = {}
    monkeypatch.setattr(p_mod, "_load_cursor", lambda: None)
    monkeypatch.setattr(p_mod, "_save_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "logs": [{"t": first_log_ms, "d": {}}, {"t": last_log_ms, "d": {}}],
                "count": 2,
            },
        }
        result = runner.invoke(cli, ["perceive", "logs"])
    assert result.exit_code == 0
    assert saved["ms"] == last_log_ms


def test_perceive_logs_agent_mode_empty_no_cursor_update(runner, monkeypatch):
    """无日志时不更新 cursor。"""
    import miloco_cli.commands.perceive as p_mod

    saved = {}
    monkeypatch.setattr(p_mod, "_load_cursor", lambda: None)
    monkeypatch.setattr(p_mod, "_save_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"logs": [], "count": 0}}
        result = runner.invoke(cli, ["perceive", "logs"])
    assert result.exit_code == 0
    assert "ms" not in saved


def test_perceive_logs_since_debug_mode(runner, monkeypatch):
    """--since 调试模式：传 since 参数，不读写 cursor。"""
    import miloco_cli.commands.perceive as p_mod

    cursor_touched = {}
    monkeypatch.setattr(
        p_mod, "_load_cursor", lambda: cursor_touched.update({"loaded": True}) or None
    )
    monkeypatch.setattr(
        p_mod, "_save_cursor", lambda ms: cursor_touched.update({"saved": True})
    )
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"logs": [], "count": 0}}
        result = runner.invoke(cli, ["perceive", "logs", "--since", "1h"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/perception/logs", {"since": "1h"})
    assert "loaded" not in cursor_touched
    assert "saved" not in cursor_touched


# ─── admin ────────────────────────────────────────────────────────────────────


def test_admin_status(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {}}
        result = runner.invoke(cli, ["admin", "status"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/admin/status")


def test_admin_home_info(runner, fake_home_info):
    result = runner.invoke(cli, ["admin", "home-info"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["devices"] == 1


def test_device_refresh(runner, fake_home_info):
    result = runner.invoke(cli, ["device", "refresh"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["code"] == 0


# ─── rule update ──────────────────────────────────────────────────────────────


def test_rule_update_name_only(runner):
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "update", "r-1", "--name", "新名字"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1", {"name": "新名字"})


def test_rule_update_condition_only(runner):
    """`rule update --condition ...` 单独传 condition 时 body 只含 condition.query。"""
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli,
            ["rule", "update", "r-1", "--condition", "用户在客厅"],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body == {"condition": {"query": "用户在客厅"}}


def test_rule_update_static_actions(runner):
    action = '{"did":"lamp_001","iid":"prop.2.1","value":true,"idempotent":true}'
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli,
            [
                "rule",
                "update",
                "r-1",
                "--action",
                action,
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("actions")  # STATIC: 写入 actions
    assert "type" not in body


def test_rule_update_dynamic_descs(runner):
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli,
            [
                "rule",
                "update",
                "r-1",
                "--action-desc",
                "调暗灯光",
            ],
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("action_descriptions")  # DYNAMIC: 写入 action_descriptions
    assert "type" not in body


def test_rule_update_no_fields_errors(runner):
    result = runner.invoke(cli, ["rule", "update", "r-1"])
    assert result.exit_code != 0


def test_rule_update_action_and_desc_conflict(runner):
    result = runner.invoke(
        cli,
        [
            "rule",
            "update",
            "r-1",
            "--action",
            '{"did":"x","iid":"y","value":true,"idempotent":true}',
            "--action-desc",
            "冲突",
        ],
    )
    assert result.exit_code != 0


def test_rule_update_action_writes_actions(runner):
    """提供 --action 时只写 actions 字段，不再写 type。"""
    action = '{"did":"lamp_001","iid":"prop.2.1","value":false,"idempotent":true}'
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["rule", "update", "r-1", "--action", action])
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("actions")
    assert "type" not in body


def test_rule_update_action_desc_writes_descriptions(runner):
    """提供 --action-desc 时只写 action_descriptions 字段，不再写 type。"""
    with patch("miloco_cli.client.api_patch") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["rule", "update", "r-1", "--action-desc", "调暗灯光"]
        )
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert body.get("action_descriptions")
    assert "type" not in body


# ─── rule logs ────────────────────────────────────────────────────────────────


def test_rule_logs_agent_mode_no_cursor(runner, monkeypatch):
    """无 cursor 文件时，agent 模式不传 after 参数（但带 backend 上限的 limit）。"""
    import miloco_cli.commands.rule as r_mod

    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: None)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: None)
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"rule_logs": [], "total_items": 0}}
        result = runner.invoke(cli, ["rule", "logs"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/logs", {"limit": 500})


def test_rule_logs_agent_mode_with_cursor(runner, monkeypatch):
    """有 cursor 时，agent 模式自动传 after 参数。"""
    import miloco_cli.commands.rule as r_mod

    existing_cursor_ms = 100
    new_log_ms = 200
    saved = {}
    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: existing_cursor_ms)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "rule_logs": [{"timestamp": new_log_ms, "rule_id": "r-1"}],
                "total_items": 1,
            },
        }
        result = runner.invoke(cli, ["rule", "logs"])
    assert result.exit_code == 0
    call_params = mock.call_args[0][1]
    assert "after" in call_params
    assert saved["ms"] == new_log_ms


def test_rule_logs_agent_mode_updates_cursor(runner, monkeypatch):
    """多条日志时，cursor 推进到最新一条的 timestamp（backend 按 DESC 返回，logs[0] 最新）。"""
    import miloco_cli.commands.rule as r_mod

    newest_ms = 300
    older_ms = 100
    saved = {}
    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: None)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "rule_logs": [
                    {"timestamp": newest_ms, "rule_id": "r-2"},
                    {"timestamp": older_ms, "rule_id": "r-1"},
                ],
                "total_items": 2,
            },
        }
        result = runner.invoke(cli, ["rule", "logs"])
    assert result.exit_code == 0
    assert saved["ms"] == newest_ms


def test_rule_logs_agent_mode_paginates_when_page_full(runner, monkeypatch):
    """单批 logs 满 page_size 时循环翻页（before 收紧）直到本批不满，避免丢日志。"""
    import miloco_cli.commands.rule as r_mod

    page_limit = 500
    # 第一页：500 条 timestamps 1000..501（DESC），需要再翻
    page_one = [{"timestamp": 1000 - i, "rule_id": f"r-{i}"} for i in range(page_limit)]
    # 第二页：3 条 timestamps 500..498（DESC），不满 → 停止翻页
    page_two = [{"timestamp": 500 - i, "rule_id": f"r-{500 + i}"} for i in range(3)]

    saved = {}
    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: None)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: saved.update({"ms": ms}))

    responses = [
        {"code": 0, "data": {"rule_logs": page_one, "total_items": page_limit}},
        {"code": 0, "data": {"rule_logs": page_two, "total_items": 3}},
    ]

    with patch("miloco_cli.client.api_get", side_effect=responses) as mock:
        result = runner.invoke(cli, ["rule", "logs"])

    assert result.exit_code == 0
    assert mock.call_count == 2
    # 第二页用 before = 第一页最旧那条 timestamp 的 ISO 形式做上限
    second_call_params = mock.call_args_list[1][0][1]
    assert "before" in second_call_params
    # cursor 必须推到整个区间的最新（page_one[0] = 1000），否则下次会重复拿到这一批
    assert saved["ms"] == 1000


def test_rule_logs_agent_mode_empty_no_cursor_update(runner, monkeypatch):
    """无日志返回时，cursor 不更新。"""
    import miloco_cli.commands.rule as r_mod

    saved = {}
    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: None)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: saved.update({"ms": ms}))
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"rule_logs": [], "total_items": 0}}
        result = runner.invoke(cli, ["rule", "logs"])
    assert result.exit_code == 0
    assert saved == {}


def test_rule_logs_by_rule(runner, monkeypatch):
    """--rule 过滤时使用规则专属路径。"""
    import miloco_cli.commands.rule as r_mod

    monkeypatch.setattr(r_mod, "_load_rule_cursor", lambda: None)
    monkeypatch.setattr(r_mod, "_save_rule_cursor", lambda ms: None)
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"rule_logs": [], "total_items": 0}}
        result = runner.invoke(cli, ["rule", "logs", "--rule", "r-1"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/rules/r-1/logs", {"limit": 500})


def test_rule_logs_since_debug_mode(runner, monkeypatch):
    """--since 调试模式不读写 cursor 文件。"""
    import miloco_cli.commands.rule as r_mod

    load_called = []
    monkeypatch.setattr(
        r_mod, "_load_rule_cursor", lambda: load_called.append(1) or None
    )
    monkeypatch.setattr(
        r_mod,
        "_save_rule_cursor",
        lambda ms: (_ for _ in ()).throw(AssertionError("should not save cursor")),
    )
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"rule_logs": [], "total_items": 0}}
        result = runner.invoke(cli, ["rule", "logs", "--since", "24h"])
    assert result.exit_code == 0
    assert load_called == []
    mock.assert_called_once_with("/api/rules/logs", {"since": "24h"})


def test_rule_logs_limit_debug_mode(runner, monkeypatch):
    """--limit 调试模式不读写 cursor 文件。"""
    import miloco_cli.commands.rule as r_mod

    load_called = []
    monkeypatch.setattr(
        r_mod, "_load_rule_cursor", lambda: load_called.append(1) or None
    )
    monkeypatch.setattr(
        r_mod,
        "_save_rule_cursor",
        lambda ms: (_ for _ in ()).throw(AssertionError("should not save cursor")),
    )
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "rule_logs": [{"timestamp": 100, "rule_id": "r-1"}],
                "total_items": 1,
            },
        }
        result = runner.invoke(cli, ["rule", "logs", "--limit", "5"])
    assert result.exit_code == 0
    assert load_called == []
    mock.assert_called_once_with("/api/rules/logs", {"limit": 5})


# ─── admin cost ───────────────────────────────────────────────────────────────


def test_admin_cost_exits_1(runner):
    result = runner.invoke(cli, ["admin", "cost"])
    assert result.exit_code != 0


# ─── account (formerly miot) ──────────────────────────────────────────────────


def test_account_status(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"bound": True, "uid": "123"}}
        result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/status")


def test_account_bind_no_wait_prints_oauth_url(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "data": {"oauth_url": "https://auth.mi.com/auth"},
        }
        result = runner.invoke(cli, ["account", "bind", "--no-wait"])
    assert result.exit_code == 0
    assert "https://auth.mi.com/auth" in result.output
    mock.assert_called_once_with("/api/miot/bind")


def test_account_bind_interactive_submits_authorize(runner):
    """交互式 bind：粘贴 base64(JSON) 授权码后调用 /authorize。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.side_effect = [
            {"code": 0, "data": {"oauth_url": "https://auth.mi.com/auth"}},
            {"code": 0, "data": None},
        ]
        # base64({"code": "ABC", "state": "XYZ"})
        result = runner.invoke(
            cli,
            ["account", "bind"],
            input="eyJjb2RlIjogIkFCQyIsICJzdGF0ZSI6ICJYWVoifQ==\n",
        )
    assert result.exit_code == 0
    assert mock.call_args_list[0].args == ("/api/miot/bind",)
    assert mock.call_args_list[1].args == (
        "/api/miot/authorize",
        {"code": "ABC", "state": "XYZ"},
    )


def test_account_bind_no_oauth_url(runner):
    """bind 返回无 oauth_url 时报错退出，不进入交互。"""
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": {}}
        result = runner.invoke(cli, ["account", "bind", "--no-wait"])
    assert result.exit_code != 0


def test_account_authorize_submits_payload(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {"code": 0, "data": None}
        result = runner.invoke(
            cli,
            ["account", "authorize", "eyJjb2RlIjogIkFCQyIsICJzdGF0ZSI6ICJYWVoifQ=="],
        )
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/authorize", {"code": "ABC", "state": "XYZ"})


def test_account_authorize_rejects_bad_payload(runner):
    result = runner.invoke(cli, ["account", "authorize", "not-a-valid-payload"])
    assert result.exit_code != 0


# base64({"code": "ABC", "state": "XYZ"})
_AUTH_PAYLOAD = "eyJjb2RlIjogIkFCQyIsICJzdGF0ZSI6ICJYWVoifQ=="


def _fake_sys_isatty(monkeypatch, value: bool):
    """替换 account 模块里的 sys，使 sys.stdin.isatty() 返回受控值。

    CliRunner.invoke 只改写真实 sys.stdin，不会触及这个独立对象，
    因此 isatty 判定稳定；而 click.prompt 仍从 runner 的真实 stdin 读输入。
    """
    import types

    import miloco_cli.commands.account as acct_mod

    fake = types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: value))
    monkeypatch.setattr(acct_mod, "sys", fake)


def test_account_authorize_single_home_auto_enables(runner):
    """只有一个家庭时，授权后直接启用它。"""
    homes = {"code": 0, "data": [{"home_id": "h1", "home_name": "主卧"}]}
    with (
        patch("miloco_cli.client.api_post", return_value={"code": 0, "data": None}),
        patch("miloco_cli.client.api_get", return_value=homes),
        patch("miloco_cli.client.api_put", return_value=_SUCCESS) as put,
    ):
        result = runner.invoke(cli, ["account", "authorize", _AUTH_PAYLOAD])
    assert result.exit_code == 0, result.output
    put.assert_called_once_with("/api/miot/scope/homes", {"home_id": "h1"})
    assert "已启用家庭：主卧" in result.output


def test_account_authorize_multi_home_non_interactive_picks_first(runner, monkeypatch):
    """非交互终端 + 多家庭：自动 fallback 启用第一个家庭，不进入交互选择。"""
    _fake_sys_isatty(monkeypatch, False)
    homes = {
        "code": 0,
        "data": [
            {"home_id": "h1", "home_name": "主卧"},
            {"home_id": "h2", "home_name": "客厅"},
        ],
    }
    with (
        patch("miloco_cli.client.api_post", return_value={"code": 0, "data": None}),
        patch("miloco_cli.client.api_get", return_value=homes),
        patch("miloco_cli.client.api_put", return_value=_SUCCESS) as put,
    ):
        result = runner.invoke(cli, ["account", "authorize", _AUTH_PAYLOAD])
    assert result.exit_code == 0, result.output
    put.assert_called_once_with("/api/miot/scope/homes", {"home_id": "h1"})
    assert "非交互终端" in result.output
    assert "主卧" in result.output


def test_account_authorize_multi_home_interactive_prompts(runner, monkeypatch):
    """交互终端 + 多家庭：按编号选择，启用所选家庭。"""
    _fake_sys_isatty(monkeypatch, True)
    homes = {
        "code": 0,
        "data": [
            {"home_id": "h1", "home_name": "主卧"},
            {"home_id": "h2", "home_name": "客厅"},
        ],
    }
    with (
        patch("miloco_cli.client.api_post", return_value={"code": 0, "data": None}),
        patch("miloco_cli.client.api_get", return_value=homes),
        patch("miloco_cli.client.api_put", return_value=_SUCCESS) as put,
    ):
        result = runner.invoke(
            cli, ["account", "authorize", _AUTH_PAYLOAD], input="2\n"
        )
    assert result.exit_code == 0, result.output
    put.assert_called_once_with("/api/miot/scope/homes", {"home_id": "h2"})


def test_account_authorize_no_homes_skips_enable(runner):
    """拿不到家庭列表时不调用 api_put，提示稍后手动查看。"""
    with (
        patch("miloco_cli.client.api_post", return_value={"code": 0, "data": None}),
        patch("miloco_cli.client.api_get", return_value={"code": 0, "data": []}),
        patch("miloco_cli.client.api_put") as put,
    ):
        result = runner.invoke(cli, ["account", "authorize", _AUTH_PAYLOAD])
    assert result.exit_code == 0, result.output
    put.assert_not_called()
    assert "暂未获取到家庭列表" in result.output


def test_account_unbind(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["account", "unbind"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/miot/unbind")


def test_account_group_has_no_miot_alias(runner):
    """重命名后不应再有 miot 命令组。"""
    result = runner.invoke(cli, ["miot", "--help"])
    assert result.exit_code != 0


# ─── service ──────────────────────────────────────────────────────────────────


def test_service_status_not_running(runner, tmp_path, monkeypatch):
    """supervisord 未运行且端口未被占用时，status 输出 running=false。"""
    import miloco_cli.commands.service as svc_mod

    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_find_pid_by_port", return_value=None),
    ):
        result = runner.invoke(cli, ["service", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["running"] is False


def test_service_status_running_via_port(runner, tmp_path, monkeypatch):
    """supervisord 未接管但端口上有进程监听时，status 输出 running=true (managed=false)。"""
    import miloco_cli.commands.service as svc_mod

    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_find_pid_by_port", return_value=99999),
    ):
        result = runner.invoke(cli, ["service", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["running"] is True
    assert data["managed"] is False
    assert data["pid"] == 99999


def test_service_stop_not_running(runner, tmp_path, monkeypatch):
    """服务未运行时，stop 以 code=0 输出 not running。"""
    import miloco_cli.commands.service as svc_mod

    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_find_pid_by_port", return_value=None),
    ):
        result = runner.invoke(cli, ["service", "stop"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["message"] == "not running"


def test_generate_supervisor_conf_injects_timezone_from_config(runner, tmp_path, monkeypatch):
    """config.json 有 timezone → 生成的 supervisord.conf environment 行带 TZ + MILOCO_TIMEZONE。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8")

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    conf = svc_mod._supervisor_conf().read_text()
    assert 'TZ="Asia/Shanghai"' in conf
    assert 'MILOCO_TIMEZONE="Asia/Shanghai"' in conf


def test_generate_supervisor_conf_env_overrides_config_timezone(runner, tmp_path, monkeypatch):
    """MILOCO_TIMEZONE env 优先于 config.json（对齐 backend pydantic env > file）。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8")
    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    conf = svc_mod._supervisor_conf().read_text()
    assert 'TZ="America/Los_Angeles"' in conf
    assert 'MILOCO_TIMEZONE="America/Los_Angeles"' in conf
    assert "Asia/Shanghai" not in conf


def test_generate_supervisor_conf_omits_timezone_when_unset(runner, tmp_path, monkeypatch):
    """无 env 无 config.json timezone → 不注入 TZ，仅保留原有 environment 键。"""
    import miloco_cli.commands.service as svc_mod

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    conf = svc_mod._supervisor_conf().read_text()
    assert ',TZ="' not in conf
    assert "MILOCO_TIMEZONE" not in conf
    assert 'MILOCO_SUPERVISED="1"' in conf


def test_service_logs_dir_not_found(runner, tmp_path, monkeypatch):
    """日志目录不存在时，logs 以非零退出。"""
    # 切换 MILOCO_HOME 到一个不存在 log/ 子目录的临时目录
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "empty_home"))
    result = runner.invoke(cli, ["service", "logs"])
    assert result.exit_code != 0


def test_service_start_requires_python_bin(runner, tmp_path, monkeypatch):
    """未配置 ``server.python_bin`` 时 start 应报错退出。"""
    import miloco_cli.commands.service as svc_mod

    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_is_port_in_use", return_value=False),
    ):
        result = runner.invoke(cli, ["service", "start"])
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert "python_bin" in data.get("error", "")


def test_service_start_rejects_nonexistent_python_bin(
    runner, tmp_path, isolated_config, monkeypatch
):
    """``server.python_bin`` 指向不存在的路径时 start 应拒绝。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import set_value

    set_value("server.python_bin", str(tmp_path / "no_such_python"))
    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_is_port_in_use", return_value=False),
    ):
        result = runner.invoke(cli, ["service", "start"])
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert "python_bin" in data.get("error", "") or "不可执行" in data.get("error", "")


def test_service_start_accepts_valid_python_bin(
    runner, tmp_path, isolated_config, monkeypatch
):
    """python_bin 合法时通过校验，返回 ``python -m miloco.main`` 命令。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import set_value

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    set_value("server.python_bin", str(fake_python))

    # Confirm _server_cmd_or_exit resolves without calling sys.exit
    cmd = svc_mod._server_cmd_or_exit(pretty=False)
    assert cmd == [str(fake_python), "-m", "miloco.main"]


# ─── scope home ───────────────────────────────────────────────────────────────


def test_scope_home_switch(runner):
    """PUT switch → exit 0。"""
    with patch("miloco_cli.commands.scope.api_put") as mock_put:
        mock_put.return_value = _SUCCESS
        result = runner.invoke(cli, ["scope", "home", "switch", "home_1"])
    assert result.exit_code == 0
    mock_put.assert_called_once_with("/api/miot/scope/homes", {"home_id": "home_1"})


# ─── home-profile ───────────────────────────────────────────────────────────


def test_home_profile_list_default_both(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"profile": [], "candidates": []}}
        result = runner.invoke(cli, ["home-profile", "list"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/home-profile/entries", params={"target": "both"})


def test_home_profile_list_target_profile(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"profile": []}}
        result = runner.invoke(cli, ["home-profile", "list", "--target", "profile"])
    assert result.exit_code == 0
    mock.assert_called_once_with(
        "/api/home-profile/entries", params={"target": "profile"}
    )


def test_home_profile_list_rejects_bad_target(runner):
    result = runner.invoke(cli, ["home-profile", "list", "--target", "bogus"])
    assert result.exit_code != 0


def test_home_profile_candidate_write_inline_ops(runner):
    ops = '[{"op":"add","entry":{"type":"member_routine","subject_name":"爸爸","content":"7:30 出门"}}]'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["home-profile", "candidate-write", "--ops", ops])
    assert result.exit_code == 0
    body = mock.call_args[0][1]
    assert mock.call_args[0][0] == "/api/home-profile/candidates:write"
    assert body["ops"][0]["op"] == "add"


def test_home_profile_candidate_write_missing_ops_errors(runner):
    result = runner.invoke(cli, ["home-profile", "candidate-write"])
    assert result.exit_code != 0


def test_home_profile_profile_write_user_edit_flag(runner):
    ops = '[{"op":"add","entry":{"type":"family","subject_name":"shared","content":"22:00 后静音"}}]'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["home-profile", "profile-write", "--ops", ops, "--user-edit"]
        )
    assert result.exit_code == 0
    assert mock.call_args[0][0] == "/api/home-profile/profile:write"
    body = mock.call_args[0][1]
    assert body["user_edit"] is True
    assert body["ops"][0]["op"] == "add"


def test_home_profile_profile_write_default_not_user_edit(runner):
    ops = '[{"op":"delete","id":"e1"}]'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["home-profile", "profile-write", "--ops", ops])
    assert result.exit_code == 0
    assert mock.call_args[0][1]["user_edit"] is False


def test_home_profile_ops_file(runner, tmp_path):
    ops_file = tmp_path / "ops.json"
    ops_file.write_text('[{"op":"merge","id":"e1"}]', encoding="utf-8")
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["home-profile", "profile-write", "--ops-file", str(ops_file)]
        )
    assert result.exit_code == 0
    assert mock.call_args[0][1]["ops"][0]["op"] == "merge"


def test_home_profile_commit(runner):
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["home-profile", "commit"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/home-profile/commit")


def test_home_profile_reassign(runner):
    maps = '[{"from_subject_names":["父亲","老王"],"to_subject_id":"p1","to_subject_name":"爸爸"}]'
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(cli, ["home-profile", "reassign", "--mappings", maps])
    assert result.exit_code == 0
    assert mock.call_args[0][0] == "/api/home-profile/subject:reassign"
    body = mock.call_args[0][1]
    assert body["mappings"][0]["to_subject_name"] == "爸爸"


def test_home_profile_reassign_missing_mappings_errors(runner):
    result = runner.invoke(cli, ["home-profile", "reassign"])
    assert result.exit_code != 0


def test_home_profile_show_prints_markdown(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {"code": 0, "data": {"markdown": "# 家庭档案\n- 爸爸"}}
        result = runner.invoke(cli, ["home-profile", "show"])
    assert result.exit_code == 0
    mock.assert_called_once_with("/api/home-profile/rendered")
    assert "家庭档案" in result.output


def test_home_profile_migrate_maps_subject_to_subject_name(runner, tmp_path):
    """旧 .home-memory profile.json 迁移：subject→subject_name，subject_id 留空。"""
    old = tmp_path / "profile.json"
    old.write_text(
        json.dumps(
            {"entries": [{"id": "e1", "subject": "爸爸", "content": "喜欢咖啡"}]}
        ),
        encoding="utf-8",
    )
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = _SUCCESS
        result = runner.invoke(
            cli, ["home-profile", "migrate", "--profile-file", str(old)]
        )
    assert result.exit_code == 0
    assert mock.call_args[0][0] == "/api/home-profile/import"
    body = mock.call_args[0][1]
    entry = body["profile"][0]
    assert entry["subject_name"] == "爸爸"
    assert entry["subject_id"] is None
    assert "subject" not in entry
    assert body["candidates"] == []


# ─── dashboard ──────────────────────────────────────────────────────────────


def test_dashboard_opens_base_url(runner, monkeypatch):
    import miloco_cli.commands.dashboard as dash

    opened = {}

    def _fake_open(url):
        opened["url"] = url
        return True

    monkeypatch.setattr(dash, "_is_healthy", lambda url: True)
    monkeypatch.setattr(dash, "_can_open_browser", lambda: True)
    monkeypatch.setattr(dash.webbrowser, "open", _fake_open)

    result = runner.invoke(cli, ["dashboard"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["url"].endswith(":1810/")
    assert data["running"] is True
    assert data["opened"] is True
    assert opened["url"] == data["url"]


def test_dashboard_monitor_appends_perf_hash(runner, monkeypatch):
    import miloco_cli.commands.dashboard as dash

    monkeypatch.setattr(dash, "_is_healthy", lambda url: True)
    monkeypatch.setattr(dash, "_can_open_browser", lambda: True)
    monkeypatch.setattr(dash.webbrowser, "open", lambda url: True)

    result = runner.invoke(cli, ["dashboard", "--monitor"])
    assert result.exit_code == 0
    assert json.loads(result.output)["url"].endswith("/#perf")


def test_dashboard_not_running_hint(runner, monkeypatch):
    import miloco_cli.commands.dashboard as dash

    monkeypatch.setattr(dash, "_is_healthy", lambda url: False)
    monkeypatch.setattr(dash, "_can_open_browser", lambda: False)
    monkeypatch.setattr(dash.webbrowser, "open", lambda url: True)

    result = runner.invoke(cli, ["dashboard"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["running"] is False
    assert data["opened"] is False  # 无头环境不开浏览器
    assert "hint" in data
