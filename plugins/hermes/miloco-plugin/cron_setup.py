"""reconcile miloco 受管 cron job。

移植自 openclaw TypeScript 插件
``plugins/openclaw/src/home-profile/scheduler.ts``。

对齐 OpenClaw 行为：
- cron delivery = none（输出静默，agent 主动调 miloco_im_push 才通知）
- cron 创建不依赖 deliver target（无 IM 也能跑）
- 缺 IM 只记 warning，不阻拦 reconcile

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
``payload.skills``（按顺序依次加载）。

【L1 守门】:
- 增 ``resume_job`` import + 调用：hermes cron 有独立 state 字段
  (state=paused/running/scheduled)，``update_job`` 只改 enabled 不改 state。
  L1 守门要真激活 paused cron 必须再调 ``resume_job()``。
- 增 ``_check_backend_ready()``：启动时检 backend .env::model.omni.api_key，
  没配齐时 4 个受管 cron 创为 paused（避免每 15min 推 [SILENT] 骚扰用户）。
  配齐后 plugin register 自动 unpause。
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 受管 job 标签：塞进 name 字段前缀，reconcile 据此识别。
MANAGED_TAG = "[miloco:home-profile]"


def _is_force_enabled() -> bool:
    return os.environ.get("MILOCO_FORCE_CRON_ENABLED", "0") == "1"


def _managed_name(task_name: str) -> str:
    return f"{MANAGED_TAG} {task_name}"


# 4 个受管 cron 任务定义。prompt 与 OpenClaw scheduler.ts 保持 1:1 一致。
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
        "prompt": (
            "执行家庭巡检。加载 miloco-home-patrol skill 进行巡检。每次巡检都是隔离会话，"
            "务必先读巡检日志（已处理台账）知道已做过什么，回看最近约 2 小时的新情况、"
            "只做没做过的，处理完把做过的追加回台账，避免重复提醒 / 重复操作。"
            "注意：老人长时间无活动 / 成员远超回家时间未归这类缺席型安全信号"
            "不受 2 小时近窗限制，须按 skill 内规则回看历史评估。"
        ),
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
            create_job,
            list_jobs,
            pause_job,
            remove_job,
            resume_job,
            update_job,
        )
        return create_job, list_jobs, update_job, remove_job, resume_job, pause_job
    except Exception as exc:  # noqa: BLE001
        logger.info("cron.jobs 不可用，跳过 miloco 受管 cron reconcile: %s", exc)
        return None


def _check_backend_ready() -> bool:
    """【L1 守门】检测 backend .env 是否配齐 omni model key。

    没配齐时返回 False → reconcile_cron_jobs 把 4 个受管 cron 创为 paused 状态，
    避免向用户推 [SILENT] 消息（每 15min 推一条太骚扰）。

    检测方法：``miloco-cli config get model.omni.api_key``。

    override 开关：环境变量 MILOCO_FORCE_CRON_ENABLED=1 跳过此检查。
    """
    if _is_force_enabled():
        logger.info("MILOCO_FORCE_CRON_ENABLED=1 → 跳过 backend .env 检测，4 个 cron 创为 active")
        return True
    try:
        import shutil
        if not shutil.which("miloco-cli"):
            logger.warning("miloco-cli 不在 PATH，降级为 backend ready=True")
            return True
        proc = subprocess.run(
            ["miloco-cli", "config", "get", "model.omni.api_key"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            logger.warning(
                "miloco-cli config get 失败 rc=%s，降级 backend ready=True，后续 cron 触发时再诊断",
                proc.returncode,
            )
            return True
        api_key = (proc.stdout or "").strip()
        ready = bool(api_key) and api_key not in ('""', "''")
        if not ready:
            logger.warning(
                "miloco backend .env 未配齐 model.omni.api_key。"
                "4 个 miloco cron 保持 paused（避免 [SILENT] 消息骚扰）。\n"
                "  启用方法：\n"
                "  1. miloco-cli config set model.omni.api_key '<your-key>'\n"
                "  2. hermes gateway restart  # 重新注册触发 cron resume\n"
                "  3. 或：export MILOCO_FORCE_CRON_ENABLED=1 强制 active（忽略检测）"
            )
        return ready
    except subprocess.TimeoutExpired:
        logger.warning("miloco-cli config get 超时（5s），降级 backend ready=True")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("miloco-cli config get 异常（%s），降级 backend ready=True", exc)
        return True


def reconcile_cron_jobs(ctx: Optional[Any] = None) -> Dict[str, Any]:
    """对齐 4 个受管 cron job。返回 ``{created, updated, removed, skipped}``。

    对齐 OpenClaw ``scheduler.ts::reconcile``：
    - delivery = none（不传 deliver 参数，cron 输出静默）
    - agent 显式调 miloco_im_push 才通知用户
    - 无 deliver target 不阻拦 reconcile（与 OpenClaw 行为一致）

    逻辑：
    1. 列出现有 job，按 ``name.startswith(MANAGED_TAG)`` 过滤出受管集合。
    2. 对每个期望任务：找不到 → create；找到 → update（刷新 schedule/skills/prompt）。
    3. 受管集合里不在期望名单的 → remove（清理已废弃的受管 job）。
    """
    funcs = _import_cron_jobs()
    if funcs is None:
        return {"created": 0, "updated": 0, "removed": 0, "skipped": True}

    # 对齐 OpenClaw：cron 默认 delivery:none，agent 调 miloco_im_push 才主动通知。

    backend_ready = _check_backend_ready()
    cron_active = backend_ready

    create_job, list_jobs, update_job, remove_job, resume_job, pause_job = funcs
    created = updated = removed = resumed = paused = 0

    try:
        existing = list_jobs(include_disabled=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_jobs 失败，跳过 reconcile: %s", exc)
        return {"created": 0, "updated": 0, "removed": 0, "skipped": True, "error": str(exc)}

    managed = [j for j in existing if str(j.get("name", "")).startswith(MANAGED_TAG)]

    for task in _CRON_TASKS:
        target_name = _managed_name(task["name"])
        found = next((j for j in managed if j.get("name") == target_name), None)

        if found is None:
            try:
                base_kwargs: Dict[str, Any] = {
                    "prompt": task["prompt"],
                    "schedule": task["schedule"],
                    "name": target_name,
                    "skills": list(task["skills"]),
                }
                # 对齐 OpenClaw：不传 deliver → cron 输出静默
                # agent 需要通知时显式调 miloco_im_push
                create_job(**base_kwargs)
                if not cron_active:
                    try:
                        pause_job(target_name)
                    except Exception:
                        pass
                created += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("create_job(%s) 失败: %s", target_name, exc)
        else:
            updates = {
                "schedule": task["schedule"],
                "skills": list(task["skills"]),
                "prompt": task["prompt"],
                "deliver": None,
            }
            if not cron_active:
                if found.get("state") != "paused":
                    try:
                        pause_job(found["id"])
                        paused += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("pause_job(%s) 失败: %s", found.get("id"), exc)
                try:
                    update_job(found["id"], updates)
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("update_job(%s) 失败: %s", found.get("id"), exc)
            else:
                try:
                    update_job(found["id"], updates)
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("update_job(%s) 失败: %s", found.get("id"), exc)
                if found.get("state") == "paused":
                    try:
                        resume_job(found["id"])
                        resumed += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("resume_job(%s) 失败: %s", found.get("id"), exc)

    valid_names = {_managed_name(t["name"]) for t in _CRON_TASKS}
    for job in managed:
        if job.get("name") not in valid_names:
            try:
                remove_job(job["id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_job(%s) 失败: %s", job.get("id"), exc)

    logger.info(
        "miloco cron reconcile: created=%d updated=%d removed=%d resumed=%d enabled=%s",
        created, updated, removed, resumed, cron_active,
    )
    return {
        "created": created,
        "updated": updated,
        "removed": removed,
        "resumed": resumed,
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
