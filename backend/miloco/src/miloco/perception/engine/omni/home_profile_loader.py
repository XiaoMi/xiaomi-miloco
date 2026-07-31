"""home_profile_loader.py — 进程内直读 canonical profile.md 注入 omni system prompt。

canonical 路径 ``$MILOCO_HOME/home-profile/profile.md`` 由 backend commit 时重写；
此处只读不渲染，缺失即注入空内容（不报错、不触发 render）。
"""

from __future__ import annotations

import logging

from miloco.home_profile.store import profile_md_path

logger = logging.getLogger(__name__)


_PET_SECTION_HEADING = "## 宠物"


def get_home_profile_prefix() -> str:
    """返回家庭背景信息（Home Profile）字符串，注入到 system prompt L1 层。

    ``pet_recognition`` 关闭时**在读侧剥掉「## 宠物」段**：commit 时的软关闭只在"下一次
    commit"生效，而开关可经 web / CLI / ``MILOCO_FEATURES__*`` 环境变量三条路径改动——
    env 那条根本没有"写入时机"可挂重渲。读侧过滤是唯一能覆盖全部入口的位置，杜绝
    "宠物名还在 prompt 里、称呼护栏已撤"的危险态（关闭即回到无此功能时的样子）。
    """
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
    return body if _pet_recognition_on() else _strip_pet_section(body)


def _pet_recognition_on() -> bool:
    try:
        from miloco.config import get_settings

        return bool(get_settings().features.pet_recognition)
    except Exception:  # noqa: BLE001 — 读不到开关按关处理（宁可不注入，也别有名字没护栏）
        logger.warning("event=pet_flag_probe_fail", exc_info=True)
        return False


def _strip_pet_section(body: str) -> str:
    """剥掉「## 宠物」整段（到下一个 ``## `` 二级标题为止；``### <宠物名>`` 天然含在段内）。"""
    out: list[str] = []
    skipping = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == _PET_SECTION_HEADING:
            skipping = True
            continue
        if skipping and stripped.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


def home_profile_has_pets() -> bool:
    """是否有已登记宠物（**花名册非空**）。

    **不**用 profile.md 的「## 宠物」标题作判据：那段 md 只在存在未归档宠物档案条目时才渲染，
    会被 token 预算归档 / 住户删条目 / ``pet add`` 直接建档 / skill 允许空外观 四条路径抹掉，
    导致花名册与参考图都在、识别整条静默停摆且无日志。花名册是 ``pet_refs`` 注入参考图的**同一
    数据源**，两处同源才不会出现「名单里没有、参考图里却有」的自相矛盾 prompt。
    读盘失败 → fail-closed（宁可不注入，也不要有名字没纪律）。
    """
    try:
        from miloco.perception.engine.identity.pet_library import get_pet_library

        return bool(get_pet_library().list())
    except Exception:  # noqa: BLE001 — 每感知窗口一次，任何异常都退回不注入
        logger.warning("event=pet_roster_probe_fail", exc_info=True)
        return False
