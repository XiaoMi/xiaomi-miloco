"""reconcile miloco 受管 cron job。

移植自 openclaw TypeScript 插件
``plugins/openclaw/src/home-profile/scheduler.ts``。

注册 4 个带 ``[miloco:home-profile]`` 标签的 cron job，并在每次启动时 reconcile
（增删改对齐）：

- miloco-perception-digest  ``*/15 * * * *``  skills=[miloco-perception-digest]
- miloco-home-patrol        ``*/30 * * * *``  skills=[miloco-home-patrol]
- miloco-home-dreaming      ``0 0 * * *``     skills=[miloco-home-observe, miloco-home-promote, miloco-home-prune]
- miloco-habit-suggest      ``0 10 * * *``    skills=[miloco-habit-suggest]

**与 openclaw 的差异**：Hermes ``cron.jobs.create_job`` 没有 ``description`` 字段，
故把 ``[miloco:home-profile]`` 标签塞进 ``name``（``f"{MANAGED_TAG} {task_name}"``），
reconcile 时按 ``name.startswith(MANAGED_TAG)`` 过滤受管 job。Hermes job 的
``prompt`` 对应 openclaw ``payload.message``；``skills=[...]`` 对应 openclaw
``payload.skills``（按顺序依次加载）。home-dreaming 的 prompt 显式要求按
Observe → Promote → Prune 顺序执行。

import 失败要 graceful：Hermes 不在运行环境时 ``cron.jobs`` 模块不可用，
``reconcile_cron_jobs`` 直接返回，不影响插件其余功能。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 从 state.json 读 deliver.target（install-hermes.sh 在安装时由
# detect_im_platforms.py 自动写入）。导出为模块级符号，便于测试 monkeypatch，
# 也便于 tools_notify 之外的 cron 路径复用同一份状态读取逻辑。
from .tools_notify import get_deliver_target

# 受管 job 标签：塞进 name 字段前缀，reconcile 据此识别。
MANAGED_TAG = "[miloco:home-profile]"


# 4 个受管 cron 任务定义。schedule 是 Hermes ``parse_schedule`` 接受的 cron 表达式。
_CRON_TASKS: List[Dict[str, Any]] = [
    {
        "name": "miloco-perception-digest",
        "schedule": "*/15 * * * *",
        "skills": ["miloco-perception-digest"],
        "prompt": "执行感知日志摘要。加载 miloco-perception-digest skill 进行处理。",
    },
    {
        "name": "miloco-home-patrol",
        "schedule": "*/30 * * * *",
        "skills": ["miloco-home-patrol"],
        "prompt": "执行家庭巡检。加载 miloco-home-patrol skill 进行巡检。",
    },
    {
        "name": "miloco-home-dreaming",
        "schedule": "0 0 * * *",
        "skills": ["miloco-home-observe", "miloco-home-promote", "miloco-home-prune"],
        "prompt": (
            "执行 home-dreaming 流程。依次完成以下步骤：\n"
            "1. **Observe** — 加载 miloco-home-observe skill，从感知/交互记忆中提取新知识写入候选区\n"
            "2. **Promote** — 加载 miloco-home-promote skill，将候选区中达到条件的知识提升到正式档案\n"
            "3. **Prune** — 加载 miloco-home-prune skill，统一主体命名、清理过期数据、提交持久化\n\n"
            "执行规则：按顺序依次执行不可跳过。Step 1 没有新知识时仍需执行 Step 2（处理已有候选的提升）。"
        ),
    },
    {
        "name": "miloco-habit-suggest",
        "schedule": "0 10 * * *",
        "skills": ["miloco-habit-suggest"],
        "prompt": (
            "执行每日习惯洞察。加载 miloco-habit-suggest skill，按【路径 A · 扫描推荐】处理："
            "从家庭档案识别值得建成任务的习惯，至多主动推荐一条。"
        ),
    },
]


def _import_cron_jobs():
    """延迟 import cron.jobs；失败返回 None（graceful）。"""
    try:
        from cron.jobs import create_job, list_jobs, update_job, remove_job
        return create_job, list_jobs, update_job, remove_job
    except Exception as exc:  # noqa: BLE001
        logger.info("cron.jobs 不可用，跳过 miloco 受管 cron reconcile: %s", exc)
        return None


def _managed_name(task_name: str) -> str:
    return f"{MANAGED_TAG} {task_name}"


def reconcile_cron_jobs(ctx: Optional[Any] = None) -> Dict[str, Any]:
    """对齐 4 个受管 cron job。返回 ``{created, updated, removed, skipped}``。

    ``deliver`` 参数从 ``state.json::deliver.target`` 取（install-hermes.sh 探测
    IM 平台时由 ``detect_im_platforms.py`` 写入），而不是字面量 ``"all"``——
    ``Platform("all")`` 不是合法 enum 值，``DeliveryTarget.parse("all")`` 会
    回退到 ``Platform.LOCAL``，让所有 cron 输出落到本地 markdown 而非 IM 推送
    （PR #279 reviewer Zirconi 标记的 critical bug）。

    没有 deliver target（state.json 缺失 / 损坏 / 还没配 IM）→ 直接跳过，
    返回 ``{skipped: True, reason: "no deliver target"}``，并 log warning 指
    引用户跑 ``install-hermes.sh`` 或手动编辑 ``state.json``。**绝不静默退化**
    （旧代码 hardcoded ``"all"`` 的行为就是静默退化）。

    逻辑（与 TS 端 ``reconcile`` 对齐）：
    1. 列出现有 job，按 ``name.startswith(MANAGED_TAG)`` 过滤出受管集合。
    2. 对每个期望任务：找不到 → create；找到 → update（刷新 schedule/skills/prompt）。
    3. 受管集合里不在期望名单的 → remove（清理已废弃的受管 job）。
    """
    funcs = _import_cron_jobs()
    if funcs is None:
        return {"created": 0, "updated": 0, "removed": 0, "skipped": True}

    # deliver target 必须先于 list/create 取，避免 list 成功后又因 target 缺失
    # 留下半残留 job（已存在的 managed job 也不会被错误地 update 成 deliver="all"）。
    deliver_target = get_deliver_target(ctx)
    if not deliver_target:
        logger.warning(
            "miloco cron reconcile 跳过：state.json::deliver.target 为空。"
            "请跑 install-hermes.sh（装好会探测 IM 平台并写入 target），"
            "或手动编辑 ~/.hermes/plugins/miloco/miloco-plugin/state.json："
            "{\"deliver\": {\"target\": \"telegram\"}}。"
        )
        return {"created": 0, "updated": 0, "removed": 0, "skipped": True,
                "reason": "no deliver target"}

    create_job, list_jobs, update_job, remove_job = funcs
    created = updated = removed = 0

    try:
        existing = list_jobs(include_disabled=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_jobs 失败，跳过 reconcile: %s", exc)
        return {"created": 0, "updated": 0, "removed": 0, "skipped": True, "error": str(exc)}

    # 受管 job：name 以 MANAGED_TAG 开头。
    managed = [j for j in existing if str(j.get("name", "")).startswith(MANAGED_TAG)]

    for task in _CRON_TASKS:
        target_name = _managed_name(task["name"])
        found = next((j for j in managed if j.get("name") == target_name), None)

        if found is None:
            try:
                create_job(
                    prompt=task["prompt"],
                    schedule=task["schedule"],
                    name=target_name,
                    skills=list(task["skills"]),
                    deliver=deliver_target,
                )
                created += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("create_job(%s) 失败: %s", target_name, exc)
        else:
            # update：刷新 schedule / skills / prompt / deliver（name / id 不动）。
            # deliver 来自 state.json::deliver.target（不是字面量 "all"）；
            # 用户想单推可在 Hermes 里手动改 cron job 的 deliver。
            updates = {
                "schedule": task["schedule"],
                "skills": list(task["skills"]),
                "prompt": task["prompt"],
                "deliver": deliver_target,
                "enabled": True,
            }
            try:
                update_job(found["id"], updates)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("update_job(%s) 失败: %s", found.get("id"), exc)

    # 清理受管集合里不在期望名单的 job。
    valid_names = {_managed_name(t["name"]) for t in _CRON_TASKS}
    for job in managed:
        if job.get("name") not in valid_names:
            try:
                remove_job(job["id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_job(%s) 失败: %s", job.get("id"), exc)

    logger.info(
        "miloco cron reconcile: created=%d updated=%d removed=%d deliver=%s",
        created, updated, removed, deliver_target,
    )
    return {"created": created, "updated": updated, "removed": removed, "skipped": False}


def teardown_cron_jobs() -> int:
    """卸载时移除所有受管 cron job（与 TS 端 ``teardown`` 对齐）。返回移除数。"""
    funcs = _import_cron_jobs()
    if funcs is None:
        return 0
    _, list_jobs, _, remove_job = funcs
    removed = 0
    try:
        existing = list_jobs(include_disabled=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("teardown list_jobs 失败: %s", exc)
        return 0
    for job in existing:
        if str(job.get("name", "")).startswith(MANAGED_TAG):
            try:
                remove_job(job["id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("teardown remove_job(%s) 失败: %s", job.get("id"), exc)
    return removed
