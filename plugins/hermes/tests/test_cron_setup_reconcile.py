"""miloco 受管 cron job reconcile 行为测试。

对齐 OpenClaw scheduler.ts：
- cron 输出静默（不传 deliver，agent 调 miloco_im_push 才通知）
- reconcile 不因缺 deliver target 跳过
- L1 守门：backend 未配齐时 paused
"""

from __future__ import annotations

from miloco_plugin_pkg import cron_setup


class _Recording:
    def __init__(self, existing: list | None = None) -> None:
        self.calls: list = []
        self._existing = list(existing or [])

    def make_create(self):
        def _create(**kw):
            self.calls.append(("create", kw))
        return _create

    def make_list(self):
        def _list(include_disabled: bool = True):
            self.calls.append(("list", include_disabled))
            return list(self._existing)
        return _list

    def make_update(self):
        def _update(jid, updates):
            self.calls.append(("update", jid, updates))
        return _update

    def make_remove(self):
        def _remove(jid):
            self.calls.append(("remove", jid))
        return _remove

    def make_resume(self):
        def _resume(jid):
            self.calls.append(("resume", jid))
        return _resume

    def make_pause(self):
        def _pause(jid):
            self.calls.append(("pause", jid))
        return _pause


def _stub_import_cron_jobs(rec: _Recording):
    return lambda: (rec.make_create(), rec.make_list(), rec.make_update(),
                    rec.make_remove(), rec.make_resume(), rec.make_pause())


def _stub_check_backend_ready(value: bool):
    return lambda: value


def test_creates_4_cron_jobs_when_none_exist(monkeypatch):
    """空列表 → 创建 4 个 cron，不传 deliver（对齐 OpenClaw delivery:none）。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert result["created"] == 4
    assert not result["skipped"]
    for _, kw in [c for c in rec.calls if c[0] == "create"]:
        assert "deliver" not in kw


def test_updates_existing_cron_jobs(monkeypatch):
    existing = [
        {"id": "abc-123", "name": "[miloco:home-profile] miloco-perception-digest",
         "schedule": "*/5 * * * *", "skills": ["old"], "prompt": "old"},
        {"id": "def-456", "name": "[miloco:home-profile] miloco-home-patrol",
         "schedule": "*/5 * * * *", "skills": ["old"], "prompt": "old"},
        {"id": "ghi-789", "name": "[miloco:home-profile] miloco-home-dreaming",
         "schedule": "0 8 * * *", "skills": ["old"], "prompt": "old"},
        {"id": "jkl-012", "name": "[miloco:home-profile] miloco-habit-suggest",
         "schedule": "0 8 * * *", "skills": ["old"], "prompt": "old"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert result["updated"] >= 1
    assert not result["skipped"]
    for _, _, updates in [c for c in rec.calls if c[0] == "update"]:
        assert "deliver" not in updates


def test_removes_orphaned_managed_jobs(monkeypatch):
    existing = [
        {"id": "old-1", "name": "[miloco:home-profile] miloco-old-job"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert result["removed"] == 1


def test_paused_when_backend_not_ready(monkeypatch):
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(False))
    result = cron_setup.reconcile_cron_jobs()
    assert result["active"] is False
    assert result["created"] == 4
    # 每个创建的 job 都应该被 pause
    pauses = [c for c in rec.calls if c[0] == "pause"]
    assert len(pauses) == 4


def test_reconcile_proceeds_without_deliver_target(monkeypatch):
    """对齐 OpenClaw：无 deliver target 不阻拦 reconcile，正常创建 4 个 cron。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert not result["skipped"]
    assert result["created"] == 4


def test_resume_paused_jobs_when_backend_becomes_ready(monkeypatch):
    existing = [
        {"id": "abc-123", "name": "[miloco:home-profile] miloco-perception-digest",
         "schedule": "*/15 * * * *", "skills": ["miloco-perception-digest"],
         "prompt": "digest", "state": "paused"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert result["resumed"] == 1


def test_home_patrol_prompt_has_guardrails(monkeypatch):
    """补齐巡检护栏文案，对齐 OpenClaw。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    cron_setup.reconcile_cron_jobs()
    patrol_call = [c for c in rec.calls if c[0] == "create" and "miloco-home-patrol" in str(c[1].get("name", ""))]
    assert len(patrol_call) == 1
    prompt = patrol_call[0][1].get("prompt", "")
    assert "隔离会话" in prompt
    assert "已处理台账" in prompt
    assert "2 小时" in prompt
    assert "缺席型安全信号" in prompt


def test_jobs_not_managed_are_ignored(monkeypatch):
    """非 miloco 标签的 job 不受影响。"""
    existing = [
        {"id": "other-1", "name": "daily-weather"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))
    result = cron_setup.reconcile_cron_jobs()
    assert result["removed"] == 0  # 不删非受管 job
    assert result["created"] == 4
