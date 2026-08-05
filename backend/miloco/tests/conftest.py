# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""测试全局隔离：任何测试都不许读到开发机上的真实部署状态。

``get_settings()`` 的加载链是 环境变量 > ``$MILOCO_HOME/config.json`` >
settings.yaml > 默认值。开发机上通常存在真实的 ``~/.openclaw/miloco/config.json``
（含 ``server.token`` 等），不隔离时所有 TestClient 路由测试会因缺
Authorization 头而 401 假红（CI 干净环境反而全绿）。

在 conftest **import 期**（早于任何测试模块的收集/导入，防止模块级
``get_settings()`` 把真实配置烧进 lru_cache）：

1. 把 ``MILOCO_HOME`` 指到本次会话的临时目录（空 config）；
2. 清掉继承自 shell 的 ``MILOCO_*`` 环境变量（例如 ``MILOCO_SERVER__TOKEN``）；
3. ``reset_settings()`` 清缓存，保证后续解析用隔离后的环境。

单测仍可用 ``monkeypatch.setenv("MILOCO_HOME", ...)`` 或
``MilocoSettings(...)`` init 覆盖，各自作用域退出后回到这里的隔离基线。
"""

from __future__ import annotations

import os
import tempfile

_ISOLATED_HOME = tempfile.mkdtemp(prefix="miloco-test-home-")

for _key in [k for k in os.environ if k.startswith("MILOCO_")]:
    del os.environ[_key]
os.environ["MILOCO_HOME"] = _ISOLATED_HOME

from miloco.config import reset_settings  # noqa: E402

reset_settings()
