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

【hermes-pr.md §五 #11 完成】+【L1 守门:hermes-pr.md §五 #12 准备】:
- 增 ``resume_job`` import + 调用:hermes cron 有独立 state 字段
  (state=paused/running/scheduled),``update_job`` 只改 enabled 不改 state。
  L1 守门要真激活 paused cron 必须再调 ``resume_job()``。
- 增 ``_check_backend_ready()``:启动时检 backend .env::model.omni.api_key,
  没配齐时 4 个受管 cron 创为 paused(避免每 15min 推 [SILENT] 骚扰用户)。
  配齐后 plugin register 自动 unpause(用户可 hermes cron resume 加速)。
- 配齐后 create/update 都用 cron_active=True(传入 enabled= 参数)。
- 配齐后还调 ``resume_job`` 确保 state=scheduled(防 hermes 老版本
  create_job 默认 state=paused 行为)。

import 失败要 graceful：Hermes 不在运行环境时 ``cron.jobs`` 模块不可用，
``reconcile_cron_jobs`` 直接返回，不影响插件其余功能。
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 从 state.json 读 deliver.target（用户通过 miloco_notify_bind 或手动编辑设置）。
from .tools_notify import get_deliver_target

# 受管 job 标签：塞进 name 字段前缀，reconcile 据此识别。
MANAGED_TAG = "[miloco:home-profile]"

# 【L1 守门:hermes-pr.md §五 #12 准备】环境变量 override,1 = 强制把 cron 创为 active
# (忽略 backend .env 检测,用于压测/调试);0/未设 = 默认行为(检测 .env)。
# 注意:每次调用 _check_backend_ready() 时读 env(不在 module 顶层固化),
# 这样测试 monkeypatch.setenv 能影响下一次调用。
def _is_force_enabled() -> bool:
    return os.environ.get("MILOCO_FORCE_CRON_ENABLED", "0") == "1"


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
        from cron.jobs import (
            create_job, list_jobs, update_job, remove_job, resume_job, pause_job,
        )
        return create_job, list_jobs, update_job, remove_job, resume_job, pause_job
    except Exception as exc:  # noqa: BLE001
        logger.info("cron.jobs 不可用，跳过 miloco 受管 cron reconcile: %s", exc)
        return None


def _check_backend_ready() -> bool:
    """【L1 守门:hermes-pr.md §五 #12 准备】检测 backend .env 是否配齐 omni model key。

    没配齐时返回 False → reconcile_cron_jobs 把 4 个受管 cron 创为 paused 状态,
    避免向用户推 [SILENT] 消息(每 15min 推一条太骚扰)。

    检测方法: ``miloco-cli config get model.omni.api_key`` — 这是 backend 暴露的
    稳定接口,与 plugin 配置读取保持一致。

    override 开关: 环境变量 MILOCO_FORCE_CRON_ENABLED=1 跳过此检查(用于压测/调试)。
    """
    if _is_force_enabled():
        logger.info("MILOCO_FORCE_CRON_ENABLED=1 → 跳过 backend .env 检测,4 个 cron 创为 active")
        return True
    try:
        # miloco-cli 不在 PATH 时(罕见,装时必装),降级 True 让 cron 创出来
        # (下次 plugin register 仍会触发,用户看到 paused 后能诊断)
        import shutil
        if not shutil.which("miloco-cli"):
            logger.warning("miloco-cli 不在 PATH,降级为 backend ready=True")
            return True
        proc = subprocess.run(
            ["miloco-cli", "config", "get", "model.omni.api_key"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            logger.warning(
                "miloco-cli config get 失败 rc=%s,降级 backend ready=True,后续 cron 触发时再诊断",
                proc.returncode,
            )
            return True
        api_key = (proc.stdout or "").strip()
        # 输出格式:"api_key" 或空字符串(用户没配)
        # 用引号存在判断(避免空字符串误判)
        ready = bool(api_key) and api_key not in ('""', "''")
        if not ready:
            logger.warning(
                "miloco backend .env 未配齐 model.omni.api_key。"
                "4 个 miloco cron 保持 paused(避免 [SILENT] 消息骚扰)。\n"
                "  启用方法: \n"
                "  1. 编辑 ~/.openclaw/miloco/config.json(或 backend/.env) 配 model.omni.api_key\n"
                "  2. 跑: hermes cron resume aca1af7a0fd2  # miloco-perception-digest\n"
                "           hermes cron resume 3171cc142767  # miloco-home-patrol\n"
                "           hermes cron resume f889fb99432f  # miloco-home-dreaming\n"
                "           hermes cron resume 98ecd7d4d438  # miloco-habit-suggest\n"
                "  3. 或: export MILOCO_FORCE_CRON_ENABLED=1 强制 active(忽略检测)"
            )
        return ready
    except subprocess.TimeoutExpired:
        logger.warning("miloco-cli config get 超时(5s),降级 backend ready=True")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("miloco-cli config get 异常(%s),降级 backend ready=True", exc)
        return True


def _managed_name(task_name: str) -> str:
    return f"{MANAGED_TAG} {task_name}"


def reconcile_cron_jobs(ctx: Optional[Any] = None) -> Dict[str, Any]:
    """对齐 4 个受管 cron job。返回 ``{created, updated, removed, skipped}``。

    ``deliver`` 参数从 ``state.json::deliver.target`` 取（用户通过
    miloco_notify_bind 或手动编辑设置），而不是字面量 ``"all"``——
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

    # 【L1 守门:hermes-pr.md §五 #12 准备】检测 backend .env 是否配齐 model key。
    # 没配齐时把 4 个受管 cron 创为 paused(active=False),避免每 15 分钟推一条
    # [SILENT] 通知骚扰用户(用户需填 .env + 跑 hermes cron resume 激活)。
    backend_ready = _check_backend_ready()
    cron_active = backend_ready  # paused when not ready

    create_job, list_jobs, update_job, remove_job, resume_job, pause_job = funcs
    created = updated = removed = resumed = paused = 0

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
                # 【L1 守门】backend 没配齐时 pause(防止创了就跑)
                if not cron_active:
                    try:
                        pause_job(target_name)
                    except Exception:
                        pass
                created += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("create_job(%s) 失败: %s", target_name, exc)
        else:
            # update：刷新 schedule / skills / prompt / deliver（name / id 不动）。
            # deliver 来自 state.json::deliver.target（不是字面量 "all"）；
            # 用户想单推可在 Hermes 里手动改 cron job 的 deliver。
            # 【L1 守门】backend 没配齐时,先 pause_job 真正设 state=paused
            # (只 update enabled=False 不足以关停 hermes — 推 [SILENT] 是用
            # 走完整个 agent 循环再投递,即便 enabled=False 仍可能 state=scheduled 跑)。
            # pause_job 是 hermes 的显式 L1 守门:把 state 也设到 paused。
            if not cron_active:
                if found.get("state") != "paused":
                    try:
                        pause_job(found["id"])
                        paused += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("pause_job(%s) 失败: %s", found.get("id"), exc)
                updates = {
                    "schedule": task["schedule"],
                    "skills": list(task["skills"]),
                    "prompt": task["prompt"],
                    "deliver": deliver_target,
                }
                try:
                    update_job(found["id"], updates)
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("update_job(%s) 失败: %s", found.get("id"), exc)
            else:
                # backend 配齐:正常 update
                updates = {
                    "schedule": task["schedule"],
                    "skills": list(task["skills"]),
                    "prompt": task["prompt"],
                    "deliver": deliver_target,
                }
                try:
                    update_job(found["id"], updates)
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("update_job(%s) 失败: %s", found.get("id"), exc)
                # 【L1 守门补】hermes cron 有独立 state 字段(state=paused/running/scheduled),
                # update_job 只改 enabled 不改 state。如果 cron_active=True 但 state=paused
                # (之前手动 pause 过),需调 resume_job 真正激活。L1 守门才算"端到端可用"。
                if found.get("state") == "paused":
                    try:
                        resume_job(found["id"])
                        resumed += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("resume_job(%s) 失败: %s", found.get("id"), exc)

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
        "miloco cron reconcile: created=%d updated=%d removed=%d resumed=%d enabled=%s deliver=%s",
        created, updated, removed, resumed, cron_active, deliver_target,
    )
    return {
        "created": created,
        "updated": updated,
        "removed": removed,
        "resumed": resumed,  # 报告 L1 守门实际 unpause 数量
        "skipped": False,
        "active": cron_active,
    }


def teardown_cron_jobs() -> int:
    """卸载时移除所有受管 cron job（与 TS 端 ``teardown`` 对齐）。返回移除数。"""
    funcs = _import_cron_jobs()
    if funcs is None:
        return 0
    _, list_jobs, _, remove_job, _, _ = funcs
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
