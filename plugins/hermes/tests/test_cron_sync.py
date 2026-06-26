from hermes import cron_sync


def test_cron_tasks_has_four_entries():
    assert len(cron_sync.CRON_TASKS) == 4


def test_cron_tasks_names_in_order():
    names = [t["name"] for t in cron_sync.CRON_TASKS]
    assert names == [
        "miloco-perception-digest",
        "miloco-home-patrol",
        "miloco-home-dreaming",
        "miloco-habit-suggest",
    ]


def test_cron_tasks_have_schedule_prompt_and_skills():
    for task in cron_sync.CRON_TASKS:
        assert task["schedule"]
        assert task["prompt"]
        assert isinstance(task["skills"], list) and task["skills"]
        assert task["deliver"] == "none"


def test_register_cron_sync_tolerates_missing_cron_jobs():
    cron_sync.register_cron_sync(None)
