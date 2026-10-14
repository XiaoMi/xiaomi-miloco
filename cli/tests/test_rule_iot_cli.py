"""rule 的 iot 条件参数与诊断出口。"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from miloco_cli.main import cli

_SPEC = {
    "code": 0,
    "data": {
        "spec": {
            "prop.5.1": {"format": "uint8", "description": "门 门状态"},
            "prop.2.1": {"format": "bool", "description": "开关"},
            "prop.3.1": {"format": "float", "description": "温度"},
            "prop.7.1": {"format": "iids", "description": "预设"},
        }
    },
}
_OK = {"code": 0, "message": "ok", "data": {"rule_id": "r1"}}


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    import os as _os

    for key in [k for k in _os.environ if k.startswith("MILOCO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco"))


def _create(runner, *args):
    with (
        patch("miloco_cli.client.api_get", return_value=_SPEC),
        patch("miloco_cli.client.api_post", return_value=_OK) as post,
    ):
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--name",
                "门开了",
                "--task-id",
                "door_alert",
                "--action-desc",
                "播报",
                *args,
            ],
        )
    return result, post


def _payload(post):
    return post.call_args[0][1]


# ── 参数组合 ──────────────────────────────────────────────────────────


def test_neither_condition_nor_iot_args_is_an_error(runner):
    """摘掉 --condition 的 required=True 之后接替的那道闸。

    **断错误文案，不断退出码** —— required=True 的缺失和这条新校验都是非零退出。
    """
    result, post = _create(runner)

    assert "--condition" in result.output and "--iot-" in result.output
    assert not post.called


def test_both_condition_and_iot_args_is_an_error(runner):
    result, _post = _create(
        runner,
        "--condition",
        "有人在门口",
        "--iot-did",
        "d1",
        "--iot-iid",
        "5.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert "不能一起给" in result.output


def test_source_with_iot_args_is_an_error_on_create(runner):
    """--source 与 iot 四件套同给要报错, 不能把设备列表静默清掉。

    **断 post 没被调用** —— 静默清空那版本会成功建出一条规则、退出码 0。
    """
    result, post = _create(
        runner,
        "--source",
        "cam-001",
        "--iot-did",
        "d1",
        "--iot-iid",
        "5.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert "--source" in result.output and "不能一起给" in result.output
    assert not post.called


def test_source_with_iot_args_is_an_error_on_update(runner):
    """同一道判据在 update 侧也要生效 —— 两侧共用一份, 各装一份就会漏。"""
    with (
        patch("miloco_cli.client.api_get", return_value=_SPEC),
        patch("miloco_cli.client.api_patch", return_value=_OK) as patch_call,
    ):
        result = runner.invoke(
            cli,
            [
                "rule", "update", "r-1",
                "--source", "cam-001",
                "--iot-did", "d1",
                "--iot-iid", "5.1",
                "--iot-op", "eq",
                "--iot-value", "1",
            ],
        )

    assert "--source" in result.output and "不能一起给" in result.output
    assert not patch_call.called


def test_partial_iot_args_is_an_error(runner):
    """半套参数建不出条件项。"""
    result, _post = _create(runner, "--iot-did", "d1", "--iot-iid", "5.1")

    assert "四个都给" in result.output


def test_condition_alone_still_builds_an_omni_rule(runner):
    """收口不能顺手改掉 omni 的既有用法。"""
    result, post = _create(runner, "--condition", "有人在门口")

    assert result.exit_code == 0
    payload = _payload(post)
    assert payload["condition"]["query"] == "有人在门口"
    assert "condition_dnf" not in payload


# ── iot payload ───────────────────────────────────────────────────────


def test_iot_args_build_the_condition_dnf(runner):
    result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "5.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert result.exit_code == 0
    item = _payload(post)["condition_dnf"]["any_of"][0][0]
    assert item["source_type"] == "iot"
    assert item["spec"] == {"did": "d1", "iid": "5.1", "op": "eq", "value": 1}


def test_iot_args_on_update_replace_the_condition_dnf(runner):
    """改 iot 规则的条件走 update 侧的四件套 —— 它是 crud-ops 修改表给 iot 指的路，
    而同一张表对 omni 指的 ``--condition`` 在 iot 上会被服务端拒。"""
    with (
        patch("miloco_cli.client.api_get", return_value=_SPEC),
        patch("miloco_cli.client.api_patch", return_value=_OK) as patch_call,
    ):
        result = runner.invoke(
            cli,
            [
                "rule", "update", "r-1",
                "--iot-did", "d1",
                "--iot-iid", "3.1",
                "--iot-op", "gt",
                "--iot-value", "30.0",
            ],
        )

    assert result.exit_code == 0
    payload = patch_call.call_args[0][1]
    item = payload["condition_dnf"]["any_of"][0][0]
    assert item["spec"] == {"did": "d1", "iid": "3.1", "op": "gt", "value": 30.0}
    # 占位 condition 只在 create 侧发: PATCH 带上它会撞服务端「非 omni 不能改
    # condition.query」那道闸。
    assert "condition" not in payload


def test_iot_rule_sends_a_placeholder_condition(runner):
    """iot rule 的 condition 是占位：设备列表留空、query 由服务端渲染。"""
    _result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "5.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert _payload(post)["condition"] == {"perceive_device_ids": [], "query": ""}


# ── --iot-value 按 format 解析 ────────────────────────────────────────


def test_value_is_parsed_as_int_for_an_integer_property(runner):
    """断类型不断字面值：解析没做的话这里是字符串 "1"，服务端拿它去比 uint8 会判
    类型不兼容，而那是运行期的事、离这里很远。"""
    _result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "5.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    value = _payload(post)["condition_dnf"]["any_of"][0][0]["spec"]["value"]
    assert value == 1 and isinstance(value, int) and not isinstance(value, bool)


def test_value_is_parsed_as_float(runner):
    _result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "3.1",
        "--iot-op",
        "gt",
        "--iot-value",
        "25.5",
    )

    assert _payload(post)["condition_dnf"]["any_of"][0][0]["spec"]["value"] == 25.5


def test_bool_property_only_accepts_true_or_false(runner):
    """不接受 1 / 0 —— 那会让「开关配了数值」这类错误在 CLI 层就溜过去。"""
    result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "2.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert "true / false" in result.output
    assert not post.called


def test_bool_property_accepts_true(runner):
    _result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "2.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "true",
    )

    assert _payload(post)["condition_dnf"]["any_of"][0][0]["spec"]["value"] is True


def test_non_scalar_format_is_rejected_in_the_cli(runner):
    result, post = _create(
        runner,
        "--iot-did",
        "d1",
        "--iot-iid",
        "7.1",
        "--iot-op",
        "eq",
        "--iot-value",
        "1",
    )

    assert "不是标量" in result.output
    assert not post.called


def test_missing_spec_errors_instead_of_guessing_the_type(runner):
    """猜错类型的后果是规则建得成功、运行期恒判不兼容、条件恒未就绪。"""
    with (
        patch("miloco_cli.client.api_get", return_value={"code": 0, "data": {}}),
        patch("miloco_cli.client.api_post") as post,
    ):
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--name",
                "x",
                "--task-id",
                "t",
                "--action-desc",
                "播报",
                "--iot-did",
                "d1",
                "--iot-iid",
                "5.1",
                "--iot-op",
                "eq",
                "--iot-value",
                "1",
            ],
        )

    assert "拿不到设备" in result.output
    assert not post.called


# ── 事件参数：另一张表、另一条通道 ────────────────────────────────────

_EVENT_IID = "event.2.1020.unlock-operation-position"

# 事件请求的响应比属性那次多一张 event_spec；属性请求里没有它。
_EVENT_SPEC = {
    "code": 0,
    "data": {
        "spec": _SPEC["data"]["spec"],
        "event_spec": {
            _EVENT_IID: {"format": "uint8", "description": "开锁操作"},
            "event.2.1020.other": {"format": "iids", "description": "预设"},
        },
    },
}


def _create_with_spec(runner, response, *args):
    """返回 (result, post, get)。get 要看一眼请求的是哪条 URL。"""
    with (
        patch("miloco_cli.client.api_get", return_value=response) as get,
        patch("miloco_cli.client.api_post", return_value=_OK) as post,
    ):
        result = runner.invoke(
            cli,
            [
                "rule",
                "create",
                "--name",
                "门开了",
                "--task-id",
                "door_alert",
                "--action-desc",
                "播报",
                *args,
            ],
        )
    return result, post, get


def _event_args(*extra):
    return ("--iot-did", "d1", "--iot-iid", _EVENT_IID, "--iot-op", "eq", *extra)


def test_an_event_arg_is_fetched_from_the_event_table(runner):
    """问服务端要 `include_events=true` 才会带上事件表。

    不带这个参数的话拿到的是空表，于是每一种事件 iid 都会被报成「事件 spec 里没有
    这一项」—— 一个取数缺陷伪装成用户写错了 iid。
    """
    _result, post, get = _create_with_spec(
        runner, _EVENT_SPEC, *_event_args("--iot-value", "2")
    )

    assert get.call_args[0][0] == "/api/miot/devices/d1/spec"
    assert get.call_args[1]["params"] == {"include_events": "true"}
    item = _payload(post)["condition_dnf"]["any_of"][0][0]
    assert item["spec"] == {"did": "d1", "iid": _EVENT_IID, "op": "eq", "value": 2}


def test_a_property_does_not_ask_for_the_event_table(runner):
    """事件表要走另一个解析器，默认不取是路由那边定死的。属性也带上这个参数，
    每次建规则都白解析一份事件表。
    """
    _result, _post, get = _create_with_spec(
        runner, _SPEC, "--iot-did", "d1", "--iot-iid", "5.1", "--iot-op", "eq",
        "--iot-value", "1",
    )

    assert get.call_args[0][0] == "/api/miot/devices/d1/spec"
    assert get.call_args[1]["params"] is None


def test_an_event_value_is_parsed_by_the_arg_format(runner):
    """事件参数与属性共用一套解析 —— 但 format 得来自事件表那一份。"""
    _result, post, _get = _create_with_spec(
        runner, _EVENT_SPEC, *_event_args("--iot-value", "2")
    )

    value = _payload(post)["condition_dnf"]["any_of"][0][0]["spec"]["value"]
    assert isinstance(value, int)


def test_an_unknown_event_arg_errors_with_the_event_wording(runner):
    """**断文案里的命名空间。** 说成「spec 里没有属性 prop.event.…」会把人指到
    一个根本不存在的键上 —— 属性表里既没有 `event.…` 也没有 `prop.event.…`。
    """
    result, post, _get = _create_with_spec(
        runner,
        _EVENT_SPEC,
        "--iot-did", "d1", "--iot-iid", "event.2.1020.nope", "--iot-op", "eq",
        "--iot-value", "1",
    )

    assert "事件 spec 里没有" in result.output
    assert "prop.event." not in result.output
    assert not post.called


def test_a_missing_event_table_errors_with_the_event_wording(runner):
    """两张表都不在时报错要各说各的，否则「服务端没给事件表」会被读成
    「这台设备没有事件」。
    """
    result, post, _get = _create_with_spec(
        runner,
        {"code": 0, "data": {}},
        *_event_args("--iot-value", "1"),
    )

    assert "事件 spec" in result.output
    assert not post.called


def test_the_value_error_names_the_event_namespace(runner):
    """非标量那条出口也要带上命名空间 —— 报「属性 prop.event.…」同样指错地方。"""
    result, post, _get = _create_with_spec(
        runner,
        _EVENT_SPEC,
        "--iot-did", "d1", "--iot-iid", "event.2.1020.other", "--iot-op", "eq",
        "--iot-value", "1",
    )

    assert "不是标量" in result.output
    assert "事件参数" in result.output
    assert "属性 prop." not in result.output
    assert not post.called


# ── 诊断出口 ──────────────────────────────────────────────────────────


def test_iot_diagnostics_prints_the_report(runner):
    """只写了 diagnostics() 而没接命令行时这条会红，而「diagnostics() 返回值对不对」
    那些用例照样绿。"""
    report = {"code": 0, "data": {"consumer_alive": True, "rules": {"r1": {}}}}
    with patch("miloco_cli.client.api_get", return_value=report):
        result = runner.invoke(cli, ["rule", "iot-diagnostics"])

    assert result.exit_code == 0
    assert "consumer_alive" in result.output


def test_state_stats_prints_the_counters(runner):
    report = {"code": 0, "data": {"push": {"prop_written": 9}, "store": {}}}
    with patch("miloco_cli.client.api_get", return_value=report):
        result = runner.invoke(cli, ["state", "stats"])

    assert result.exit_code == 0
    assert "prop_written" in result.output


def test_state_dump_passes_the_pattern_through(runner):
    with patch(
        "miloco_cli.client.api_get", return_value={"code": 0, "data": {"lines": []}}
    ) as get:
        result = runner.invoke(
            cli, ["state", "dump", "--pattern", "iot/device/*/prop/*", "--limit", "10"]
        )

    assert result.exit_code == 0
    assert get.call_args[1]["params"] == {
        "pattern": "iot/device/*/prop/*",
        "limit": 10,
    }
