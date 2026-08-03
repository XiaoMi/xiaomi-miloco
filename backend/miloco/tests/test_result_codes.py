"""miot.result_codes.summarize_results:负码即失败(镜像 PR #394)。

外加一条跨包一致性守卫:CLI 侧 device.py 手抄了一份 _MIOT_SPEC_CODES / _MIOT_OK_CODES
(CLI 不能 import backend)。docstring 声称两份是同步镜像——本测试用 ast 解析 CLI 源码,
把「手动同步」变成会失败的守卫,一处漏改立即红。backend-only 检出(无 cli/)时优雅跳过。
"""

import ast
from pathlib import Path

import pytest
from miloco.miot.result_codes import (
    _MIOT_OK_CODES,
    _MIOT_SPEC_CODES,
    _UNKNOWN_FAIL_MSG,
    _is_failure,
    code_message,
    is_result_unknown,
    summarize_results,
)


def _find_cli_device_py() -> Path | None:
    """从本测试文件向上走到仓库根,定位 CLI 的 device.py;找不到返回 None。"""
    here = Path(__file__).resolve()
    for root in here.parents:
        candidate = root / "cli" / "src" / "miloco_cli" / "commands" / "device.py"
        if candidate.exists():
            return candidate
    return None


def _literal_of_assignment(tree: ast.Module, name: str) -> object:
    """从模块 AST 里取顶层 ``name = <literal>`` 的求值结果。

    支持裸字面量(dict/set)与 ``frozenset({...})`` 包裹——后者取其单一集合字面量实参。
    找不到该赋值时抛 AssertionError(源码结构变了,守卫也该红)。
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets:
            continue
        value = node.value
        # frozenset({...}) → 解包成里面的 set 字面量再求值
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            value = value.args[0]
        return ast.literal_eval(value)
    raise AssertionError(f"CLI device.py 中未找到顶层赋值 {name}")


def test_cli_result_codes_mirror_backend():
    """CLI 的 _MIOT_SPEC_CODES / _MIOT_OK_CODES 必须逐条等于 backend 的镜像副本。"""
    cli_path = _find_cli_device_py()
    if cli_path is None:
        pytest.skip("CLI device.py 不在本检出中(backend-only),跳过跨包一致性守卫")

    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    cli_spec = _literal_of_assignment(tree, "_MIOT_SPEC_CODES")
    cli_ok = _literal_of_assignment(tree, "_MIOT_OK_CODES")

    assert cli_spec == _MIOT_SPEC_CODES, "CLI 与 backend 的 _MIOT_SPEC_CODES 已漂移"
    assert set(cli_ok) == set(_MIOT_OK_CODES), (
        "CLI 与 backend 的 _MIOT_OK_CODES 已漂移"
    )


def test_all_zero_codes_is_success():
    assert summarize_results([{"code": 0}, {"code": 0}]) == (True, None, None)


def test_single_result_dict_success():
    assert summarize_results({"code": 0}) == (True, None, None)


def test_negative_code_is_failure_decoded():
    ok, code, msg = summarize_results([{"code": -704042011}])
    assert ok is False
    assert code == -704042011
    assert msg == "设备离线"


def test_positive_code_not_failure():
    # 正码不判失败(只有负码算失败)
    assert summarize_results([{"code": 12345}]) == (True, None, None)


def test_miot_ok_negative_codes_not_failure():
    # -702000000 / -702010000 在 OK 集里,不判失败
    assert summarize_results([{"code": -702000000}]) == (True, None, None)


def test_missing_code_is_success():
    assert summarize_results([{"siid": 2, "piid": 1}]) == (True, None, None)


def test_first_failure_wins():
    ok, code, msg = summarize_results(
        [{"code": 0}, {"code": -704030023}, {"code": -704042011}]
    )
    assert ok is False
    assert code == -704030023  # 第一个失败项
    assert msg == "属性不可写"


def test_unknown_negative_code_gets_generic_msg():
    ok, code, msg = summarize_results([{"code": -799999999}])
    assert ok is False
    assert code == -799999999
    assert "未知错误码" in msg


def test_none_input_is_success():
    assert summarize_results(None) == (True, None, None)


def test_sdk_internal_codes_registered():
    """本地中枢引入的 SDK 内部码必须有专属文案。

    -10006 是"本地网关超时、指令可能已执行"——SDK 正因如此才不做云端重发。
    若落到 _UNKNOWN_FAIL_MSG("设备侧执行失败"),agent 会照台账告诉用户"关灯
    失败了",而用户看着已经关掉的灯;文案还会指引去查一张根本不含该码的表。
    """
    msg = code_message(-10006)
    assert msg != _UNKNOWN_FAIL_MSG
    assert "可能已执行" in msg
    assert code_message(-10004) != _UNKNOWN_FAIL_MSG
    # 仍应判为失败(负码),只是文案不同——语义是"结果未知",不能当成功。
    assert _is_failure(-10006) is True
    assert _is_failure(-10004) is True


def test_result_unknown_codes_track_sdk_enum():
    """守住"引用枚举而非抄裸数字":集合必须与 SDK 枚举一致。

    这三个值的真源在 miot/error.py。若这里退回硬编码字面量、而 SDK 改了枚举值，
    is_result_unknown 会静默失配 —— 规则层不再落冷却，SDK 层避开的双发就被重新
    引入，且没有任何报错。
    """
    from miloco.miot.result_codes import _RESULT_UNKNOWN_CODES
    from miot.error import MIoTErrorCode

    assert _RESULT_UNKNOWN_CODES == {
        MIoTErrorCode.CODE_TIMEOUT.value,
        MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value,
        MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value,
    }
    # 这三个码在码表里都得有"可能已执行"的文案(否则 agent 会当失败去重试)
    for code in _RESULT_UNKNOWN_CODES:
        assert is_result_unknown(code) is True
        assert "可能已执行" in code_message(code)
    # 反例:普通设备失败码不是"结果未知"
    assert is_result_unknown(-704030023) is False
    assert is_result_unknown(None) is False  # 空返回/不可判定
    assert is_result_unknown(0) is False
