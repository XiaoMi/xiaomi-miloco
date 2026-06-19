"""Miloco MCP Tools — Person Identity & Home Profile (成员识别与家庭档案)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..client import MilocoClient
from ..config import settings

mcp = FastMCP("miloco-home")


def _client() -> MilocoClient:
    return MilocoClient(settings.base_url, settings.get_token(), settings.timeout, settings.tls_verify)


# ── Person Identity ──────────────────────────────────────────────


@mcp.tool()
async def person_list() -> str:
    """获取所有已注册的家庭成员列表（姓名、ID、样本数等）。"""
    client = _client()
    try:
        data = await client.get("/api/person/persons")
        if not data:
            return "No persons registered."
        lines = ["person_id\tname\tsamples"]
        persons = data if isinstance(data, list) else data.get("persons", [])
        for p in persons:
            lines.append(
                f"{p.get('person_id', '')}\t{p.get('name', '')}\t{p.get('sample_count', p.get('samples', ''))}"
            )
        return "\n".join(lines)
    finally:
        await client.aclose()


@mcp.tool()
async def person_create(
    name: Annotated[str, Field(description="成员姓名")],
) -> dict:
    """创建一个新的家庭成员档案。"""
    client = _client()
    try:
        return await client.post("/api/person/persons", json={"name": name})
    finally:
        await client.aclose()


@mcp.tool()
async def person_update(
    person_id: Annotated[str, Field(description="成员 ID")],
    name: Annotated[str | None, Field(description="新姓名")] = None,
) -> dict:
    """更新家庭成员信息（改名等）。"""
    client = _client()
    try:
        body = {}
        if name:
            body["name"] = name
        return await client.put(f"/api/person/persons/{person_id}", json=body)
    finally:
        await client.aclose()


@mcp.tool()
async def person_delete(
    person_id: Annotated[str, Field(description="成员 ID")],
) -> dict:
    """删除一个家庭成员。"""
    client = _client()
    try:
        return await client.delete(f"/api/person/persons/{person_id}")
    finally:
        await client.aclose()


@mcp.tool()
async def person_samples(
    person_id: Annotated[str, Field(description="成员 ID")],
) -> dict:
    """查看某成员的注册样本（人脸/身形照片）。"""
    client = _client()
    try:
        return await client.get(f"/api/person/persons/{person_id}/samples")
    finally:
        await client.aclose()


# ── Home Profile (家庭档案) ──────────────────────────────────────


@mcp.tool()
async def home_profile_list() -> str:
    """获取家庭档案的全部条目（成员偏好、习惯、家庭规则等）。

    档案内容由感知和交互自动积累，也包括用户主动告知的信息。
    用于了解家庭成员的习惯和偏好。"""
    client = _client()
    try:
        data = await client.get("/api/home_profile/entries")
        if not data:
            return "No home profile entries."
        return str(data)
    finally:
        await client.aclose()


@mcp.tool()
async def home_profile_rendered() -> str:
    """获取已渲染的家庭档案（Markdown 格式，人类可读）。

    这是经过整理和格式化的家庭档案摘要。"""
    client = _client()
    try:
        data = await client.get("/api/home_profile/rendered")
        return str(data) if data else "No rendered profile available."
    finally:
        await client.aclose()


@mcp.tool()
async def home_profile_write(
    entries: list[dict],
    target: Annotated[str, Field(description="写入目标: profile（正式档案）或 candidate（候选区）")] = "profile",
) -> dict:
    """写入家庭档案条目（成员偏好、习惯、家庭规则等）。

    entries 格式示例：
    [{"subject": "妈妈", "category": "preference", "content": "不喜欢灯太亮", "evidence": "用户原话"}]
    """
    client = _client()
    try:
        return await client.post("/api/home_profile/profile:write", json={"entries": entries, "target": target})
    finally:
        await client.aclose()


@mcp.tool()
async def home_profile_commit() -> dict:
    """提交候选区的档案条目到正式区（确认写入）。"""
    client = _client()
    try:
        return await client.post("/api/home_profile/commit")
    finally:
        await client.aclose()


# ── Admin / Status ───────────────────────────────────────────────


@mcp.tool()
async def miloco_status() -> dict:
    """获取 Miloco 系统整体状态（后端版本、感知引擎、MIoT 连接、数据库等）。"""
    client = _client()
    try:
        return await client.get("/api/admin/status")
    finally:
        await client.aclose()


@mcp.tool()
async def token_usage_summary() -> dict:
    """获取大模型 token 使用量统计（感知消耗、Agent 消耗、总消耗等）。"""
    client = _client()
    try:
        return await client.get("/api/admin/token-usage")
    finally:
        await client.aclose()


@mcp.tool()
async def token_usage_daily() -> dict:
    """获取每日 token 使用量明细。"""
    client = _client()
    try:
        return await client.get("/api/admin/token-usage/daily")
    finally:
        await client.aclose()
