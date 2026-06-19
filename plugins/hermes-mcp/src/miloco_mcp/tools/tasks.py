"""Miloco MCP Tools — Tasks & Rules (任务管理与自动化规则)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..client import MilocoClient
from ..config import settings

mcp = FastMCP("miloco-tasks")


def _client() -> MilocoClient:
    return MilocoClient(settings.base_url, settings.get_token(), settings.timeout, settings.tls_verify)


@mcp.tool()
async def task_summary() -> str:
    """获取所有家庭任务的摘要列表（任务名、类型、状态、关联规则等）。"""
    client = _client()
    try:
        data = await client.get("/api/task/summary")
        if not data:
            return "No tasks found."
        if isinstance(data, list):
            lines = ["task_id\tname\ttype\tstatus"]
            for t in data:
                lines.append(
                    f"{t.get('task_id', '')}\t{t.get('name', '')}\t{t.get('type', '')}\t{t.get('status', '')}"
                )
            return "\n".join(lines)
        return str(data)
    finally:
        await client.aclose()


@mcp.tool()
async def task_get(
    task_id: Annotated[str, Field(description="任务 ID")],
) -> dict:
    """获取指定任务的详细信息（规则、记录、状态等）。"""
    client = _client()
    try:
        return await client.get(f"/api/task/{task_id}")
    finally:
        await client.aclose()


@mcp.tool()
async def task_update(
    task_id: Annotated[str, Field(description="任务 ID")],
    patch: Annotated[dict, Field(description="要更新的字段，如 {'name': '新名字', 'status': 'paused'}")],
) -> dict:
    """更新任务属性（改名、暂停等）。"""
    client = _client()
    try:
        return await client.patch(f"/api/task/{task_id}", json=patch)
    finally:
        await client.aclose()


@mcp.tool()
async def task_disable(
    task_id: Annotated[str, Field(description="任务 ID")],
) -> dict:
    """暂停/禁用一个任务。"""
    client = _client()
    try:
        return await client.post(f"/api/task/{task_id}/disable")
    finally:
        await client.aclose()


@mcp.tool()
async def task_enable(
    task_id: Annotated[str, Field(description="任务 ID")],
) -> dict:
    """启用一个已暂停的任务。"""
    client = _client()
    try:
        return await client.post(f"/api/task/{task_id}/enable")
    finally:
        await client.aclose()


@mcp.tool()
async def task_delete(
    task_id: Annotated[str, Field(description="任务 ID")],
    reason: Annotated[str, Field(description="删除原因: completed / expired / abandoned")] = "abandoned",
) -> dict:
    """删除一个任务（级联删除关联规则和记录）。"""
    client = _client()
    try:
        return await client.delete(f"/api/task/{task_id}?reason={reason}")
    finally:
        await client.aclose()


@mcp.tool()
async def rule_logs(
    limit: Annotated[int, Field(description="返回日志条数上限")] = 20,
) -> str:
    """获取规则引擎的触发日志。"""
    client = _client()
    try:
        data = await client.get("/api/rule/logs", params={"limit": limit})
        if not data:
            return "No rule logs found."
        return str(data)
    finally:
        await client.aclose()


@mcp.tool()
async def rule_trigger(
    rule_id: Annotated[str, Field(description="规则 ID")],
) -> dict:
    """手动触发一个规则（测试用）。"""
    client = _client()
    try:
        return await client.post(f"/api/rule/{rule_id}/trigger")
    finally:
        await client.aclose()


@mcp.tool()
async def task_record_get(
    task_id: Annotated[str, Field(description="任务 ID")],
) -> dict:
    """获取任务的累积记录（计数、事件列表、会话时长等）。"""
    client = _client()
    try:
        return await client.get(f"/api/task/{task_id}/record")
    finally:
        await client.aclose()


@mcp.tool()
async def task_record_increment(
    task_id: Annotated[str, Field(description="任务 ID")],
    count: Annotated[int, Field(description="增量计数")] = 1,
) -> dict:
    """给任务的累积计数加一（如记录「喝了一杯水」）。"""
    client = _client()
    try:
        return await client.post(f"/api/task/{task_id}/record/progress/increment", json={"count": count})
    finally:
        await client.aclose()


@mcp.tool()
async def task_record_event_append(
    task_id: Annotated[str, Field(description="任务 ID")],
    event: Annotated[str, Field(description="事件描述")],
) -> dict:
    """给任务追加一条事件记录。"""
    client = _client()
    try:
        return await client.post(f"/api/task/{task_id}/record/event/append", json={"event": event})
    finally:
        await client.aclose()
