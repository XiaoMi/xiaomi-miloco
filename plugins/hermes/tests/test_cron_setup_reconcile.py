"""miloco 受管 cron job 的 reconcile 行为测试。

关键防回归点（PR #279 reviewer critical bug）：

- ``deliver`` 参数不能硬编码成 ``"all"``，因为 ``Platform("all")`` 不是合法
  的 ``Platform`` enum 成员——``DeliveryTarget.parse("all")`` 会抛
  ``ValueError`` 然后回落到 ``Platform.LOCAL``，导致所有 cron job 输出去本地
  markdown 而不是 IM 推送。
- ``deliver`` 应该读 ``state.json::deliver.target``（install-hermes.sh 在安装
  时由 miloco_notify_bind 或手动编辑设置），不是字面量 ``"all"``。

这些测试只覆盖 ``reconcile_cron_jobs`` 的 deliver 取值行为；具体的 4 个
cron job 调度（``*/15`` / ``*/30`` / ``0 0`` / ``0 10``）属于 schedule 正确性
测试，spec 里明确说这次任务不做。
"""

from __future__ import annotations

from miloco_plugin_pkg import cron_setup


class _Recording:
    """捕获 cron.jobs 函数的调用，模拟 create/list/update/remove。"""

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
        def _pause(jid, reason=None):
            self.calls.append(("pause", jid, reason))
        return _pause


def _stub_import_cron_jobs(rec: _Recording):
    """返回 lambda,匹配 cron_setup._import_cron_jobs 的 (create, list, update, remove, resume, pause) 元组。

    PR3 (L1 守门补): 加 pause_job 让 backend 没配齐时真正 set state=paused
    (只 update enabled=False 不足以关停 hermes 推 [SILENT] 消息)。
    """
    funcs = (
        rec.make_create(),
        rec.make_list(),
        rec.make_update(),
        rec.make_remove(),
        rec.make_resume(),
        rec.make_pause(),
    )
    return lambda: funcs


# ────────────────────────────────────────────────────────────────────────
# 防回归：deliver 必须从 state.json::deliver.target 取，绝不能是 "all"
# ────────────────────────────────────────────────────────────────────────


def test_reconcile_uses_state_json_target_not_all(monkeypatch):
    """deliver 必须来自 state.json，硬编码 'all' 会让 DeliveryTarget.parse 回退到 LOCAL。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "feishu")

    result = cron_setup.reconcile_cron_jobs()

    creates = [c for c in rec.calls if c[0] == "create"]
    assert creates, "expected create_job to be called"
    for _, kw in creates:
        assert kw.get("deliver") not in (None, "", "all"), (
            "deliver 不能硬编码 'all'——DeliveryTarget.parse('all') 会回退到 "
            "Platform.LOCAL，cron 输出落到本地文件而不是 IM 推送"
        )
    assert result.get("skipped") is False
    assert result.get("created", 0) >= 1


def test_reconcile_update_uses_state_json_target_not_all(monkeypatch):
    """update_job 的 deliver 同样不能硬编码 'all'——否则已存在的 job 也会突然变成 LOCAL 输出。"""
    existing = [
        {
            "id": "job-1",
            "name": f"{cron_setup.MANAGED_TAG} miloco-perception-digest",
        },
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "telegram")

    result = cron_setup.reconcile_cron_jobs()

    updates = [c for c in rec.calls if c[0] == "update"]
    assert updates, "expected update_job to be called"
    for _, _jid, upd in updates:
        assert upd.get("deliver") != "all", (
            "update_job 的 deliver 同样不能硬编码 'all'"
        )
        assert upd.get("deliver") == "telegram"
    assert result.get("updated", 0) >= 1


def test_reconcile_skips_with_clear_reason_when_no_deliver_target(monkeypatch):
    """没有 deliver target 时直接跳过，返回明确的 reason（不静默失败）。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: None)

    result = cron_setup.reconcile_cron_jobs()

    creates = [c for c in rec.calls if c[0] == "create"]
    assert not creates, "没有 deliver target 时不应该调 create_job"
    assert result.get("skipped") is True
    assert result.get("reason") == "no deliver target"


# ────────────────────────────────────────────────────────────────────────
# 【L1 守门:hermes-pr.md §五 #12 准备】backend .env 没配齐 model key
# → 4 个受管 cron 创为 paused,避免每 15min 推 [SILENT] 骚扰用户
# ────────────────────────────────────────────────────────────────────────


def _stub_check_backend_ready(ready: bool):
    """返回 lambda 替换 cron_setup._check_backend_ready。"""
    return lambda: ready


def test_reconcile_creates_crons_paused_when_backend_not_ready(monkeypatch, tmp_path):
    """backend .env 没配齐 model key → create_job 后调 pause_job。

    防骚扰:没配 key 时 cron 跑也返 [SILENT],hermes 仍发 Cronjob Response
    通知,每 15min 推一条太烦。守门:register 时检 backend 配齐才 active,没配齐
    创完立刻 pause(用户填 .env 后下次 plugin register 自动激活,无需手动 resume)。
    """
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(False))

    result = cron_setup.reconcile_cron_jobs()

    creates = [c for c in rec.calls if c[0] == "create"]
    pauses = [c for c in rec.calls if c[0] == "pause"]
    assert len(creates) == 4, "4 个受管 cron 都应被创建"
    assert len(pauses) == 4, "backend 没配齐时,create 后应调 pause_job"
    assert result.get("active") is False
    assert result.get("skipped") is False


def test_reconcile_creates_crons_active_when_backend_ready(monkeypatch, tmp_path):
    """backend 配齐 model key → 只 create,不调 pause_job(正常路径)。"""
    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))

    result = cron_setup.reconcile_cron_jobs()

    creates = [c for c in rec.calls if c[0] == "create"]
    pauses = [c for c in rec.calls if c[0] == "pause"]
    assert len(creates) == 4
    assert len(pauses) == 0, "backend 配齐时,不应调 pause_job"
    assert result.get("active") is True


def test_reconcile_update_pauses_existing_cron_when_backend_not_ready(monkeypatch):
    """已有 cron 是 active,backend 突然没配齐 → update 后调 pause_job(自动 pause)。"""
    existing = [
        {"id": "abc-123", "name": "[miloco:home-profile] miloco-perception-digest"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(False))

    cron_setup.reconcile_cron_jobs()

    updates = [c for c in rec.calls if c[0] == "update"]
    pauses = [c for c in rec.calls if c[0] == "pause"]
    assert len(updates) == 1
    assert len(pauses) == 4, "backend 没配齐时,已有+新建的全部 cron 都 pause"


def test_reconcile_resumes_existing_paused_cron_when_backend_ready(monkeypatch):
    """【L1 守门补】已有 cron 是 state=paused,backend 配齐 → 调 resume_job 真正激活。

    关键:hermes cron 有独立 state 字段(state=paused / running / scheduled),
    update_job 只改 enabled 不改 state。所以 enabled=True 但 state=paused 的 job
    不会真正跑。L1 守门要真激活必须再调 resume_job()。
    """
    # 只放一个 paused 的 job,其他 3 个是 scheduled(避免意外 resume 全部)
    existing = [
        {"id": "abc-123", "name": "[miloco:home-profile] miloco-perception-digest",
         "state": "paused"},
        {"id": "def-456", "name": "[miloco:home-profile] miloco-home-patrol",
         "state": "scheduled"},
        {"id": "ghi-789", "name": "[miloco:home-profile] miloco-home-dreaming",
         "state": "scheduled"},
        {"id": "jkl-012", "name": "[miloco:home-profile] miloco-habit-suggest",
         "state": "scheduled"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))

    result = cron_setup.reconcile_cron_jobs()

    updates = [c for c in rec.calls if c[0] == "update"]
    resumes = [c for c in rec.calls if c[0] == "resume"]
    assert len(updates) == 4
    assert len(resumes) == 1, "只有 1 个 paused → 只有 1 个 resume"
    assert resumes[0][1] == "abc-123"
    assert result.get("resumed") == 1


def test_reconcile_no_resume_when_not_paused(monkeypatch):
    """cron state 不是 paused(已是 scheduled/running)→ 不调 resume_job(避免无谓操作)。"""
    existing = [
        {"id": "abc-123", "name": "[miloco:home-profile] miloco-perception-digest",
         "state": "scheduled"},
        {"id": "def-456", "name": "[miloco:home-profile] miloco-home-patrol",
         "state": "scheduled"},
        {"id": "ghi-789", "name": "[miloco:home-profile] miloco-home-dreaming",
         "state": "scheduled"},
        {"id": "jkl-012", "name": "[miloco:home-profile] miloco-habit-suggest",
         "state": "scheduled"},
    ]
    rec = _Recording(existing=existing)
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")
    monkeypatch.setattr(cron_setup, "_check_backend_ready", _stub_check_backend_ready(True))

    cron_setup.reconcile_cron_jobs()

    resumes = [c for c in rec.calls if c[0] == "resume"]
    assert len(resumes) == 0, "4 个 scheduled cron 都不需再 resume"


def test_reconcile_force_env_overrides_backend_check(monkeypatch, tmp_path):
    """MILOCO_FORCE_CRON_ENABLED=1 → 跳过 .env 检测,创 active cron(用于压测/调试)。

    不能 mock 整个 _check_backend_ready(那会跳过 env 检查),只能 mock 它的
    依赖(shutil.which / subprocess.run)返空,让 function 自然返 False。
    然后 setenv MILOCO_FORCE_CRON_ENABLED=1 → _is_force_enabled 返 True → override 生效。
    """
    from types import SimpleNamespace
    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/miloco-cli")
    monkeypatch.setattr("subprocess.run", fake_run)  # 自然返空 key → backend NOT ready
    monkeypatch.setenv("MILOCO_FORCE_CRON_ENABLED", "1")  # 但 env 强制 enabled

    rec = _Recording(existing=[])
    monkeypatch.setattr(cron_setup, "_import_cron_jobs", _stub_import_cron_jobs(rec))
    monkeypatch.setattr(cron_setup, "get_deliver_target", lambda ctx=None: "weixin:abc")

    result = cron_setup.reconcile_cron_jobs()

    creates = [c for c in rec.calls if c[0] == "create"]
    pauses = [c for c in rec.calls if c[0] == "pause"]
    assert len(creates) == 4
    assert len(pauses) == 0, "override env 跳过检测,不应调 pause_job"
    assert result.get("active") is True


def test_check_backend_ready_detects_empty_key(monkeypatch):
    """miloco-cli 返空字符串 → backend NOT ready(没配 key)。"""
    from types import SimpleNamespace
    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/miloco-cli")
    monkeypatch.setattr("subprocess.run", fake_run)
    # 【hermes-pr.md §五 #12 准备】override 在函数内读,monkeypatch.setenv 生效
    monkeypatch.delenv("MILOCO_FORCE_CRON_ENABLED", raising=False)
    ready = cron_setup._check_backend_ready()
    assert ready is False, "空 key 返 False"


def test_check_backend_ready_detects_real_key(monkeypatch):
    """miloco-cli 返非空 key → backend ready。"""
    from types import SimpleNamespace
    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=0, stdout='"sk-abc123456"', stderr="")
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/miloco-cli")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.delenv("MILOCO_FORCE_CRON_ENABLED", raising=False)
    ready = cron_setup._check_backend_ready()
    assert ready is True, "非空 key 返 True"


def test_check_backend_ready_handles_miloco_cli_missing(monkeypatch):
    """miloco-cli 不在 PATH → 降级 ready=True(让 cron 创出来,后续触发时再诊断)。"""
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.delenv("MILOCO_FORCE_CRON_ENABLED", raising=False)
    ready = cron_setup._check_backend_ready()
    assert ready is True, "miloco-cli 缺失时降级 True"
