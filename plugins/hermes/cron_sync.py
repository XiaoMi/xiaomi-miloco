import logging

__all__ = [
    "register_cron_sync",
    "CRON_TASKS",
]

logger = logging.getLogger(__name__)

CRON_TASKS = [
    {
        "name": "miloco-perception-digest",
        "prompt": "执行感知日志摘要。加载 miloco:miloco-perception-digest skill 进行处理。",
        "schedule": "*/15 * * * *",
        "skills": ["miloco:miloco-perception-digest"],
        "deliver": "none",
    },
    {
        "name": "miloco-home-patrol",
        "prompt": "执行家庭巡检。加载 miloco:miloco-home-patrol skill 进行巡检。",
        "schedule": "*/30 * * * *",
        "skills": ["miloco:miloco-home-patrol"],
        "deliver": "none",
    },
    {
        "name": "miloco-home-dreaming",
        "prompt": "执行 home-dreaming 流程。依次完成 Observe→Promote→Prune。",
        "schedule": "0 0 * * *",
        "skills": [
            "miloco:miloco-home-observe",
            "miloco:miloco-home-promote",
            "miloco:miloco-home-prune",
        ],
        "deliver": "none",
    },
    {
        "name": "miloco-habit-suggest",
        "prompt": "执行每日习惯洞察。加载 miloco:miloco-habit-suggest skill。",
        "schedule": "0 10 * * *",
        "skills": ["miloco:miloco-habit-suggest"],
        "deliver": "none",
    },
]


def register_cron_sync(ctx):
    try:
        from cron import jobs as cron_jobs
    except ImportError:
        logger.warning("cron module unavailable, skipping cron sync")
        return

    for task in CRON_TASKS:
        existing = cron_jobs.resolve_job_ref(task["name"])
        if existing:
            logger.info("cron job %s already exists, skipping", task["name"])
            continue
        cron_jobs.create_job(
            name=task["name"],
            prompt=task["prompt"],
            schedule=task["schedule"],
            skills=task["skills"],
            deliver=task["deliver"],
        )
        logger.info("created cron job: %s", task["name"])
