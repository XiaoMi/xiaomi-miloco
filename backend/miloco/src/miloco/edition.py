# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""发行版本（edition）判定：``full`` / ``slim``。

``full``（默认）
    完整后端：全量感知（身份 / 音频 / 宠物）、agent 联动、定时任务、可观测性。

``slim``
    面向"独立 macOS App"的精简发行版：只保留 rule_only 场景触发（摄像头画面 +
    云端大模型）与 web 管理页，不注册身份 / 宠物 / 家庭档案路由，不启动 agent
    dispatcher、定时任务与 ReID 补齐，从而可以完全不依赖 onnxruntime / scipy /
    tokenizers 与任何 ONNX 模型运行。

判定优先级：``MILOCO_EDITION`` 环境变量 > ``settings.app.edition`` > ``full``。
独立 App 的 launcher 通过 ``MILOCO_EDITION=slim`` 注入，**不写磁盘配置**——
同一份 wheel 既能跑完整版也能跑精简版，full 行为零变化。
"""

from __future__ import annotations

import os

FULL_EDITION = "full"
SLIM_EDITION = "slim"

#: 环境变量名；launcher / 构建脚本都引用这个常量，避免各处硬编码字符串。
ENV_EDITION = "MILOCO_EDITION"

_VALID = (FULL_EDITION, SLIM_EDITION)


def edition_from_env() -> str | None:
    """只读 ``MILOCO_EDITION`` 环境变量，不触碰 settings。

    供 ``miloco.utils.paths.miloco_home()`` 这类早期启动代码使用：那里若反过来
    调 ``get_settings()``，会经由 ``$MILOCO_HOME/config.json`` 再回到
    ``miloco_home()``，形成递归。非法值一律当未设置。
    """
    raw = (os.environ.get(ENV_EDITION) or "").strip().lower()
    return raw if raw in _VALID else None


def get_edition() -> str:
    """返回当前生效的 edition（``full`` / ``slim``）。

    每次调用现读：测试用 ``monkeypatch.setenv`` 改环境变量后立刻生效（与
    ``miloco_home()`` 同一约定）。settings 兜底路径包在 try/except 里——导入期
    或配置损坏时宁可判定为 ``full``（保守：完整版不会因缺依赖而少注册路由）。
    """
    if env := edition_from_env():
        return env
    try:
        from miloco.config import get_settings

        raw = (getattr(get_settings().app, "edition", "") or "").strip().lower()
        if raw in _VALID:
            return raw
    except Exception:  # noqa: BLE001
        pass
    return FULL_EDITION


def is_slim_edition() -> bool:
    """是否为精简 App 发行版。"""
    return get_edition() == SLIM_EDITION
