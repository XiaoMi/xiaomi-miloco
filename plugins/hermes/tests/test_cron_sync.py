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


def test_managed_tag_value():
    assert cron_sync.MANAGED_TAG == "[miloco:hermes]"


def test_register_cron_sync_registers_miloco_cli_command():
    class FakeCtx:
        def __init__(self):
            self.commands = []

        def register_cli_command(self, name, handler):
            self.commands.append((name, handler))

    ctx = FakeCtx()
    cron_sync.register_cron_sync(ctx)
    assert len(ctx.commands) == 1
    assert ctx.commands[0][0] == "miloco"


def test_register_cron_sync_tolerates_missing_cron_jobs():
    class FakeCtx:
        def register_cli_command(self, name, handler):
            pass

    cron_sync.register_cron_sync(FakeCtx())


def test_miloco_cli_status_reports_managed_tasks():
    out = cron_sync._miloco_cli_handler(["status"])
    text = str(out)
    assert "miloco-perception-digest" in text
    assert "4" in text
