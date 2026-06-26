"""测试公共夹具：把 hyphen 目录 miloco-plugin/ 与 adapter/ 作为包加载进 sys.modules。

miloco-plugin 目录名含连字符，不是合法 Python 包名，Hermes 走路径加载无碍，
但 pytest 直接 import 不行——这里用 importlib 以唯一别名装载，让相对导入
(``from .catalog import ...``) 能解析。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HERMES_DIR = TESTS_DIR.parent  # plugins/hermes/

_ADAPTER_DIR = HERMES_DIR / "adapter"
_PLUGIN_DIR = HERMES_DIR / "miloco-plugin"


def _load_pkg(alias: str, pkg_dir: Path) -> None:
    if alias in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        alias,
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    assert spec and spec.loader, f"无法加载 {pkg_dir}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)


def _load_single(alias: str, file: Path) -> None:
    """加载无相对导入的独立模块（如 session_map.py）。"""
    if alias in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(alias, file)
    assert spec and spec.loader, f"无法加载 {file}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)


# 适配进程：作为包 adapter_pkg 装载（server/hermes_client 间有相对导入）
_load_pkg("adapter_pkg", _ADAPTER_DIR)

# 插件：作为包 miloco_plugin_pkg 装载（context_injection/tools_* 间有相对导入）
_load_pkg("miloco_plugin_pkg", _PLUGIN_DIR)
