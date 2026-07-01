# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""home_profile domain 单测 —— 移植 TS store.ts 行为 + 迁移字段保真。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from miloco.home_profile import store
from miloco.home_profile.render import estimate_tokens, render_profile_markdown
from miloco.home_profile.schema import (
    CandidateOp,
    EntryEdit,
    EntryPayload,
    ImportBody,
    ProfileEntry,
    ProfileOp,
    ReassignMapping,
    ResetBody,
)
from miloco.home_profile.service import (
    HomeProfileService,
    calculate_weight,
    get_ready_to_promote,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    yield


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _payload(**kw) -> EntryPayload:
    base = dict(
        type="member_persona",
        subject_id=None,
        subject_name="爸爸",
        content="喜欢喝茶",
        confidence=0.5,
        source="observed",
        evidence_log=[],
    )
    base.update(kw)
    return EntryPayload(**base)


def _svc() -> HomeProfileService:
    return HomeProfileService(person_service=None)


# ─── weight ──────────────────────────────────────────────────────────────────


def test_calculate_weight_recent_higher_than_old():
    recent = ProfileEntry(
        id="p1", type="member_routine", content="x", confidence=0.5,
        evidence_count=3, first_seen=_today(), last_seen=_today(),
        source="observed", evidence_log=[],
    )
    old = recent.model_copy(update={"last_seen": _days_ago(180)})
    assert calculate_weight(recent) > calculate_weight(old)


def test_user_told_floor_keeps_old_entry_ranked():
    # 老旧的 user_told 偏好：floor 抬到 0.3，recency 不会衰减到接近 0
    told = ProfileEntry(
        id="p1", type="member_preference", content="喜欢24度", confidence=0.5,
        evidence_count=1, first_seen=_days_ago(800), last_seen=_days_ago(800),
        source="user_told", evidence_log=[],
    )
    observed = told.model_copy(update={"source": "observed"})
    # user_told 受 floor 保护，权重显著高于同样老旧的 observed
    assert calculate_weight(told) > calculate_weight(observed)
    # recency 下限 0.3 → 权重 = 2(bonus) * log2(2) * 0.3 = 0.6
    assert calculate_weight(told) == pytest.approx(0.6)


def test_user_told_bonus():
    observed = ProfileEntry(
        id="p1", type="family", content="x", confidence=0.5,
        evidence_count=3, first_seen=_today(), last_seen=_today(),
        source="observed", evidence_log=[],
    )
    told = observed.model_copy(update={"source": "user_told"})
    assert calculate_weight(told) == pytest.approx(2 * calculate_weight(observed))


# ─── ready to promote ──────────────────────────────────────────────────────────


def test_ready_to_promote_by_confidence_and_span():
    svc = _svc()
    # 满格 confidence(1.0) 走快速通道，单次观察即可提升
    [full] = svc.candidate_write(
        [CandidateOp(op="add", date=_today(), entry=_payload(confidence=1.0))]
    )
    # 高置信但未满格(0.95) + 单日观察：不可秒提升
    [high] = svc.candidate_write(
        [CandidateOp(op="add", date=_today(), entry=_payload(confidence=0.95))]
    )
    # 低置信但累计 ≥3 次证据且跨度 ≥2 天：满足硬门槛可提升
    [span] = svc.candidate_write(
        [CandidateOp(op="add", date=_days_ago(5), entry=_payload(confidence=0.3))]
    )
    svc.candidate_write([CandidateOp(op="merge", id=span.id, date=_days_ago(5))])
    svc.candidate_write([CandidateOp(op="merge", id=span.id, date=_today())])

    ready = get_ready_to_promote(store.load_candidates())
    assert full.id in ready
    assert high.id not in ready
    assert span.id in ready


# ─── candidate / profile ops ───────────────────────────────────────────────────


def test_candidate_add_and_merge():
    svc = _svc()
    [r] = svc.candidate_write([CandidateOp(op="add", date=_today(), entry=_payload())])
    assert r.ok and r.id.startswith("c_")
    [m] = svc.candidate_write(
        [CandidateOp(op="merge", id=r.id, date=_today(), evidence_log="又见", confidence_delta=0.2)]
    )
    assert m.ok
    e = next(x for x in store.load_candidates().entries if x.id == r.id)
    assert e.evidence_count == 2
    assert e.confidence == pytest.approx(0.7)
    assert e.evidence_log == ["又见"]


def test_candidate_op_requires_date():
    # CandidateOp.date 必填：缺省直接 Pydantic 校验失败，不回落今天
    with pytest.raises(ValidationError):
        CandidateOp(op="add", entry=_payload())


def test_profile_add_from_candidate_consumes_it():
    svc = _svc()
    [c] = svc.candidate_write([CandidateOp(op="add", date=_today(), entry=_payload())])
    [p] = svc.profile_write([ProfileOp(op="add", **{"from": c.id})], user_edit=False)
    assert p.ok and p.id.startswith("p_")
    assert all(x.id != c.id for x in store.load_candidates().entries)
    assert any(x.id == p.id for x in store.load_profile().entries)


def test_profile_user_edit_sets_user_told():
    svc = _svc()
    [p] = svc.profile_write(
        [ProfileOp(op="add", entry=_payload(confidence=0.2))], user_edit=True
    )
    e = next(x for x in store.load_profile().entries if x.id == p.id)
    assert e.source == "user_told" and e.confidence == 1.0


def test_entry_payload_confidence_source_optional():
    # confidence/source 可缺省，缺省回落 0.5/observed（observe skill 仍会显式传值）
    e = EntryPayload(type="member_preference", subject_name="羊哥", content="喜欢吃土豆丝")
    assert e.confidence == 0.5 and e.source == "observed"


def test_profile_user_edit_add_omitting_confidence_source():
    # 复现上报场景：--user-edit 写入但 entry 省略 confidence/source，
    # 不应被校验拦下，且最终由 service 覆盖成 user_told/1.0
    svc = _svc()
    entry = EntryPayload(type="member_preference", subject_name="羊哥", content="喜欢吃土豆丝")
    [p] = svc.profile_write([ProfileOp(op="add", entry=entry)], user_edit=True)
    assert p.ok
    e = next(x for x in store.load_profile().entries if x.id == p.id)
    assert e.source == "user_told" and e.confidence == 1.0


def test_candidate_add_omitting_confidence_source_falls_back():
    # 观察路径省略时兜底 0.5/observed（保持 observed，不误标 user_told）
    svc = _svc()
    entry = EntryPayload(type="member_routine", subject_name="爸爸", content="7:30 出门")
    [c] = svc.candidate_write([CandidateOp(op="add", date=_today(), entry=entry)])
    assert c.ok
    e = next(x for x in store.load_candidates().entries if x.id == c.id)
    assert e.confidence == 0.5 and e.source == "observed"


def test_profile_delete_rejects_from():
    svc = _svc()
    [p] = svc.profile_write([ProfileOp(op="add", entry=_payload())], user_edit=False)
    [r] = svc.profile_write([ProfileOp(op="delete", id=p.id, **{"from": "x"})], user_edit=False)
    assert not r.ok


# ─── 表格直编 CRUD（web）─────────────────────────────────────────────────────


def test_candidate_update_patches_only_given_fields():
    svc = _svc()
    [c] = svc.candidate_write(
        [CandidateOp(op="add", date=_today(), entry=_payload(content="旧", confidence=0.5))]
    )
    [r] = svc.candidate_write(
        [CandidateOp(op="update", id=c.id, date=_today(), edit=EntryEdit(content="新"))]
    )
    assert r.ok and r.op == "update"
    e = next(x for x in store.load_candidates().entries if x.id == c.id)
    assert e.content == "新"
    # 未提供的字段保持不变（不像 merge 那样累加证据/置信）
    assert e.confidence == pytest.approx(0.5)
    assert e.evidence_count == 1


def test_candidate_update_can_clear_subject_binding():
    svc = _svc()
    [c] = svc.candidate_write(
        [CandidateOp(op="add", date=_today(), entry=_payload(subject_id="pid-1", subject_name="爸爸"))]
    )
    svc.candidate_write(
        [CandidateOp(op="update", id=c.id, date=_today(), edit=EntryEdit(subject_id=None))]
    )
    e = next(x for x in store.load_candidates().entries if x.id == c.id)
    assert e.subject_id is None
    # 未在 edit 中出现的 subject_name 不受影响
    assert e.subject_name == "爸爸"


def test_candidate_delete():
    svc = _svc()
    [c] = svc.candidate_write([CandidateOp(op="add", date=_today(), entry=_payload())])
    [r] = svc.candidate_write([CandidateOp(op="delete", id=c.id, date=_today())])
    assert r.ok
    assert all(x.id != c.id for x in store.load_candidates().entries)


def test_candidate_update_missing_target():
    svc = _svc()
    [r] = svc.candidate_write(
        [CandidateOp(op="update", id="nope", date=_today(), edit=EntryEdit(content="x"))]
    )
    assert not r.ok


def test_profile_update_preserves_counts_unlike_replace():
    svc = _svc()
    entry = ProfileEntry(
        id="p_keep", type="member_persona", subject_name="爸爸", content="旧",
        confidence=0.42, evidence_count=7, first_seen="2025-01-01",
        last_seen=_today(), source="observed", evidence_log=["e1"],
    )
    svc.import_data(ImportBody(profile=[entry], candidates=[]))
    [r] = svc.profile_write(
        [ProfileOp(op="update", id="p_keep", edit=EntryEdit(content="新"))], user_edit=False
    )
    assert r.ok and r.op == "update"
    got = next(x for x in store.load_profile().entries if x.id == "p_keep")
    assert got.content == "新"
    # 纯 patch：计数/时间戳/置信均保留
    assert got.evidence_count == 7
    assert got.confidence == pytest.approx(0.42)
    assert got.first_seen == "2025-01-01"


def test_profile_update_rejects_from():
    svc = _svc()
    [p] = svc.profile_write([ProfileOp(op="add", entry=_payload())], user_edit=False)
    [r] = svc.profile_write(
        [ProfileOp(op="update", id=p.id, edit=EntryEdit(content="x"), **{"from": "y"})],
        user_edit=False,
    )
    assert not r.ok


# ─── commit ────────────────────────────────────────────────────────────────────


def test_commit_expires_stale_candidate():
    from miloco.home_profile.schema import Entry

    svc = _svc()
    # 经 import 直接写入（绕过 write 的轻量清理），由 commit 负责过期
    stale = Entry(
        id="c_stale", type="member_persona", subject_name="爸爸", content="x",
        confidence=0.2, evidence_count=1, first_seen=_days_ago(40),
        last_seen=_days_ago(40), source="observed", evidence_log=[],
    )
    svc.import_data(ImportBody(profile=[], candidates=[stale]))
    out = svc.commit()
    assert out["stats"]["candidates_total"] == 0
    assert "c_stale" in out["changes"]["expired"]


def test_commit_keeps_user_told_past_max_age():
    svc = _svc()
    # 800 天未再见的 user_told 习惯条目（expirable 类型），应被豁免、不过期
    told = ProfileEntry(
        id="p_told", type="member_routine", subject_name="爸爸", content="睡前看书",
        confidence=1.0, evidence_count=1, first_seen=_days_ago(800),
        last_seen=_days_ago(800), source="user_told", evidence_log=[],
    )
    observed = told.model_copy(
        update={"id": "p_obs", "source": "observed", "content": "随手关灯"}
    )
    svc.import_data(ImportBody(profile=[told, observed], candidates=[]))
    out = svc.commit()
    ids = {e.id for e in store.load_profile().entries}
    assert "p_told" in ids  # user_told 豁免过期
    assert "p_obs" not in ids and "p_obs" in out["changes"]["expired"]  # observed 过期


def test_commit_keeps_space_past_max_age():
    svc = _svc()
    # space 现为不可过期：800 天未更新的户型信息仍保留
    space = ProfileEntry(
        id="p_space", type="space", subject_name="general", content="3室1厅朝南",
        confidence=0.8, evidence_count=1, first_seen=_days_ago(800),
        last_seen=_days_ago(800), source="observed", evidence_log=[],
    )
    svc.import_data(ImportBody(profile=[space], candidates=[]))
    svc.commit()
    assert any(e.id == "p_space" for e in store.load_profile().entries)


def test_light_cleanup_on_write():
    svc = _svc()
    svc.candidate_write(
        [CandidateOp(op="add", date=_days_ago(40), entry=_payload(confidence=0.2))]
    )
    # 40 天 & evidence<3 的候选在 write 时即被轻量清理
    assert store.load_candidates().entries == []


def test_commit_renders_md():
    svc = _svc()
    svc.profile_write([ProfileOp(op="add", entry=_payload(content="爱喝普洱"))], user_edit=True)
    svc.commit()
    md = store.read_rendered_md()
    assert "家庭成员" in md and "爱喝普洱" in md


# ─── reassign ──────────────────────────────────────────────────────────────────


def test_reassign_by_name_to_id():
    svc = _svc()
    svc.profile_write(
        [ProfileOp(op="add", entry=_payload(subject_name="老王", subject_id=None))],
        user_edit=False,
    )
    [res] = svc.reassign_subject(
        [ReassignMapping(from_subject_names=["老王"], to_subject_id="pid-1", to_subject_name="老王")]
    )
    assert res["count"] == 1
    e = store.load_profile().entries[0]
    assert e.subject_id == "pid-1"


def test_reassign_name_only_keeps_bound_subject_id():
    """name-only 归并（不填 to_subject_id）撞上已绑定成员条目时，保留原 subject_id。"""
    svc = _svc()
    svc.profile_write(
        [ProfileOp(op="add", entry=_payload(subject_name="老王", subject_id="pid-1"))],
        user_edit=False,
    )
    [res] = svc.reassign_subject(
        [ReassignMapping(from_subject_names=["老王"], to_subject_name="王刚")]
    )
    assert res["count"] == 1
    e = store.load_profile().entries[0]
    assert e.subject_id == "pid-1"  # 绑定未丢
    assert e.subject_name == "王刚"  # 显示名已改


# ─── person 级联：删人 / 改名 ────────────────────────────────────────────────────


def _member_entry(eid: str, subject_id: str, name: str, content: str) -> ProfileEntry:
    return ProfileEntry(
        id=eid, type="member_persona", subject_id=subject_id, subject_name=name,
        content=content, confidence=0.8, evidence_count=2,
        first_seen=_today(), last_seen=_today(), source="observed", evidence_log=[],
    )


def test_remove_subject_drops_bound_entries():
    from miloco.home_profile.schema import Entry

    svc = _svc()
    p1 = _member_entry("p_1", "pid-1", "爸爸", "喜欢喝茶")
    p2 = _member_entry("p_2", "pid-2", "妈妈", "爱跑步")
    c1 = Entry(
        id="c_1", type="member_persona", subject_id="pid-1", subject_name="爸爸",
        content="候选条目", confidence=0.3, evidence_count=1,
        first_seen=_today(), last_seen=_today(), source="observed", evidence_log=[],
    )
    svc.import_data(ImportBody(profile=[p1, p2], candidates=[c1]))

    out = svc.remove_subject("pid-1")
    assert out["removed_profile"] == ["p_1"]
    assert out["removed_candidates"] == ["c_1"]
    prof_ids = {e.id for e in store.load_profile().entries}
    assert prof_ids == {"p_2"}
    assert store.load_candidates().entries == []
    # md 已重渲染：只剩 pid-2 的内容
    md = store.read_rendered_md()
    assert "爱跑步" in md and "喜欢喝茶" not in md


def test_commit_autocorrects_bound_subject_name():
    # 成员改名后触发 commit：已绑定 subject_id 的条目 subject_name 自动纠偏为当前 name，
    # 只取 name 不取 role，json 数据与 md 一并刷新。
    from types import SimpleNamespace

    fake_ps = SimpleNamespace(
        list_persons=lambda: [SimpleNamespace(id="pid-1", name="新名", role="爸爸")]
    )
    svc = HomeProfileService(person_service=fake_ps)
    p1 = _member_entry("p_1", "pid-1", "旧名", "喜欢喝茶")
    svc.import_data(ImportBody(profile=[p1], candidates=[]))

    svc.commit()
    e = next(x for x in store.load_profile().entries if x.id == "p_1")
    assert e.subject_name == "新名"
    md = store.read_rendered_md()
    # 分组标题取 name；role 仅出现在「家庭成员」名册区，不作分组标题
    assert "\n### 新名\n" in md and "旧名" not in md and "\n### 爸爸\n" not in md


# ─── 用户直编内容重新计数 ────────────────────────────────────────────────────────


def test_profile_user_edit_content_resets_counts():
    svc = _svc()
    entry = ProfileEntry(
        id="p_keep", type="member_persona", subject_name="爸爸", content="旧",
        confidence=0.42, evidence_count=7, first_seen="2025-01-01",
        last_seen="2025-01-01", source="observed", evidence_log=["e1", "e2"],
    )
    svc.import_data(ImportBody(profile=[entry], candidates=[]))
    [r] = svc.profile_write(
        [ProfileOp(op="update", id="p_keep", edit=EntryEdit(content="新"))],
        user_edit=True,
    )
    assert r.ok
    got = next(x for x in store.load_profile().entries if x.id == "p_keep")
    assert got.content == "新"
    # 用户改了内容 → 旧证据作废，计数/日志/时间戳重置
    assert got.evidence_count == 1
    assert got.evidence_log == []
    assert got.first_seen == _today()
    assert got.last_seen == _today()
    # user_edit 仍把来源/置信拉满
    assert got.source == "user_told"
    assert got.confidence == pytest.approx(1.0)


def test_profile_user_edit_reassign_keeps_counts():
    svc = _svc()
    entry = ProfileEntry(
        id="p_keep", type="member_persona", subject_id=None, subject_name="未知",
        content="喜欢喝茶", confidence=0.42, evidence_count=7,
        first_seen="2025-01-01", last_seen="2025-01-01", source="observed",
        evidence_log=["e1"],
    )
    svc.import_data(ImportBody(profile=[entry], candidates=[]))
    # 仅改 subject（关联成员），不带 content → 不应触发重算
    svc.profile_write(
        [ProfileOp(op="update", id="p_keep",
                   edit=EntryEdit(subject_id="pid-1", subject_name="爸爸"))],
        user_edit=True,
    )
    got = next(x for x in store.load_profile().entries if x.id == "p_keep")
    assert got.subject_id == "pid-1"
    assert got.evidence_count == 7
    assert got.evidence_log == ["e1"]
    assert got.first_seen == "2025-01-01"


# ─── import 字段保真 ────────────────────────────────────────────────────────────


def test_import_preserves_fields():
    svc = _svc()
    entry = ProfileEntry(
        id="p_keep", type="member_health", subject_id=None, subject_name="妈妈",
        content="对花粉过敏", confidence=0.87, evidence_count=5,
        first_seen="2025-01-01", last_seen="2025-02-02", source="user_told",
        evidence_log=["note1"], archived=True,
    )
    out = svc.import_data(ImportBody(profile=[entry], candidates=[]))
    assert out["profile_imported"] == 1
    got = store.load_profile().entries[0]
    assert got.id == "p_keep"
    assert got.confidence == 0.87
    assert got.evidence_count == 5
    assert got.first_seen == "2025-01-01"
    assert got.evidence_log == ["note1"]
    assert got.archived is True


# ─── reset（测试场景全量覆盖 + 自动 commit）────────────────────────────────────


def test_reset_overwrites_and_renders_md():
    svc = _svc()
    # 先放入一些旧数据
    svc.profile_write([ProfileOp(op="add", entry=_payload(content="将被覆盖"))], user_edit=True)
    svc.candidate_write([CandidateOp(op="add", date=_today(), entry=_payload(content="旧候选"))])

    new = ProfileEntry(
        id="p_new", type="member_preference", subject_name="妈妈", content="爱喝普洱",
        confidence=1.0, evidence_count=1, first_seen=_today(), last_seen=_today(),
        source="user_told", evidence_log=[],
    )
    out = svc.reset(ResetBody(profile=[new], candidates=[]))

    # 全量覆盖：旧数据清空，仅留新数据
    ids = {e.id for e in store.load_profile().entries}
    assert ids == {"p_new"}
    assert store.load_candidates().entries == []
    # 自动 commit：返回 commit 结果且 md 已渲染
    assert "commit" in out
    md = store.read_rendered_md()
    assert "爱喝普洱" in md
    assert "将被覆盖" not in md


def test_reset_can_clear_everything():
    svc = _svc()
    svc.profile_write([ProfileOp(op="add", entry=_payload())], user_edit=True)
    svc.commit()
    svc.reset(ResetBody(profile=[], candidates=[]))
    assert store.load_profile().entries == []
    assert store.load_candidates().entries == []
    # 清空后 md 不应再含旧内容
    assert "喜欢喝茶" not in store.read_rendered_md()


def test_reset_skip_commit():
    svc = _svc()
    out = svc.reset(
        ResetBody(
            profile=[
                ProfileEntry(
                    id="p1", type="family", subject_name="general", content="x",
                    confidence=0.5, evidence_count=1, first_seen=_today(),
                    last_seen=_today(), source="observed", evidence_log=[],
                )
            ],
            candidates=[],
            commit=False,
        )
    )
    assert "commit" not in out
    assert out["profile_imported"] == 1


# ─── render / token ─────────────────────────────────────────────────────────────


def test_render_includes_archived_entries_when_passed():
    # render 不再自行过滤 archived：archived 过滤由调用方负责，
    # 否则 commit 的 token 二分查找会漏算前缀中已归档条目，max_active 被高估。
    entries = [
        ProfileEntry(
            id="p1", type="member_persona", subject_id=None, subject_name="爸爸",
            content="可见内容", confidence=1.0, evidence_count=1,
            first_seen=_today(), last_seen=_today(), source="observed", evidence_log=[],
        ),
        ProfileEntry(
            id="p2", type="member_persona", subject_id=None, subject_name="爸爸",
            content="已归档内容", confidence=1.0, evidence_count=1,
            first_seen=_today(), last_seen=_today(), source="observed",
            evidence_log=[], archived=True,
        ),
    ]
    md = render_profile_markdown(entries, [])
    assert "可见内容" in md and "已归档内容" in md


def test_commit_token_budget_stable_across_recommits(monkeypatch):
    # 回归：二分查找曾因 render 内部过滤 archived 而漏算前缀 token，
    # 第 1 次 commit 归档尾部条目后，第 2 次 commit 测量时这些条目不被计入，
    # 导致过度激活、最终渲染超预算（隔次振荡）。修复后每次 commit 都不超预算。
    from miloco.home_profile.constants import LIMITS

    monkeypatch.setitem(LIMITS, "max_profile_tokens", 200)
    budget = 200

    svc = _svc()
    ops = [
        ProfileOp(
            op="add",
            entry=_payload(
                subject_name=f"成员{i:02d}",
                content="这是一条用于撑高token预算的较长家庭档案内容样本",
            ),
        )
        for i in range(12)
    ]
    svc.profile_write(ops, user_edit=True)

    out1 = svc.commit()
    assert out1["changes"]["archived"], "预期首次 commit 应归档超预算的尾部条目"
    assert estimate_tokens(store.read_rendered_md()) <= budget

    svc.commit()
    assert estimate_tokens(store.read_rendered_md()) <= budget


def test_commit_filters_items_created_as_task():
    # 已建成任务（task-suggestions.json status=created 且带 item_id）的源档案条目
    # 不再渲染进 profile.md，但仍完整保留在 profile.json。
    import json

    svc = _svc()
    svc.profile_write(
        [
            ProfileOp(op="add", entry=_payload(subject_name="王磊", content="傍晚约19点健身约30分钟")),
            ProfileOp(op="add", entry=_payload(subject_name="王磊", content="睡前听白噪音")),
        ],
        user_edit=True,
    )

    by_content = {e.content: e.id for e in store.load_profile().entries}
    fitness_id = by_content["傍晚约19点健身约30分钟"]

    store.home_profile_dir().mkdir(parents=True, exist_ok=True)
    store.task_suggestions_path().write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"key": "wanglei_fitness", "status": "created", "item_id": fitness_id, "task_id": "t1"}
                ],
            }
        ),
        encoding="utf-8",
    )

    svc.commit()
    md = store.read_rendered_md()
    assert "傍晚约19点健身约30分钟" not in md
    assert "睡前听白噪音" in md
    # 仅不渲染，条目本身不被删
    assert any(e.id == fitness_id for e in store.load_profile().entries)


def test_render_groups_by_resolved_member_name():
    members = [{"id": "pid-1", "name": "王伟", "role": "爸爸"}]
    entries = [
        ProfileEntry(
            id="p1", type="member_persona", subject_id="pid-1", subject_name="爸爸",
            content="爱喝茶", confidence=1.0, evidence_count=1,
            first_seen=_today(), last_seen=_today(), source="user_told", evidence_log=[],
        )
    ]
    md = render_profile_markdown(entries, members)
    # 统一层级：# 家庭档案(H1) / ## 分类(H2) / ### 分组(H3)
    assert md.startswith("# 家庭档案")
    assert "## 家庭成员" in md
    # 分组标题取成员当前 name（只取 name 不取 role）
    assert "\n### 王伟\n" in md
    assert "\n### 爸爸\n" not in md
    assert "id:pid-1, name:王伟, role:爸爸" in md


def test_estimate_tokens_counts_cjk_heavier():
    assert estimate_tokens("中文中文") > estimate_tokens("abcd")


# ─── 宠物（P2：花名册接合 / 渲染 / 软关闭）──────────────────────────────────────


def _pet_entry(eid: str, subject_id, subject_name: str, content: str) -> ProfileEntry:
    return ProfileEntry(
        id=eid,
        type="member_persona",
        subject_id=subject_id,
        subject_name=subject_name,
        content=content,
        confidence=1.0,
        evidence_count=1,
        first_seen=_today(),
        last_seen=_today(),
        source="user_told",
        evidence_log=[],
    )


def _stub_pet_flag(monkeypatch, enabled: bool) -> None:
    """仅替换 service 读取的 get_settings，控制 pet_recognition 开关（不污染全局缓存）。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "miloco.home_profile.service.get_settings",
        lambda: SimpleNamespace(features=SimpleNamespace(pet_recognition=enabled)),
    )


def test_render_pets_section_grouped_under_pets_heading():
    pets = [{"id": "pet_a", "name": "小黑", "species": "猫"}]
    entries = [_pet_entry("p1", "pet_a", "小黑", "中等体型黑色短毛英短猫，尾尖一撮白")]
    md = render_profile_markdown(entries, [], pets)
    assert "## 宠物" in md
    assert "\n### 小黑\n" in md
    assert "中等体型黑色短毛英短猫" in md
    # 宠物段位于家庭成员段之后；且不混进家庭成员段
    assert md.index("## 宠物") > md.index("## 家庭成员")
    assert "小黑" not in md.split("## 宠物")[0]


def test_render_pets_legacy_empty_subject_id_by_name():
    # #298 旧数据：subject_id 留空、subject_name = 宠物名 → 仍按花名册名归入宠物段
    pets = [{"id": "pet_a", "name": "小黑", "species": "猫"}]
    entries = [_pet_entry("p1", None, "小黑", "黑色短毛猫")]
    md = render_profile_markdown(entries, [], pets)
    assert "## 宠物" in md and "黑色短毛猫" in md


def test_render_no_pets_arg_backward_compat():
    # 不传 pets（旧调用）：宠物样条目回退到家庭成员段，行为不变
    entries = [_pet_entry("p1", None, "小黑", "黑色短毛猫")]
    md = render_profile_markdown(entries, [])
    assert "## 宠物" not in md
    assert "\n### 小黑\n" in md and "黑色短毛猫" in md


def test_commit_renders_pets_when_enabled(tmp_path, monkeypatch):
    from miloco.perception.engine.identity.pet_library import PetLibrary

    lib = PetLibrary(root_dir=tmp_path / "petlib")
    pet = lib.create(name="小黑", species="猫")
    svc = HomeProfileService(person_service=None, pet_library=lib)
    svc.import_data(
        ImportBody(
            profile=[_pet_entry("p_pet", pet.id, "小黑", "黑色短毛英短猫，尾尖一撮白")],
            candidates=[],
        )
    )
    _stub_pet_flag(monkeypatch, True)
    svc.commit()
    md = store.read_rendered_md()
    assert "## 宠物" in md
    assert "\n### 小黑\n" in md
    assert "黑色短毛英短猫" in md


def test_commit_hides_pets_when_disabled_but_keeps_data(tmp_path, monkeypatch):
    from miloco.perception.engine.identity.pet_library import PetLibrary

    lib = PetLibrary(root_dir=tmp_path / "petlib")
    pet = lib.create(name="小黑", species="猫")
    svc = HomeProfileService(person_service=None, pet_library=lib)
    svc.import_data(
        ImportBody(
            profile=[_pet_entry("p_pet", pet.id, "小黑", "黑色短毛英短猫")],
            candidates=[],
        )
    )
    _stub_pet_flag(monkeypatch, False)
    svc.commit()
    md = store.read_rendered_md()
    # 关闭：宠物段与外观都不渲染（隐藏）
    assert "## 宠物" not in md
    assert "黑色短毛英短猫" not in md
    # 但数据保留在 json
    assert any(e.id == "p_pet" for e in store.load_profile().entries)


def test_commit_syncs_pet_name_on_rename(tmp_path, monkeypatch):
    from miloco.perception.engine.identity.pet_library import PetLibrary

    lib = PetLibrary(root_dir=tmp_path / "petlib")
    pet = lib.create(name="小黑", species="猫")
    svc = HomeProfileService(person_service=None, pet_library=lib)
    svc.import_data(
        ImportBody(profile=[_pet_entry("p_pet", pet.id, "小黑", "黑色短毛猫")], candidates=[])
    )
    lib.update(pet.id, name="小白")  # 花名册改名
    _stub_pet_flag(monkeypatch, True)
    svc.commit()
    e = next(x for x in store.load_profile().entries if x.id == "p_pet")
    assert e.subject_name == "小白"
    md = store.read_rendered_md()
    assert "\n### 小白\n" in md and "小黑" not in md


def test_commit_backfills_legacy_pet_subject_id(tmp_path, monkeypatch):
    from miloco.perception.engine.identity.pet_library import PetLibrary

    lib = PetLibrary(root_dir=tmp_path / "petlib")
    pet = lib.create(name="小黑", species="猫")
    svc = HomeProfileService(person_service=None, pet_library=lib)
    # 旧数据：subject_id 空、subject_name 命中花名册
    svc.import_data(
        ImportBody(profile=[_pet_entry("p_legacy", None, "小黑", "黑色短毛猫")], candidates=[])
    )
    _stub_pet_flag(monkeypatch, True)
    svc.commit()
    e = next(x for x in store.load_profile().entries if x.id == "p_legacy")
    assert e.subject_id == pet.id  # 已收敛回 pet_id
