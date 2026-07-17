# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Adapter 动态加载器。

Adapter 实现由 Plugin 侧提供,随插件打包,装到
``$MILOCO_HOME/agent_platform/<adapter_name>/``。Backend 启动时按
``settings.agent.platform`` 选择 Adapter,从对应子目录 ``importlib`` 加载 ``adapter`` 子模块,
期望它导出 :class:`miloco.agent_platform.base.AgentPlatformAdapter` 子类。

**为何用 importlib + 独立子目录**:
- 避免 plugin 与 backend wheel 的强耦合(backend 不 import plugin 任何符号)
- 各平台的依赖(Hermes 的 httpx、OpenClaw 的 SDK 等)由 plugin 各自负责
- 卸载/升级 plugin 时,删 ``agent_platform/<name>/`` 即可

**duck typing 校验**:
- Plugin 的 adapter.py **不强制** import 后端的 ABC(避免 plugin 依赖 backend wheel)
- Loader 用 ``hasattr`` 检查 ``name`` / ``send_turn`` / ``read_trace_meta``
  三个接口,缺一即失败
- ``aclose`` 在 base.py 有默认实现,plugin 可不实现(非必需)
- 这样 plugin 实现可零依赖 backend,但必须实现约定的方法集

**MILOCO_HOME 解析**:
- 默认 ``~/.openclaw/miloco``(对齐上游 settings.directories.miloco_home)
- 安装时 export / 写进 backend supervisor conf,所有进程同一份
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from miloco.agent_platform.base import AgentPlatformAdapter
from miloco.config import get_settings
from miloco.utils.paths import miloco_home

logger = logging.getLogger(__name__)


# duck-typed required 接口集:plugin adapter 必须暴露这些 attr/方法
_REQUIRED_ATTRS = ("name", "send_turn", "read_trace_meta")


def _resolve_adapter_dir(adapter_name: str) -> Path:
    """``$MILOCO_HOME/agent_platform/<adapter_name>/``"""
    return miloco_home() / "agent_platform" / adapter_name


def _find_adapter_class(module: Any, adapter_name: str) -> type:
    """在 module 里找一个类(优先名字 ``Adapter``,否则第一个匹配 duck-typed 的类)。
    duck-typed 校验:暴露 ``name`` + ``send_turn`` + ``read_trace_meta``。
    """
    # 优先 ``Adapter`` 通用名(避免 plugin 写无关名字)
    candidate = getattr(module, "Adapter", None)
    if candidate is not None and isinstance(candidate, type):
        if all(hasattr(candidate, attr) for attr in _REQUIRED_ATTRS):
            return candidate

    # 否则扫所有类,挑第一个 duck-typed 通过的
    found = None
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if not isinstance(attr, type):
            continue
        if attr.__module__ != module.__name__:
            continue  # 排除 imported 类
        if all(hasattr(attr, a) for a in _REQUIRED_ATTRS):
            found = attr
            break
    if found is None:
        raise ImportError(
            f"adapter '{adapter_name}' 模块里找不到符合 duck-typed 契约的 Adapter 类 "
            f"(需暴露 {list(_REQUIRED_ATTRS)})"
        )
    return found


def _load_adapter_class(adapter_name: str) -> type:
    """从 ``$MILOCO_HOME/agent_platform/<adapter_name>/`` 加载 ``adapter`` 模块,
    期望它导出 duck-typed ``Adapter`` 类。
    """
    adapter_dir = _resolve_adapter_dir(adapter_name)
    if not adapter_dir.is_dir():
        raise FileNotFoundError(
            f"adapter '{adapter_name}' 目录不存在:{adapter_dir}\n"
            f"请重跑 install-hermes.sh(会把 plugin 里的 adapter cp 到 MILOCO_HOME)"
        )

    adapter_py = adapter_dir / "adapter.py"
    if not adapter_py.is_file():
        raise FileNotFoundError(
            f"adapter '{adapter_name}' 缺少 adapter.py:{adapter_py}"
        )

    spec = importlib.util.spec_from_file_location(
        f"miloco_agent_platform_{adapter_name}",
        adapter_py,
        submodule_search_locations=[str(adapter_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 adapter '{adapter_name}':spec 为空")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImportError(f"adapter '{adapter_name}' 执行失败:{exc}") from exc

    return _find_adapter_class(module, adapter_name)


_cached_adapter: Optional[AgentPlatformAdapter] = None
# 注意：load_adapter() 初次加载后设为非 None；reset_cache() 清回 None。


def load_adapter(adapter_name: Optional[str] = None) -> AgentPlatformAdapter:
    """按 ``settings.agent.platform`` 加载 Adapter 单例。

    若 platform 未配置或加载失败，返回内置的 :class:`WebhookAdapter` 作为兜底。
    ``get_adapter()`` 永远返回实现了同一接口的非 None 对象。
    """
    global _cached_adapter
    if _cached_adapter is not None:
        return _cached_adapter

    settings = get_settings()
    name = adapter_name or getattr(settings.agent, "platform", None)
    if not name:
        logger.info(
            "agent.platform 未配置,使用内置 WebhookAdapter 兜底"
        )
        from miloco.agent_platform.base import WebhookAdapter
        _cached_adapter = WebhookAdapter()
        return _cached_adapter

    try:
        cls = _load_adapter_class(name)
        inst = cls()
        if not inst.name:
            inst.name = name
        _cached_adapter = inst
        logger.info(
            "agent adapter loaded: name=%s class=%s dir=%s",
            name, cls.__name__, _resolve_adapter_dir(name),
        )
        return _cached_adapter
    except Exception as exc:
        # 故意宽捕获：动态 importlib 加载第三方插件，任何异常都兜底到 WebhookAdapter
        logger.error(
            "agent adapter '%s' 加载失败:%s,使用内置 WebhookAdapter 兜底", name, exc,
        )
        from miloco.agent_platform.base import WebhookAdapter
        _cached_adapter = WebhookAdapter()
        return _cached_adapter


def get_adapter() -> AgentPlatformAdapter:
    """按 agent.platform 返回 adapter 单例（模块级缓存）。

    首次调用后缓存实例，实例内状态（如 Hermes adapter 的 _pending_texts）
    跨 send_turn / read_trace_meta 存活。运行时切换 agent.platform 需重启
    backend 或调 reset_cache() 才生效——install-hermes.sh Step 7 已做重启。
    """
    return load_adapter()


def reset_cache() -> None:
    """清缓存(测试用)。"""
    global _cached_adapter
    if _cached_adapter is not None:
        try:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_cached_adapter.aclose())
                else:
                    loop.run_until_complete(_cached_adapter.aclose())
            except RuntimeError:
                pass  # 没有可用事件循环（如已在解释器退出阶段），跳过 aclose
        except Exception:
            logger.debug("reset_cache: aclose 失败，忽略", exc_info=True)
    _cached_adapter = None