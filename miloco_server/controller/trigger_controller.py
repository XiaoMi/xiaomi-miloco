# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Trigger Rule Controller
Implements CRUD interfaces for trigger rules
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, Query, WebSocket
from fastapi.websockets import WebSocketDisconnect
from miloco_server.middleware.exceptions import BusinessException

from miloco_server.middleware import verify_token, verify_websocket_token
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.schema.trigger_schema import Action, TriggerRule, TriggerRuleDetail
from miloco_server.service.manager import get_manager


# Create logger
logger = logging.getLogger(name=__name__)

# Create router
router = APIRouter(prefix="/trigger", tags=["Trigger Rules"])

# Get manager instance
manager = get_manager()


@router.post("/rule", summary="Create Trigger Rule", response_model=NormalResponse)
async def create_trigger_rule(
    trigger_rule: TriggerRule,
    current_user: str = Depends(verify_token)
):
    """
    Create trigger rule
    - Requires admin permissions
    - Rule name must be unique
    """
    logger.info("Create trigger rule API called - User: %s, Rule name: %s", current_user, trigger_rule.name)
    rule_id = await manager.trigger_rule_service.create_trigger_rule(trigger_rule)
    logger.info("Trigger rule created successfully - Rule ID: %s", rule_id)
    return NormalResponse(
        code=0,
        message="Trigger rule created successfully",
        data={"rule_id": rule_id}
    )


@router.get("/rules", summary="Get All Trigger Rules", response_model=NormalResponse)
async def get_all_trigger_rules(
    enabled_only: bool = Query(False, description="Whether to return only enabled rules"),
    current_user: str = Depends(verify_token)
):
    """
    Get all trigger rules
    - Requires admin permissions
    - Optionally return only enabled rules
    """
    logger.info("Get all trigger rules API called - User: %s, Enabled only: %s", current_user, enabled_only)
    trigger_rules: list[TriggerRuleDetail] = await manager.trigger_rule_service.get_all_trigger_rules(enabled_only)
    logger.info("Trigger rules list retrieved successfully - Count: %s", len(trigger_rules))
    return NormalResponse(
        code=0,
        message=f"Trigger rules retrieved successfully, total {len(trigger_rules)} records",
        data=trigger_rules
    )


@router.put("/rule/{rule_id}", summary="Update Trigger Rule", response_model=NormalResponse)
async def update_trigger_rule(
    rule_id: str,
    trigger_rule: TriggerRule,
    current_user: str = Depends(verify_token)
):
    """
    Update trigger rule
    - Requires admin permissions
    - Rule name must be unique (cannot conflict with other rules)
    """
    logger.info("Update trigger rule API called - User: %s, Rule ID: %s", current_user, rule_id)
    trigger_rule.id = rule_id
    await manager.trigger_rule_service.update_trigger_rule(trigger_rule)
    logger.info("Trigger rule updated successfully - Rule ID: %s", rule_id)
    return NormalResponse(
        code=0,
        message="Trigger rule updated successfully",
        data=None
    )


@router.delete("/rule/{rule_id}", summary="Delete Trigger Rule", response_model=NormalResponse)
async def delete_trigger_rule(
    rule_id: str,
    current_user: str = Depends(verify_token)
):
    """
    Delete trigger rule
    - Requires admin permissions
    """
    logger.info("Delete trigger rule API called - User: %s, Rule ID: %s", current_user, rule_id)
    success = await manager.trigger_rule_service.delete_trigger_rule(rule_id)
    if not success:
        raise BusinessException("Trigger rule deletion failed")
    logger.info("Trigger rule deleted successfully - Rule ID: %s", rule_id)
    return NormalResponse(
        code=0,
        message="Trigger rule deleted successfully",
        data=None
    )


@router.get("/logs", summary="Get Trigger Rule Logs", response_model=NormalResponse)
async def get_trigger_rule_logs(
    limit: int = Query(10, description="Number of recent log entries to retrieve", ge=1, le=500),
    current_user: str = Depends(verify_token)
):
    """
    Get trigger rule logs
    - Requires login
    - Get the most recent n log entries, returned in reverse chronological order
    - Limit parameter restricted to 1-500
    """
    logger.info("Get trigger rule logs API called - User: %s, Limit: %s", current_user, limit)
    trigger_rule_logs, total_items = await manager.trigger_rule_service.get_trigger_rule_logs(limit)
    dict_data = {
        "rule_logs": trigger_rule_logs,
        "total_items": total_items
    }
    logger.info("Trigger rule logs retrieved successfully - Count: %s", len(trigger_rule_logs))
    return NormalResponse(
        code=0,
        message=f"Trigger rule logs retrieved successfully, total {total_items} records",
        data=dict_data
    )


@router.post(path="/execute_actions", summary="Execute actions", response_model=NormalResponse)
async def execute_actions(actions: list[Action], current_user: str = Depends(verify_token)):
    """Execute actions"""
    logger.info("Execute action API called, user: %s, actions: %s", current_user, actions)
    results: list[bool] = await manager.trigger_rule_service.execute_actions(actions)

    msg = f"Actions executed, success/total: {results.count(True)}/{len(results)}"

    return NormalResponse(
        code=0,
        message=msg,
        data=results
    )

@router.websocket("/ws/dynamic_execute_log")
async def ws_dynamic_execute_log(
    websocket: WebSocket,
    log_id: str,
    current_user: str = Depends(verify_websocket_token)):  # pylint: disable=unused-argument
    logger.info("[%s] WebSocket ws dynamic execute log connection", log_id)
    try:
        await websocket.accept()
        await manager.trigger_rule_service.send_dynamic_execute_log(log_id, websocket)
        while True:
            message = await websocket.receive_text()
            logger.info("[%s] Received unknown message from client, %s", log_id, message)
            await asyncio.sleep(0.1)

    except WebSocketDisconnect:
        logger.warning("[%s] Client disconnected", log_id)
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.error("[%s] WebSocket error: %s", log_id, err)
        await websocket.close(code=1011, reason=f"Server error: {str(err)}")
    finally:
        logger.info("[%s] WebSocket connection closed", log_id)
