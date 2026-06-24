"""Miloco MCP Tools — Devices (查询与控制米家设备)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..client import MilocoClient
from ..config import settings

mcp = FastMCP("miloco-devices")


def _client() -> MilocoClient:
    return MilocoClient(settings.base_url, settings.get_token(), settings.timeout, settings.tls_verify)


@mcp.tool()
async def device_list(
    refresh: Annotated[bool, Field(description="是否刷新设备列表缓存")] = False,
) -> str:
    """获取所有米家智能家居设备列表（含设备ID、名称、型号、房间、在线状态等）。

    返回格式为 TSV 文本，每行一台设备。字段：did, name, model, room, online, spec_name。
    用于查找设备 did 以便后续控制。"""
    client = _client()
    try:
        data = await client.get("/api/miot/device_list", params={"refresh": str(refresh).lower()})
        if not data:
            return "No devices found. Account may not be bound yet."
        # Format as readable text
        lines = ["did\tname\tmodel\troom\tonline\tspec_name"]
        devices = data if isinstance(data, list) else data.get("devices", [])
        for d in devices:
            lines.append(
                f"{d.get('did', '')}\t{d.get('name', '')}\t{d.get('model', '')}\t"
                f"{d.get('room_name', d.get('room', ''))}\t{d.get('online', False)}\t"
                f"{d.get('spec_name', '')}"
            )
        return "\n".join(lines)
    finally:
        await client.aclose()


@mcp.tool()
async def device_status(
    did: Annotated[str, Field(description="设备 ID (did)")],
    iids: Annotated[str | None, Field(description="属性 IID 列表，逗号分隔，如 'prop.2.1,prop.2.2'。留空返回全部。")] = None,
) -> dict:
    """获取指定设备的属性状态（开关状态、运行状态、电量、温度等）。"""
    client = _client()
    try:
        params = {"iid": iids} if iids else None
        return await client.get(f"/api/miot/devices/{did}/status", params=params)
    finally:
        await client.aclose()


@mcp.tool()
async def device_control(
    did: Annotated[str, Field(description="设备 ID (did)")],
    type: Annotated[str, Field(description="控制类型: set_properties / call_action")],
    params: Annotated[dict, Field(description="控制参数，如 {'properties': [{'iid': 'prop.2.1', 'value': True}]} 或 {'siid': 2, 'aiid': 1, 'in': []}")],
) -> dict:
    """控制米家设备：开关灯、调节空调温度/模式/风速、控制窗帘开合等。

    type 取值：
    - set_properties: 批量设置属性 (params: {properties: [{iid: "prop.2.1", value: True}, ...]})
    - call_action: 调用动作 (params: {siid, aiid, in: [...]})

    iid 格式为 "prop.{siid}.{piid}"，如 "prop.2.1" 表示开关。
    """
    client = _client()
    try:
        return await client.post(f"/api/miot/devices/{did}/control", json={"type": type, **params})
    finally:
        await client.aclose()


@mcp.tool()
async def device_spec(
    did: Annotated[str, Field(description="设备 ID (did)")],
) -> dict:
    """获取设备的 MIoT Spec（能力描述），包含所有可操作的属性和动作定义。"""
    client = _client()
    try:
        return await client.get(f"/api/miot/devices/{did}/spec")
    finally:
        await client.aclose()


@mcp.tool()
async def scene_list() -> str:
    """获取所有可用的米家手动场景列表（回家模式、离家模式、睡眠模式等）。"""
    client = _client()
    try:
        data = await client.get("/api/miot/scenes")
        if not data:
            return "No scenes found."
        lines = ["scene_id\tscene_name"]
        scenes = data if isinstance(data, list) else data.get("scenes", [])
        for s in scenes:
            lines.append(f"{s.get('scene_id', '')}\t{s.get('scene_name', s.get('name', ''))}")
        return "\n".join(lines)
    finally:
        await client.aclose()


@mcp.tool()
async def scene_trigger(
    scene_id: Annotated[str, Field(description="场景 ID")],
) -> dict:
    """触发一个米家手动场景（如回家模式、离家模式、睡眠模式）。"""
    client = _client()
    try:
        return await client.post(f"/api/miot/scenes/{scene_id}/trigger")
    finally:
        await client.aclose()


@mcp.tool()
async def home_info(
    refresh: Annotated[bool, Field(description="是否强制刷新")] = False,
) -> dict:
    """获取全屋信息概览（家庭名称、房间列表、设备总数、在线设备数等）。"""
    client = _client()
    try:
        return await client.get("/api/miot/home", params={"refresh": str(refresh).lower()})
    finally:
        await client.aclose()


@mcp.tool()
async def user_info() -> dict:
    """获取当前绑定的小米账号和家庭信息。"""
    client = _client()
    try:
        return await client.get("/api/miot/user_info")
    finally:
        await client.aclose()
