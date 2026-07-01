"""home_profile_loader.py — 进程内直读 canonical profile.md 注入 omni system prompt。

canonical 路径 ``$MILOCO_HOME/home-profile/profile.md`` 由 backend commit 时重写；
此处只读不渲染，缺失即注入空内容（不报错、不触发 render）。
"""

from __future__ import annotations

import logging

from miloco.home_profile.store import profile_md_path

logger = logging.getLogger(__name__)


def get_home_profile_prefix() -> str:
    """返回家庭背景信息（Home Profile）字符串，注入到 system prompt L1 层。"""
    profile_file = profile_md_path()
    if not profile_file.exists():
        return ""

    try:
        content = profile_file.read_text("utf-8")
    except Exception:
        logger.warning("读取家庭档案失败: %s", profile_file, exc_info=True)
        return ""

    body = content.strip()
    if not body:
        return ""
    return body


_PET_SECTION_HEADING = "## 宠物"


def home_profile_has_pets() -> bool:
    """档案是否含「## 宠物」段（已登记宠物、且未被软关闭隐藏）。

    按行精确匹配 ``## 宠物`` 标题——避免被人类成员名渲染出的 ``### 宠物`` 之类误判
    （子串匹配会因 ``###`` 含 ``##`` 而误中）。开关关闭时 commit 不渲染该段，故自然为 False。
    """
    text = get_home_profile_prefix()
    return any(line.strip() == _PET_SECTION_HEADING for line in text.splitlines())
