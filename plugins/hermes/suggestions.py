import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from .config import miloco_home

STORE_VERSION = 1
MAX_OPEN_QUESTIONS = 1
MAX_NEW_ASK_PER_DAY = 1
STALE_DAYS = 7
STALE_MS = STALE_DAYS * 86_400_000
MAX_ASKS = 3

SuggestionStatus = Literal[
    "pending",
    "asked",
    "accepted",
    "created",
    "rejected",
    "expired",
]

_lock = threading.Lock()


def habit_suggestions_path() -> Path:
    return miloco_home() / "home-profile" / "task-suggestions.json"


def _deploy_timezone() -> str:
    return os.environ.get("MILOCO_TIMEZONE") or "Asia/Shanghai"


def _tz():
    return ZoneInfo(_deploy_timezone())


def _parse_iso(iso):
    if not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_date_key(iso: str) -> str:
    dt = _parse_iso(iso)
    if dt is None or ZoneInfo is None:
        return ""
    return dt.astimezone(_tz()).strftime("%Y-%m-%d")


def elapsed_ms(from_iso: str, now_iso: str) -> int:
    a = _parse_iso(from_iso)
    b = _parse_iso(now_iso)
    if a is None or b is None:
        return 0
    return int((b - a).total_seconds() * 1000)


def _now_local_iso() -> str:
    if ZoneInfo is None:
        return datetime.now().isoformat(timespec="seconds")
    return datetime.now(_tz()).isoformat(timespec="seconds")


def apply_expiry(store: dict, now_iso: str) -> bool:
    changed = False
    for e in store["entries"]:
        status = e.get("status")
        if status == "asked":
            stamp = e.get("asked_at")
        elif status == "accepted":
            stamp = e.get("resolved_at")
        else:
            stamp = None
        if stamp and elapsed_ms(stamp, now_iso) > STALE_MS:
            e["status"] = "expired"
            e["resolved_at"] = now_iso
            e["reason"] = f"{STALE_DAYS} 天无明确回应自动过期（可重新推荐）"
            e["updated_at"] = now_iso
            changed = True
    return changed


def _asked_today(store: dict, now_iso: str) -> bool:
    today = local_date_key(now_iso)
    if not today:
        return False
    return any(
        e.get("asked_at") and local_date_key(e.get("asked_at")) == today
        for e in store["entries"]
    )


def _open_count(store: dict) -> int:
    return sum(1 for e in store["entries"] if e.get("status") == "asked")


def can_ask_now(store: dict, now_iso: str) -> dict:
    if _open_count(store) >= MAX_OPEN_QUESTIONS:
        return {"can": False, "reason": "已有待回应的建议，本次不再打扰"}
    if MAX_NEW_ASK_PER_DAY > 0 and _asked_today(store, now_iso):
        return {"can": False, "reason": "今天已经推荐过一条，明天再说"}
    return {"can": True}


def _load_store() -> dict:
    path = habit_suggestions_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": STORE_VERSION, "entries": []}
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"version": STORE_VERSION, "entries": []}
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return {
            "version": raw.get("version", STORE_VERSION),
            "entries": raw["entries"],
        }
    return {"version": STORE_VERSION, "entries": []}


def _save_store(store: dict) -> None:
    path = habit_suggestions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_open_questions(now_iso: str | None = None) -> list:
    if now_iso is None:
        now_iso = _now_local_iso()
    store = _load_store()
    return [
        e
        for e in store["entries"]
        if e.get("status") == "asked"
        and e.get("asked_at")
        and elapsed_ms(e.get("asked_at"), now_iso) <= STALE_MS
    ]


def _str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _view(e: dict) -> dict:
    return {
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


def _do_list(store: dict, now: str) -> dict:
    gate = can_ask_now(store, now)
    entries = store["entries"]
    open_q = [e for e in entries if e.get("status") == "asked"]
    pending = [e for e in entries if e.get("status") == "pending"]
    counts: dict = {}
    for e in entries:
        s = e.get("status")
        counts[s] = counts.get(s, 0) + 1
    return {
        "dirty": False,
        "res": {
            "ok": True,
            "can_ask_now": gate["can"],
            "blocked_reason": gate.get("reason"),
            "open_questions": [_view(e) for e in open_q],
            "askable_pending": [_view(e) for e in pending],
            "entries": [_view(e) for e in entries],
            "counts": counts,
        },
    }


def _do_record(store: dict, now: str, p: dict) -> dict:
    key = _str(p.get("key"))
    subject = _str(p.get("subject")) or "shared"
    habit = _str(p.get("habit"))
    suggestion = _str(p.get("suggestion"))
    title = _str(p.get("title")) or habit[:24]
    if not key or not habit or not suggestion:
        return {
            "dirty": False,
            "res": {"ok": False, "error": "record 需要 key / habit / suggestion"},
        }
    existing = next((e for e in store["entries"] if e.get("key") == key), None)
    if existing:
        estatus = existing.get("status")
        if estatus in ("rejected", "created"):
            return {
                "dirty": False,
                "res": {
                    "ok": True,
                    "key": key,
                    "status": estatus,
                    "deduped": True,
                    "note": f"已存在且状态为 {estatus}，永久不再推荐",
                },
            }
        if estatus == "expired":
            if existing.get("ask_count", 0) >= MAX_ASKS:
                return {
                    "dirty": False,
                    "res": {
                        "ok": True,
                        "key": key,
                        "status": "expired",
                        "deduped": True,
                        "note": f"已主动询问 {existing.get('ask_count')} 次仍无果，放弃、不再推荐",
                    },
                }
            existing["status"] = "pending"
            existing["asked_at"] = None
            existing["resolved_at"] = None
            existing["reason"] = None
            existing["title"] = title
            existing["subject"] = subject
            existing["habit"] = habit
            existing["suggestion"] = suggestion
            existing["evidence"] = _str(p.get("evidence")) or existing.get("evidence")
            existing["item_id"] = _str(p.get("item_id")) or existing.get("item_id")
            existing["updated_at"] = now
            return {
                "dirty": True,
                "res": {
                    "ok": True,
                    "key": key,
                    "status": "pending",
                    "deduped": True,
                    "revived": True,
                    "note": (
                        f"过期未答复，已重新纳入推荐候选（将是第 "
                        f"{existing.get('ask_count', 0) + 1} 次询问，上限 {MAX_ASKS}）"
                    ),
                },
            }
        dirty = False
        if estatus == "pending":
            existing["title"] = title
            existing["subject"] = subject
            existing["habit"] = habit
            existing["suggestion"] = suggestion
            existing["evidence"] = _str(p.get("evidence")) or existing.get("evidence")
            existing["item_id"] = _str(p.get("item_id")) or existing.get("item_id")
            existing["updated_at"] = now
            dirty = True
        note = (
            "已存在待处理候选（已刷新）"
            if estatus == "pending"
            else f"已存在且状态为 {estatus}"
        )
        return {
            "dirty": dirty,
            "res": {
                "ok": True,
                "key": key,
                "status": estatus,
                "deduped": True,
                "note": note,
            },
        }
    entry = {
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
    store["entries"].append(entry)
    return {
        "dirty": True,
        "res": {"ok": True, "key": key, "status": "pending", "deduped": False},
    }


def _do_mark_asked(store: dict, now: str, p: dict) -> dict:
    key = _str(p.get("key"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"dirty": False, "res": {"ok": False, "error": "找不到该建议 key"}}
    if e.get("status") != "pending":
        return {
            "dirty": False,
            "res": {
                "ok": False,
                "status": e.get("status"),
                "error": f"状态为 {e.get('status')}，不能标记为已询问",
            },
        }
    gate = can_ask_now(store, now)
    if not gate["can"]:
        return {
            "dirty": False,
            "res": {
                "ok": False,
                "blocked_reason": gate.get("reason"),
                "error": gate.get("reason"),
            },
        }
    e["status"] = "asked"
    e["asked_at"] = now
    e["updated_at"] = now
    e["ask_count"] = e.get("ask_count", 0) + 1
    return {"dirty": True, "res": {"ok": True, "key": key, "status": "asked"}}


def _do_resolve(store: dict, now: str, p: dict) -> dict:
    key = _str(p.get("key"))
    outcome = _str(p.get("outcome"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"dirty": False, "res": {"ok": False, "error": "找不到该建议 key"}}
    from_status = e.get("status")

    if outcome == "rejected":
        if from_status in ("created", "expired"):
            return {
                "dirty": False,
                "res": {
                    "ok": False,
                    "status": from_status,
                    "error": f"状态为 {from_status}，不能拒绝",
                },
            }
        e["status"] = "rejected"
        e["reason"] = _str(p.get("reason")) or None
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"dirty": True, "res": {"ok": True, "key": key, "status": "rejected"}}

    if outcome == "accepted":
        if from_status != "asked":
            return {
                "dirty": False,
                "res": {
                    "ok": False,
                    "status": from_status,
                    "error": f"状态为 {from_status}，不能接受（需处于 asked）",
                },
            }
        e["status"] = "accepted"
        e["resolved_at"] = now
        e["updated_at"] = now
        return {
            "dirty": True,
            "res": {
                "ok": True,
                "key": key,
                "status": "accepted",
                "suggestion": e.get("suggestion"),
                "next": (
                    "加载 miloco-create-task 据此建任务；"
                    "建成后再次 resolve outcome=created 并回填 task_id"
                ),
            },
        }

    if outcome == "created":
        if from_status not in ("accepted", "asked"):
            return {
                "dirty": False,
                "res": {
                    "ok": False,
                    "status": from_status,
                    "error": (
                        f"状态为 {from_status}，"
                        "不能标记为已建（需先 accepted，或处于 asked）"
                    ),
                },
            }
        e["status"] = "created"
        e["task_id"] = _str(p.get("task_id")) or e.get("task_id")
        e["resolved_at"] = now
        e["updated_at"] = now
        return {
            "dirty": True,
            "res": {
                "ok": True,
                "key": key,
                "status": "created",
                "task_id": e.get("task_id"),
            },
        }

    return {
        "dirty": False,
        "res": {"ok": False, "error": f"未知 outcome：{outcome}"},
    }


def apply_habit_action(input_dict: dict, now_override: str | None = None) -> dict:
    with _lock:
        now = now_override if now_override is not None else _now_local_iso()
        store = _load_store()
        expired = apply_expiry(store, now)
        action = _str(input_dict.get("action"))
        if action == "list":
            out = _do_list(store, now)
        elif action == "record":
            out = _do_record(store, now, input_dict)
        elif action == "mark_asked":
            out = _do_mark_asked(store, now, input_dict)
        elif action == "resolve":
            out = _do_resolve(store, now, input_dict)
        else:
            out = {
                "dirty": False,
                "res": {"ok": False, "error": f"未知 action：{action}"},
            }
        if expired or out["dirty"]:
            _save_store(store)
        return out["res"]
