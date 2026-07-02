"""miloco 受管 cron job 的 reconcile 行为测试。

关键防回归点（PR #279 reviewer critical bug）：

- ``deliver`` 参数不能硬编码成 ``"all"``，因为 ``Platform("all")`` 不是合法
  的 ``Platform`` enum 成员——``DeliveryTarget.parse("all")`` 会抛
  ``ValueError`` 然后回落到 ``Platform.LOCAL``，导致所有 cron job 输出去本地
  markdown 而不是 IM 推送。
- ``deliver`` 应该读 ``state.json::deliver.target``（install-hermes.sh 在安装
  时由 detect_im_platforms.py 自动写入），不是字面量 ``"all"``。

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


def _stub_import_cron_jobs(rec: _Recording):
    """返回 lambda，匹配 cron_setup._import_cron_jobs 的 (create, list, update, remove) 返回元组。"""
    funcs = (rec.make_create(), rec.make_list(), rec.make_update(), rec.make_remove())
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
