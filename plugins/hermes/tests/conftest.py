"""测试公共夹具：把 hyphen 目录 miloco-plugin/ 与 adapter/ 作为包加载进 sys.modules。

miloco-plugin 目录名含连字符，不是合法 Python 包名，Hermes 走路径加载无碍，
但 pytest 直接 import 不行——这里用 importlib 以唯一别名装载，让相对导入
(``from .catalog import ...``) 能解析。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
HERMES_DIR = TESTS_DIR.parent  # plugins/hermes/

_PLUGIN_DIR = HERMES_DIR / "miloco-plugin"


@pytest.fixture
def anyio_backend():
    """固定 anyio 后端，避免 CI 隐式依赖。"""
    return "asyncio"


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


# 插件：作为包 miloco_plugin_pkg 装载（context_injection/tools_* 间有相对导入）
_load_pkg("miloco_plugin_pkg", _PLUGIN_DIR)


def _alias_flat_adapter_deps() -> None:
    """复刻 install-hermes.sh 的摊平部署布局，让 adapter 的相对导入能解析。

    生产里 install-hermes.sh（见 :615-680）把 ``hermes_adapter/{__init__,adapter}.py``
    和 ``{context_injection,catalog,paths,tools_habit}.py`` 一起拷进
    ``$MILOCO_HOME/agent_platform/hermes/``——adapter.py 与这几个模块同级，所以它写的是
    ``from .context_injection import ...``。仓库布局里 hermes_adapter/ 只有 adapter.py，
    相对导入解析不到，``build_system`` 这条**生效路径**就没法在单测里跑。

    这里按生产布局补上 sys.modules 别名（同一个模块对象，monkeypatch 两边同时生效）。
    """
    import importlib

    parent = "miloco_plugin_pkg.hermes_adapter"
    importlib.import_module(parent)
    for name in ("context_injection", "catalog", "paths", "tools_habit"):
        alias = f"{parent}.{name}"
        if alias not in sys.modules:
            sys.modules[alias] = importlib.import_module(f"miloco_plugin_pkg.{name}")


_alias_flat_adapter_deps()
