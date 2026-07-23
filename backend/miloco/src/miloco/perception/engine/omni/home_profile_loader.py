"""home_profile_loader.py — 进程内直读 canonical profile.md 注入 omni system prompt。

canonical 路径 ``$MILOCO_HOME/home-profile/profile.md`` 由 backend commit 时重写；
此处只读不渲染，缺失即注入空内容（不报错、不触发 render）。
"""

from __future__ import annotations

import logging

from miloco.home_profile.store import profile_md_path

from ._pet_force_fixture import pet_samples_on, synthetic_pet_section

logger = logging.getLogger(__name__)


def get_home_profile_prefix() -> str:
    """返回家庭背景信息（Home Profile）字符串，注入到 system prompt L1 层。

    【test/pet-prompt-force】``pet_samples_on()`` 时，把内嵌样本合成的「## 宠物」名单追加到末尾
    （即便无真实 profile.md 也注入），供 pet_identities 有名单可对——仅本测试分支，生产见 git 主线。
    """
    profile_file = profile_md_path()
    body = ""
    if profile_file.exists():
        try:
            body = profile_file.read_text("utf-8").strip()
        except Exception:
            logger.warning("读取家庭档案失败: %s", profile_file, exc_info=True)
            body = ""

    # pet_samples_on 且真实档案里**没有**「## 宠物」段时，才补合成名单
    # （注册了真实宠物 → 用真实名单、不叠加合成，与 <pets> 的「真实优先」一致）。
    if pet_samples_on() and not any(
        line.strip() == "## 宠物" for line in body.splitlines()
    ):
        section = synthetic_pet_section()
        if section:
            body = f"{body}\n\n{section}".strip() if body else section
    return body


_PET_SECTION_HEADING = "## 宠物"


def home_profile_has_pets() -> bool:
    """档案是否含「## 宠物」段（已登记宠物、且未被软关闭隐藏）。

    按行精确匹配 ``## 宠物`` 标题——避免被人类成员名渲染出的 ``### 宠物`` 之类误判
    （子串匹配会因 ``###`` 含 ``##`` 而误中）。开关关闭时 commit 不渲染该段，故自然为 False。
    """
    text = get_home_profile_prefix()
    return any(line.strip() == _PET_SECTION_HEADING for line in text.splitlines())
