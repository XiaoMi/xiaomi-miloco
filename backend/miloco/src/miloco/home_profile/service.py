# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""home_profile service — 业务逻辑（从 plugin store.ts 下沉）。

权重衰减 / 提升判定 / candidate·profile op / commit / reassign 全部移到此处。
所有写/commit 在 store.file_lock() 内做「读-改-写」串行化（R2）。
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import date

from miloco.config import get_settings
from miloco.home_profile import store
from miloco.home_profile.constants import (
    DECAY,
    DEFAULT_DECAY,
    LIMITS,
    PROMOTE,
    SOURCE_BONUS,
    USER_TOLD_FLOOR,
)
from miloco.home_profile.render import estimate_tokens, render_profile_markdown
from miloco.home_profile.schema import (
    CandidateOp,
    CandidatesIndex,
    Entry,
    EntryEdit,
    ImportBody,
    OpResult,
    ProfileEntry,
    ProfileIndex,
    ProfileOp,
    ReassignMapping,
    ResetBody,
)

logger = logging.getLogger(__name__)


# ─── 日期 / id 工具 ──────────────────────────────────────────────────────────


def _today() -> str:
    return date.today().isoformat()


def _days_since(date_str: str, *, now: str | None = None) -> int:
    ref = date.fromisoformat(now) if now else date.today()
    then = date.fromisoformat(date_str)
    return (ref - then).days


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)


def _max_date(a: str, b: str) -> str:
    return a if a >= b else b


def _generate_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# ─── 纯算法（可独立单测）──────────────────────────────────────────────────────


def calculate_weight(entry: ProfileEntry, *, now: str | None = None) -> float:
    cfg = DECAY.get(entry.type, DEFAULT_DECAY)
    floor = max(cfg.floor, USER_TOLD_FLOOR) if entry.source == "user_told" else cfg.floor
    recency = max(floor, 2 ** (-_days_since(entry.last_seen, now=now) / cfg.half_life))
    source_bonus = SOURCE_BONUS.get(entry.source, 1.0)
    evidence = math.log2(entry.evidence_count + 1)
    return source_bonus * evidence * recency


def get_ready_to_promote(candidates: CandidatesIndex) -> list[str]:
    out: list[str] = []
    for e in candidates.entries:
        if e.confidence >= PROMOTE["min_confidence"]:
            out.append(e.id)
        elif (
            e.evidence_count >= PROMOTE["min_evidence"]
            and _days_between(e.first_seen, e.last_seen) >= PROMOTE["min_span_days"]
        ):
            out.append(e.id)
    return out


# ─── 表格直编 patch（web CRUD 的 update）─────────────────────────────────────


def _apply_edit(entry: Entry, edit: EntryEdit, max_log: int) -> None:
    """按 model_fields_set 仅覆盖显式提供的字段（含置空 subject 绑定）。"""
    fields = edit.model_fields_set
    # 可置空字段：显式提供即覆盖（含 None）
    if "subject_id" in fields:
        entry.subject_id = edit.subject_id
    if "subject_name" in fields:
        entry.subject_name = edit.subject_name
    # 非空字段：仅在提供且非 None 时覆盖
    if "type" in fields and edit.type is not None:
        entry.type = edit.type
    if "content" in fields and edit.content is not None:
        entry.content = edit.content
    if "confidence" in fields and edit.confidence is not None:
        entry.confidence = edit.confidence
    if "source" in fields and edit.source is not None:
        entry.source = edit.source
    if "evidence_count" in fields and edit.evidence_count is not None:
        entry.evidence_count = edit.evidence_count
    if "first_seen" in fields and edit.first_seen is not None:
        entry.first_seen = edit.first_seen
    if "last_seen" in fields and edit.last_seen is not None:
        entry.last_seen = edit.last_seen
    if "evidence_log" in fields and edit.evidence_log is not None:
        entry.evidence_log = list(edit.evidence_log[:max_log])


# ─── candidate op ────────────────────────────────────────────────────────────


def _execute_candidate_op(op: CandidateOp, candidates: CandidatesIndex) -> OpResult:
    max_log = LIMITS["max_evidence_log"]
    if op.op == "add":
        if not op.entry:
            return OpResult(op="add", id="", ok=False, message="entry required for add")
        new_id = _generate_id("c")
        candidates.entries.append(
            Entry(
                id=new_id,
                type=op.entry.type,
                subject_id=op.entry.subject_id,
                subject_name=op.entry.subject_name,
                content=op.entry.content,
                confidence=op.entry.confidence,
                source=op.entry.source,
                evidence_count=1,
                first_seen=op.date,
                last_seen=op.date,
                evidence_log=list(op.entry.evidence_log[:max_log]),
            )
        )
        return OpResult(op="add", id=new_id, ok=True)

    if op.op == "merge":
        if not op.id:
            return OpResult(op="merge", id="", ok=False, message="id required for merge")
        target = next((e for e in candidates.entries if e.id == op.id), None)
        if not target:
            return OpResult(op="merge", id=op.id, ok=False, message="candidate not found")
        target.evidence_count += 1
        target.confidence = min(1.0, target.confidence + (op.confidence_delta or 0.1))
        target.last_seen = op.date
        if op.evidence_log:
            target.evidence_log.insert(0, op.evidence_log)
            del target.evidence_log[max_log:]
        return OpResult(op="merge", id=op.id, ok=True)

    if op.op == "update":
        if not op.id:
            return OpResult(op="update", id="", ok=False, message="id required for update")
        target = next((e for e in candidates.entries if e.id == op.id), None)
        if not target:
            return OpResult(op="update", id=op.id, ok=False, message="candidate not found")
        if not op.edit:
            return OpResult(op="update", id=op.id, ok=False, message="edit required for update")
        _apply_edit(target, op.edit, max_log)
        return OpResult(op="update", id=op.id, ok=True)

    if op.op == "delete":
        if not op.id:
            return OpResult(op="delete", id="", ok=False, message="id required for delete")
        if not any(e.id == op.id for e in candidates.entries):
            return OpResult(op="delete", id=op.id, ok=False, message="candidate not found")
        candidates.entries[:] = [e for e in candidates.entries if e.id != op.id]
        return OpResult(op="delete", id=op.id, ok=True)

    return OpResult(op=op.op, id=op.id or "", ok=False, message="unknown action")


# ─── profile op ──────────────────────────────────────────────────────────────


def _execute_profile_op(
    op: ProfileOp,
    profile: ProfileIndex,
    candidates: CandidatesIndex,
    user_edit: bool,
) -> OpResult:
    max_log = LIMITS["max_evidence_log"]
    source = next((e for e in candidates.entries if e.id == op.from_), None) if op.from_ else None
    if op.from_ and not source:
        return OpResult(op=op.op, id=op.id or "", ok=False, message=f"candidate {op.from_} not found")

    def _drop_source() -> None:
        if source:
            candidates.entries[:] = [e for e in candidates.entries if e.id != op.from_]

    if op.op == "add":
        base = source or op.entry
        if not base:
            return OpResult(op="add", id="", ok=False, message="entry or from required")
        new_id = _generate_id("p")
        entry = ProfileEntry(
            id=new_id,
            type=base.type,
            subject_id=base.subject_id,
            subject_name=base.subject_name,
            content=base.content,
            confidence=base.confidence,
            source=base.source,
            evidence_log=list((source.evidence_log if source else base.evidence_log)[:max_log]),
            archived=False,
            evidence_count=source.evidence_count if source else 1,
            first_seen=source.first_seen if source else (op.date or _today()),
            last_seen=source.last_seen if source else (op.date or _today()),
        )
        if user_edit:
            entry.source = "user_told"
            entry.confidence = 1.0
        profile.entries.append(entry)
        _drop_source()
        return OpResult(op="add", id=new_id, ok=True)

    if op.op == "merge":
        if not op.id:
            return OpResult(op="merge", id="", ok=False, message="id required")
        target = next((e for e in profile.entries if e.id == op.id), None)
        if not target:
            return OpResult(op="merge", id=op.id, ok=False, message="profile entry not found")
        if source:
            target.evidence_count += source.evidence_count
            target.confidence = min(1.0, target.confidence + 0.1)
            target.last_seen = _max_date(target.last_seen, source.last_seen)
            target.evidence_log = (source.evidence_log + target.evidence_log)[:max_log]
        else:
            op_date = op.date or _today()
            target.evidence_count += 1
            target.confidence = min(1.0, target.confidence + (op.confidence_delta or 0.1))
            target.last_seen = op_date
            if op.evidence_log:
                target.evidence_log.insert(0, op.evidence_log)
                del target.evidence_log[max_log:]
        if user_edit:
            target.source = "user_told"
            target.confidence = 1.0
        _drop_source()
        return OpResult(op="merge", id=op.id, ok=True)

    if op.op == "replace":
        if not op.id:
            return OpResult(op="replace", id="", ok=False, message="id required")
        target = next((e for e in profile.entries if e.id == op.id), None)
        if not target:
            return OpResult(op="replace", id=op.id, ok=False, message="profile entry not found")
        new_entry = source or op.entry
        if not new_entry:
            return OpResult(op="replace", id=op.id, ok=False, message="entry or from required")
        target.type = new_entry.type
        target.subject_id = new_entry.subject_id
        target.subject_name = new_entry.subject_name
        target.content = new_entry.content
        target.confidence = new_entry.confidence
        target.source = new_entry.source
        target.evidence_count = source.evidence_count if source else 1
        target.first_seen = source.first_seen if source else (op.date or _today())
        target.last_seen = source.last_seen if source else (op.date or _today())
        target.evidence_log = list((source.evidence_log if source else new_entry.evidence_log)[:max_log])
        if user_edit:
            target.source = "user_told"
            target.confidence = 1.0
        _drop_source()
        return OpResult(op="replace", id=op.id, ok=True)

    if op.op == "update":
        if not op.id:
            return OpResult(op="update", id="", ok=False, message="id required")
        if op.from_:
            return OpResult(op="update", id=op.id, ok=False, message="update does not accept 'from'")
        target = next((e for e in profile.entries if e.id == op.id), None)
        if not target:
            return OpResult(op="update", id=op.id, ok=False, message="profile entry not found")
        if not op.edit:
            return OpResult(op="update", id=op.id, ok=False, message="edit required for update")
        _apply_edit(target, op.edit, max_log)
        # 用户亲手改了内容：旧证据不再支撑新表述，重置计数与证据日志，避免计数失真。
        # 仅 user_edit（web 直编）且 content 被显式提供时触发；agent/omni（user_edit=False）
        # 及「关联成员」（只改 subject、不带 content）行为不变。用户若显式给了 evidence_count
        # 则尊重，不覆盖。
        edited = op.edit.model_fields_set
        if (
            user_edit
            and "content" in edited
            and op.edit.content is not None
            and "evidence_count" not in edited
        ):
            today = op.date or _today()
            target.evidence_count = 1
            target.evidence_log = []
            target.first_seen = today
            target.last_seen = today
        if user_edit:
            target.source = "user_told"
            target.confidence = 1.0
        return OpResult(op="update", id=op.id, ok=True)

    if op.op == "delete":
        if not op.id:
            return OpResult(op="delete", id="", ok=False, message="id required")
        if op.from_:
            return OpResult(op="delete", id=op.id, ok=False, message="delete does not accept 'from'")
        if not any(e.id == op.id for e in profile.entries):
            return OpResult(op="delete", id=op.id, ok=False, message="profile entry not found")
        profile.entries[:] = [e for e in profile.entries if e.id != op.id]
        return OpResult(op="delete", id=op.id, ok=True)

    return OpResult(op=op.op, id=op.id or "", ok=False, message="unknown action")


# ─── 轻量过期清理（commit (a)）——可在每次 write 后顺带跑 ─────────────────────


def _light_cleanup(profile: ProfileIndex, candidates: CandidatesIndex) -> list[str]:
    expired: list[str] = []
    max_age = LIMITS["max_last_seen_days"]

    kept = []
    for e in candidates.entries:
        if e.source != "user_told" and _days_since(e.last_seen) > PROMOTE["expire_days"] and e.evidence_count < PROMOTE["min_evidence"]:
            expired.append(e.id)
            continue
        cfg = DECAY.get(e.type, DEFAULT_DECAY)
        if cfg.expirable and _days_since(e.last_seen) > max_age:
            expired.append(e.id)
            continue
        kept.append(e)
    candidates.entries[:] = kept

    kept_p = []
    for e in profile.entries:
        cfg = DECAY.get(e.type, DEFAULT_DECAY)
        # 用户明示的知识豁免过期清理，不随时间静默消失
        if cfg.expirable and e.source != "user_told" and _days_since(e.last_seen) > max_age:
            expired.append(e.id)
            continue
        kept_p.append(e)
    profile.entries[:] = kept_p
    return expired


# ─── service ─────────────────────────────────────────────────────────────────


class HomeProfileService:
    def __init__(self, person_service=None, pet_library=None):
        self._person_service = person_service
        self._pet_library = pet_library

    def _members(self) -> list[dict]:
        if not self._person_service:
            return []
        try:
            persons = self._person_service.list_persons()
        except Exception:  # noqa: BLE001
            logger.warning("home_profile: 拉取成员列表失败", exc_info=True)
            return []
        return [{"id": p.id, "name": p.name, "role": p.role} for p in persons]

    def _pets(self) -> list[dict]:
        """宠物花名册 → ``[{id, name, species}]``；pet_library 未注入时回退进程内单例。

        延迟 import pet_library，避免 home_profile 导入期连带拉起 identity 配置链。
        """
        lib = self._pet_library
        if lib is None:
            try:
                from miloco.perception.engine.identity.pet_library import (
                    get_pet_library,
                )

                lib = get_pet_library()
            except Exception:  # noqa: BLE001
                logger.warning("home_profile: 获取宠物花名册失败", exc_info=True)
                return []
        try:
            return [
                {"id": p.id, "name": p.name, "species": p.species} for p in lib.list()
            ]
        except Exception:  # noqa: BLE001
            logger.warning("home_profile: 拉取宠物列表失败", exc_info=True)
            return []

    # —— 读 ——

    def list_entries(self, target: str) -> dict:
        # 带上 evidence_log：agent 据此对已记录的证据去重，避免重复处理隔日事件。
        out: dict = {}
        if target in ("profile", "both"):
            prof = store.load_profile()
            out["profile"] = [e.model_dump() for e in prof.entries]
        if target in ("candidates", "both"):
            cand = store.load_candidates()
            out["candidates"] = [e.model_dump() for e in cand.entries]
            out["ready_to_promote"] = get_ready_to_promote(cand)
        return out

    # —— 写 ——

    def candidate_write(self, ops: list[CandidateOp]) -> list[OpResult]:
        with store.file_lock():
            candidates = store.load_candidates()
            results = [_execute_candidate_op(op, candidates) for op in ops]
            profile = store.load_profile()
            _light_cleanup(profile, candidates)
            store.save_candidates(candidates)
            store.save_profile(profile)
        return results

    def profile_write(self, ops: list[ProfileOp], user_edit: bool) -> list[OpResult]:
        with store.file_lock():
            profile = store.load_profile()
            candidates = store.load_candidates()
            results = [_execute_profile_op(op, profile, candidates, user_edit) for op in ops]
            _light_cleanup(profile, candidates)
            store.save_profile(profile)
            store.save_candidates(candidates)
        return results

    def commit(self) -> dict:
        with store.file_lock():
            return self._commit_locked()

    def _commit_locked(self) -> dict:
        """commit 主体，调用方须已持有 store.file_lock()（避免重入死锁）。"""
        profile = store.load_profile()
        candidates = store.load_candidates()
        changes = {"expired": [], "archived": [], "activated": []}

        changes["expired"].extend(_light_cleanup(profile, candidates))

        # 候选硬上限：保留最近
        if len(candidates.entries) > LIMITS["max_candidates"]:
            candidates.entries.sort(key=lambda e: e.last_seen, reverse=True)
            removed = candidates.entries[LIMITS["max_candidates"]:]
            del candidates.entries[LIMITS["max_candidates"]:]
            changes["expired"].extend(e.id for e in removed)

        members = self._members()
        members_by_id = {m["id"]: m for m in members if m.get("id")}
        pets = self._pets()
        pets_by_id = {p["id"]: p for p in pets if p.get("id")}
        pet_ids = set(pets_by_id)
        pet_id_by_name = {
            p["name"]: p["id"] for p in pets if p.get("name") and p.get("id")
        }
        # 成员 / 宠物改名后 subject_name 自动纠偏（只取 name，不取 role）；宠物旧数据
        # （#298 留空 subject_id）按名收敛到 pet_id。属数据维护，无论 pet_recognition
        # 是否开启都执行，保证再次开启时数据一致；person_service / 花名册不可用时保留原值。
        for index in (profile.entries, candidates.entries):
            for e in index:
                if e.subject_id and e.subject_id in members_by_id:
                    e.subject_name = members_by_id[e.subject_id].get("name") or e.subject_name
                elif e.subject_id and e.subject_id in pets_by_id:
                    e.subject_name = pets_by_id[e.subject_id].get("name") or e.subject_name
                elif not e.subject_id and e.subject_name in pet_id_by_name:
                    e.subject_id = pet_id_by_name[e.subject_name]

        # pet_recognition 关闭时：宠物条目从渲染中剔除（隐藏、json 数据保留），实现 D4
        # 「软关闭」——与下面「已建任务源条目剔除」同机制（仅过滤渲染、不动存储）。
        pet_enabled = get_settings().features.pet_recognition
        render_pets = pets if pet_enabled else []

        def _is_pet_entry(e) -> bool:
            return bool(e.subject_id and e.subject_id in pet_ids) or (
                not e.subject_id and e.subject_name in pet_id_by_name
            )

        # 已建成任务的源条目从渲染中剔除：习惯已被显式任务接管，不再当习惯重复展示。
        # 必须在二分查找前就排除（而非 render 内部过滤），否则前缀 token 漏算——参见
        # render.py 关于「过滤由调用方负责」的说明。条目仍完整保存在 profile.json。
        excluded = store.load_task_created_item_ids()
        renderable = [
            e
            for e in profile.entries
            if e.id not in excluded and (pet_enabled or not _is_pet_entry(e))
        ]

        # 按权重排序
        weighted = sorted(renderable, key=lambda e: calculate_weight(e), reverse=True)

        # token 截断（render-based 测量，二分查找最大前缀）
        max_tokens = LIMITS["max_profile_tokens"]
        max_active = len(weighted)
        if estimate_tokens(render_profile_markdown(weighted, members, render_pets)) > max_tokens:
            lo, hi = 0, len(weighted)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                rendered = render_profile_markdown(weighted[:mid], members, render_pets)
                if estimate_tokens(rendered) <= max_tokens:
                    lo = mid
                else:
                    hi = mid - 1
            max_active = lo

        # 档案硬上限：移除超限的最低权重
        if len(weighted) > LIMITS["max_profile_entries"]:
            removed = weighted[LIMITS["max_profile_entries"]:]
            removed_ids = {r.id for r in removed}
            profile.entries[:] = [e for e in profile.entries if e.id not in removed_ids]
            changes["expired"].extend(removed_ids)
            weighted = weighted[: LIMITS["max_profile_entries"]]

        # 归档 / 激活
        for i, entry in enumerate(weighted):
            should_active = i < max_active
            if should_active and entry.archived:
                entry.archived = False
                changes["activated"].append(entry.id)
            elif not should_active and not entry.archived:
                entry.archived = True
                changes["archived"].append(entry.id)

        store.save_profile(profile)
        store.save_candidates(candidates)

        active = [e for e in renderable if not e.archived]
        store.save_rendered_md(render_profile_markdown(active, members, render_pets))

        return {
            "changes": changes,
            "stats": {
                "profile_active": len(active),
                "profile_archived": len(renderable) - len(active),
                "candidates_total": len(candidates.entries),
            },
        }

    def reassign_subject(self, mappings: list[ReassignMapping]) -> list[dict]:
        with store.file_lock():
            profile = store.load_profile()
            candidates = store.load_candidates()
            results: list[dict] = []
            for m in mappings:
                count = 0
                for index in (profile.entries, candidates.entries):
                    for e in index:
                        hit = (
                            (e.subject_id is not None and e.subject_id in m.from_subject_ids)
                            or (e.subject_name is not None and e.subject_name in m.from_subject_names)
                        )
                        if hit:
                            if m.to_subject_id is not None:
                                e.subject_id = m.to_subject_id
                            if m.to_subject_name is not None:
                                e.subject_name = m.to_subject_name
                            count += 1
                results.append(
                    {
                        "to_subject_id": m.to_subject_id,
                        "to_subject_name": m.to_subject_name,
                        "count": count,
                    }
                )
            store.save_profile(profile)
            store.save_candidates(candidates)
            return results

    def remove_subject(self, subject_id: str) -> dict:
        """删除某成员后，移除候选区+正式区里所有 subject_id 命中的条目，并重渲染 md。

        被删成员若不清理：render 解析不到 subject_id 会回落到陈旧 subject_name，
        web 也会把条目「漂移」到家庭档案面板而非消失。故连数据带 md 一并清。
        """
        with store.file_lock():
            profile = store.load_profile()
            candidates = store.load_candidates()
            removed_p = [e.id for e in profile.entries if e.subject_id == subject_id]
            removed_c = [e.id for e in candidates.entries if e.subject_id == subject_id]
            profile.entries[:] = [e for e in profile.entries if e.subject_id != subject_id]
            candidates.entries[:] = [
                e for e in candidates.entries if e.subject_id != subject_id
            ]
            store.save_profile(profile)
            store.save_candidates(candidates)
            self._commit_locked()
            return {"removed_profile": removed_p, "removed_candidates": removed_c}

    def import_data(self, body: ImportBody) -> dict:
        """整体导入旧结构条目，保留 id/时间戳/evidence/confidence/archived。"""
        with store.file_lock():
            profile = ProfileIndex(entries=list(body.profile))
            candidates = CandidatesIndex(entries=list(body.candidates))
            store.save_profile(profile)
            store.save_candidates(candidates)
            return {
                "profile_imported": len(profile.entries),
                "candidates_imported": len(candidates.entries),
            }

    def reset(self, body: ResetBody) -> dict:
        """测试场景重置：全量覆盖候选区+正式区，默认随即 commit 渲染 md。

        覆盖与 commit 在同一把锁内完成，保证重置后落盘的 md 与数据一致。
        """
        with store.file_lock():
            store.save_profile(ProfileIndex(entries=list(body.profile)))
            store.save_candidates(CandidatesIndex(entries=list(body.candidates)))
            result: dict = {
                "profile_imported": len(body.profile),
                "candidates_imported": len(body.candidates),
            }
            if body.commit:
                result["commit"] = self._commit_locked()
        return result

    def rendered(self) -> str:
        return store.read_rendered_md()
