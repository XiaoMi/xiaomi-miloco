# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Trigger data models
Define trigger-related data structures
"""
from enum import Enum
from typing import Optional, List

from miloco_server.schema.mcp_schema import MCPClientStatus
from miloco_server.schema.miot_schema import CameraInfo
from pydantic import BaseModel, Field


class Action(BaseModel):
    """Action data model"""
    mcp_client_id: str = Field(..., description="MCP client ID")
    mcp_tool_name: str = Field(..., description="MCP tool name")
    mcp_tool_input: dict = Field(..., description="MCP tool input")
    mcp_server_name: str = Field(..., description="MCP service name")
    introduction: str = Field(..., description="Introduction, used for user to understand the action introduction")


class ExecuteType(Enum):
    """Execute type enumeration"""
    STATIC = "static" # LLM generated static action, direct action
    DYNAMIC = "dynamic" # action description, use LLM dynamic action


class Notify(BaseModel):
    """Notification data model"""
    id: Optional[str] = Field(None, description="Notification ID")
    content: str = Field(..., description="Notification content")


class ExecuteInfo(BaseModel):
    """Execute info"""
    ai_recommend_execute_type: ExecuteType = Field(
        ExecuteType.STATIC, description="AI recommend execute type")
    ai_recommend_action_descriptions: Optional[list[str]] = Field(
        None, description="Action descriptions")
    ai_recommend_actions: Optional[list[Action]] = Field(
        None, description="Actions to execute")
    automation_actions: Optional[list[Action]] = Field(
        None, description="MIoT or Home Assistant automation actions to execute")
    mcp_list: Optional[list[str]] = Field(None, description="MCP list")
    notify: Optional[Notify] = Field(None, description="Mi Home send notification")

class ExecuteInfoDetail(ExecuteInfo):
    """Execute info detail"""
    mcp_list: Optional[list[MCPClientStatus]] = Field(None, description="MCP list")

    @classmethod
    def from_execute_info(
            cls, execute_info: ExecuteInfo,
            mcp_list: Optional[list[MCPClientStatus]]) -> "ExecuteInfoDetail":
        execute_info_data = execute_info.model_dump(exclude={"mcp_list"})
        if mcp_list:
            execute_info_data["mcp_list"] = [mcp.model_dump() for mcp in mcp_list]
        return cls.model_validate(execute_info_data)

    @classmethod
    def to_execute_info(cls, instance) -> ExecuteInfo:
        execute_info_data = instance.model_dump(exclude={"mcp_list"})
        mcp_list = [client.client_id for client in instance.mcp_list] if instance.mcp_list else None
        return ExecuteInfo(**execute_info_data, mcp_list=mcp_list)


class TriggerFrequencyFilter(BaseModel):
    """Trigger frequency filter data model"""
    frequency: int = Field(..., description="Trigger frequency/times", le=50)
    period: int = Field(..., description="Trigger period/seconds")


class TriggerFilter(BaseModel):
    """Trigger filter data model"""
    period: Optional[str] = Field(None, description="Trigger time period filter, cron expression")
    interval: Optional[int] = Field(None, description="Trigger interval filter/seconds")
    frequency: Optional[TriggerFrequencyFilter] = Field(None, description="Trigger frequency filter")


class TriggerRule(BaseModel):
    """Trigger rule data model - supports create/update and query operations"""
    id: Optional[str] = Field(None, description="Rule ID (UUID format)")
    enabled: bool = Field(True, description="Whether enabled")
    name: str = Field(..., description="Rule name")
    cameras: List[str] = Field(..., description="Camera device ID list")
    condition: str = Field(..., description="Trigger condition")
    execute_info: ExecuteInfo = Field(..., description="Trigger execute info")
    filter: Optional[TriggerFilter] = Field(None, description="Trigger filter")


class TriggerRuleDetail(TriggerRule):
    """Trigger rule response data model, includes camera name and scene name"""
    cameras: List[CameraInfo] = Field(..., description="Camera information list, includes ID and name")
    execute_info: ExecuteInfoDetail = Field(..., description="Trigger execute info details")

    @classmethod
    def from_trigger_rule(cls,
        trigger_rule: TriggerRule,
        cameras: List[CameraInfo],
        execute_info: ExecuteInfoDetail,
    ) -> "TriggerRuleDetail":
        trigger_rule_data = trigger_rule.model_dump(exclude={"cameras", "execute_info"})
        return cls(
            **trigger_rule_data,
            cameras=cameras,
            execute_info=execute_info,
        )

    @classmethod
    def to_trigger_rule(cls, instance) -> TriggerRule:
        camera_dids = [camera.did for camera in instance.cameras]
        execute_info = ExecuteInfoDetail.to_execute_info(instance.execute_info)
        instance_data = instance.model_dump(exclude={"cameras", "execute_info"})
        return TriggerRule(
            **instance_data, cameras=camera_dids, execute_info=execute_info)

class SendingState(BaseModel):
    """Sending state data model"""
    flag: bool = Field(False, description="Sending flag")
    time: float = Field(0.0, description="Last sending flag time")