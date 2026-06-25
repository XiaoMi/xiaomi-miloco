#!/usr/bin/env python3
"""
Miloco Habit Suggest Tool — 习惯建议状态管理

action:
  list         — 列出所有建议条目及状态
  record       — 记录新建议 (key, title, habit, suggestion, [evidence])
  mark_asked   — 标记已询问 (key)
  resolve      — 解决建议 (key, outcome: accepted/rejected/created, [task_id])

状态机: pending → asked → (accepted → created) | rejected | expired

约束:
- 同时最多 1 条 asked（待回应）
- 每天最多 1 条新推荐
- asked 超 7 天未回应 → expired
- 同 key 累计询问 3 次仍无果 → 永久放弃
- rejected / created 永久终态
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── 常量 ──────────────────────────────────────────────────────────────────

STORE_VERSION = 1
MAX_OPEN_QUESTIONS = 1
MAX_NEW_ASK_PER_DAY = 1
STALE_DAYS = 7
STALE_MS = STALE_DAYS * 86_400_000
MAX_ASKS = 3
MILOCO_HOME = os.environ.get("MILOCO_HOME", os.path.expanduser("~/.openclaw/miloco"))
STORE_PATH = os.path.join(MILOCO_HOME, "home-profile", "task-suggestions.json")


# ─── 工具函数 ──────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_date_key(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        from datetime import timedelta
        dt = dt + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def elapsed_ms(from_iso: str, now: str) -> int:
    try:
        a = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return int((b - a).total_seconds() * 1000)
    except Exception:
        return 0


def load_store() -> dict:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    if not os.path.exists(STORE_PATH):
        return {"version": STORE_VERSION, "entries": []}
    try:
        with open(STORE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and "entries" in data:
            data.setdefault("version", STORE_VERSION)
            return data
    except Exception:
        pass
    return {"version": STORE_VERSION, "entries": []}


def save_store(store: dict):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def apply_expiry(store: dict, now: str) -> bool:
    changed = False
    for e in store["entries"]:
        stamp = None
        if e.get("status") == "asked":
            stamp = e.get("asked_at")
        elif e.get("status") == "accepted":
            stamp = e.get("resolved_at")
        if stamp and elapsed_ms(stamp, now) > STALE_MS:
            e["status"] = "expired"
            e["resolved_at"] = now
            e["reason"] = f"{STALE_DAYS} 天无明确回应自动过期（可重新推荐）"
            e["updated_at"] = now
            changed = True
    return changed


def can_ask_now(store: dict, now: str) -> tuple:
    open_count = sum(1 for e in store["entries"] if e.get("status") == "asked")
    if open_count >= MAX_OPEN_QUESTIONS:
        return False, "已有待回应的建议，本次不再打扰"
    today = local_date_key(now)
    asked_today = any(
        e.get("asked_at") and local_date_key(e.get("asked_at", "")) == today
        for e in store["entries"]
    )
    if MAX_NEW_ASK_PER_DAY > 0 and asked_today:
        return False, "今天已经推荐过一条，明天再说"
    return True, ""


def view_entry(e: dict) -> dict:
    return {
        "key": e.get("key"),
        "title": e.get("title"),
        "subject": e.get("subject"),
        "habit": e.get("habit"),
        "suggestion": e.get("suggestion"),
        "status": e.get("status"),
        "ask_count": e.get("ask_count", 0),
        "asked_at": e.get("asked_at"),
        "task_id": e.get("task_id"),
        "item_id": e.get("item_id"),
        "created_at": e.get("created_at"),
    }


# ─── Actions ───────────────────────────────────────────────────────────────

def do_list(store: dict, now: str) -> dict:
    can, reason = can_ask_now(store, now)
    open_qs = [view_entry(e) for e in store["entries"] if e.get("status") == "asked"]
    pending = [view_entry(e) for e in store["entries"] if e.get("status") == "pending"]
    counts = {}
    for e in store["entries"]:
        s = e.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    all_entries = [view_entry(e) for e in store["entries"]]
    return {
        "ok": True,
        "can_ask_now": can,
        "blocked_reason": reason if not can else None,
        "open_questions": open_qs,
        "askable_pending": pending,
        "entries": all_entries,
        "counts": counts,
    }


def do_record(store: dict, now: str, params: dict) -> dict:
    key = (params.get("key") or "").strip()
    title = (params.get("title") or "").strip()
    subject = (params.get("subject") or "shared").strip()
    habit = (params.get("habit") or "").strip()
    suggestion = (params.get("suggestion") or "").strip()
    if not key or not habit or not suggestion:
        return {"ok": False, "error": "record 需要 key / habit / suggestion"}
    if not title:
        title = habit[:24]

    existing = next((e for e in store["entries"] if e.get("key") == key), None)
    if existing:
        status = existing.get("status")
        if status in ("rejected", "created"):
            return {"ok": True, "key": key, "status": status, "deduped": True,
                    "note": f"已存在且状态为 {status}，永久不再推荐"}
        if status == "expired":
            if existing.get("ask_count", 0) >= MAX_ASKS:
                return {"ok": True, "key": key, "status": "expired", "deduped": True,
                        "note": f"已主动询问 {existing.get('ask_count')} 次仍无果，放弃"}
            existing["status"] = "pending"
            existing["asked_at"] = None
            existing["resolved_at"] = None
            existing["reason"] = None
            existing["title"] = title
            existing["subject"] = subject
            existing["habit"] = habit
            existing["suggestion"] = suggestion
            existing["updated_at"] = now
            return {"ok": True, "key": key, "status": "pending", "deduped": True, "revived": True,
                    "note": f"过期未答复，已重新纳入推荐候选（将是第 {existing.get('ask_count', 0) + 1} 次询问，上限 {MAX_ASKS}）"}
        if status == "pending":
            existing["title"] = title
            existing["subject"] = subject
            existing["habit"] = habit
            existing["suggestion"] = suggestion
            existing["updated_at"] = now
        return {"ok": True, "key": key, "status": status, "deduped": True,
                "note": f"已存在且状态为 {status}"}

    entry = {
        "key": key, "title": title, "subject": subject,
        "habit": habit, "suggestion": suggestion,
        "evidence": params.get("evidence"),
        "item_id": params.get("item_id"),
        "status": "pending", "ask_count": 0,
        "created_at": now, "updated_at": now,
    }
    store["entries"].append(entry)
    return {"ok": True, "key": key, "status": "pending", "deduped": False}


def do_mark_asked(store: dict, now: str, params: dict) -> dict:
    key = (params.get("key") or "").strip()
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"ok": False, "error": "找不到该建议 key"}
    if e.get("status") != "pending":
        return {"ok": False, "status": e.get("status"), "error": f"状态为 {e.get('status')}，不能标记为已询问"}
    can, reason = can_ask_now(store, now)
    if not can:
        return {"ok": False, "blocked_reason": reason, "error": reason}
    e["status"] = "asked"
    e["asked_at"] = now
    e["updated_at"] = now
    e["ask_count"] = e.get("ask_count", 0) + 1
    return {"ok": True, "key": key, "status": "asked"}


def do_resolve(store: dict, now: str, params: dict) -> dict:
    key = (params.get("key") or "").strip()
    outcome = (params.get("outcome") or "").strip()
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"ok": False, "error": "找不到该建议 key"}

    from_status = e.get("status")

    if outcome == "rejected":
        if from_status in ("created", "expired"):
            return {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能拒绝"}
        e["status"] = "rejected"
        e["reason"] = params.get("reason")
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"ok": True, "key": key, "status": "rejected"}

    if outcome == "accepted":
        if from_status != "asked":
            return {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能接受"}
        e["status"] = "accepted"
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"ok": True, "key": key, "status": "accepted",
                "suggestion": e.get("suggestion", ""),
                "next": "加载 miloco-create-task 据此建任务；建成后再次 resolve outcome=created 并回填 task_id"}

    if outcome == "created":
        if from_status not in ("accepted", "asked"):
            return {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能标记为已建"}
        e["status"] = "created"
        e["task_id"] = params.get("task_id", e.get("task_id"))
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"ok": True, "key": key, "status": "created", "task_id": e.get("task_id")}

    return {"ok": False, "error": f"未知 outcome：{outcome}"}


# ─── 入口 ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Miloco Habit Suggestion Tool")
    parser.add_argument("action", choices=["list", "record", "mark_asked", "resolve"])
    parser.add_argument("--key", type=str, default="")
    parser.add_argument("--title", type=str, default="")
    parser.add_argument("--subject", type=str, default="")
    parser.add_argument("--habit", type=str, default="")
    parser.add_argument("--suggestion", type=str, default="")
    parser.add_argument("--evidence", type=str, default="")
    parser.add_argument("--item-id", type=str, default="")
    parser.add_argument("--outcome", type=str, default="")
    parser.add_argument("--task-id", type=str, default="")
    parser.add_argument("--reason", type=str, default="")
    args = parser.parse_args()

    now = now_iso()
    store = load_store()
    expired = apply_expiry(store, now)

    params = {
        "key": args.key, "title": args.title, "subject": args.subject,
        "habit": args.habit, "suggestion": args.suggestion,
        "evidence": args.evidence, "item_id": args.item_id,
    }

    result = {"ok": False, "error": f"未知 action：{args.action}"}
    if args.action == "list":
        result = do_list(store, now)
    elif args.action == "record":
        result = do_record(store, now, params)
    elif args.action == "mark_asked":
        result = do_mark_asked(store, now, params)
    elif args.action == "resolve":
        resolve_params = {
            "key": args.key, "outcome": args.outcome,
            "task_id": args.task_id, "reason": args.reason,
        }
        result = do_resolve(store, now, resolve_params)

    if expired:
        save_store(store)
    elif result.get("ok") and args.action != "list":
        save_store(store)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
