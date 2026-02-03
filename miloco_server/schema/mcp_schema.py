# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MCP configuration definition module - simplified version
Supports frontend upload configuration format, compatible with HTTP/SSE, Streamable HTTP and Stdio transport types
"""

from typing import Dict, List, Optional, Any
from enum import Enum
from pydantic import BaseModel, Field


class TransportType(Enum):
    """Transport type enumeration"""
    LOCAL = "local"
    HTTP_SSE = "http_sse"
    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"

class MCPConfigModel(BaseModel):
    """MCP configuration model - compatible with three transport types"""

    # Basic fields
    id: Optional[str] = Field(None, description="Configuration ID")
    access_type: TransportType = Field(..., description="Access type")
    name: str = Field(..., description="Service name")
    description: str = Field("", description="Service description")
    provider: str = Field("", description="Provider")
    provider_website: str = Field("", description="Provider website")
    timeout: int = Field(60, description="Timeout setting (seconds)")
    enable: bool = Field(True, description="Enable status")

    # HTTP/SSE and Streamable HTTP specific fields
    url: Optional[str] = Field(None, description="Service URL")
    request_header_token: Optional[str] = Field(None, description="Request header token")

    # Stdio specific fields
    command: Optional[str] = Field(None, description="Command")
    args: Optional[List[str]] = Field(None, description="Argument list")
    env_vars: Optional[Dict[str, str]] = Field(None, description="Environment variables")
    working_directory: Optional[str] = Field(None, description="Working directory")


class MCPClientBaseInfo(BaseModel):
    """MCP client base info model"""
    client_id: str = Field(..., description="Client ID")
    server_name: str = Field(..., description="Service name")


class MCPClientStatus(MCPClientBaseInfo):
    """MCP client status model"""
    connected: bool = Field(..., description="Connection status")

def choose_mcp_list(mcp_ids: Optional[list[str]], all_mcp_list: list[MCPClientStatus]) -> list[MCPClientStatus]:
    """Choose MCP list"""
    if not mcp_ids:
        return []
    choosed_mcp_list = []
    all_mcp_dict = {client.client_id: client for client in all_mcp_list}
    for mcp_id in mcp_ids:
        mcp_client = all_mcp_dict.get(mcp_id)
        if mcp_client:
            choosed_mcp_list.append(MCPClientStatus.model_validate(mcp_client.model_dump()))
        else:
            choosed_mcp_list.append(MCPClientStatus(
                client_id=mcp_id,
                server_name="Unknown MCP",
                connected=False
            ))
    return choosed_mcp_list


class MCPClientStatusList(BaseModel):
    """MCP client status list model"""
    count: int = Field(..., description="Total number of clients")
    clients: List[MCPClientStatus] = Field(..., description="Client status list")


class MCPConfigResponse(BaseModel):
    """MCP configuration operation response model"""
    config_id: str = Field(..., description="Configuration ID")
    connection_success: bool = Field(..., description="Whether connection is successful")
    connection_error: Optional[str] = Field(None, description="Connection error information")


class CallToolResult(BaseModel):
    """Tool call result model"""
    success: bool = Field(..., description="Whether successful")
    error_message: Optional[str] = Field(None, description="Error message")
    response: Optional[Dict[str, Any]] = Field(None, description="Response result")


class MCPToolInfo(BaseModel):
    """MCP tool information model"""
    client_id: str = Field(..., description="Client ID")
    tool_name: str = Field(..., description="Tool name")
    description: str = Field(..., description="Tool description")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Tool parameters schema")
    tool_info: Any = Field(..., description="Tool info object")


class LocalMcpClientId:
    """Local MCP client ID constants"""
    LOCAL_DEFAULT = "local_default"
    MIOT_MANUAL_SCENES = "miot_manual_scenes"
    MIOT_DEVICES = "miot_devices"
    HA_AUTOMATIONS = "ha_automations"
    HA_DEVICES = "ha_devices"
