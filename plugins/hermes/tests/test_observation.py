from unittest.mock import Mock

from miloco_plugin_pkg import cron_setup


def test_iot_is_observe_sidecar_not_patrol_hook(monkeypatch):
    import miloco_plugin_pkg as plugin

    monkeypatch.setattr(plugin, "reconcile_cron_jobs", Mock())
    monkeypatch.setattr(plugin, "register_trace_hooks", Mock(return_value=0))
    monkeypatch.setattr("threading.Thread", Mock())
    context = Mock()
    plugin.register(context)
    hooks = [entry.args[1] for entry in context.register_hook.call_args_list if entry.args[0] == "pre_llm_call"]
    assert len(hooks) == 1
    assert hooks[0]("patrol", "[miloco:iot-observation]", [], True, "model", "cron") is None
    patrol = next(task for task in cron_setup._CRON_TASKS if task["name"] == "miloco-home-patrol")
    dreaming = next(task for task in cron_setup._CRON_TASKS if task["name"] == "miloco-home-dreaming")
    assert "iot-observation" not in patrol["prompt"]
    assert "IoT" not in patrol["prompt"]
    assert "IoT 旁路" in dreaming["prompt"]
    assert "miloco-home-observe" in dreaming["skills"]
    assert len(cron_setup._CRON_TASKS) == 4
