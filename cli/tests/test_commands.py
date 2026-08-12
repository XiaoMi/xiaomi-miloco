"""CLI 命令测试：使用 Click CliRunner，mock 底层 API 调用。"""

import json
import os
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
    # 默认关掉 jemalloc 预加载：生成 supervisord.conf 会真起一个探针子进程验证 libjemalloc，
    # 于是用例会因为"本机装没装 libjemalloc2"而行为不同。专门测分配器的用例自己 delenv 覆盖。
    monkeypatch.setenv("MILOCO_MALLOC", "glibc")
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


def test_config_set_timezone_valid_iana(runner, isolated_config):
    """timezone 在白名单内，合法 IANA 名可写入（用户/agent 均经此配置部署时区）。"""
    result = runner.invoke(
        cli, ["config", "set", "timezone", "Asia/Shanghai", "--no-restart"]
    )
    assert result.exit_code == 0
    from miloco_cli.config import load_config

    assert load_config()["timezone"] == "Asia/Shanghai"


def test_config_set_timezone_rejects_non_iana(runner, isolated_config):
    """非法时区名被拦（否则 backend 启动期才炸 ValidationError，定位困难）。"""
    for garbage in ("Beijing", "+08:00", "CST"):
        result = runner.invoke(
            cli, ["config", "set", "timezone", garbage, "--no-restart"]
        )
        assert result.exit_code != 0, f"{garbage!r} 不该被接受"
        assert "IANA" in result.output


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


def test_config_features_paths_available(runner, isolated_config):
    """features.* 已进 CLI 白名单：home-profile skill 靠 config get 判分支，默认(关)必须能读到
    False（此前不在白名单 → KeyError exit 1，分流立不住）。默认值对齐 backend FeaturesSettings。"""
    from miloco_cli.config import get_value, set_value

    assert get_value("features.pet_recognition") is False
    assert get_value("features.pet_head_grounding") is True
    assert get_value("features.pet_body_grounding") is True
    assert get_value("features.pet_reid_diverse") is True
    # config get --value-only → 裸 True/False（skill 直接判，无需解 JSON）
    r = runner.invoke(cli, ["config", "get", "features.pet_recognition", "--value-only"])
    assert r.exit_code == 0 and r.output.rstrip("\n") == "False"
    # 可用 CLI 开启（此前 set 被拒为 unknown config path）；set_value 收 CLI 原始字符串、内部 _coerce
    set_value("features.pet_recognition", "true")
    assert get_value("features.pet_recognition") is True


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


def test_device_control_positive_code_is_success(runner, fake_home_info):
    """设备侧正数码（如 code:1，指令已执行生效）不可误判为失败——对齐"负值即失败"。

    回归：某些开关 set_property 成功后仍返回 code:1。旧逻辑（非白名单即失败）把它
    打成"失败：设备侧执行失败"，上层 agent 据此盲目重试 / 谎报失败。修复后正数码
    不补失败释义、外层信封保持成功。
    """
    with patch("miloco_cli.client.api_post") as mock:
        mock.return_value = {
            "code": 0,
            "message": "executed successfully",
            "data": {"results": [{"code": 1}]},
        }
        result = runner.invoke(
            cli, ["device", "control", "lamp_001", "prop.2.1", "true"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "code_msg" not in data["data"]["results"][0]
    assert data["code"] == 0
    assert "失败" not in data["message"]


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
    assert "[service 2]" in text  # 按 service 分组的标题行
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


def test_device_spec_groups_by_service_with_description(runner, fake_home_info):
    """spec 按 service 分组：标题行带 service_type_name（service_description），
    props/action 归到各自 service 小节下。"""
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = {
            "code": 0,
            "data": {
                "did": "lamp_001",
                "name": "台灯",
                "online": True,
                "category": "light",
                "spec": {
                    "prop.2.1": {
                        "type_name": "on", "format": "bool",
                        "writeable": True, "readable": True,
                        "service_type_name": "light",
                        "service_description": "灯光",
                    },
                    "action.2.1": {
                        "type_name": "toggle",
                        "service_type_name": "light",
                        "service_description": "灯光",
                    },
                    "prop.3.1": {
                        "type_name": "on", "format": "bool",
                        "writeable": True, "readable": True,
                        "service_type_name": "indicator-light",
                        "service_description": "指示灯",
                    },
                },
            },
        }
        result = runner.invoke(cli, ["device", "spec", "lamp_001"])
    assert result.exit_code == 0
    text = result.output
    # 两个 service 各成小节，标题带类型与中文描述
    assert "[service 2] light（灯光）" in text
    assert "[service 3] indicator-light（指示灯）" in text
    # action 归到 service 2 下，且在 service 3 标题之前出现
    assert text.index("action.2.1") < text.index("[service 3]")
    # 无 properties:/actions: 小节标题，行不缩进（iid 顶格）
    assert "properties:" not in text and "actions:" not in text
    assert "\nprop.2.1  " in text and "\naction.2.1  " in text


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


# ─── scope camera enable/disable ─────────────────────────────────────────────


def test_scope_camera_enable_batch(runner):
    with patch("miloco_cli.commands.scope.api_put") as mock_put:
        mock_put.return_value = _SUCCESS
        result = runner.invoke(cli, ["scope", "camera", "enable", "c1", "c2"])
    assert result.exit_code == 0
    mock_put.assert_called_once_with(
        "/api/miot/scope/cameras",
        {"items": [{"did": "c1", "in_use": True}, {"did": "c2", "in_use": True}]},
    )


def test_scope_camera_disable(runner):
    with patch("miloco_cli.commands.scope.api_put") as mock_put:
        mock_put.return_value = _SUCCESS
        result = runner.invoke(cli, ["scope", "camera", "disable", "c1"])
    assert result.exit_code == 0
    mock_put.assert_called_once_with(
        "/api/miot/scope/cameras", {"items": [{"did": "c1", "in_use": False}]}
    )


# ─── scope camera mic-on / mic-off（拾音开关，走 voice 端点）──────────────────


def test_scope_camera_mic_off(runner):
    """mic-off → PUT voice 端点 voice_in_use=false。"""
    with patch("miloco_cli.commands.scope.api_put") as mock_put:
        mock_put.return_value = _SUCCESS
        result = runner.invoke(cli, ["scope", "camera", "mic-off", "c1"])
    assert result.exit_code == 0
    mock_put.assert_called_once_with(
        "/api/miot/scope/cameras/voice",
        {"items": [{"did": "c1", "voice_in_use": False}]},
    )


def test_scope_camera_mic_on_batch(runner):
    """批量 did 语义与 enable/disable 同款。"""
    with patch("miloco_cli.commands.scope.api_put") as mock_put:
        mock_put.return_value = _SUCCESS
        result = runner.invoke(cli, ["scope", "camera", "mic-on", "c1", "c2", "c3"])
    assert result.exit_code == 0
    mock_put.assert_called_once_with(
        "/api/miot/scope/cameras/voice",
        {
            "items": [
                {"did": "c1", "voice_in_use": True},
                {"did": "c2", "voice_in_use": True},
                {"did": "c3", "voice_in_use": True},
            ]
        },
    )


def test_scope_camera_mic_requires_did(runner):
    result = runner.invoke(cli, ["scope", "camera", "mic-off"])
    assert result.exit_code != 0  # 缺 did 由 click 拒绝


def test_scope_camera_mic_backend_rejection_passthrough(runner):
    """backend 拒绝（未知 did / 感知已关闭不可设拾音）→ api_put 打错误并 exit 3，
    CLI 不吞不改写（api_put 内部 sys.exit(3)，这里以 SystemExit 模拟其行为）。"""
    with patch(
        "miloco_cli.commands.scope.api_put",
        side_effect=SystemExit(3),
    ) as mock_put:
        result = runner.invoke(cli, ["scope", "camera", "mic-off", "ghost"])
    assert result.exit_code == 3
    mock_put.assert_called_once()


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


# ─── actions list ─────────────────────────────────────────────────────────────


_ACTIONS_RESP = [
    {
        "id": "a1", "timestamp": 1_700_000_000_000,
        "action_type": "set_property", "did": "lamp_001",
        "device_name": "台灯", "room": "客厅", "iid": "prop.2.1",
        "value_json": "true", "result_code": None, "result_msg": None,
        "success": 1, "error": None, "trace_id": None,
    },
    {
        "id": "a2", "timestamp": 1_700_000_100_000,
        "action_type": "call_action", "did": "spk_001",
        "device_name": "音箱", "room": "卧室", "iid": "action.5.1",
        "value_json": "[\"你好\"]", "result_code": -704042011,
        "result_msg": "设备离线", "success": 0, "error": None, "trace_id": None,
    },
]


def test_actions_list_renders_tsv(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = _ACTIONS_RESP
        result = runner.invoke(cli, ["actions", "list"])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    # 头注释行 + 两行数据
    assert lines[0].startswith("# ts|action_type|did")
    assert len(lines) == 3
    # 成功项:ok;失败项:fail + 中文 reason
    assert "|set_property|lamp_001|台灯|客厅|prop.2.1|ok|ok|" in lines[1]
    assert "|call_action|spk_001|音箱|卧室|action.5.1|fail|设备离线|" in lines[2]


def test_actions_list_passes_filters(runner):
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = []
        result = runner.invoke(
            cli,
            ["actions", "list", "--did", "lamp_001", "--failed-only",
             "--since", "24h", "--limit", "10"],
        )
    assert result.exit_code == 0
    path, kwargs = mock.call_args[0][0], mock.call_args[1]
    assert path == "/api/actions"
    params = dict(kwargs["params"])
    assert params["did"] == "lamp_001"
    assert params["failed_only"] == 1
    assert params["limit"] == 10
    assert "since_ms" in params  # 24h 已转 epoch ms


def test_actions_list_value_json_truncated(runner):
    long_val = "x" * 200
    with patch("miloco_cli.client.api_get") as mock:
        mock.return_value = [{
            "id": "a3", "timestamp": 1_700_000_000_000,
            "action_type": "call_action", "did": "d", "device_name": None,
            "room": None, "iid": "action.5.1", "value_json": long_val,
            "result_code": None, "result_msg": None, "success": 1,
            "error": None, "trace_id": None,
        }]
        result = runner.invoke(cli, ["actions", "list"])
    assert result.exit_code == 0
    data_line = result.output.strip().splitlines()[1]
    # value 字段截断到 ~60 字符(含省略号),远短于 200
    value_field = data_line.split("|")[-1]
    assert len(value_field) <= 61
    assert value_field.endswith("…")


def test_actions_list_bad_since_errors(runner):
    result = runner.invoke(cli, ["actions", "list", "--since", "notatime"])
    assert result.exit_code == 1


# ─── scope camera list：多通道合成 did 展示（A：仅展示层，不动 API）─────────────


def test_compose_channel_dids_transforms_multi_only():
    """多通道相机每行 did → 合成 did、去 channel 列；单摄保持裸 did、也去 channel 列。"""
    from miloco_cli.commands.scope import _compose_channel_dids

    resp = {
        "code": 0,
        "message": "ok",
        "data": [
            {"did": "solo", "name": "单摄", "channel_count": 1, "channel": 0},
            {"did": "dual", "name": "双摄", "channel_count": 2, "channel": 0},
            {"did": "dual", "name": "双摄", "channel_count": 2, "channel": 1},
        ],
    }
    out = _compose_channel_dids(resp)["data"]
    # 单摄:裸 did；双摄:合成 did。channel / channel_count 都从展示里去掉。
    assert out[0]["did"] == "solo"
    assert out[1]["did"] == "dual:ch0"
    assert out[2]["did"] == "dual:ch1"
    for r in out:
        assert "channel" not in r and "channel_count" not in r


def test_scope_camera_list_shows_composite_did(runner):
    """双摄两行 did 展示成 dual:ch0 / dual:ch1（不再是相同 did），单摄裸 did。"""
    resp = {
        "code": 0,
        "message": "ok",
        "data": [
            {"did": "solo", "name": "单摄", "channel_count": 1, "channel": 0},
            {"did": "dual", "name": "双摄", "channel_count": 2, "channel": 0},
            {"did": "dual", "name": "双摄", "channel_count": 2, "channel": 1},
        ],
    }
    with patch("miloco_cli.commands.scope.api_get", return_value=resp):
        result = runner.invoke(cli, ["scope", "camera", "list", "--pretty"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert [r["did"] for r in data] == ["solo", "dual:ch0", "dual:ch1"]
    assert all("channel" not in r and "channel_count" not in r for r in data)


# ─── 分配器：jemalloc 预加载 ──────────────────────────────────────────────────
#
# 全部 mock 子进程。真起探针会让用例行为取决于"本机装没装 libjemalloc2"，
# 而 isolated_config 默认设了 MILOCO_MALLOC=glibc，下面这些用例靠 malloc_probe 覆盖回来。


def _probe_stdout(ver="5.3.1", page=4096, bg=True, dirty=5000, muzzy=5000):
    """造一份符合探针 stdout 契约的输出（5 行）。"""
    return f"ver={ver}\npage={page}\nbg={bg}\ndirty={dirty}\nmuzzy={muzzy}\n"


_INVALID = "<jemalloc>: Invalid conf pair: "


@pytest.fixture
def malloc_probe(monkeypatch, tmp_path):
    """把分配器逻辑放进可控环境：假的系统库目录 + 假的子进程。

    返回一个 dict，用例改它来控制探针行为：``stdout`` / ``stderr`` / ``returncode`` / ``raise``
    （抛什么异常）/ ``bundled``（自带那份的路径）。读 ``probe_calls`` 看探针被怎么调的。
    """
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    monkeypatch.delenv("MILOCO_MALLOC", raising=False)
    # 这两个不带 MILOCO_ 前缀，isolated_config 清不到；而被测代码对它们是"环境里有就沿用"
    # 的语义（LD_PRELOAD 追加、MALLOC_CONF 整份取用），不清掉会让导出过它们的开发机拿到假失败。
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.delenv("MALLOC_CONF", raising=False)
    monkeypatch.setattr(svc_mod.sys, "platform", "linux")

    lib_dir = tmp_path / "usr-lib"
    lib_dir.mkdir()
    (lib_dir / "libjemalloc.so.2").write_bytes(b"\x7fELF")
    # triplet 目录清掉，候选只剩下面这一个假目录，用例才好数探针次数。
    monkeypatch.setattr(svc_mod, "_SYSTEM_LIB_DIRS", (lib_dir,))
    monkeypatch.setattr(svc_mod, "_ARCH_LIB_DIR_NAMES", {})

    state = {
        "stdout": _probe_stdout(),
        "stderr": "",
        "returncode": 0,
        "raise": None,
        "bundled": None,
        "probe_calls": [],
        "lib_dir": lib_dir,
        "so": lib_dir / "libjemalloc.so.2",
    }

    def fake_run(cmd, **kwargs):
        if "-E" in cmd:  # 预加载探针
            state["probe_calls"].append({"cmd": cmd, "env": kwargs.get("env") or {}})
            if state["raise"] is not None:
                raise state["raise"]
            return sp.CompletedProcess(
                cmd, state["returncode"], state["stdout"], state["stderr"]
            )
        # 问 backend 解释器要自带那份的路径
        bundled = state["bundled"]
        return sp.CompletedProcess(cmd, 0 if bundled else 1, f"{bundled or ''}\n", "")

    monkeypatch.setattr(svc_mod.subprocess, "run", fake_run)
    yield state
    # 前台模式那条路径直接改真的 os.environ（exec 前的 os.environ.update），不经过
    # monkeypatch；而 monkeypatch.delenv 对"本来就不存在"的变量不记 undo，还不回来。
    # 不兜这一道，那个假 .so 路径会留在 pytest 进程里直到 session 结束，后面任何真 fork
    # 子进程的用例都会白拿一行 ld.so 报错，且是在跟它无关的断言里炸。
    # 本 finalizer 先于 monkeypatch 的 undo 执行，导出过这两个变量的开发机仍能拿回原值。
    for leaked in ("LD_PRELOAD", "MALLOC_CONF"):
        os.environ.pop(leaked, None)


def test_malloc_default_injects_jemalloc_and_conf(malloc_probe):
    """默认路径（不设 MILOCO_MALLOC）注入 LD_PRELOAD + 三个旋钮。"""
    import miloco_cli.commands.service as svc_mod

    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"] == str(malloc_probe["so"])
    assert pairs["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF


def test_malloc_default_is_silent_when_nothing_found(malloc_probe, capsys):
    """默认路径一份都找不到 → 不注入且不告警（没装 libjemalloc2 是常态）。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["so"].unlink()
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert capsys.readouterr().err == ""


def test_malloc_explicit_jemalloc_warns_when_missing(malloc_probe, monkeypatch, capsys):
    """显式点名 jemalloc 却找不到 → 必须告警（和默认路径的静默相反）。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["so"].unlink()
    monkeypatch.setenv("MILOCO_MALLOC", "jemalloc")
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert "找不到可用的 libjemalloc" in capsys.readouterr().err


def test_malloc_absolute_path_used_directly(malloc_probe, monkeypatch, tmp_path):
    """绝对路径只试这一份，不走候选链。"""
    import miloco_cli.commands.service as svc_mod

    mine = tmp_path / "mine" / "libjemalloc.so.2"
    mine.parent.mkdir()
    mine.write_bytes(b"\x7fELF")
    monkeypatch.setenv("MILOCO_MALLOC", str(mine))

    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"] == str(mine)
    # 只探了这一份，候选链里那个假的没被碰
    assert len(malloc_probe["probe_calls"]) == 1
    assert str(mine) in malloc_probe["probe_calls"][0]["env"]["LD_PRELOAD"]


def test_malloc_glibc_injects_nothing(malloc_probe, monkeypatch, capsys):
    """MILOCO_MALLOC=glibc 是"原样、什么都不注入"，不再注入任何钉死参数。"""
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MILOCO_MALLOC", "glibc")
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert malloc_probe["probe_calls"] == []  # 不跑探针
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "value",
    [
        "tcmalloc",  # 不认识的名字
        "relative/path.so",  # 不是绝对路径
    ],
)
def test_malloc_unrecognized_values_warn_and_skip(
    malloc_probe, monkeypatch, capsys, value
):
    """认不出的取值一律告警 + 不注入。

    含危险字符的**绝对路径**不走这里——它确实是绝对路径，归
    test_malloc_hostile_char_in_abs_path_says_the_real_reason 管。
    """
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MILOCO_MALLOC", value)
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert "无法识别 MILOCO_MALLOC" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    [
        '/opt/a"b/libjemalloc.so.2',  # Unexpected end of key/value pairs
        "/opt/a%2Fb/libjemalloc.so.2",  # supervisord 展开时 badly formatted
        "/opt/a\nb/libjemalloc.so.2",  # No closing quotation
    ],
)
def test_malloc_hostile_char_in_abs_path_says_the_real_reason(
    malloc_probe, monkeypatch, capsys, path
):
    """含危险字符的绝对路径被挡下时要说真实原因，不能答"这不是绝对路径"。

    三种字符任意一个都会让 supervisord 拒绝加载配置（错误信息见各行注释，均为实测）。
    检查在探针入口，所以路径来源（候选链 / MILOCO_MALLOC / 自带那份）都被同一处覆盖。
    """
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MILOCO_MALLOC", path)
    monkeypatch.setattr(svc_mod.Path, "is_file", lambda self: True)
    assert svc_mod._resolve_malloc_env("/x/python") == []
    err = capsys.readouterr().err
    assert "supervisord" in err  # 说清后果
    assert "无法识别" not in err  # 不再答非所问
    assert malloc_probe["probe_calls"] == []  # 拼不出来就不必真起进程


def test_malloc_comma_in_path_is_allowed(malloc_probe, monkeypatch, capsys):
    """含逗号的路径**不能**被拒——值用双引号包住后 supervisord 解析得好好的（实测）。

    这条钉的是"别把逗号加进 _CONF_HOSTILE_CHARS"：同一个判断函数也管 MALLOC_CONF，
    而它的合法值本身就是逗号分隔的，拒掉逗号会让默认旋钮串一个都注入不进去。
    """
    import miloco_cli.commands.service as svc_mod

    so_path = "/tmp/a,b/libjemalloc.so.2"
    monkeypatch.setenv("MILOCO_MALLOC", so_path)
    monkeypatch.setattr(svc_mod.Path, "is_file", lambda self: True)
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"] == so_path
    assert "无法识别" not in capsys.readouterr().err


def test_malloc_conf_with_hostile_char_falls_back_to_default(
    malloc_probe, monkeypatch, capsys
):
    """环境里的 MALLOC_CONF 含引号时换回默认旋钮，而不是原样写进 conf。

    路径那侧早有这道闸，MALLOC_CONF 这侧原来一个字符都没检查——它同样是用户可控、
    同样原样进 environment= 行，漏了它等于闸只关了一半。
    """
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MALLOC_CONF", 'dirty_decay_ms:5000,x:"q')
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF
    assert "MALLOC_CONF 含有会写坏" in capsys.readouterr().err


def test_malloc_candidates_dedupe_symlinks(malloc_probe, tmp_path):
    """候选按 resolve() 去重：.so 是指向 .so.2 的软链时只算一个候选。

    真机上这一步能把 8 个名义候选（4 目录 × 2 文件名）压到 1 次探针：
    /usr/lib64 是指向 lib 的软链、.so 是指向 .so.2 的软链。
    """
    import miloco_cli.commands.service as svc_mod

    lib_dir = malloc_probe["lib_dir"]
    (lib_dir / "libjemalloc.so").symlink_to(lib_dir / "libjemalloc.so.2")
    alias_dir = tmp_path / "usr-lib64"
    alias_dir.symlink_to(lib_dir)  # 整个目录也是软链，像 Arch 的 /usr/lib64
    svc_mod._SYSTEM_LIB_DIRS = (alias_dir, lib_dir)

    got = list(svc_mod._jemalloc_candidates("/x/python"))
    assert len(got) == 1, got


def test_malloc_candidates_are_closed_set(malloc_probe):
    """只认 libjemalloc.so.2 / libjemalloc.so 两个名字，目录里别的文件不进候选。

    机器上装着 libjemalloc1（.so.1，3.6 一代）之类的时候，不能被误选。
    """
    import miloco_cli.commands.service as svc_mod

    lib_dir = malloc_probe["lib_dir"]
    (lib_dir / "libjemalloc.so.1").write_bytes(b"\x7fELF")
    (lib_dir / "libjemalloc_pic.a").write_bytes(b"!<arch>")

    got = [p for p, _ in svc_mod._jemalloc_candidates("/x/python")]
    assert got == [lib_dir / "libjemalloc.so.2"]


def test_malloc_separate_lib64_yields_two_candidates(malloc_probe, tmp_path):
    """lib/lib64 真分离（不是软链）时两份都是候选，且 lib64 那份先探（RHEL 系）。"""
    import miloco_cli.commands.service as svc_mod

    lib64 = tmp_path / "real-lib64"
    lib64.mkdir()
    (lib64 / "libjemalloc.so.2").write_bytes(b"\x7fELF-64")
    svc_mod._SYSTEM_LIB_DIRS = (lib64, malloc_probe["lib_dir"])

    got = [p for p, _ in svc_mod._jemalloc_candidates("/x/python")]
    assert got == [lib64 / "libjemalloc.so.2", malloc_probe["so"]]


def test_malloc_system_preferred_over_bundled(malloc_probe, tmp_path):
    """系统那份可用时根本不问自带那份（惰性，不白起子进程）。"""
    import miloco_cli.commands.service as svc_mod

    bundled = tmp_path / "bundled" / "libjemalloc.so.2"
    bundled.parent.mkdir()
    bundled.write_bytes(b"\x7fELF")
    malloc_probe["bundled"] = bundled

    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"] == str(malloc_probe["so"])


def test_malloc_falls_back_to_bundled_when_system_probe_fails(
    malloc_probe, tmp_path, capsys
):
    """系统那份探针不过时才落到自带那份。"""
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    bundled = tmp_path / "bundled" / "libjemalloc.so.2"
    bundled.parent.mkdir()
    bundled.write_bytes(b"\x7fELF")
    malloc_probe["bundled"] = bundled

    system_so = str(malloc_probe["so"])

    def fake_run(cmd, **kwargs):
        if "-E" in cmd:
            env = kwargs.get("env") or {}
            malloc_probe["probe_calls"].append({"cmd": cmd, "env": env})
            if env["LD_PRELOAD"].startswith(system_so):
                return sp.CompletedProcess(cmd, 0, "not-taken-over\n", "")
            return sp.CompletedProcess(cmd, 0, _probe_stdout(), "")
        return sp.CompletedProcess(cmd, 0, f"{bundled}\n", "")

    svc_mod.subprocess.run = fake_run
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"] == str(bundled)
    assert "不可用" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["import_error", "not_a_file", "timeout", "oserror"])
def test_malloc_bundled_lookup_failures_yield_none(malloc_probe, tmp_path, failure):
    """问自带那份路径的各种失败都退回 None，不让候选链带着垃圾路径往下走。"""
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    def fake_run(cmd, **kwargs):
        if failure == "import_error":  # backend 里没装 miot
            return sp.CompletedProcess(cmd, 1, "", "ModuleNotFoundError: miot")
        if failure == "not_a_file":  # 归档里没带这个 .so
            return sp.CompletedProcess(cmd, 0, str(tmp_path / "nope.so") + "\n", "")
        if failure == "timeout":
            raise sp.TimeoutExpired(cmd, 3)
        raise OSError("解释器起不来")

    svc_mod.subprocess.run = fake_run
    assert svc_mod._bundled_jemalloc("/x/python") is None


def test_malloc_no_backend_python_skips_bundled(malloc_probe):
    """拿不到 backend 解释器（server_cmd 解析为空）时不问自带那份。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["so"].unlink()
    assert list(svc_mod._jemalloc_candidates(None)) == []


def test_malloc_probe_uses_backend_interpreter_isolated(malloc_probe):
    """探针必须用 backend 的解释器 + -E -S，并带上被检查的 LD_PRELOAD / MALLOC_CONF。

    用 backend 的而不是 CLI 自己的：两个解释器的 libc 和链接方式可能不同，
    "CLI 能被接管"推不出"backend 能被接管"，后者才是要保护的目标。
    -E -S 屏蔽环境变量和 site 目录：sitecustomize / .pth 里的东西可能自己就崩掉或改写
    分配器，那样测的就不是这份 .so 了。
    """
    import miloco_cli.commands.service as svc_mod

    svc_mod._resolve_malloc_env("/x/backend-python")
    call = malloc_probe["probe_calls"][0]
    assert call["cmd"][0] == "/x/backend-python"
    assert call["cmd"][1:4] == ["-E", "-S", "-c"]
    assert call["env"]["LD_PRELOAD"].startswith(str(malloc_probe["so"]))
    assert call["env"]["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF


def test_malloc_probe_falls_back_to_own_interpreter(malloc_probe):
    """拿不到 backend 解释器时退回 sys.executable，而不是不探针就用。"""
    import miloco_cli.commands.service as svc_mod

    svc_mod._probe_jemalloc(malloc_probe["so"], "x:1", None, 3)
    assert malloc_probe["probe_calls"][0]["cmd"][0] == svc_mod.sys.executable


@pytest.mark.parametrize(
    ("setup", "expect_in_fatal"),
    [
        # ld.so 对加载不了的预加载库是"打一行 ERROR 然后忽略、程序照常跑"，退出码仍是 0，
        # 所以这条静默降级只有靠 mallctl 符号取不到才抓得住。
        ({"stdout": "not-taken-over\n"}, "没有接管"),
        ({"stdout": "probe-crashed: ValueError()\n"}, "探针自身异常"),
        ({"returncode": -11}, "信号 11"),
        ({"returncode": 1}, "退出码 1"),
        # 契约不符时把 stderr 末行附进原因里——它不参与判定，但页大小不符那种情况下
        # jemalloc 的原话比"输出不符合契约"好排查得多。
        ({"stdout": "", "stderr": "boom\n"}, "boom"),
        # 没有"契约"这条判据，意料外的输出会默认落到"通过"
        ({"stdout": ""}, "不符合契约"),
        ({"stdout": "hello world\n"}, "不符合契约"),
        ({"stdout": "page=4096\nbg=True\n"}, "不符合契约"),
    ],
)
def test_malloc_probe_fatal_branches(malloc_probe, setup, expect_in_fatal):
    """致命判定的各条分支。任何一条漏掉，坏的 .so 都会被写进 supervisord.conf。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe.update(setup)
    probe = svc_mod._probe_jemalloc(malloc_probe["so"], "x:1", "/x/python", 3)
    assert probe.fatal is not None
    assert expect_in_fatal in probe.fatal


@pytest.mark.parametrize(
    ("exc", "expect"),
    [
        (__import__("subprocess").TimeoutExpired("cmd", 3), "未返回"),
        (OSError("boom"), "无法执行"),
    ],
)
def test_malloc_probe_subprocess_failures_are_fatal(malloc_probe, exc, expect):
    """探针起不来或超时也是致命，要换下一个候选而不是当它通过。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["raise"] = exc
    probe = svc_mod._probe_jemalloc(malloc_probe["so"], "x:1", "/x/python", 3)
    assert probe.fatal is not None and expect in probe.fatal


def test_malloc_page_size_mismatch_is_fatal_even_on_rc_zero(malloc_probe, capsys):
    """页大小不符：stderr 有 Unsupported system page size + stdout 空 + **退出码 0**。

    退出码不能作为判据：release 构建 opt_abort 默认 false，jemalloc 打一行 stderr 就返回，
    宿主进程怎么死取决于它自己，不保证被信号打死。
    """
    import miloco_cli.commands.service as svc_mod

    malloc_probe.update(
        {
            "stdout": "",
            "stderr": "<jemalloc>: Unsupported system page size\n",
            "returncode": 0,
        }
    )
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert "Unsupported system page size" in capsys.readouterr().err


def test_malloc_page_mismatch_leaves_no_persistent_state(malloc_probe, monkeypatch):
    """全部候选都因页大小失败时不注入，且不留任何持久状态——下次启动会重试。

    换回好内核后一次 service restart 就能自动恢复，不需要人工清什么东西。
    """
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file, load_config

    malloc_probe.update({"stdout": "", "stderr": "<jemalloc>: Unsupported system page size\n"})
    assert svc_mod._resolve_malloc_env("/x/python") == []
    persisted = json.loads(config_file().read_text()) if config_file().exists() else {}
    assert "safe_mode" not in persisted
    assert load_config()["safe_mode"] is False

    # 同一进程内换成好结果，立刻就能注入（没有被记住的黑名单）
    malloc_probe.update({"stdout": _probe_stdout(), "stderr": ""})
    assert dict(svc_mod._resolve_malloc_env("/x/python"))["LD_PRELOAD"]


def test_malloc_stderr_never_decides_usability(malloc_probe, capsys):
    """stderr 不参与"能不能用"的判定：只要 jemalloc 接管了，它打什么都照常用。

    这三类输出都是库在正常工作时打的，任何一类被判死都会让预加载在整类机器上静默失效：
    不认识的旋钮（老版本 jemalloc）、qemu/gVisor 下 MADV_DONTNEED 退化成 memset、
    环境里别人的 LD_PRELOAD 条目加载失败。用白名单逐条豁免则永远补不完。
    """
    import miloco_cli.commands.service as svc_mod

    malloc_probe["stderr"] = (
        f"{_INVALID}dirty_decay_ms:5000\n"
        "<jemalloc>: MADV_DONTNEED does not work (memset will be used instead)\n"
        "<jemalloc>: (This is the expected behaviour if you are running under QEMU)\n"
        "ERROR: ld.so: object '/x/libfoo.so' from LD_PRELOAD cannot be preloaded: ignored.\n"
    )
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["LD_PRELOAD"].startswith(str(malloc_probe["so"]))
    # 不认识的旋钮原样留着：jemalloc 逐个独立解析，被拒的那个不影响其它旋钮生效，
    # 它自己会在 backend 日志里打 Invalid conf pair —— 那正是用户该看到的。
    assert pairs["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF
    assert "不可用" not in capsys.readouterr().err


def test_malloc_not_taken_over_is_fatal_regardless_of_stderr(malloc_probe):
    """不看 stderr 不会放过真故障：库自己加载不了时 stdout 就是 not-taken-over。

    这是"删掉 stderr 判定"的安全前提——ld.so 忽略掉加载不了的预加载库后退出码仍是 0，
    但 mallctl 符号取不到，stdout 那条判据独立且可靠。
    """
    import miloco_cli.commands.service as svc_mod

    malloc_probe["stdout"] = "not-taken-over"
    malloc_probe["stderr"] = ""  # 连一行报错都没有，照样判死
    assert svc_mod._resolve_malloc_env("/x/python") == []


def test_malloc_readback_is_reported_as_is(malloc_probe, capsys):
    """读回值原样照打，不替它分类、不额外告警。

    老版本 jemalloc 三个旋钮全缺时读回全是 None —— `dirty/muzzy_decay_ms=None/None`
    摆在成功行里就是事实，用户一眼看得见。jemalloc 是锦上添花的优化项，为"它属于
    没生效的哪一态"加判断分支不值得；旋钮真被拒时它自己还会在 backend 日志里打
    Invalid conf pair。
    """
    import miloco_cli.commands.service as svc_mod

    malloc_probe["stdout"] = _probe_stdout(
        ver="3.6.0", page="None", bg="None", dirty="None", muzzy="None"
    )
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    # 照常用：旋钮没生效仍远好过 glibc 的 arena 碎片
    assert "LD_PRELOAD" in pairs
    assert pairs["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF
    err = capsys.readouterr().err
    assert "dirty/muzzy_decay_ms=None/None" in err
    assert "background_thread=None" in err


@pytest.mark.parametrize(
    ("stdout", "expect_version", "expect_page"),
    [
        (_probe_stdout(ver="?"), "unknown", 4096),  # mallctl 取不到 version
        (_probe_stdout(page="None"), "5.3.1", None),  # 老版本没有 arenas.page
    ],
)
def test_malloc_tolerates_missing_version_and_page(
    malloc_probe, capsys, stdout, expect_version, expect_page
):
    """版本 / page 读不到都是可容忍：malloc 已被接管这件事由拿到 mallctl 符号证明了。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["stdout"] = stdout
    probe = svc_mod._probe_jemalloc(malloc_probe["so"], "x:1", "/x/python", 3)
    assert probe.fatal is None
    assert probe.version == expect_version
    assert probe.page == expect_page


def test_malloc_empty_conf_env_falls_back_to_default(malloc_probe, monkeypatch):
    """环境里 MALLOC_CONF 是空串时按"没设"处理，不能把三个旋钮整体清空。

    os.environ.get 的默认值只在 key 不存在时才给；`export MALLOC_CONF=` 或 Dockerfile 的
    `ENV MALLOC_CONF=` 会让它返回空串。真注进去的话 jemalloc 照常接管、探针照常通过，
    但后台归还线程关着、decay 回到默认 10 秒——这个方案想买的东西全部落空，日志还看不出来。
    """
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MALLOC_CONF", "")
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    assert pairs["MALLOC_CONF"] == svc_mod._JEMALLOC_MALLOC_CONF


@pytest.mark.parametrize(
    ("machine", "expect_subdir"),
    [
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("armv7l", ""),  # 没有自带那份
        ("riscv64", ""),
    ],
)
def test_bundled_path_script_maps_arch(monkeypatch, capsys, tmp_path, machine, expect_subdir):
    """真跑 _BUNDLED_PATH_SCRIPT 这段脚本本身，而不是在测试里重抄一份映射表。

    未知架构（armv7 / riscv64）必须算出空串：二选一会让 armv7 拿到 x86_64 那份的路径，而那
    文件在源码树里真实存在 → 进候选 → 白起一次探针 → 被 ld.so 以 wrong ELF class 打回来，
    还多一行看着像故障的告警。与 _system_lib_dirs 对未知架构"跳过这一项"的口径也不一致。
    """
    import platform as platform_mod
    import sys
    import types

    import miloco_cli.commands.service as svc_mod

    fake_miot = types.ModuleType("miot")
    fake_miot.__file__ = str(tmp_path / "miot" / "__init__.py")
    monkeypatch.setitem(sys.modules, "miot", fake_miot)
    monkeypatch.setattr(platform_mod, "machine", lambda: machine)

    exec(svc_mod._BUNDLED_PATH_SCRIPT, {})  # noqa: S102
    printed = capsys.readouterr().out.strip()

    if expect_subdir:
        assert printed.endswith(f"libs/linux/{expect_subdir}/libjemalloc.so.2")
    else:
        assert printed == ""


def test_supervisord_start_failure_hints_and_keeps_stderr(
    runner, isolated_config, monkeypatch, tmp_path
):
    """supervisord 自己起不来时也要打逃生提示，并带出它的 stderr。

    conf 的 environment= 行被写坏时，supervisord 起不来是唯一的症状——FATAL 与 health
    超时那两条提示都要求它已经起来了。而 CalledProcessError 自己只说"returned non-zero
    exit status 2"，真正的原因在 e.stderr 里，不带出来用户无从下手。
    """
    import subprocess

    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import set_value

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    set_value("server.python_bin", str(fake_python))

    conf = tmp_path / "supervisord.conf"
    conf.write_text('environment=LD_PRELOAD="/usr/lib/libjemalloc.so.2"\n')
    monkeypatch.setattr(svc_mod, "_supervisor_conf", lambda: conf)
    monkeypatch.setattr(svc_mod, "_generate_supervisor_conf", lambda cmd: None)
    monkeypatch.setattr(svc_mod, "_supervisord_is_running", lambda: False)
    monkeypatch.setattr(svc_mod, "_is_port_in_use", lambda url: False)
    monkeypatch.setattr(svc_mod, "_find_supervisord_pids", lambda: [])

    real_reason = "Error: Format string '...' for 'environment' is badly formatted"
    with patch.object(
        svc_mod.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(2, ["supervisord"], stderr=real_reason),
    ):
        result = runner.invoke(cli, ["service", "start"])

    assert result.exit_code == 1
    assert real_reason in result.output  # supervisord 的原话没被丢掉
    assert "safe_mode true" in result.output  # 逃生开关给到了


@pytest.mark.parametrize("action", ["start", "restart"])
def test_supervisorctl_failure_hints_safe_mode(
    runner, isolated_config, malloc_probe, monkeypatch, tmp_path, action
):
    """supervisord 活着但程序没起来时也要给逃生开关。

    启动失败一共五条出口（FATAL / health 超时 / supervisorctl start / supervisord
    起不来 / supervisorctl restart）。FATAL 和 health 超时那两条要求先走到
    _wait_for_health，而本用例覆盖的这两条在那之前就 exit 了。升级后第一次 restart
    恰恰是配置里刚带上 LD_PRELOAD 的那一次，最可能撞上注入问题，反而拿不到开关。
    """
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import set_value

    py = tmp_path / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    set_value("server.python_bin", str(py))

    conf = tmp_path / "supervisord.conf"
    conf.write_text('environment=LD_PRELOAD="/usr/lib/libjemalloc.so.2"\n')
    monkeypatch.setattr(svc_mod, "_supervisor_conf", lambda: conf)
    monkeypatch.setattr(svc_mod, "_generate_supervisor_conf", lambda cmd: None)
    monkeypatch.setattr(svc_mod, "_supervisord_is_running", lambda: True)
    monkeypatch.setattr(svc_mod, "_is_port_in_use", lambda url: False)
    monkeypatch.setattr(svc_mod, "_get_backend_pid_from_supervisor", lambda: None)
    monkeypatch.setattr(
        svc_mod,
        "_supervisorctl",
        lambda *a: __import__("subprocess").CompletedProcess(
            [], 1 if a[0] in ("start", "restart") else 0, "abnormal termination", ""
        ),
    )

    result = runner.invoke(cli, ["service", action])
    assert result.exit_code == 1
    assert "miloco-cli config set safe_mode true" in result.output


def test_malloc_bundled_empty_path_treated_as_missing(malloc_probe):
    """脚本打空串时按"拿不到自带那份"处理，不能把空路径塞进候选链。"""
    import miloco_cli.commands.service as svc_mod

    malloc_probe["bundled"] = ""
    assert svc_mod._bundled_jemalloc("/x/python") is None


def test_malloc_dropped_preload_warns_once_not_per_candidate(
    malloc_probe, monkeypatch, capsys, tmp_path
):
    """原有 LD_PRELOAD 被丢掉的提醒只打一次，不随候选数量翻倍。

    值拼装在每个候选的探针里都会调一次，把 echo 放在那里会让同一句重复 N+1 遍，
    中间还夹着候选失败的告警，读起来像几个不同的问题。
    """
    import miloco_cli.commands.service as svc_mod

    lib_b = tmp_path / "lib-b"
    lib_b.mkdir()
    (lib_b / "libjemalloc.so.2").write_bytes(b"\x7fELF")
    monkeypatch.setattr(svc_mod, "_SYSTEM_LIB_DIRS", (malloc_probe["so"].parent, lib_b))
    monkeypatch.setenv("LD_PRELOAD", '/opt/vendor/lib"x.so')

    svc_mod._resolve_malloc_env("/x/python")
    assert capsys.readouterr().err.count("环境里已有的 LD_PRELOAD") == 1


def test_malloc_ld_preload_appends_not_overwrites(malloc_probe, monkeypatch):
    """环境里已有 LD_PRELOAD 时拼成 <我们的>:<原有>，我们的在最前（要它接管 malloc）。

    探针环境用同一套拼法，保证"探的"和"注的"一致。
    """
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("LD_PRELOAD", "/opt/other/libfoo.so")
    pairs = dict(svc_mod._resolve_malloc_env("/x/python"))
    expected = f"{malloc_probe['so']}:/opt/other/libfoo.so"
    assert pairs["LD_PRELOAD"] == expected
    assert malloc_probe["probe_calls"][0]["env"]["LD_PRELOAD"] == expected


def test_malloc_not_linux_injects_nothing(malloc_probe, monkeypatch):
    """非 Linux 不注入任何变量：macOS 上 LD_PRELOAD 本就无效。"""
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setattr(svc_mod.sys, "platform", "darwin")
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert malloc_probe["probe_calls"] == []


def test_malloc_system_budget_exhausted_still_tries_bundled(
    malloc_probe, monkeypatch, tmp_path, capsys
):
    """系统段预算耗尽后跳过剩余系统候选，但自带那份仍会被尝试。

    预算分两段的理由就在这里：共用一条总预算的话，前面几个卡住的系统库能把预算吃光，
    让"系统没装 jemalloc 时唯一可用的那份"永远探不到——故障场景和它的兜底目标高度重合。
    """
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    # 三个系统候选，每次探针"耗时"3s，总预算 5s：第 1 个超时取 min(3, 5)=3s，探完剩 2s；
    # 第 2 个超时被剩余预算压到 2s，探完剩 -1s；第 3 个因 -1 < 0.5 被跳过。用假时钟精确控制。
    sys_dirs = []
    for i in range(3):
        d = tmp_path / f"lib{i}"
        d.mkdir()
        (d / "libjemalloc.so.2").write_bytes(b"\x7fELF")
        sys_dirs.append(d)
    monkeypatch.setattr(svc_mod, "_SYSTEM_LIB_DIRS", tuple(sys_dirs))

    bundled = tmp_path / "bundled" / "libjemalloc.so.2"
    bundled.parent.mkdir()
    bundled.write_bytes(b"\x7fELF")

    clock = {"t": 0.0}
    monkeypatch.setattr(svc_mod.time, "monotonic", lambda: clock["t"])

    def fake_run(cmd, **kwargs):
        if "-E" in cmd:
            env = kwargs.get("env") or {}
            malloc_probe["probe_calls"].append(env["LD_PRELOAD"].split(":")[0])
            clock["t"] += 3.0  # 每次探针吃掉 3s，两次就把 5s 预算用光
            return sp.CompletedProcess(cmd, 0, "not-taken-over\n", "")
        return sp.CompletedProcess(cmd, 0, f"{bundled}\n", "")

    svc_mod.subprocess.run = fake_run
    svc_mod._resolve_malloc_env("/x/python")

    probed = malloc_probe["probe_calls"]
    assert str(sys_dirs[2] / "libjemalloc.so.2") not in probed  # 第三个被预算挡掉
    assert str(bundled) in probed  # 自带那份仍被尝试
    assert "超过时间预算" in capsys.readouterr().err


def test_malloc_probe_timeout_shrinks_to_remaining_budget(
    malloc_probe, monkeypatch, tmp_path
):
    """单次探针超时取 min(3s, 剩余预算)，系统段才严格不超 5s。

    deadline 检查只发生在起探针**之前**，管得住"要不要再起一个"、管不住"已经起的能跑多久"。
    不取 min 的话，最后一个候选能在 deadline 前一刻启动再跑满 3s，把总预算打穿。
    """
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    sys_dirs = []
    for i in range(2):
        d = tmp_path / f"lib{i}"
        d.mkdir()
        (d / "libjemalloc.so.2").write_bytes(b"\x7fELF")
        sys_dirs.append(d)
    monkeypatch.setattr(svc_mod, "_SYSTEM_LIB_DIRS", tuple(sys_dirs))

    clock = {"t": 0.0}
    monkeypatch.setattr(svc_mod.time, "monotonic", lambda: clock["t"])
    timeouts = []

    def fake_run(cmd, **kwargs):
        if "-E" in cmd:
            timeouts.append(kwargs["timeout"])
            clock["t"] += 3.5  # 第一次探完剩 1.5s
            return sp.CompletedProcess(cmd, 0, "not-taken-over\n", "")
        return sp.CompletedProcess(cmd, 1, "", "")

    svc_mod.subprocess.run = fake_run
    svc_mod._resolve_malloc_env("/x/python")

    assert timeouts[0] == 3  # 预算充足时用满 3s
    assert timeouts[1] == 1.5  # 只剩 1.5s 就只给 1.5s
    assert sum(timeouts) <= svc_mod._SYSTEM_PROBE_BUDGET_S


def test_malloc_no_new_probe_below_min_slice(malloc_probe, monkeypatch, tmp_path):
    """剩余预算不足 0.5s 时不再起新探针（起了也来不及，白等）。"""
    import subprocess as sp

    import miloco_cli.commands.service as svc_mod

    sys_dirs = []
    for i in range(2):
        d = tmp_path / f"lib{i}"
        d.mkdir()
        (d / "libjemalloc.so.2").write_bytes(b"\x7fELF")
        sys_dirs.append(d)
    monkeypatch.setattr(svc_mod, "_SYSTEM_LIB_DIRS", tuple(sys_dirs))

    clock = {"t": 0.0}
    monkeypatch.setattr(svc_mod.time, "monotonic", lambda: clock["t"])
    n = {"probes": 0}

    def fake_run(cmd, **kwargs):
        if "-E" in cmd:
            n["probes"] += 1
            clock["t"] += 4.8  # 探完只剩 0.2s < 0.5s
            return sp.CompletedProcess(cmd, 0, "not-taken-over\n", "")
        return sp.CompletedProcess(cmd, 1, "", "")

    svc_mod.subprocess.run = fake_run
    svc_mod._resolve_malloc_env("/x/python")
    assert n["probes"] == 1


def test_malloc_resolve_failure_still_produces_usable_conf(malloc_probe, monkeypatch):
    """_resolve_malloc_env 自己抛异常时，supervisord.conf 照常产出且可用。

    一个优化项的代码 bug 绝不能让服务起不来。
    """
    import miloco_cli.commands.service as svc_mod

    def boom(_py=None):
        raise RuntimeError("分配器逻辑有 bug")

    monkeypatch.setattr(svc_mod, "_resolve_malloc_env", boom)
    svc_mod._generate_supervisor_conf("/x/python -m miloco")

    conf = svc_mod._supervisor_conf().read_text()
    assert 'MILOCO_SUPERVISED="1"' in conf
    assert "LD_PRELOAD" not in conf
    assert f"[program:{svc_mod._PROGRAM_NAME}]" in conf


# ─── 安全模式 ─────────────────────────────────────────────────────────────────


def test_safe_mode_injects_nothing_and_skips_probe(malloc_probe, capsys):
    """safe_mode=true → 一个变量都不注入，且不跑探针（省掉子进程开销）。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps({"safe_mode": True}), encoding="utf-8")

    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert malloc_probe["probe_calls"] == []
    assert "安全模式已开启" in capsys.readouterr().err


def test_safe_mode_beats_explicit_milocomalloc(malloc_probe, monkeypatch, capsys):
    """safe_mode 赢过 MILOCO_MALLOC 的一切取值，并说明后者被忽略。

    它的语义是"我遇到问题了"，不该被更细的设置推翻；不说清楚用户会困惑"设了却没生效"。
    """
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps({"safe_mode": True}), encoding="utf-8")
    monkeypatch.setenv("MILOCO_MALLOC", str(malloc_probe["so"]))

    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert "被忽略" in capsys.readouterr().err


def test_safe_mode_env_override_works(malloc_probe, monkeypatch):
    """MILOCO_SAFE_MODE=1 能临时覆盖 config.json 里的 false（_apply_env_overrides 既有能力）。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps({"safe_mode": False}), encoding="utf-8")
    assert dict(svc_mod._resolve_malloc_env("/x/python"))  # 先确认没开时会注入

    monkeypatch.setenv("MILOCO_SAFE_MODE", "1")
    assert svc_mod._resolve_malloc_env("/x/python") == []


def test_safe_mode_on_non_linux_is_noop(malloc_probe, monkeypatch, capsys):
    """非 Linux 上 safe_mode 开与不开行为一致（都不注入），不报错也不额外告警。

    macOS 上 LD_PRELOAD 本就无效，所以这个开关在那里没有可观测变化。
    """
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    monkeypatch.setattr(svc_mod.sys, "platform", "darwin")
    config_file().parent.mkdir(parents=True, exist_ok=True)

    config_file().write_text(json.dumps({"safe_mode": True}), encoding="utf-8")
    assert svc_mod._resolve_malloc_env("/x/python") == []
    on_err = capsys.readouterr().err

    config_file().write_text(json.dumps({"safe_mode": False}), encoding="utf-8")
    assert svc_mod._resolve_malloc_env("/x/python") == []
    assert capsys.readouterr().err == on_err == ""


def test_safe_mode_removes_ld_preload_from_generated_conf(malloc_probe):
    """safe_mode 一开，重新生成的 conf 里确实没有 LD_PRELOAD；关掉又恢复注入。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" in svc_mod._supervisor_conf().read_text()

    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps({"safe_mode": True}), encoding="utf-8")
    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" not in svc_mod._supervisor_conf().read_text()

    config_file().write_text(json.dumps({"safe_mode": False}), encoding="utf-8")
    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" in svc_mod._supervisor_conf().read_text()


def test_safe_mode_is_known_config_path():
    """safe_mode 在 CLI schema 白名单里，所以 config get/set/show 全都免费可用。"""
    from miloco_cli.config import known_paths, load_config

    assert "safe_mode" in known_paths()
    assert load_config()["safe_mode"] is False


def test_config_set_alone_leaves_ld_preload_in_conf(
    runner, isolated_config, malloc_probe
):
    """钉住逃生提示为什么必须给两条命令，而不是只给 config set。

    提示只在启动失败后打出，此时后端不在跑，config set 顺带的那次重启判定 not
    running、不触发，于是 conf 不会重新生成、LD_PRELOAD 原样还在，而命令本身返回
    ok。这个前提哪天变了（config set 改成无条件重新生成 conf），这条会红——届时
    提示里第二条命令才可以删。
    """
    import miloco_cli.commands.service as svc_mod

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" in svc_mod._supervisor_conf().read_text()

    result = runner.invoke(cli, ["config", "set", "safe_mode", "true"])
    assert result.exit_code == 0
    assert json.loads(result.output)["restart"] == {
        "triggered": False,
        "reason": "not running",
    }
    assert "LD_PRELOAD" in svc_mod._supervisor_conf().read_text()


# ─── conf 原子写 + health 失败提示 ────────────────────────────────────────────


def test_supervisor_conf_written_atomically(malloc_probe, monkeypatch):
    """conf 走临时文件 + os.replace，不是先截断再写。"""
    import miloco_cli.commands.service as svc_mod

    replaced = []
    real_replace = svc_mod.atomic_write_text.__globals__["os"].replace

    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(svc_mod.atomic_write_text.__globals__["os"], "replace", spy_replace)
    svc_mod._generate_supervisor_conf("/x/python -m miloco")

    assert len(replaced) == 1
    tmp_src, dst = replaced[0]
    assert dst == str(svc_mod._supervisor_conf())
    assert tmp_src.endswith(".tmp") and tmp_src != dst


def test_supervisor_conf_survives_failed_write(malloc_probe, monkeypatch):
    """写临时文件失败时原 conf 保持不变（不被截断成空文件）。

    先截断再写的话，进程在中间被杀或磁盘满就会留下半截 conf，supervisord 直接起不来。
    """
    import miloco_cli.commands.service as svc_mod

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    good = svc_mod._supervisor_conf().read_text()

    def boom(*a, **k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(svc_mod.atomic_write_text.__globals__["os"], "fsync", boom)
    with pytest.raises(OSError):
        svc_mod._generate_supervisor_conf("/x/python -m miloco --changed")

    assert svc_mod._supervisor_conf().read_text() == good


def test_supervisor_conf_skips_write_when_identical(malloc_probe, monkeypatch):
    """内容相同就跳过写入那层短路要保留在最前面。"""
    import miloco_cli.commands.service as svc_mod

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    writes = []
    monkeypatch.setattr(
        svc_mod, "atomic_write_text", lambda p, t: writes.append(str(p))
    )
    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert writes == []


@pytest.mark.parametrize(
    ("conf_body", "expected"),
    [
        ('environment=LD_PRELOAD="/usr/lib/libjemalloc.so.2"', "/usr/lib/libjemalloc.so.2"),
        # 追加语义：值是 <我们的>:<原有>，取第一段
        (
            'environment=LD_PRELOAD="/usr/lib/libjemalloc.so.2:/opt/x.so"',
            "/usr/lib/libjemalloc.so.2",
        ),
        ('environment=MILOCO_SUPERVISED="1"', None),  # 没注入
        ("", None),
    ],
)
def test_injected_jemalloc_from_conf(malloc_probe, conf_body, expected):
    """提示行的事实来源是 conf 本身：有 LD_PRELOAD 就取首段，没有就返回 None。"""
    import miloco_cli.commands.service as svc_mod

    path = svc_mod._supervisor_conf()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(conf_body)
    assert svc_mod._injected_jemalloc_from_conf() == expected


def test_injected_jemalloc_from_conf_missing_file(malloc_probe):
    """conf 还不存在时返回 None 而不是抛异常。"""
    import miloco_cli.commands.service as svc_mod

    assert not svc_mod._supervisor_conf().exists()
    assert svc_mod._injected_jemalloc_from_conf() is None


@pytest.mark.parametrize("failure", ["fatal", "timeout"])
def test_health_failure_hints_safe_mode_when_injected(malloc_probe, capsys, failure):
    """health 失败且本次注入了 jemalloc → 打安全模式提示，但不下结论。"""
    import miloco_cli.commands.service as svc_mod

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" in svc_mod._supervisor_conf().read_text()

    cfg = {"server": {"url": "http://127.0.0.1:65500"}}
    with (
        patch.object(
            svc_mod,
            "_supervisorctl",
            return_value=__import__("subprocess").CompletedProcess(
                [], 0, "FATAL" if failure == "fatal" else "STARTING", ""
            ),
        ),
        patch.object(svc_mod.time, "time", side_effect=[0, 0, 1e9, 1e9]),
        pytest.raises(SystemExit),
    ):
        svc_mod._wait_for_health(cfg, pretty=False)

    err = capsys.readouterr().err
    assert "本次启用了 jemalloc" in err
    # 钉住可执行文件名：这行是服务起不来时唯一的逃生指引，用户会直接照抄。
    # 只钉 "config set safe_mode true" 不够——写成 `miloco config set` 也能过，
    # 而本仓库只注册了 miloco-cli 这一个 console script。
    assert "miloco-cli config set safe_mode true" in err
    # 第二条命令同样要给，理由见 test_config_set_alone_leaves_ld_preload_in_conf。
    assert "miloco-cli service start" in err
    # 不下结论：启动失败原因很多，不能说"是 jemalloc 导致的"
    assert "导致" not in err


def test_health_failure_no_hint_when_not_injected(malloc_probe, capsys, monkeypatch):
    """没注入 jemalloc 时 health 失败不打那行提示（否则是误导）。"""
    import miloco_cli.commands.service as svc_mod

    monkeypatch.setenv("MILOCO_MALLOC", "glibc")
    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    assert "LD_PRELOAD" not in svc_mod._supervisor_conf().read_text()

    cfg = {"server": {"url": "http://127.0.0.1:65500"}}
    with (
        patch.object(
            svc_mod,
            "_supervisorctl",
            return_value=__import__("subprocess").CompletedProcess([], 0, "FATAL", ""),
        ),
        pytest.raises(SystemExit),
    ):
        svc_mod._wait_for_health(cfg, pretty=False)

    assert "jemalloc" not in capsys.readouterr().err


def test_health_failure_does_not_self_heal(malloc_probe, capsys):
    """health 失败时不做任何自动重启、不写任何禁用状态。

    "摘掉 jemalloc 后成功"推不出"是 jemalloc 的错"（端口 TIME_WAIT、依赖慢启动、概率性崩溃
    都会造成同样结果）。用一次不可靠的推断做永久决定，比让用户自己看更糟。
    """
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    svc_mod._generate_supervisor_conf("/x/python -m miloco")
    calls = []

    def spy_ctl(*args):
        calls.append(args)
        return __import__("subprocess").CompletedProcess([], 0, "FATAL", "")

    with patch.object(svc_mod, "_supervisorctl", spy_ctl), pytest.raises(SystemExit):
        svc_mod._wait_for_health({"server": {"url": "http://127.0.0.1:65500"}}, False)

    assert all(a[0] == "status" for a in calls)  # 只查状态，没 restart
    persisted = json.loads(config_file().read_text()) if config_file().exists() else {}
    assert "safe_mode" not in persisted


def test_foreground_mode_injects_into_environ(malloc_probe, monkeypatch, tmp_path):
    """前台模式不经 supervisord，分配器变量要塞进 os.environ 再 exec（ld.so 在 exec 时读它）。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps({"server": {"python_bin": str(py)}}))

    execs = []
    monkeypatch.setattr(svc_mod.os, "execvp", lambda f, a: execs.append((f, a)))
    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_is_port_in_use", return_value=False),
    ):
        CliRunner().invoke(cli, ["service", "start", "--foreground"])

    assert execs, "execvp 没被调到"
    assert svc_mod.os.environ["LD_PRELOAD"].startswith(str(malloc_probe["so"]))


def test_foreground_mode_respects_safe_mode(malloc_probe, monkeypatch, tmp_path):
    """前台模式同样读 safe_mode——它是调试入口，不该绕过这个开关。"""
    import miloco_cli.commands.service as svc_mod
    from miloco_cli.config import config_file

    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(
        json.dumps({"safe_mode": True, "server": {"python_bin": str(py)}})
    )

    execs = []
    monkeypatch.setattr(svc_mod.os, "execvp", lambda f, a: execs.append((f, a)))
    with (
        patch.object(svc_mod, "_supervisord_is_running", return_value=False),
        patch.object(svc_mod, "_is_port_in_use", return_value=False),
    ):
        CliRunner().invoke(cli, ["service", "start", "--foreground"])

    assert execs
    assert "LD_PRELOAD" not in svc_mod.os.environ
    assert malloc_probe["probe_calls"] == []


def test_malloc_fixture_left_no_env_behind():
    """前面那些用例跑完，pytest 进程里不该还留着 LD_PRELOAD / MALLOC_CONF。

    前台模式那条被测路径直接改真的 os.environ（exec 前的 os.environ.update），而
    monkeypatch.delenv 对"本来就不存在"的变量不记 undo、还不回来（实测）。漏出去的话
    后面任何真 fork 子进程的用例都会白拿一行 ld.so 报错，且是在跟它无关的断言里炸。
    由 malloc_probe 的 finalizer 兜住。

    这条**依赖执行顺序**（放在文件末尾、pytest 按定义顺序跑，仓库未装 randomly/xdist）：
    要钉的是跨用例的残留，单个用例内部看不见它。
    """
    assert "LD_PRELOAD" not in os.environ
    assert "MALLOC_CONF" not in os.environ
