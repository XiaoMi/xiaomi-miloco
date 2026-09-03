# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Rule Controller
Implements CRUD interfaces for rules
"""

import logging

from fastapi import APIRouter, Depends, Query

from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import BusinessException
from miloco.rule.schema import (
    Rule,
    RuleDirection,
    RuleLogKind,
    RuleTriggerRequest,
    RuleUpdate,
)
from miloco.schema.common_schema import NormalResponse
from miloco.utils.time_utils import parse_iso_ms, since_to_ms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rules", tags=["Rules"])

manager = get_manager()


def _resolve_after_ts(
    after: str | None,
    since: str | None,
) -> int | None:
    """Resolve ``after`` (ISO 8601) / ``since`` (relative, e.g. '1h', '2h30m') to a
    millisecond Unix timestamp.  ``after`` takes precedence if both are given."""
    if after is not None:
        return parse_iso_ms(after, "after")

    if since is not None:
        return since_to_ms(since)

    return None


# ---- Rule CRUD ----


@router.post("", summary="Create Rule", response_model=NormalResponse)
async def create_rule(rule: Rule, current_user: str = Depends(verify_token)):
    """Create a new rule.

    Response data includes V3 fields needed by miloco-cli stdout:
    rule_id / name / task_id / direction / mode / lifecycle.
    """
    logger.info("Create rule - User: %s, Name: %s", current_user, rule.name)
    rule_id = await manager.rule_service.create_rule(rule)
    return NormalResponse(
        code=0,
        message="Rule created successfully",
        data={
            "rule_id": rule_id,
            "name": rule.name,
            "task_id": rule.task_id,
            # direction 是权威语义, mode 只是它在阶段 A 的存储投影。只回 mode 的话
            # `--direction exit` 建出来的规则回显成 event, 看着像没生效。
            "direction": rule.resolved_direction.value,
            "mode": rule.mode.value,
            "lifecycle": rule.lifecycle.value,
        },
    )


@router.get("", summary="Get All Rules", response_model=NormalResponse)
async def get_all_rules(
    enabled_only: bool = Query(
        False, description="Only return effectively enabled rules (task not paused)"
    ),
    include_milestone: bool = Query(
        False, description="Include server-managed milestone rules"
    ),
    current_user: str = Depends(verify_token),
):
    """Get all rules.

    达标规则默认不给: 它是服务端按 task 的达标动作 + duration record 维护的派生物,
    不是用户建的东西。混在列表里会让人以为自己多建了一条规则、进而去删它。
    """
    logger.info(
        "Get all rules - User: %s, enabled_only: %s, include_milestone: %s",
        current_user,
        enabled_only,
        include_milestone,
    )
    # 「启用」是派生量: 用户意图 AND task 未停用。直接读 enabled 列会把停用 task
    # 名下的规则报成启用, 用户排查"规则为什么不触发"时正好被这个答案带偏。
    rules = (
        await manager.rule_service.get_effectively_enabled_rules()
        if enabled_only
        else await manager.rule_service.get_all_rules(False)
    )
    if not include_milestone:
        rules = [
            r for r in rules if r.resolved_direction is not RuleDirection.MILESTONE
        ]
    return NormalResponse(
        code=0,
        message=f"Retrieved {len(rules)} rules",
        data=rules,
    )


# ---- Logs (must be before /{rule_id} to avoid path conflict) ----


@router.get("/logs", summary="Get Rule Logs", response_model=NormalResponse)
async def get_logs(
    limit: int = Query(10, ge=1, le=500, description="Number of recent logs"),
    after: str | None = Query(
        None, description="ISO 8601 timestamp cursor, e.g. '2026-03-30T12:00:00Z'"
    ),
    before: str | None = Query(
        None,
        description="ISO 8601 upper-bound; combined with ``after`` allows clients "
        "to page through windows larger than ``limit``",
    ),
    since: str | None = Query(
        None, description="Relative time window, e.g. '1h', '30m', '7d'"
    ),
    kind: RuleLogKind | None = Query(
        None,
        description="Filter by log kind (e.g. RULE_TRIGGER_SUCCESS, RULE_TRIGGER_FAILURE)",
    ),
    current_user: str = Depends(verify_token),
):
    """Get all rule execution logs.

    Use ``after`` for cursor-based pagination (returns logs newer than the timestamp).
    Use ``before`` together with ``after`` to page within an open-ended window:
    keep ``after`` fixed at the original cursor and lower ``before`` to the oldest
    timestamp of the previous page.
    Use ``since`` to get logs within a relative time window.
    If both ``after`` and ``since`` are provided, ``after`` takes precedence.
    Optional ``kind`` filters by ExecutionLog kind (V3 §11.2).
    """
    after_ts = _resolve_after_ts(after, since)
    before_ts = parse_iso_ms(before, "before") if before is not None else None
    logger.info(
        "Get rule logs - User: %s, limit: %d, after_ts: %s, before_ts: %s, kind: %s",
        current_user,
        limit,
        after_ts,
        before_ts,
        kind,
    )
    logs, total = await manager.rule_service.get_logs(
        limit, after_ts=after_ts, before_ts=before_ts, kind=kind
    )
    return NormalResponse(
        code=0,
        message=f"Retrieved {total} logs",
        data={"rule_logs": logs, "total_items": total},
    )


@router.delete("/logs", summary="Cleanup Rule Logs", response_model=NormalResponse)
async def cleanup_logs(
    keep_days: int = Query(7, ge=1, description="Retain logs within N days"),
    current_user: str = Depends(verify_token),
):
    """Delete rule logs older than keep_days"""
    logger.info("Cleanup rule logs - User: %s, keep_days: %d", current_user, keep_days)
    deleted = await manager.rule_service.cleanup_logs(keep_days)
    return NormalResponse(
        code=0,
        message=f"Deleted {deleted} log records older than {keep_days} days",
        data={"deleted": deleted},
    )


# ---- Trigger ----


@router.post(
    "/{rule_id}/trigger", summary="Trigger Rule", response_model=NormalResponse
)
async def trigger_rule(
    rule_id: str,
    request: RuleTriggerRequest,
    current_user: str = Depends(verify_token),
):
    """Trigger execution of a specific rule.

    Called externally (e.g. by the perception engine) when conditions are met.
    """
    logger.info("Trigger rule - User: %s, ID: %s", current_user, rule_id)
    result = await manager.rule_service.trigger_rule(rule_id, request.context)
    if result is None:
        raise BusinessException(f"Rule '{rule_id}' not found or disabled")
    return NormalResponse(code=0, message="Rule triggered", data=result)


# ---- Single rule operations ----


@router.get("/{rule_id}", summary="Get Rule", response_model=NormalResponse)
async def get_rule(rule_id: str, current_user: str = Depends(verify_token)):
    """Get a single rule by ID"""
    logger.info("Get rule - User: %s, ID: %s", current_user, rule_id)
    rule = await manager.rule_service.get_rule(rule_id)
    return NormalResponse(code=0, message="Rule retrieved", data=rule)


@router.put("/{rule_id}", summary="Update Rule", response_model=NormalResponse)
async def update_rule(
    rule_id: str, rule: Rule, current_user: str = Depends(verify_token)
):
    """Full update of a rule"""
    logger.info("Update rule - User: %s, ID: %s", current_user, rule_id)
    rule.id = rule_id
    await manager.rule_service.update_rule(rule)
    return NormalResponse(code=0, message="Rule updated successfully", data=None)


@router.patch(
    "/{rule_id}", summary="Partial Update Rule", response_model=NormalResponse
)
async def patch_rule(
    rule_id: str, update: RuleUpdate, current_user: str = Depends(verify_token)
):
    """Partial update — only provided fields are changed"""
    logger.info("Patch rule - User: %s, ID: %s", current_user, rule_id)
    await manager.rule_service.patch_rule(rule_id, update)
    return NormalResponse(code=0, message="Rule updated successfully", data=None)


@router.delete("/{rule_id}", summary="Delete Rule", response_model=NormalResponse)
async def delete_rule(rule_id: str, current_user: str = Depends(verify_token)):
    """Delete a rule"""
    logger.info("Delete rule - User: %s, ID: %s", current_user, rule_id)
    success = await manager.rule_service.delete_rule(rule_id)
    if not success:
        raise BusinessException("Rule deletion failed")
    return NormalResponse(code=0, message="Rule deleted successfully", data=None)


@router.get(
    "/{rule_id}/logs", summary="Get Rule Logs by Rule", response_model=NormalResponse
)
async def get_logs_by_rule(
    rule_id: str,
    limit: int = Query(10, ge=1, le=500, description="Number of recent logs"),
    after: str | None = Query(None, description="ISO 8601 timestamp cursor"),
    before: str | None = Query(
        None,
        description="ISO 8601 upper-bound; pair with ``after`` to page within a window",
    ),
    since: str | None = Query(
        None, description="Relative time window, e.g. '1h', '30m', '7d'"
    ),
    kind: RuleLogKind | None = Query(
        None, description="Filter by log kind"
    ),
    current_user: str = Depends(verify_token),
):
    """Get execution logs for a specific rule.

    Use ``after`` for cursor-based pagination or ``since`` for a relative window;
    pair ``after`` with ``before`` to page through windows larger than ``limit``.
    Optional ``kind`` filters by ExecutionLog kind.
    """
    after_ts = _resolve_after_ts(after, since)
    before_ts = parse_iso_ms(before, "before") if before is not None else None
    logger.info(
        "Get rule logs - User: %s, rule_id: %s, limit: %d, after_ts: %s, before_ts: %s, kind: %s",
        current_user,
        rule_id,
        limit,
        after_ts,
        before_ts,
        kind,
    )
    logs, total = await manager.rule_service.get_logs_by_rule_id(
        rule_id, limit, after_ts=after_ts, before_ts=before_ts, kind=kind
    )
    return NormalResponse(
        code=0,
        message=f"Retrieved {total} logs",
        data={"rule_logs": logs, "total_items": total},
    )
