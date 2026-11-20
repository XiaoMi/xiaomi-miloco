"""「自动定时任务」开关（``scheduler.enabled``）在 Hermes 插件 register() 中生效（#612）。

对齐 OpenClaw ``home-profile/scheduler.ts``：gateway 启动时若开关关闭，
teardown 全部受管 cron 并跳过 reconcile；环境变量 ``MILOCO_SCHEDULER__ENABLED``
优先于 config.json，缺省按开启处理。
"""

from __future__ import annotations

import json

import miloco_plugin_pkg as plugin
import pytest
from miloco_plugin_pkg import cron_setup

ENV_NAME = "MILOCO_SCHEDULER__ENABLED"


class _FakeCtx:
    """最小 Hermes ctx：只接住 register() 里的 hook / tool 注册。"""

    def register_hook(self, *args, **kwargs) -> None:
        pass

    def register_tool(self, *args, **kwargs) -> None:
        pass


class _CronRecorder:
    def __init__(self, existing: list) -> None:
        self.calls: list = []
        self._existing = list(existing)

    def funcs(self):
        def _create(**kw):
            self.calls.append(("create", kw.get("name")))

        def _list(include_disabled: bool = True):
            return list(self._existing)

        def _update(jid, updates):
            self.calls.append(("update", jid))

        def _remove(jid):
            self.calls.append(("remove", jid))

        def _resume(jid):
            self.calls.append(("resume", jid))

        def _pause(jid):
            self.calls.append(("pause", jid))

        return lambda: (_create, _list, _update, _remove, _resume, _pause)

    def ops(self, op: str) -> list:
        return [c[1] for c in self.calls if c[0] == op]


def _managed_jobs() -> list:
    """4 个已存在的受管 job（用户手动 pause 过）+ 1 个用户自己的 job。"""
    jobs = [
        {
            "id": f"job-{i}",
            "name": cron_setup._managed_name(task["name"]),
            "enabled": False,
            "state": "paused",
        }
        for i, task in enumerate(cron_setup._CRON_TASKS)
    ]
    jobs.append({"id": "user-job", "name": "my own reminder", "enabled": True})
    return jobs


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """隔离 MILOCO_HOME / 环境变量，并把 cron 接口换成记录器。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.delenv(ENV_NAME.lower(), raising=False)
    rec = _CronRecorder(_managed_jobs())
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", rec.funcs())
    monkeypatch.setattr(cron_setup, "_check_backend_ready", lambda: True)

    def _write_config(data: dict) -> None:
        (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")

    return rec, _write_config


def _assert_torn_down(rec: _CronRecorder) -> None:
    managed_ids = [f"job-{i}" for i in range(len(cron_setup._CRON_TASKS))]
    assert sorted(rec.ops("remove")) == sorted(managed_ids)
    assert "user-job" not in rec.ops("remove")
    assert rec.ops("create") == []
    assert rec.ops("update") == []
    assert rec.ops("resume") == []


def _assert_reconciled(rec: _CronRecorder) -> None:
    assert rec.ops("remove") == []
    assert len(rec.ops("update")) == len(cron_setup._CRON_TASKS)
    assert len(rec.ops("resume")) == len(cron_setup._CRON_TASKS)


def test_disabled_in_config_tears_down_and_skips_reconcile(setup):
    """#612：config.json 里 scheduler.enabled=false → 移除受管 job，不重建、不 resume。"""
    rec, write_config = setup
    write_config({"scheduler": {"enabled": False}})
    plugin.register(_FakeCtx())
    _assert_torn_down(rec)


def test_enabled_by_default_when_config_missing(setup):
    """没有 config.json / 没有 scheduler 段 → 按开启处理，照常 reconcile。"""
    rec, _ = setup
    plugin.register(_FakeCtx())
    _assert_reconciled(rec)


def test_enabled_true_in_config_reconciles(setup):
    rec, write_config = setup
    write_config({"scheduler": {"enabled": True}})
    plugin.register(_FakeCtx())
    _assert_reconciled(rec)


@pytest.mark.parametrize("value", ["false", "0", "off", "No"])
def test_env_false_overrides_config_true(setup, monkeypatch, value):
    """环境变量优先于 config.json（对齐后端 pydantic-settings 与 OpenClaw）。"""
    rec, write_config = setup
    write_config({"scheduler": {"enabled": True}})
    monkeypatch.setenv(ENV_NAME, value)
    plugin.register(_FakeCtx())
    _assert_torn_down(rec)


def test_env_true_overrides_config_false(setup, monkeypatch):
    rec, write_config = setup
    write_config({"scheduler": {"enabled": False}})
    monkeypatch.setenv(ENV_NAME, "true")
    plugin.register(_FakeCtx())
    _assert_reconciled(rec)


def test_unparsable_env_falls_back_to_config(setup, monkeypatch):
    """env 值无法解析时忽略它，回落 config.json。"""
    rec, write_config = setup
    write_config({"scheduler": {"enabled": False}})
    monkeypatch.setenv(ENV_NAME, "maybe")
    plugin.register(_FakeCtx())
    _assert_torn_down(rec)
