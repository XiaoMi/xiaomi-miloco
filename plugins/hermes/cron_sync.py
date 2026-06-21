import logging

__all__ = [
    "register_cron_sync",
    "CRON_TASKS",
]

logger = logging.getLogger(__name__)

MANAGED_TAG = "[miloco:hermes]"

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


def _reconcile_cron(cron_jobs):
    managed_names = {task["name"] for task in CRON_TASKS}
    existing = {}
    list_fn = getattr(cron_jobs, "list_jobs", None)
    if callable(list_fn):
        try:
            for job in list_fn():
                name = getattr(job, "name", None) or (
                    job.get("name") if isinstance(job, dict) else None
                )
                if name:
                    existing[name] = job
        except Exception:
            logger.exception("failed to list cron jobs")
    for task in CRON_TASKS:
        upsert_fn = getattr(cron_jobs, "upsert", None)
        if callable(upsert_fn):
            try:
                upsert_fn(task)
            except Exception:
                logger.exception("failed to upsert cron task %s", task["name"])
    for name, job in existing.items():
        if name in managed_names:
            continue
    return len(managed_names)


def _miloco_cli_handler(args):
    sub = getattr(args, "miloco_command", None) or "status"
    if sub == "status":
        lines = ["managed cron tasks ({}):".format(len(CRON_TASKS))]
        for task in CRON_TASKS:
            lines.append("- {} [{}]".format(task["name"], task["schedule"]))
        print("\n".join(lines))
    elif sub == "restart":
        try:
            from cron import jobs as cron_jobs

            count = _reconcile_cron(cron_jobs)
            print("reconciled {} managed cron tasks".format(count))
        except ImportError:
            print("cron module unavailable; nothing to restart")


def _setup_cli(subparser):
    subs = subparser.add_subparsers(dest="miloco_command")
    subs.add_parser("status", help="Show Miloco managed cron tasks")
    subs.add_parser("restart", help="Reconcile Miloco cron tasks")


def register_cron_sync(ctx):
    try:
        from cron import jobs as cron_jobs
    except ImportError:
        cron_jobs = None
    if cron_jobs is not None:
        try:
            _reconcile_cron(cron_jobs)
        except Exception:
            logger.exception("cron reconcile failed")
    ctx.register_cli_command(
        name="miloco",
        help="Miloco management",
        setup_fn=_setup_cli,
        handler_fn=_miloco_cli_handler,
    )
