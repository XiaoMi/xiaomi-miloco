# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MCP client manager module for managing MCP clients and tools."""

import asyncio
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from miloco_server.proxy.ha_proxy import HAProxy
from miloco_server.proxy.miot_proxy import MiotProxy
from miloco_server.mcp.mcp_client import LocalMCPConfig, MCPClientBase, MCPClientFactory, TransportType, MCPClientConfig
from miloco_server.mcp.local_mcp_servers import LocalMCPServerFactory
from miloco_server.dao.mcp_config_dao import MCPConfigDAO
from miloco_server.schema.mcp_schema import LocalMcpClientId, MCPClientStatus, CallToolResult, MCPToolInfo
from miloco_server.utils.mcp_util import MCPConfigConverter
from miot.mcp import (
    MIoTManualSceneMcp,
    HomeAssistantAutomationMcp,
    HomeAssistantDeviceMcp,
    MIoTDeviceMcp,
    MIoTManualSceneMcpInterface,
    MIoTDeviceMcpInterface,
    HomeAssistantAutomationMcpInterface,
    HomeAssistantDeviceMcpInterface
)

logger = logging.getLogger(__name__)


@dataclass
class ToolInfo:
    """Tool information"""
    client_id: str
    client_name: str
    tool_name: str
    client: MCPClientBase


class MCPClientManager:
    """MCP Client Manager"""

    def __init__(self, config_dao: MCPConfigDAO, miot_proxy: MiotProxy, ha_proxy: HAProxy):
        # Simplified constructor, only basic initialization, async initialization through factory method
        self.clients: Dict[str, MCPClientBase] = {}
        self.config_dao = config_dao
        self._initialized = False
        self.miot_proxy = miot_proxy
        self.ha_proxy = ha_proxy

    @classmethod
    async def create(cls, config_dao: MCPConfigDAO, miot_proxy: MiotProxy, ha_proxy: HAProxy) -> "MCPClientManager":
        """
        Async factory method to ensure initialization in correct async context

        Args:
            config_dao: MCP configuration DAO
            miot_proxy: MIoT proxy
            ha_proxy: Home Assistant proxy
        Returns:
            MCPClientManager: Fully initialized instance
        """
        # Create instance and perform async initialization
        instance = cls(config_dao, miot_proxy, ha_proxy)
        await instance._init_all_clients()
        return instance

    async def _init_all_clients(self):
        """Initialize all clients (including configured clients and default clients)"""
        if self._initialized:
            return

        logger.info("Starting to initialize all MCP clients...")
        try:
            # Initialize all clients and default clients in parallel
            await asyncio.gather(
                self._init_default_clients(),
                self._init_clients(),
                return_exceptions=True
            )
            self._initialized = True
            logger.info("All MCP clients initialization completed")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Error occurred while initializing MCP clients: %s", str(e))
            raise

    # Initialization Methods

    async def _init_clients(self):
        """Initialize all MCP Clients"""
        logger.info("init mcp clients")
        configs = self.config_dao.get_all()
        tasks = []

        for config in configs:
            try:
                if not config.enable:
                    logger.info("MCP client %s is disabled, skipping", config.name)
                    continue
                client_config = MCPConfigConverter.to_mcp_client_config(config)
                client_transport_type = config.access_type

                tasks.append(asyncio.create_task(self._add_client(client_transport_type, client_config)))
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Failed to convert MCP config: %s, error: %s", config.name, str(e))
                continue

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error("Failed to initialize MCP Client: %s, error: %s", configs[i].name, str(result))
                elif result:
                    logger.info("Successfully initialized MCP Client: %s", configs[i].name)
                else:
                    logger.warning("Failed to initialize MCP Client: %s", configs[i].name)

    async def _init_default_clients(self):
        """Initialize default MCP Clients"""
        logger.info("init default mcp clients")
        
        # Initialize local MCP servers
        try:
            await self._init_local_mcp_servers()
        except Exception as e:
            logger.error("Failed to initialize local MCP servers: %s", e)
        
        # Initialize MIoT MCP clients
        try:
            await self.init_miot_mcp_clients()
        except Exception as e:
            logger.error("Failed to initialize MIoT MCP clients: %s", e)
        
        # Initialize HA MCP clients (optional, failure won't block startup)
        try:
            await self.init_ha_automations()
        except Exception as e:
            logger.warning("Failed to initialize HA automations MCP (non-blocking): %s", e)
        
        try:
            await self.init_ha_devices()
        except Exception as e:
            logger.warning("Failed to initialize HA devices MCP (non-blocking): %s", e)
        
        logger.info("init default mcp clients done")

    async def init_miot_mcp_clients(self):
        """Initialize MIoT MCP Clients"""
        miot_client = self.miot_proxy.miot_client
        try:
            miot_scenes_mcp = MIoTManualSceneMcp(
                interface=MIoTManualSceneMcpInterface(
                    translate_async=miot_client.i18n.translate_async,
                    get_manual_scenes_async=miot_client.get_manual_scenes_async,
                    trigger_manual_scene_async=miot_client.run_manual_scene_async,
                    send_app_notify_async=miot_client.send_app_notify_once_async,
                )
            )
            await miot_scenes_mcp.init_async()
            await self._add_client(
                transport_type=TransportType.LOCAL,
                config=LocalMCPConfig(
                    client_id=LocalMcpClientId.MIOT_MANUAL_SCENES,
                    server_name="米家自动化 (MIoT Automation)",
                    mcp_server=miot_scenes_mcp.mcp_instance))
            logger.info("Successfully initialized MIoT Manual Scene MCP client")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Failed to initialize MIoT Manual Scene MCP client: %s", e)
        # Add XiaomiHome Device MCP Client
        try:
            miot_devices_mcp = MIoTDeviceMcp(
                interface=MIoTDeviceMcpInterface(
                    translate_async=miot_client.i18n.translate_async,
                    get_homes_async=miot_client.get_homes_async,
                    get_devices_async=miot_client.get_devices_async,
                    get_prop_async=miot_client.http_client.get_prop_async,
                    set_prop_async=miot_client.http_client.set_prop_async,
                    action_async=miot_client.http_client.action_async
                ),
                spec_parser=miot_client.spec_parser,
            )
            await miot_devices_mcp.init_async()
            await self._add_client(
                transport_type=TransportType.LOCAL,
                config=LocalMCPConfig(
                    client_id=LocalMcpClientId.MIOT_DEVICES,
                    server_name="米家设备控制 (MIoT Device Control)",
                    mcp_server=miot_devices_mcp.mcp_instance))
            logger.info("Successfully initialized MIoT Device MCP client")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Failed to initialize MIoT Device MCP client: %s", e)

    async def init_ha_automations(self) -> bool:
        """Initialize Home Assistant Automation MCP Client"""
        miot_client = self.miot_proxy.miot_client
        ha_client = self.ha_proxy.ha_client

        # Add Home Assistant Automation MCP Client
        if ha_client:
            try:
                ha_automations_mcp = HomeAssistantAutomationMcp(
                    interface=HomeAssistantAutomationMcpInterface(
                        translate_async=miot_client.i18n.translate_async,
                        get_automations_async=ha_client.get_automations_async,
                        trigger_automation_async=ha_client.trigger_automation_async,
                    ))
                await ha_automations_mcp.init_async()
                await self._add_client(
                    transport_type=TransportType.LOCAL,
                    config=LocalMCPConfig(
                        client_id=LocalMcpClientId.HA_AUTOMATIONS,
                        server_name="Home Assistant自动化 (Home Assistant Automation)",
                        mcp_server=ha_automations_mcp.mcp_instance))
                logger.info("Successfully initialized Home Assistant MCP client")
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Failed to initialize Home Assistant MCP client: %s", e)
        else:
            logger.warning("Home Assistant client not initialized")

    async def init_ha_devices(self) -> bool:
        """Initialize Home Assistant Device Control MCP Client"""
        miot_client = self.miot_proxy.miot_client
        ha_ws_client = self.ha_proxy.ha_ws_client

        # 只有当 WebSocket 客户端已配置时才初始化
        if ha_ws_client.is_configured:
            try:
                ha_devices_mcp = HomeAssistantDeviceMcp(
                    interface=HomeAssistantDeviceMcpInterface(
                        translate_async=miot_client.i18n.translate_async,
                        get_devices_async=self.ha_proxy.get_ha_devices,
                        get_areas_async=self.ha_proxy.get_ha_areas,
                        get_device_entities_async=self.ha_proxy.get_ha_device_entities,
                        get_states_async=self.ha_proxy.get_ha_states,
                        call_service_async=ha_ws_client.call_service,
                    ))
                await ha_devices_mcp.init_async()
                await self._add_client(
                    transport_type=TransportType.LOCAL,
                    config=LocalMCPConfig(
                        client_id=LocalMcpClientId.HA_DEVICES,
                        server_name="HA设备控制 (HA Device Control)",
                        mcp_server=ha_devices_mcp.mcp_instance))
                logger.info("Successfully initialized Home Assistant Device MCP client")
                return True
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Failed to initialize Home Assistant Device MCP client: %s", e)
                return False
        else:
            logger.warning("Home Assistant WebSocket client not configured, skipping HA Device MCP")
            return False

    async def _init_local_mcp_servers(self):
        """Initialize local MCP servers"""
        logger.info("Initializing local MCP servers...")
        try:
            # Create all local MCP servers
            local_servers = await LocalMCPServerFactory.create_all_servers()

            # Register local servers as MCP clients
            for client_id, server in local_servers.items():
                try:
                    await self._add_client(
                        transport_type=TransportType.LOCAL,
                        config=LocalMCPConfig(
                            client_id=client_id,
                            server_name=server.name,
                            mcp_server=server.mcp_instance
                        )
                    )
                    logger.info("Successfully initialized local MCP server: %s", server.name)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logger.error("Failed to initialize local MCP server %s: %s", server.name, e)

            logger.info("Local MCP servers initialization completed, total %d servers", len(local_servers))

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Failed to initialize local MCP servers: %s", e)
            raise

    # Client Management
    async def _add_client(self, transport_type: TransportType, config: MCPClientConfig) -> bool:
        """Add MCP Client"""
        client_id = config.id
        if client_id in self.clients:
            logger.warning("Client '%s' already exists, will be overwritten", client_id)
            await self.clients[client_id].disconnect()

        client = MCPClientFactory.create_client(transport_type, config)
        self.clients[client_id] = client
        if await client.connect():
            return True

        return False

    async def add_client(self, transport_type: TransportType, config: MCPClientConfig) -> bool:
        """Add MCP Client (public interface)"""
        return await self._add_client(transport_type, config)

    async def update_client(self, transport_type: TransportType, config: MCPClientConfig) -> bool:
        """Update MCP Client"""
        client_id = config.id
        if client_id not in self.clients:
            logger.warning("Client '%s' does not exist, attempting to add new client", client_id)
            return await self._add_client(transport_type, config)

        await self.clients[client_id].disconnect()
        client = MCPClientFactory.create_client(transport_type, config)
        if await client.connect():
            self.clients[client_id] = client
            return True
        return False

    async def remove_client(self, client_id: str):
        """Remove MCP Client"""
        if client_id in self.clients:
            await self.clients[client_id].disconnect()
            del self.clients[client_id]

    def has_client(self, client_id: str) -> bool:
        """Check if client exists"""
        return client_id in self.clients

    def get_client(self, client_id: str) -> Optional[MCPClientBase]:
        """Get MCP Client"""
        logger.debug("mcp_client_manager get_client: %s, clients: %s", client_id, self.clients)
        return self.clients.get(client_id)

    async def cleanup(self):
        """Clean up all client connections"""
        # Disconnect all client connections
        for client in self.clients.values():
            await client.disconnect()
        self.clients.clear()


    async def get_all_clients_status(self) -> List[MCPClientStatus]:
        """Get status of all clients (quick detection)"""
        # Concurrently ping all clients, client.ping() internally determines connection status
        ping_tasks = []
        client_items = list(filter(lambda x: x[0] != LocalMcpClientId.LOCAL_DEFAULT, self.clients.items()))
        for client_id, client in client_items:
            ping_tasks.append(self._verify_client_connection(client_id, client))

        # Execute all ping checks concurrently
        ping_results = await asyncio.gather(*ping_tasks, return_exceptions=True)

        # Build results
        results = []
        for i, (client_id, client) in enumerate(client_items):
            ping_result = ping_results[i]
            if isinstance(ping_result, Exception):
                logger.debug("Client %s ping check exception: %s", client_id, ping_result)
                connected = False
            else:
                connected = ping_result

            results.append(MCPClientStatus(
                client_id=client_id,
                server_name=client.config.server_name,
                connected=connected
            ))

        results.sort(key=lambda x: x.server_name)
        return results

    async def _verify_client_connection(self, client_id: str, client: MCPClientBase) -> bool:
        """Verify client connection status"""
        try:
            return await asyncio.wait_for(client.ping(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.debug("Client %s ping timeout", client_id)
            return False
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Client %s ping exception: %s", client_id, e)
            return False

    def _validate_client(self, client_id: str) -> Optional[MCPClientBase]:
        """Validate if client exists and is connected"""
        client = self.get_client(client_id)
        if not client:
            logger.error("Client %s does not exist", client_id)
            return None
        if not client.is_connected():
            logger.warning("Client %s is not connected", client_id)
            return None
        return client

    # Tool Invocation
    async def call_tool(self, client_id: str, tool_name: str, arguments: Dict[str, Any]) -> CallToolResult:
        """
        Call MCP tool (unified entry point)

        Args:
            client_id: Client ID
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            CallToolResult: Tool call result
        """
        tool_info = self._get_tool_info(client_id, tool_name)
        if not tool_info:
            return CallToolResult(success=False, error_message="Tool not found", response=None)

        return await self._execute_tool_call(tool_info, arguments)

    def _get_tool_info(self, client_id: str, tool_name: str) -> Optional[ToolInfo]:
        """Get tool information"""
        try:
            client = self._validate_client(client_id)
            if not client:
                logger.error("Client %s does not exist", client_id)
                return None
            tool = client.get_tool(tool_name)
            if not tool:
                logger.error("Tool %s does not exist in client %s", tool_name, client_id)
                return None
            return ToolInfo(client_id, client.config.server_name, tool_name, client)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Failed to get tool info for client %s, tool %s: %s", client_id, tool_name, e)
            return None

    async def _execute_tool_call(self, tool_info: ToolInfo, arguments: Dict[str, Any]) -> CallToolResult:
        """Execute tool call"""
        try:
            logger.info("Calling tool %s in client %s (%s), arguments type: %s, arguments: %s",
                        tool_info.tool_name, tool_info.client_name, tool_info.client_id, type(arguments), arguments)
            result = await tool_info.client.call_tool(tool_info.tool_name, arguments)
            return CallToolResult(success=True, error_message=None, response=result)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Failed to call tool %s: %s", tool_info.tool_name, e)
            return CallToolResult(success=False, error_message=str(e), response=None)

    # Tool Information Retrieval
    def get_tools_by_ids(self, client_ids: Optional[List[str]]) -> List[MCPToolInfo]:
        """
        Get all tool information and add client prefix

        Args:
            client_ids: Specified client ID list, if None then get all clients
        Returns:
            List[MCPToolInfo]: List containing tool information
        """
        # If client_ids is None, get all client IDs
        if client_ids is None:
            client_ids = list(self.clients.keys())

        tools = []

        # Remove duplicate client IDs to avoid processing the same client multiple times
        unique_client_ids = list(dict.fromkeys(client_ids))

        # Determine clients to process - filter out non-existent clients in one pass
        clients_to_process: List[tuple[str, MCPClientBase]] = [
            (client_id, self.clients[client_id])
            for client_id in unique_client_ids
            if client_id in self.clients
        ]

        if not clients_to_process:
            return tools

        # Process tools for each client
        for client_id, client in clients_to_process:
            if not client.is_connected():
                continue

            try:
                client_tools = client.get_tools()
                for tool in client_tools:
                    tools.append(MCPToolInfo(
                        client_id=client_id,
                        tool_name=tool.name,
                        description=tool.description or f"MCP server {client.config.server_name} tools: {tool.name}",
                        parameters=tool.inputSchema,
                        tool_info=tool
                    ))
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Failed to get tools for client %s: %s", client_id, e)
                continue

        return tools
