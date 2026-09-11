# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""仓库体检：install-hermes.sh 的必需模型清单必须与 resource_validator.MODELS 对齐。

install-hermes.sh 的 step 4.7 装完模型后要判一次"引擎必需的模型齐没齐"，判据的出处是
``resource_validator.MODELS`` 的非 optional 项。shell 侧不能依赖读到这个 Python 常量：
跑脚本的解释器不保证是装了 miloco 的那个（``$PYTHON`` 取的是系统 python3，脚本里
``import miloco`` 的地方全带 ``|| true``），而 release 通道走 ``--post-install`` 时
tarball 里也没有源码树。只能复制一份文件名，由本测试负责让两份不各自漂走。
"""

from __future__ import annotations

import re
from pathlib import Path

from miloco.perception.engine.resource_validator import MODELS

_SCRIPT = Path(__file__).resolve().parents[3] / "plugins" / "hermes" / "install-hermes.sh"


def _required_models_in_script() -> list[str]:
    m = re.search(
        r'^REQUIRED_MODELS="([^"]*)"$', _SCRIPT.read_text(encoding="utf-8"), re.M
    )
    assert m, f"{_SCRIPT} 里找不到 REQUIRED_MODELS= 赋值"
    return m.group(1).split()


def test_required_models_match_resource_validator() -> None:
    assert sorted(_required_models_in_script()) == sorted(
        m.name for m in MODELS if not m.optional
    )
