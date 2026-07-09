# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""/api/crons REST 路由.

按 dispatch_owner 分派:
- internal (POST 强制): backend 完整管理, DELETE / enable / disable 联动 in-memory scheduler
- external (仅通过迁移脚本入库): DELETE 只清 DB 行, 产 agent_pending 让 skill 处理老通路

kill switch (settings.schedule.enabled=false): POST 返 503; GET/DELETE/enable/disable 只读写 DB。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from miloco.config import get_settings
from miloco.database.task_repo import TaskRepo
from miloco.middleware import verify_token
from miloco.middleware.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from miloco.schedule.repo import CronRepo
from miloco.schedule.runner import get_runner
from miloco.schedule.schema import (
    Cron,
    CronCreateRequest,
    CronDeleteResult,
    CronView,
)
from miloco.schema.common_schema import NormalResponse
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)


def _safe_log(value) -> str:
    """去 CR/LF 防 log injection (CodeQL py/log-injection)。"""
    if value is None:
        return "None"
    return str(value).replace("\r", "").replace("\n", " ")


router = APIRouter(prefix="/crons", tags=["Schedule"])


def _repo() -> CronRepo:
    return CronRepo()


def _schedule_enabled() -> bool:
    return get_settings().schedule.enabled


def _validate_tz(tz: str | None) -> None:
    if tz is None:
        return
    from zoneinfo import ZoneInfoNotFoundError, available_timezones

    if tz not in available_timezones():
        raise ValidationException(
            f"invalid timezone (IANA required): {tz!r}"
        )
    # 触发一次 ZoneInfo 构造, 兜底不合法时 raise (虽然 available_timezones 已过)
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except ZoneInfoNotFoundError as e:
        raise ValidationException(f"invalid timezone: {tz!r}") from e


def _validate_cron_expr(expr: str, tz: str | None) -> None:
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(expr, timezone=tz)
    except (ValueError, TypeError) as e:
        raise ValidationException(f"invalid cron expression: {e}") from e


def _validate_at_iso(at_iso: str) -> int:
    """解析 at_iso, 返回 UTC epoch 毫秒;

    严格校验:
    - ISO8601 合法
    - 必须带时区偏移 (naive 拒收, 避免与部署时区耦合)
    - 未来时刻 (<= now 拒收)
    - 上限 10 年 (防误传)
    """
    try:
        dt = datetime.fromisoformat(at_iso)
    except ValueError as e:
        raise ValidationException(f"invalid at_iso: {e}") from e
    if dt.tzinfo is None:
        raise ValidationException(
            "at_iso must carry a timezone offset "
            "(e.g. 2026-06-11T09:00:00+08:00)"
        )
    at_ms = int(dt.timestamp() * 1000)
    current = now_ms()
    ten_years_ms = 10 * 365 * 86400 * 1000
    if at_ms > current + ten_years_ms:
        raise ValidationException(
            "at_iso out of reasonable range (>10y in future)"
        )
    if at_ms <= current:
        raise ValidationException("at_iso must be in the future")
    return at_ms


# ── POST /crons ─────────────────────────────────────────────────────────────


@router.post("", summary="Create cron", response_model=NormalResponse)
async def create_cron(
    req: CronCreateRequest,
    current_user: str = Depends(verify_token),
):
    if not _schedule_enabled():
        raise HTTPException(
            status_code=503, detail="schedule disabled (kill switch active)"
        )

    if req.task_id is not None and not TaskRepo().task_exists(req.task_id):
        raise ResourceNotFoundException(
            f"task_id not found: {req.task_id!r}"
        )

    _validate_tz(req.tz)
    if req.kind == "cron":
        _validate_cron_expr(req.cron_expr, req.tz)
    at_ms: int | None = None
    if req.kind == "at":
        at_ms = _validate_at_iso(req.at_iso)

    cron_id = str(uuid.uuid4())
    ts = now_ms()
    cron = Cron(
        cron_id=cron_id,
        task_id=req.task_id,
        dispatch_owner="internal",
        name=req.name.strip(),
        kind=req.kind,
        cron_expr=req.cron_expr,
        at_ms=at_ms,
        every_ms=req.every_ms,
        anchor_ms=req.anchor_ms,
        tz=req.tz,
        message=req.message.strip(),
        light_context=req.light_context,
        max_delay_seconds=req.max_delay_seconds,
        enabled=True,
        created_at=ts,
        updated_at=ts,
    )
    _repo().insert(cron)

    try:
        get_runner().apply_enabled_state(cron)
    except Exception as e:  # noqa: BLE001
        # in-memory 操作实际不会失败, 万一失败 log warning 依赖重启 rebuild 兜底
        logger.warning(
            "add_job failed for %s (row committed, rebuild will heal): %s",
            cron_id,
            e,
        )

    logger.info(
        "cron created - user=%s cron_id=%s kind=%s task_id=%s",
        _safe_log(current_user),
        cron_id,
        _safe_log(req.kind),
        _safe_log(req.task_id),
    )
    return NormalResponse(
        code=0, message="Cron created", data={"cron_id": cron_id}
    )


# ── GET /crons ──────────────────────────────────────────────────────────────


@router.get("", summary="List crons", response_model=NormalResponse)
async def list_crons(
    task_id: str | None = Query(None),
    orphan: bool = Query(False),
    dispatch_owner: Literal["internal", "external"] | None = Query(None),
    current_user: str = Depends(verify_token),
):
    repo = _repo()
    if orphan:
        rows = repo.list_where("task_id IS NULL")
    elif task_id is not None:
        rows = repo.list_by_task(task_id)
    else:
        rows = repo.list_all()
    if dispatch_owner is not None:
        rows = [r for r in rows if r.dispatch_owner == dispatch_owner]
    return NormalResponse(
        code=0,
        message=f"Retrieved {len(rows)} crons",
        data=[CronView.from_cron(r).model_dump() for r in rows],
    )


@router.get(
    "/{cron_id}", summary="Get cron detail", response_model=NormalResponse
)
async def get_cron(cron_id: str, current_user: str = Depends(verify_token)):
    cron = _repo().get(cron_id)
    if cron is None:
        raise ResourceNotFoundException(f"cron_not_found: {cron_id}")
    return NormalResponse(
        code=0, message="Cron retrieved", data=CronView.from_cron(cron).model_dump()
    )


# ── DELETE /crons/{cron_id} ─────────────────────────────────────────────────


@router.delete(
    "/{cron_id}",
    summary="Delete cron",
    response_model=NormalResponse,
)
async def delete_cron(
    cron_id: str, current_user: str = Depends(verify_token)
):
    repo = _repo()
    cron = repo.get(cron_id)
    if cron is None:
        raise ResourceNotFoundException(f"cron_not_found: {cron_id}")

    repo.delete(cron_id)

    agent_pending: list[dict] = []
    if cron.dispatch_owner == "internal":
        if _schedule_enabled():
            get_runner().remove_job(cron_id)
    else:
        agent_pending.append(
            {
                "kind": "cron",
                "ref": cron_id,
                "action": "remove",
                "source": "openclaw",
            }
        )

    return NormalResponse(
        code=0,
        message="Cron deleted",
        data=CronDeleteResult(deleted=True, agent_pending=agent_pending).model_dump(),
    )


# ── enable / disable ────────────────────────────────────────────────────────


def _toggle_enabled(cron_id: str, enabled: bool) -> NormalResponse:
    repo = _repo()
    cron = repo.get(cron_id)
    if cron is None:
        raise ResourceNotFoundException(f"cron_not_found: {cron_id}")

    repo.set_enabled(cron_id, enabled)

    agent_pending: list[dict] = []
    if cron.dispatch_owner == "internal":
        if _schedule_enabled():
            # 重查最新状态给 apply_enabled_state
            updated = repo.get(cron_id)
            if updated is not None:
                try:
                    get_runner().apply_enabled_state(updated)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "apply_enabled_state failed for %s: %s",
                        _safe_log(cron_id),
                        e,
                    )
    else:
        agent_pending.append(
            {
                "kind": "cron",
                "ref": cron_id,
                "action": "enable" if enabled else "disable",
                "source": "openclaw",
            }
        )

    action = "enabled" if enabled else "disabled"
    return NormalResponse(
        code=0,
        message=f"Cron {action}",
        data={"cron_id": cron_id, "agent_pending": agent_pending},
    )


@router.post(
    "/{cron_id}/enable", summary="Enable cron", response_model=NormalResponse
)
async def enable_cron(
    cron_id: str, current_user: str = Depends(verify_token)
):
    return _toggle_enabled(cron_id, True)


@router.post(
    "/{cron_id}/disable", summary="Disable cron", response_model=NormalResponse
)
async def disable_cron(
    cron_id: str, current_user: str = Depends(verify_token)
):
    return _toggle_enabled(cron_id, False)
