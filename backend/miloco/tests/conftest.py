# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco 测试的全局隔离层：让用例**不依赖开发者本机的配置**。

为什么必须有它：settings 是进程级单例，而默认 ``$MILOCO_HOME`` 指向本机真实数据目录
（``~/.openclaw/miloco``）。于是同一个用例在 CI（干净机器）全绿、在开发机全红：

- 本机 ``config.json`` 里配了 ``server.token`` → 所有"直接 include 真实 router、请求不带
  ``Authorization`` 头"的用例统统 401（``verify_token`` 只在 token 为空时才跳过鉴权）；
- 本机 config.json 里的 model key / 摄像头 / timezone / features 也会悄悄参与断言；
- 更糟的是反向污染：不开 ``MILOCO_DATABASE__PATH`` 的用例会把表建到开发者**真实**的
  ``miloco.db`` 里。

所以这里对**每个用例**做三件事：

1. 清掉所有 ``MILOCO_*`` 环境变量（开发者 shell 里 export 的同样会漏进来）；
2. 把 ``MILOCO_HOME`` 指到一个空的临时目录 —— 没有 config.json，全部回落到包内
   ``settings.yaml`` 的默认值（无 token、无模型 Key、无摄像头）；
3. ``reset_settings()`` 前后各一次 —— 单例缓存不跨用例，也不会把上一个用例改过的
   ``engine`` / ``features`` dict 带到下一个用例（历史上正是它让 engine 目录单独跑全绿、
   全量跑却红的用例变红）。

要自己那套配置的用例照旧用 ``monkeypatch.setenv`` / ``tmp_path`` 覆盖即可（monkeypatch 的
还原在末态，不会和这里打架）。要**显式验证鉴权**的用例请自己配 token，例如
``monkeypatch.setenv("MILOCO_SERVER__TOKEN", "test-token")``，别再依赖本机是否配过 token。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_miloco_home(tmp_path, monkeypatch):
    """每个用例默认跑在「干净、无 token、无本地配置」的 settings 上。"""
    from miloco.config.settings import reset_settings

    for key in [k for k in os.environ if k.startswith("MILOCO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    yield tmp_path
    reset_settings()
