# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""跨包一致性守卫:CLI 的 _SCHEMA_PATHS 默认值必须与随包 settings.yaml 一致。

CLI 不能 import backend,所以 ``cli/src/miloco_cli/config.py`` 手抄了一份配置默认值,喂给
``config show`` / ``config get`` 做 config.json 缺字段时的兜底。它的 docstring 声称默认值
对齐后端实际生效值——本测试用 ast 解析 CLI 源码(不 import,免执行副作用),把「手动同步」变成
会失败的守卫,一处漏改立即红。backend-only 检出(无 cli/)时优雅跳过,同 test_result_codes.py。

漏改的后果是**读到相反的值**:比如哪天随包把 Smart Crop 的发版级开关改回 false 却忘了同步这张表,
运维在没写这段的 config.json 上 `config get ...crop_enhance.enabled` 会拿到 true,而后端继承
的是 yaml 的 false,排查「为什么没裁」时线索是反的,且这类漂移本身不会让任何测试变红。
"""

import ast
from pathlib import Path

import pytest
import yaml

_SETTINGS_YAML = (
    Path(__file__).resolve().parents[1] / "src" / "miloco" / "config" / "settings.yaml"
)

_ABSENT = object()


def _find_cli_config_py() -> Path | None:
    """从本测试文件向上走到仓库根,定位 CLI 的 config.py;找不到返回 None。"""
    here = Path(__file__).resolve()
    for root in here.parents:
        candidate = root / "cli" / "src" / "miloco_cli" / "config.py"
        if candidate.exists():
            return candidate
    return None


def _cli_defaults(source: str) -> dict[str, object]:
    """解析 ``_SCHEMA_PATHS`` 字面量,返回 {点号路径: 默认值}。

    元组第 0 项是 ``bool``/``int`` 等类型对象、不是字面量,所以只对第 1 项(默认值)求值,
    整体 literal_eval 会失败。取不到该赋值时抛 AssertionError(源码结构变了,守卫也该红)。
    """
    tree = ast.parse(source)
    for node in tree.body:
        # `_SCHEMA_PATHS: dict[...] = {...}` 带类型标注,是 AnnAssign 而非 Assign
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == "_SCHEMA_PATHS"):
            continue
        assert isinstance(node.value, ast.Dict), "_SCHEMA_PATHS 不再是 dict 字面量"
        out: dict[str, object] = {}
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            path = ast.literal_eval(key)
            assert isinstance(value, ast.Tuple), f"{path} 的值不是 tuple 字面量"
            out[path] = ast.literal_eval(value.elts[1])
        return out
    raise AssertionError("CLI config.py 里找不到 _SCHEMA_PATHS 赋值")


def _yaml_value(data: dict, path: str) -> object:
    cur: object = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _ABSENT
        cur = cur[part]
    return cur


def test_cli_schema_defaults_match_settings_yaml() -> None:
    cli_config = _find_cli_config_py()
    if cli_config is None:
        pytest.skip("backend-only 检出,无 cli/ 源码")

    defaults = _cli_defaults(cli_config.read_text(encoding="utf-8"))
    data = yaml.safe_load(_SETTINGS_YAML.read_text(encoding="utf-8"))

    # 只比 yaml 里存在的 key。yaml 没有的(timezone / server.* / agent.* / scheduler.* /
    # media_resolution)默认值来自 settings.py 的 pydantic 字段,不在本守卫范围。
    mismatched = {
        path: {"cli": default, "settings.yaml": _yaml_value(data, path)}
        for path, default in defaults.items()
        if _yaml_value(data, path) is not _ABSENT and _yaml_value(data, path) != default
    }
    assert not mismatched, f"CLI _SCHEMA_PATHS 默认值与 settings.yaml 漂移: {mismatched}"

    # 守卫本身别悄悄退化成空断言:yaml 里至少要对上几条,否则说明路径匹配逻辑坏了。
    covered = [p for p in defaults if _yaml_value(data, p) is not _ABSENT]
    assert len(covered) >= 5, f"只对上 {len(covered)} 条路径,疑似匹配逻辑失效: {covered}"
