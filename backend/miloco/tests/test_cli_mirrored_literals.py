# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""跨包一致性守卫:CLI 复刻的那几个字面量必须与后端定义处逐字相同。

CLI 是独立包、不依赖 miloco,所以后端用来当**协议约定**的字面量在 CLI 里手抄了一份 ——
``rule/schema.py`` 的 ``SCENE_IID`` 与 ``miot/iid.py`` 的 ``EVENT_IID_PREFIX``。两处
各自写着「改动时两处同步」,但「手动同步」本身就是会漏的。

漏改的后果不是报错,是**判据反向**:``--iot-iid event.2.1012.click`` 在后端按事件命名
空间查 spec、在 CLI 侧按属性查,两边都拿不到对方那份表,回显变成「这条 iid 不存在」——
排查时会去查设备 spec,而真相是两边的前缀不是同一个串。本测试用 ast 解析 CLI 源码
(不 import,免执行副作用),把它变成会失败的守卫;backend-only 检出(无 cli/)时优雅跳过,
同 test_result_codes.py / test_cli_schema_defaults.py。
"""

import ast
from pathlib import Path

import pytest
from miloco.miot.iid import EVENT_IID_PREFIX
from miloco.rule.schema import SCENE_IID

# (CLI 里的常量名, 后端真值) —— 加一个复刻字面量就在这张表里加一行,别再各写一个测试。
_MIRRORED = {
    "SCENE_IID": SCENE_IID,
    "EVENT_IID_PREFIX": EVENT_IID_PREFIX,
}


def _find_cli_rule_py() -> Path | None:
    """从本测试文件向上走到仓库根,定位 CLI 的 rule.py;找不到返回 None。"""
    here = Path(__file__).resolve()
    for root in here.parents:
        candidate = root / "cli" / "src" / "miloco_cli" / "commands" / "rule.py"
        if candidate.exists():
            return candidate
    return None


def _cli_literals(source: str) -> dict[str, object]:
    """解析顶层那些 ``<名字> = <字面量>`` 赋值(带不带类型标注都认),返回 {名字: 值}。

    名字不在 ``_MIRRORED`` 里的忽略。取不到某个常量时**不抛异常**,交给调用方按「少了」报
    —— 常量改名或改成非字面量时,要能看出是这种坏法,而不是让守卫静默变空。
    """
    tree = ast.parse(source)
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id in _MIRRORED):
            continue
        out[target.id] = ast.literal_eval(value)
    return out


def test_cli_mirrored_literals_match_the_backend() -> None:
    cli_rule = _find_cli_rule_py()
    if cli_rule is None:
        pytest.skip("backend-only 检出,无 cli/ 源码")

    mirrored = _cli_literals(cli_rule.read_text(encoding="utf-8"))

    # 守卫别悄悄退化成空断言:一个都没解析到时,下面的对比是空集,永远绿。
    assert set(mirrored) == set(_MIRRORED), (
        f"CLI rule.py 里没解析到 {sorted(set(_MIRRORED) - set(mirrored))},"
        "常量改名或不再是字面量赋值时本守卫会失效"
    )
    assert mirrored == _MIRRORED, (
        "CLI 复刻的字面量与后端漂移: "
        f"{ {k: (v, _MIRRORED[k]) for k, v in mirrored.items() if v != _MIRRORED[k]} }"
    )
