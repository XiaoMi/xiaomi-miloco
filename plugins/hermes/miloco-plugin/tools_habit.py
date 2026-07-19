"""miloco_habit_suggest 工具：习惯建议候选库的防骚扰状态机。

移植自 openclaw TypeScript 插件
``plugins/openclaw/src/home-profile/suggestions.ts``。

背景：每日 10 点的 isolated cron（扫描 agent）从家庭档案识别"值得建成任务的
习惯"，主动 IM 推荐；用户在主 IM session（回应 agent，与扫描 agent 不共享上下文）
认可后加载 miloco-create-task 建任务。两个 agent 通过本库的持久状态衔接。

设计核心：**让工具成为防骚扰的权威**——"同一时刻至多 1 条待回应 / 每天至多 1 条
新推 / 拒绝永不再问 / 超 7 天没回应作废" 这些闸门都由工具裁定并拒绝越界写入，
不依赖扫描 agent 自觉。

状态机：pending → asked →（accepted → created）| rejected | expired。
- rejected / created：永久终态，不再推荐。
- expired：非永久——下次 record 同 key 复活为 pending 重新推荐；累计问满
  MAX_ASKS(3) 次仍无果则永久放弃、不再复活。

存储路径：``$MILOCO_HOME/home-profile/task-suggestions.json``（与 helpers.ts 的
``habitSuggestionsPath`` 一致，两端共用同一文件）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import miloco_home

logger = logging.getLogger(__name__)


# ─── 常量（节奏=克制，硬编码，与 TS 端一致） ──────────────────────────────

STORE_VERSION = 1
MAX_OPEN_QUESTIONS = 1
MAX_NEW_ASK_PER_DAY = 1
STALE_DAYS = 7
STALE_MS = STALE_DAYS * 86_400_000
MAX_ASKS = 3


# ─── 时间工具 ──────────────────────────────────────────────────────────────

def now_local_iso() -> str:
    """当前本地时间 ISO 字符串（与 TS 端 ``nowLocalIso`` 对齐，用本地时区）。"""
    return datetime.now().astimezone().isoformat()


def _to_timestamp(v: Any) -> int:
    """把 ISO 字符串或数字转成毫秒时间戳；解析失败返回 0。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1000)
    return 0


def _local_date_key(iso: str) -> str:
    """部署时区视角的日历日 key（YYYY-MM-DD）。"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d")


def _elapsed_ms(from_iso: str, now_iso: str) -> int:
    return _to_timestamp(now_iso) - _to_timestamp(from_iso)


# ─── 存取 ──────────────────────────────────────────────────────────────────

def _habit_suggestions_path() -> Path:
    """与 helpers.ts ``habitSuggestionsPath`` 一致。"""
    return miloco_home() / "home-profile" / "task-suggestions.json"


def _load_store() -> Dict[str, Any]:
    """读 task-suggestions.json；缺失/损坏返回空 store。"""
    path = _habit_suggestions_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"version": STORE_VERSION, "entries": []}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("task-suggestions.json 解析失败 (%s): %s", path, exc)
        return {"version": STORE_VERSION, "entries": []}
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return {"version": raw.get("version", STORE_VERSION), "entries": raw["entries"]}
    return {"version": STORE_VERSION, "entries": []}


def _save_store(store: Dict[str, Any]) -> None:
    """原子写（temp → rename）。失败仅 log。"""
    path = _habit_suggestions_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("task-suggestions.json 写入失败 (%s): %s", path, exc)


# ─── 进程内互斥 ────────────────────────────────────────────────────────────

_lock = threading.Lock()


# ─── 纯函数（便于测试，与 TS 端一一对应） ──────────────────────────────────

def apply_expiry(store: Dict[str, Any], now_iso: str) -> bool:
    """惰性过期：无明确回应的在途条目超 7 天 → expired。返回是否有变更。"""
    changed = False
    for e in store["entries"]:
        stamp = None
        if e.get("status") == "asked":
            stamp = e.get("asked_at")
        elif e.get("status") == "accepted":
            stamp = e.get("resolved_at")
        if stamp and _elapsed_ms(stamp, now_iso) > STALE_MS:
            e["status"] = "expired"
            e["resolved_at"] = now_iso
            e["reason"] = f"{STALE_DAYS} 天无明确回应自动过期（可重新推荐）"
            e["updated_at"] = now_iso
            changed = True
    return changed


def _asked_today(store: Dict[str, Any], now_iso: str) -> bool:
    today = _local_date_key(now_iso)
    return any(e.get("asked_at") and _local_date_key(e["asked_at"]) == today for e in store["entries"])


def _open_count(store: Dict[str, Any]) -> int:
    return sum(1 for e in store["entries"] if e.get("status") == "asked")


def can_ask_now(store: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    """此刻是否还能发起新询问（待回应位未满 + 今天还没问过）。"""
    if _open_count(store) >= MAX_OPEN_QUESTIONS:
        return {"can": False, "reason": "已有待回应的建议，本次不再打扰"}
    if MAX_NEW_ASK_PER_DAY > 0 and _asked_today(store, now_iso):
        return {"can": False, "reason": "今天已经推荐过一条，明天再说"}
    return {"can": True}


def load_open_questions(now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """injection.ts 用：未作废的待回应条目（不写盘，作废留给下次工具调用持久化）。"""
    now = now_iso or now_local_iso()
    store = _load_store()
    return [
        e for e in store["entries"]
        if e.get("status") == "asked"
        and e.get("asked_at")
        and _elapsed_ms(e["asked_at"], now) <= STALE_MS
    ]


# ─── action 实现 ───────────────────────────────────────────────────────────

def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _view(e: Dict[str, Any]) -> Dict[str, Any]:
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


def _do_list(store: Dict[str, Any], now: str) -> Dict[str, Any]:
    gate = can_ask_now(store, now)
    open_q = [e for e in store["entries"] if e.get("status") == "asked"]
    pending = [e for e in store["entries"] if e.get("status") == "pending"]
    counts: Dict[str, int] = {}
    for e in store["entries"]:
        s = e.get("status", "")
        counts[s] = counts.get(s, 0) + 1
    return {
        "dirty": False,
        "res": {
            "ok": True,
            "can_ask_now": gate["can"],
            "blocked_reason": gate.get("reason"),
            "open_questions": [_view(e) for e in open_q],
            "askable_pending": [_view(e) for e in pending],
            "entries": [_view(e) for e in store["entries"]],
            "counts": counts,
        },
    }


def _do_record(store: Dict[str, Any], now: str, p: Dict[str, Any]) -> Dict[str, Any]:
    key = _str(p.get("key"))
    subject = _str(p.get("subject")) or "shared"
    habit = _str(p.get("habit"))
    suggestion = _str(p.get("suggestion"))
    title = _str(p.get("title")) or habit[:24]
    if not key or not habit or not suggestion:
        return {"dirty": False, "res": {"ok": False, "error": "record 需要 key / habit / suggestion"}}

    existing = next((e for e in store["entries"] if e.get("key") == key), None)
    if existing:
        status = existing.get("status")
        if status in ("rejected", "created"):
            return {"dirty": False, "res": {
                "ok": True, "key": key, "status": status, "deduped": True,
                "note": f"已存在且状态为 {status}，永久不再推荐",
            }}
        if status == "expired":
            if existing.get("ask_count", 0) >= MAX_ASKS:
                return {"dirty": False, "res": {
                    "ok": True, "key": key, "status": "expired", "deduped": True,
                    "note": f"已主动询问 {existing.get('ask_count')} 次仍无果，放弃、不再推荐",
                }}
            existing.update({
                "status": "pending", "asked_at": None, "resolved_at": None, "reason": None,
                "title": title, "subject": subject, "habit": habit, "suggestion": suggestion,
                "evidence": _str(p.get("evidence")) or existing.get("evidence"),
                "item_id": _str(p.get("item_id")) or existing.get("item_id"),
                "updated_at": now,
            })
            return {"dirty": True, "res": {
                "ok": True, "key": key, "status": "pending", "deduped": True, "revived": True,
                "note": f"过期未答复，已重新纳入推荐候选（将是第 {existing.get('ask_count', 0) + 1} 次询问，上限 {MAX_ASKS}）",
            }}
        # pending / asked / accepted：在途
        dirty = False
        if status == "pending":
            existing.update({
                "title": title, "subject": subject, "habit": habit, "suggestion": suggestion,
                "evidence": _str(p.get("evidence")) or existing.get("evidence"),
                "item_id": _str(p.get("item_id")) or existing.get("item_id"),
                "updated_at": now,
            })
            dirty = True
        note = "已存在待处理候选（已刷新）" if status == "pending" else f"已存在且状态为 {status}"
        return {"dirty": dirty, "res": {"ok": True, "key": key, "status": status, "deduped": True, "note": note}}

    entry = {
        "key": key, "title": title, "subject": subject, "habit": habit, "suggestion": suggestion,
        "evidence": _str(p.get("evidence")) or None,
        "item_id": _str(p.get("item_id")) or None,
        "status": "pending", "ask_count": 0,
        "created_at": now, "updated_at": now,
    }
    store["entries"].append(entry)
    return {"dirty": True, "res": {"ok": True, "key": key, "status": "pending", "deduped": False}}


def _do_mark_asked(store: Dict[str, Any], now: str, p: Dict[str, Any]) -> Dict[str, Any]:
    key = _str(p.get("key"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"dirty": False, "res": {"ok": False, "error": "找不到该建议 key"}}
    if e.get("status") != "pending":
        return {"dirty": False, "res": {"ok": False, "status": e.get("status"), "error": f"状态为 {e.get('status')}，不能标记为已询问"}}
    gate = can_ask_now(store, now)
    if not gate["can"]:
        return {"dirty": False, "res": {"ok": False, "blocked_reason": gate.get("reason"), "error": gate.get("reason")}}
    e["status"] = "asked"
    e["asked_at"] = now
    e["updated_at"] = now
    e["ask_count"] = e.get("ask_count", 0) + 1
    return {"dirty": True, "res": {"ok": True, "key": key, "status": "asked"}}


def _do_resolve(store: Dict[str, Any], now: str, p: Dict[str, Any]) -> Dict[str, Any]:
    key = _str(p.get("key"))
    outcome = _str(p.get("outcome"))
    e = next((x for x in store["entries"] if x.get("key") == key), None)
    if not e:
        return {"dirty": False, "res": {"ok": False, "error": "找不到该建议 key"}}
    from_status = e.get("status")

    if outcome == "rejected":
        if from_status in ("created", "expired"):
            return {"dirty": False, "res": {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能拒绝"}}
        e["status"] = "rejected"
        e["reason"] = _str(p.get("reason")) or None
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"dirty": True, "res": {"ok": True, "key": key, "status": "rejected"}}

    if outcome == "accepted":
        if from_status != "asked":
            return {"dirty": False, "res": {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能接受（需处于 asked）"}}
        e["status"] = "accepted"
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"dirty": True, "res": {
            "ok": True, "key": key, "status": "accepted", "suggestion": e.get("suggestion"),
            "next": "加载 miloco-create-task 据此建任务；建成后再次 resolve outcome=created 并回填 task_id",
        }}

    if outcome == "created":
        if from_status not in ("accepted", "asked"):
            return {"dirty": False, "res": {"ok": False, "status": from_status, "error": f"状态为 {from_status}，不能标记为已建（需先 accepted，或处于 asked）"}}
        e["status"] = "created"
        e["task_id"] = _str(p.get("task_id")) or e.get("task_id")
        e["resolved_at"] = now
        e["updated_at"] = now
        return {"dirty": True, "res": {"ok": True, "key": key, "status": "created", "task_id": e.get("task_id")}}

    return {"dirty": False, "res": {"ok": False, "error": f"未知 outcome：{outcome}"}}


def apply_habit_action(
    input: Dict[str, Any],
    now_override: Optional[str] = None,
) -> Dict[str, Any]:
    """核心调度（load → 惰性作废 → dispatch → 按需写盘），全程持锁串行化。"""
    with _lock:
        now = now_override or now_local_iso()
        store = _load_store()
        expired = apply_expiry(store, now)
        action = _str(input.get("action"))
        if action == "list":
            out = _do_list(store, now)
        elif action == "record":
            out = _do_record(store, now, input)
        elif action == "mark_asked":
            out = _do_mark_asked(store, now, input)
        elif action == "resolve":
            out = _do_resolve(store, now, input)
        else:
            out = {"dirty": False, "res": {"ok": False, "error": f"未知 action：{action}"}}
        if expired or out.get("dirty"):
            _save_store(store)
        return out["res"]


# ─── 工具 schema + handler ─────────────────────────────────────────────────

TOOL_DESCRIPTION = (
    "习惯建议候选库的读写入口（防骚扰状态机）。配合 miloco-habit-suggest skill 使用。\n"
    "状态流转：pending → asked →（accepted → created）| rejected | expired。\n\n"
    "action 取值：\n"
    "- list：读候选库现状。返回 can_ask_now（此刻能否发起新询问，工具裁定）、"
    "open_questions（正在等用户回应的条目）、askable_pending（可挑去询问的候选）、"
    "entries（全量条目含已拒绝/已建/已作废——你据此判断是不是同一个习惯、复用既有 key、跳过终态）。\n"
    "- record：把识别到的一条习惯登记为候选（status=pending）。传你起的稳定语义 key + "
    "subject/habit/suggestion(/title/evidence/item_id)；item_id 填该习惯所依据的家庭档案条目 id，"
    "用于追踪来源 + 建成任务后从档案渲染中剔除。\n"
    "  同一 key 幂等：已 rejected/created 的只返回既有、永久不再推；过期（expired，无明确回应）"
    "的会复活为 pending 重新推荐，但累计问满 3 次仍无果即永久放弃；在途（pending/asked/accepted）"
    "的原样返回。是否同一习惯由你判断——务必先 list 复用既有 key。\n"
    "- mark_asked：把某条 pending 翻成 asked。**必须在 miloco_im_push 返回 ok:true（确认送达）"
    "之后才调**；工具会再次校验防骚扰闸门，越界（已有待回应 / 今天已问过 / 状态不对）直接返回 ok:false。\n"
    "- resolve：用户回应后落地。outcome=created（任务建成、回填 task_id，终态）/ "
    "rejected（拒绝，终态永不再问）/ accepted（可选中间态：仅当需跨轮分步建任务时先标「已同意」；"
    "正常流程应**先建成再 resolve(created)**，未完成的 accepted 会自动作废、不永久滞留）。"
)

MILOCO_HABIT_SUGGEST_SCHEMA: Dict[str, Any] = {
    "name": "miloco_habit_suggest",
    "description": TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "record", "mark_asked", "resolve"],
                "description": "操作类型：list / record / mark_asked / resolve",
            },
            "key": {
                "type": "string",
                "description": "建议的稳定语义 key，由你自己起（如 wanglei_sleep_dim_light）。"
                "record/mark_asked/resolve 都用它定位；同一习惯务必复用 list 里已有的 key，"
                "避免重复或复活已拒绝项",
            },
            "subject": {"type": "string", "description": "习惯主体：成员名（如 王磊）；全家公共填 shared。record 用"},
            "habit": {"type": "string", "description": "观察到的习惯（规范短句，如『王磊 傍晚约19点健身约30分钟』）。record 用"},
            "suggestion": {"type": "string", "description": "要推荐给用户的任务点子（自然语言，认可后即据此建任务）。record 用"},
            "title": {"type": "string", "description": "一句话标题（可选，缺省截取 habit）。record 用"},
            "evidence": {"type": "string", "description": "依据（档案条目/出现频率，可选）。record 用"},
            "item_id": {"type": "string", "description": "该习惯所依据的家庭档案条目 id（**来自 home-profile list 的 id 字段**）。record 用：**追踪建议来源，建成任务后据此把该条目从家庭档案渲染中剔除**"},
            "outcome": {
                "type": "string",
                "enum": ["accepted", "rejected", "created"],
                "description": "resolve 的结果：accepted / rejected / created",
            },
            "task_id": {"type": "string", "description": "outcome=created 时回填的任务 id"},
            "reason": {"type": "string", "description": "outcome=rejected 时的简短原因（可选）"},
        },
        "required": ["action"],
    },
}


def handle_habit_suggest(args: Dict[str, Any], **kwargs: Any) -> str:
    """``miloco_habit_suggest`` handler（无 ctx 依赖，可直接注册）。"""
    try:
        result = apply_habit_action(args if isinstance(args, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("miloco_habit_suggest 失败: %s", exc)
        result = {"ok": False, "error": f"internal error: {exc}"}
    return json.dumps(result, ensure_ascii=False)
