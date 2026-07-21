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
