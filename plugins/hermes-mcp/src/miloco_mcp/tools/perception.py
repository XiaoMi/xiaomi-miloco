"""Miloco MCP Tools — Perception (摄像头感知与视觉理解)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..client import MilocoClient
from ..config import settings

mcp = FastMCP("miloco-perception")


def _client() -> MilocoClient:
    return MilocoClient(settings.base_url, settings.get_token(), settings.timeout, settings.tls_verify)


@mcp.tool()
async def perceive(
    camera_id: Annotated[str, Field(description="摄像头设备 ID (did)")],
    channel: Annotated[str, Field(description="视频通道，通常为 'channel-0'")] = "channel-0",
    prompt: Annotated[str | None, Field(description="自定义感知提示词，如 '有没有人在门口'。留空使用默认感知。")] = None,
) -> dict:
    """调用摄像头进行一次实时感知（看画面、听声音），返回场景描述和检测结果。

    用于回答「看看门口有没有人」「客厅在干嘛」等问题。"""
    client = _client()
    try:
        body: dict = {"camera_id": camera_id, "channel": channel}
        if prompt:
            body["prompt"] = prompt
        return await client.post("/api/perception/perceive", json=body)
    finally:
        await client.aclose()


@mcp.tool()
async def perception_engine_status() -> dict:
    """获取实时感知引擎的运行状态（是否启动、处理帧率、感知摄像头列表等）。"""
    client = _client()
    try:
        return await client.get("/api/perception/engine/status")
    finally:
        await client.aclose()


@mcp.tool()
async def perception_engine_start() -> dict:
    """启动实时感知引擎，开始对已启用的摄像头进行持续感知分析。"""
    client = _client()
    try:
        return await client.post("/api/perception/engine/start")
    finally:
        await client.aclose()


@mcp.tool()
async def perception_engine_stop() -> dict:
    """停止实时感知引擎。"""
    client = _client()
    try:
        return await client.post("/api/perception/engine/stop")
    finally:
        await client.aclose()


@mcp.tool()
async def perception_logs(
    limit: Annotated[int, Field(description="返回日志条数上限")] = 20,
    since: Annotated[str | None, Field(description="起始时间 ISO 格式")] = None,
    after: Annotated[str | None, Field(description="此 ID 之后的日志")] = None,
) -> list[dict] | str:
    """获取感知引擎的历史感知日志（检测到的人、物、事件等）。"""
    client = _client()
    try:
        params: dict = {"limit": limit}
        if since:
            params["since"] = since
        if after:
            params["after"] = after
        data = await client.get("/api/perception/logs", params=params)
        if not data:
            return "No perception logs found."
        return data
    finally:
        await client.aclose()


@mcp.tool()
async def perception_cameras() -> list[dict] | str:
    """获取所有摄像头设备列表及其感知状态（是否启用、在线状态）。"""
    client = _client()
    try:
        data = await client.get("/api/perception/devices")
        if not data:
            return "No cameras found."
        return data
    finally:
        await client.aclose()


@mcp.tool()
async def camera_perception_enable(
    did: Annotated[str, Field(description="摄像头设备 ID (did)")],
) -> dict:
    """启用指定摄像头的感知功能（开始分析该摄像头画面）。"""
    client = _client()
    try:
        return await client.post("/api/miot/scope/cameras/enable", json={"did": did})
    finally:
        await client.aclose()


@mcp.tool()
async def camera_perception_disable(
    did: Annotated[str, Field(description="摄像头设备 ID (did)")],
) -> dict:
    """禁用指定摄像头的感知功能。"""
    client = _client()
    try:
        return await client.post("/api/miot/scope/cameras/disable", json={"did": did})
    finally:
        await client.aclose()
