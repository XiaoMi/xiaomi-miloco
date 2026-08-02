"""习惯建议状态库（防骚扰状态机）——`habit` 命令的落地实现。

背景：每日 10 点的 isolated cron（扫描 agent）从家庭档案识别"值得建成任务的习惯"，
主动 IM 推荐；用户在主 IM session（回应 agent，与扫描 agent 不共享上下文）认可后
加载 miloco-create-task 建任务。两个 agent 通过本库的持久状态衔接。

设计核心：**让工具成为防骚扰的权威**——"同一时刻至多 1 条待回应 / 每天至多 1 条新推 /
拒绝永不再问 / 超 7 天没回应作废" 这些闸门都由本模块裁定并拒绝越界写入，不依赖
扫描 agent 自觉。`asked` 严格等价"已确认送达"：扫描 agent 必须先 record(pending)，
IM 推送确认送达（ok:true）之后才能 mark_asked，杜绝"通知超时却把状态翻成
asked → 静默死锁 7 天"或"未送达却次日重复打扰"。

身份（key）由扫描 agent 自己起的稳定语义 slug 决定——"是不是同一个习惯"交给 agent 判断
（任意语言皆可），本模块不做规则匹配，只按 exact key 幂等 upsert + 拒绝复活终态条目。

历史：本状态机原实现于 openclaw TS 插件（进程内 mutex + 原子写）；现移入 miloco-cli，
读写同一个 `$MILOCO_HOME/home-profile/task-suggestions.json`，并发由 **fcntl 文件锁** 兜底
（扫描会话与回应会话不再共进程）。openclaw 侧只保留 prompt 注入的只读 reader。
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from miloco_cli.config import atomic_write, miloco_home
from miloco_cli.deploy_tz import deploy_timezone

# ─── 常量（节奏=克制，硬编码） ────────────────────────────────────────────

STORE_VERSION = 1
# 同一时刻最多几条待回应（占用"待回应位"）。
MAX_OPEN_QUESTIONS = 1
# 每个部署时区日历日最多新推几条。
MAX_NEW_ASK_PER_DAY = 1
# asked 超过这么多天没回应 → 过期（释放待回应位；未达 MAX_ASKS 则下次扫描复活重推）。
# 本模块是写侧权威；openclaw injection.ts 的只读注入镜像了同一 7 天口径（STALE_MS），
# 改这里务必同步 plugins/openclaw/src/home-profile/injection.ts。
STALE_DAYS = 7
STALE_MS = STALE_DAYS * 86_400_000
# 同一条建议累计最多主动询问几次；问满仍无果（无回应 / 未建成）即永久放弃、不再复活重推。
MAX_ASKS = 3


# ─── 时间（部署时区，与 backend / openclaw 同源） ────────────────────────────


def now_local_iso(tz: tzinfo | None = None) -> str:
    """当前时刻的部署时区 ISO 字符串，后缀带动态偏移（如 ``+08:00``）。

    与 openclaw ``nowLocalIso`` 同格式，历史 store 文件可无缝解析。``tz`` 供
    ``apply_habit_action`` 一次解析后下传，省去重复的 deploy_timezone()/config 读盘。
    """
    return datetime.now(tz or deploy_timezone()).isoformat(timespec="seconds")


def _parse(iso: str, tz: tzinfo | None = None) -> datetime | None:
    if not isinstance(iso, str):
        return None
    s = iso.strip()
    # datetime.fromisoformat 在 Python 3.10 不认 "Z"（3.11 才支持）；显式归一化，
    # 兼容跨工具/历史写入的 UTC "Z" 时间戳，避免解析失败被当成 elapsed=0 永不过期。
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 历史/异常写入的 naive 值：按部署时区解读，避免 astimezone 落到系统本地。
        dt = dt.replace(tzinfo=tz or deploy_timezone())
    return dt


def local_date_key(iso: str, tz: tzinfo | None = None) -> str:
    """部署时区视角的日历日 key（YYYY-MM-DD），用于"今天是否已问过"。"""
    tz = tz or deploy_timezone()
    dt = _parse(iso, tz)
    if dt is None:
        return ""
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _elapsed_ms(from_iso: str, now_iso: str, tz: tzinfo | None = None) -> float:
    a = _parse(from_iso, tz)
    b = _parse(now_iso, tz)
    if a is None or b is None:
        return 0
    return (b - a).total_seconds() * 1000


# ─── 纯函数（便于测试） ─────────────────────────────────────────────────────


def apply_expiry(store: dict, now_iso: str, tz: tzinfo | None = None) -> bool:
    """惰性过期：无明确回应的在途条目超 7 天 → expired。返回是否有变更。

    - ``asked`` 超 7 天：用户始终没回应（按 asked_at 判龄）。
    - ``accepted`` 超 7 天：用户答应了但任务始终没建成（按 resolved_at=接受时刻判龄）。

    expired **非永久终态**：释放待回应位，且下次扫描 record 同 key 会复活为 pending 重新推荐
    （只有用户明确 rejected / 已 created 才永久不再提）。
    """
    changed = False
    for e in store["entries"]:
        status = e.get("status")
        if status == "asked":
            stamp = e.get("asked_at")
        elif status == "accepted":
            stamp = e.get("resolved_at")
        else:
            stamp = None
        if stamp and _elapsed_ms(stamp, now_iso, tz) > STALE_MS:
            e["status"] = "expired"
            e["resolved_at"] = now_iso
            e["reason"] = f"{STALE_DAYS} 天无明确回应自动过期（可重新推荐）"
            e["updated_at"] = now_iso
            changed = True
    return changed


def _asked_today(store: dict, now_iso: str, tz: tzinfo | None = None) -> bool:
    today = local_date_key(now_iso, tz)
    return any(
        e.get("asked_at") and local_date_key(e["asked_at"], tz) == today
        for e in store["entries"]
    )


def _open_count(store: dict) -> int:
    return sum(1 for e in store["entries"] if e.get("status") == "asked")


def can_ask_now(
    store: dict, now_iso: str, tz: tzinfo | None = None
) -> tuple[bool, str | None]:
    """此刻是否还能发起新询问（待回应位未满 + 今天还没问过）。返回 ``(can, reason)``。"""
    if _open_count(store) >= MAX_OPEN_QUESTIONS:
        return False, "已有待回应的建议，本次不再打扰"
    if MAX_NEW_ASK_PER_DAY > 0 and _asked_today(store, now_iso, tz):
        return False, "今天已经推荐过一条，明天再说"
    return True, None


# ─── 存取（fcntl 文件锁 + 原子写） ───────────────────────────────────────────


def store_path() -> Path:
    """习惯建议候选库路径：``$MILOCO_HOME/home-profile/task-suggestions.json``。

    与 Python 端管理的 profile.json / candidates.json / profile.md 同目录但文件名独立，
    互不干扰；openclaw prompt 注入以只读方式读取同一文件。
    """
    return miloco_home() / "home-profile" / "task-suggestions.json"


def _lock_path() -> Path:
    """与 backend home_profile 写侧共用同一把目录锁（``home-profile/.lock``）。

    backend 当前只**读** task-suggestions.json（无锁读安全），但共用一把目录锁能确保：
    未来若有任何 backend 路径改为写 task-suggestions.json，它与本命令天然互斥，
    不会因为各持不同锁文件（不同 inode）而丢更新。
    """
    return miloco_home() / "home-profile" / ".lock"


@contextmanager
def _exclusive_lock():
    lp = _lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    # append 模式：不存在则创建、存在则不截断（.lock 无内容，纯做 flock 载体）。
    with open(lp, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_store() -> dict:
    path = store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return {"version": raw.get("version", STORE_VERSION), "entries": raw["entries"]}
    return {"version": STORE_VERSION, "entries": []}


def save_store(store: dict) -> None:
    atomic_write(store_path(), store)


# ─── action 实现 ───────────────────────────────────────────────────────────


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _compact(d: dict) -> dict:
    """删去值为 None 的键（对齐 TS：JSON.stringify 省略 undefined，保留 false/""）。"""
    return {k: v for k, v in d.items() if v is not None}


def _view(e: dict) -> dict:
    return _compact(
        {
            "key": e.get("key"),
            "title": e.get("title"),
            "subject": e.get("subject"),
            "habit": e.get("habit"),
            "suggestion": e.get("suggestion"),
            "status": e.get("status"),
            "asked_at": e.get("asked_at"),
            "task_id": e.get("task_id"),
            "item_id": e.get("item_id"),
        }
    )


def _apply_record_fields(
    e: dict, title: str, subject: str, habit: str, suggestion: str, p: dict, now: str
) -> None:
    """把 record 传入的展示字段写进既有条目（复活 / pending 刷新共用）。

    evidence/item_id 走"新值 or 保留旧值"；两者都无则删键，避免持久化出 null
    （与新建条目的 _compact 省略语义一致，杜绝同为 pending 却两种形状）。
    """
    e["title"] = title
    e["subject"] = subject
    e["habit"] = habit
    e["suggestion"] = suggestion
    for field in ("evidence", "item_id"):
        val = _str(p.get(field)) or e.get(field)
        if val:
            e[field] = val
        else:
            e.pop(field, None)
    e["updated_at"] = now


def _do_list(store: dict, now: str, tz: tzinfo | None, p: dict) -> tuple[dict, bool]:
    can, reason = can_ask_now(store, now, tz)
    open_q = [e for e in store["entries"] if e.get("status") == "asked"]
    pending = [e for e in store["entries"] if e.get("status") == "pending"]
    counts: dict[str, int] = {}
    for e in store["entries"]:
        s = e.get("status", "")
        counts[s] = counts.get(s, 0) + 1
    return (
        _compact(
            {
                "ok": True,
                "can_ask_now": can,
                "blocked_reason": reason,
                # 回应 agent 用：用户在回应哪条
                "open_questions": [_view(e) for e in open_q],
                # 扫描 agent 用：可挑 1 条去询问
                "askable_pending": [_view(e) for e in pending],
                # 全量条目（含已拒绝/已建/已作废）——据此判断"是不是同一个习惯"、复用 key、跳过终态
                "entries": [_view(e) for e in store["entries"]],
                "counts": counts,
            }
        ),
        False,
    )


def _do_record(store: dict, now: str, tz: tzinfo | None, p: dict) -> tuple[dict, bool]:
    key = _str(p.get("key"))
    subject = _str(p.get("subject")) or "shared"
    habit = _str(p.get("habit"))
    suggestion = _str(p.get("suggestion"))
    title = _str(p.get("title")) or habit[:24]
    if not key or not habit or not suggestion:
        return {"ok": False, "error": "record 需要 key / habit / suggestion"}, False

    existing = next((e for e in store["entries"] if e.get("key") == key), None)
    if existing:
        status = existing.get("status")
        # 命中既有 key（由 agent 判断为同一习惯），按状态分三类处理，永不新建副本：
        # 1) rejected / created：永久抑制——用户明确拒绝过、或已建成任务，绝不重提。
        if status in ("rejected", "created"):
            return (
                {
                    "ok": True,
                    "key": key,
                    "status": status,
                    "deduped": True,
                    "note": f"已存在且状态为 {status}，永久不再推荐",
                },
                False,
            )
        # 2) expired：无明确回应而过期。累计问满 MAX_ASKS 次仍无果 → 永久放弃、不再复活；
        #    否则复活为 pending 重新纳入推荐（保留 ask_count 作再推计数）。
        if status == "expired":
            if existing.get("ask_count", 0) >= MAX_ASKS:
                return (
                    {
                        "ok": True,
                        "key": key,
                        "status": "expired",
                        "deduped": True,
                        "note": f"已主动询问 {existing.get('ask_count', 0)} 次仍无果，放弃、不再推荐",
                    },
                    False,
                )
            existing["status"] = "pending"
            # 删键而非置 None：复活后的 pending 与新建 pending 结构一致，不残留 null。
            for stale in ("asked_at", "resolved_at", "reason"):
                existing.pop(stale, None)
            _apply_record_fields(existing, title, subject, habit, suggestion, p, now)
            return (
                {
                    "ok": True,
                    "key": key,
                    "status": "pending",
                    "deduped": True,
                    "revived": True,
                    "note": f"过期未答复，已重新纳入推荐候选（将是第 {existing.get('ask_count', 0) + 1} 次询问，上限 {MAX_ASKS}）",
                },
                True,
            )
        # 3) pending / asked / accepted：在途，不打扰；仅 pending 刷新展示字段。
        dirty = False
        if status == "pending":
            _apply_record_fields(existing, title, subject, habit, suggestion, p, now)
            dirty = True
        return (
            {
                "ok": True,
                "key": key,
                "status": status,
                "deduped": True,
                "note": (
                    "已存在待处理候选（已刷新）"
                    if status == "pending"
                    else f"已存在且状态为 {status}"
                ),
            },
            dirty,
        )

    entry = _compact(
        {
            "key": key,
            "title": title,
            "subject": subject,
            "habit": habit,
            "suggestion": suggestion,
            "evidence": _str(p.get("evidence")) or None,
            "item_id": _str(p.get("item_id")) or None,
            "status": "pending",
            "ask_count": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    store["entries"].append(entry)
    return {"ok": True, "key": key, "status": "pending", "deduped": False}, True


def _do_mark_asked(store: dict, now: str, tz: tzinfo | None, p: dict) -> tuple[dict, bool]:
    key = _str(p.get("key"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"ok": False, "error": "找不到该建议 key"}, False
    st = e.get("status")
    if st != "pending":
        return (
            {"ok": False, "status": st, "error": f"状态为 {st}，不能标记为已询问"},
            False,
        )
    can, reason = can_ask_now(store, now, tz)
    if not can:
        return {"ok": False, "blocked_reason": reason, "error": reason}, False
    e["status"] = "asked"
    e["asked_at"] = now
    e["updated_at"] = now
    e["ask_count"] = e.get("ask_count", 0) + 1
    return {"ok": True, "key": key, "status": "asked"}, True


def _do_resolve(store: dict, now: str, tz: tzinfo | None, p: dict) -> tuple[dict, bool]:
    key = _str(p.get("key"))
    outcome = _str(p.get("outcome"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"ok": False, "error": "找不到该建议 key"}, False
    frm = e.get("status")

    if outcome == "rejected":
        if frm in ("created", "expired"):
            return {"ok": False, "status": frm, "error": f"状态为 {frm}，不能拒绝"}, False
        e["status"] = "rejected"
        # 有原因才写，无则删键——不持久化 reason:null（与 revive/新建的省略语义一致）。
        reason = _str(p.get("reason"))
        if reason:
            e["reason"] = reason
        else:
            e.pop("reason", None)
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"ok": True, "key": key, "status": "rejected"}, True

    if outcome == "accepted":
        # accepted 仅从 asked 流转（注入只暴露 asked，pending→accepted 不可达）。
        if frm != "asked":
            return (
                {"ok": False, "status": frm, "error": f"状态为 {frm}，不能接受（需处于 asked）"},
                False,
            )
        e["status"] = "accepted"
        e["resolved_at"] = now
        e["updated_at"] = now
        return (
            {
                "ok": True,
                "key": key,
                "status": "accepted",
                "suggestion": e.get("suggestion"),
                "next": "加载 miloco-create-task 据此建任务；建成后再次 resolve outcome=created 并回填 task_id",
            },
            True,
        )

    if outcome == "created":
        # created 仅从 accepted（标准路径）或 asked（当轮接受并直接建好的快捷路径）流转。
        # 白名单显式排除 pending→created（从未询问过的条目不该凭空变成已建任务）及一切终态。
        if frm not in ("accepted", "asked"):
            return (
                {
                    "ok": False,
                    "status": frm,
                    "error": f"状态为 {frm}，不能标记为已建（需先 accepted，或处于 asked）",
                },
                False,
            )
        e["status"] = "created"
        # 新 task_id 或沿用既有；两者皆无则不写 key，不持久化 task_id:null。
        tid = _str(p.get("task_id")) or e.get("task_id")
        if tid:
            e["task_id"] = tid
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"ok": True, "key": key, "status": "created", "task_id": e.get("task_id")}, True

    return {"ok": False, "error": f"未知 outcome：{outcome}"}, False


_DISPATCH = {
    "list": _do_list,
    "record": _do_record,
    "mark_asked": _do_mark_asked,
    "resolve": _do_resolve,
}


def apply_habit_action(action: str, params: dict | None = None, now_override: str | None = None) -> dict:
    """核心调度（lock → load → 惰性过期 → dispatch → 按需写盘），全程持文件锁串行化。

    ``now_override`` 仅测试注入。返回与旧 openclaw tool 完全一致的结果 dict。
    """
    params = params or {}
    with _exclusive_lock():
        # 部署时区解析一次（含 config.json 读盘），下传给所有时间函数，避免按条目重复读盘。
        tz = deploy_timezone()
        now = now_override or now_local_iso(tz)
        store = load_store()
        expired = apply_expiry(store, now, tz)
        handler = _DISPATCH.get(action)
        if handler is None:
            res, dirty = {"ok": False, "error": f"未知 action：{action}"}, False
        else:
            res, dirty = handler(store, now, tz, params)
        if expired or dirty:
            save_store(store)
        # 顶层 _compact：与旧 tool 的 JSON.stringify 一致地省略 None 字段
        # （如未回填的 task_id、缺失条目的 status），杜绝 null-vs-undefined 漂移。
        return _compact(res)
