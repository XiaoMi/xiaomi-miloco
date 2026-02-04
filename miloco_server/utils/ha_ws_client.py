# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Home Assistant WebSocket 客户端
用于与 Home Assistant 进行实时通信
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class HAWebSocketClient:
    """Home Assistant WebSocket 客户端类"""

    def __init__(self):
        self._ws_url: Optional[str] = None
        self._token: Optional[str] = None
        self._websocket = None
        self._reconnect_interval = 5  # 重连间隔（秒）
        self._timeout = 10  # 命令超时时间（秒）
        self._message_id = 1
        self._pending_futures: Dict[int, asyncio.Future] = {}
        self._connected = False
        self._connected_event = asyncio.Event()
        self._connection_task: Optional[asyncio.Task] = None
        self._should_reconnect = False
        self._config_changed = False

    @property
    def is_connected(self) -> bool:
        """返回连接状态"""
        return self._connected

    @property
    def is_configured(self) -> bool:
        """返回是否已配置"""
        return bool(self._ws_url and self._token)

    def get_status(self) -> Dict[str, Any]:
        """获取连接状态信息"""
        return {
            "configured": self.is_configured,
            "connected": self._connected,
            "ws_url": self._ws_url if self._ws_url else None,
        }

    def configure(self, base_url: str, token: str):
        """
        配置 WebSocket 连接参数
        
        Args:
            base_url: Home Assistant 的 HTTP URL (如 http://192.168.1.100:8123)
            token: 长期访问令牌
        """
        # 将 HTTP URL 转换为 WebSocket URL
        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        if not ws_url.endswith("/"):
            ws_url += "/"
        ws_url += "api/websocket"
        
        # 检查配置是否变化
        if self._ws_url != ws_url or self._token != token:
            self._config_changed = True
            self._ws_url = ws_url
            self._token = token
            logger.info("HA WebSocket 配置已更新: %s", ws_url)

    async def start(self):
        """启动连接（如果已配置）"""
        if not self.is_configured:
            logger.info("HA WebSocket 未配置，跳过启动（这是正常的，如需使用请先配置 Home Assistant）")
            return
        
        if self._connection_task is None or self._connection_task.done():
            self._should_reconnect = True
            self._connection_task = asyncio.create_task(self._connection_loop())
            logger.info("HA WebSocket 连接任务已启动（后台运行，不阻塞主服务）")

    async def stop(self):
        """停止连接"""
        self._should_reconnect = False
        self._connected = False
        self._connected_event.clear()
        
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception as e:
                logger.warning("关闭 WebSocket 连接时出错: %s", e)
            self._websocket = None
        
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        
        # 清理等待中的 futures
        for future in self._pending_futures.values():
            if not future.done():
                future.set_exception(ConnectionError("WebSocket 连接已关闭"))
        self._pending_futures.clear()
        
        logger.info("HA WebSocket 连接已停止")

    async def reconnect(self):
        """重新连接（配置更新后调用）"""
        if self._config_changed:
            await self.stop()
            self._config_changed = False
            await self.start()

    async def _connection_loop(self):
        """主连接循环，包含自动重连逻辑"""
        while self._should_reconnect:
            try:
                if not self._ws_url or not self._token:
                    logger.warning("HA WebSocket 配置不完整")
                    break
                
                logger.info("正在连接到 Home Assistant: %s", self._ws_url)
                async with websockets.connect(self._ws_url) as ws:
                    self._websocket = ws
                    
                    # 进行认证
                    if await self._authenticate():
                        self._connected = True
                        self._connected_event.set()
                        logger.info("HA WebSocket 认证成功，已连接")
                        
                        # 开始监听
                        await self._listen()
                    else:
                        logger.error("HA WebSocket 认证失败")
            
            except ConnectionClosed as e:
                logger.warning("HA WebSocket 连接关闭: %s", e)
            except OSError as e:
                logger.warning("HA WebSocket 连接错误: %s", e)
            except Exception as e:
                logger.error("HA WebSocket 意外错误: %s", e)
            finally:
                self._connected = False
                self._connected_event.clear()
                self._websocket = None
            
            if self._should_reconnect:
                logger.info("将在 %d 秒后重连 HA WebSocket", self._reconnect_interval)
                await asyncio.sleep(self._reconnect_interval)

    async def _authenticate(self) -> bool:
        """处理认证"""
        try:
            # 等待 auth_required 消息
            message = await self._websocket.recv()
            data = json.loads(message)
            
            if data.get("type") != "auth_required":
                logger.error("预期收到 auth_required，实际收到: %s", data)
                return False

            # 发送 auth 消息
            await self._websocket.send(json.dumps({
                "type": "auth",
                "access_token": self._token
            }))

            # 等待 auth_ok 消息
            message = await self._websocket.recv()
            data = json.loads(message)
            
            if data.get("type") == "auth_ok":
                return True
            else:
                logger.error("HA WebSocket 认证失败: %s", data)
                return False
        except Exception as e:
            logger.error("HA WebSocket 认证过程中出错: %s", e)
            return False

    async def _listen(self):
        """监听传入的消息"""
        try:
            async for message in self._websocket:
                data = json.loads(message)
                msg_type = data.get("type")
                msg_id = data.get("id")

                if msg_type == "result":
                    # 处理命令结果
                    if msg_id in self._pending_futures:
                        future = self._pending_futures.pop(msg_id)
                        if future.cancelled() or future.done():
                            continue
                        if data.get("success"):
                            future.set_result(data.get("result"))
                        else:
                            error = data.get("error")
                            if isinstance(error, dict):
                                msg = error.get("message") or json.dumps(error, ensure_ascii=False)
                            else:
                                msg = str(error) if error is not None else "未知错误"
                            future.set_exception(RuntimeError(msg))
                
                elif msg_type == "event":
                    # 处理事件（可扩展）
                    pass
                
                elif msg_type == "pong":
                    pass
                else:
                    logger.debug("HA WebSocket 收到未知消息类型: %s", data)

        except ConnectionClosed:
            logger.info("HA WebSocket 监听循环结束")
            self._connected = False
            self._connected_event.clear()
            # 清理等待中的 futures
            pending = list(self._pending_futures.items())
            self._pending_futures.clear()
            for _, future in pending:
                if not future.done() and not future.cancelled():
                    future.set_exception(ConnectionError("HA WebSocket 连接已断开"))
            raise

    async def wait_until_connected(self, timeout: float = 10.0):
        """等待直到连接建立"""
        if self._connected:
            return
        
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"等待 HA WebSocket 连接超时 ({timeout}s)")

    async def send_command(self, command_type: str, **kwargs) -> Any:
        """
        发送命令到 Home Assistant 并等待结果
        
        Args:
            command_type: 命令类型
            **kwargs: 命令参数
            
        Returns:
            命令结果
        """
        if not self._connected:
            try:
                await self.wait_until_connected(timeout=5.0)
            except ConnectionError as e:
                raise ConnectionError("未连接到 Home Assistant WebSocket") from e

        if not self._websocket:
            raise ConnectionError("WebSocket 对象为空")

        msg_id = self._message_id
        self._message_id += 1

        payload = {
            "id": msg_id,
            "type": command_type,
            **kwargs
        }

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[msg_id] = future

        logger.debug("发送 HA WebSocket 消息 (ID: %d): %s", msg_id, command_type)
        await self._websocket.send(json.dumps(payload))
        
        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            if msg_id in self._pending_futures:
                del self._pending_futures[msg_id]
            raise TimeoutError(f"HA WebSocket 命令超时 ({self._timeout}s)")

    # --- 常用命令封装 ---

    async def get_config(self) -> dict:
        """获取 HA 配置信息"""
        return await self.send_command("get_config")

    async def get_states(self) -> list:
        """获取所有实体状态"""
        return await self.send_command("get_states")

    async def get_services(self) -> dict:
        """获取所有服务"""
        return await self.send_command("get_services")

    async def get_devices(self) -> list:
        """获取设备列表"""
        return await self.send_command("config/device_registry/list")

    async def get_areas(self) -> list:
        """获取区域列表"""
        return await self.send_command("config/area_registry/list")

    async def get_device_entities(self, device_id: str) -> dict:
        """获取指定设备的实体列表"""
        return await self.send_command("search/related", item_id=device_id, item_type="device")

    async def get_entity_registry(self) -> list:
        """获取实体注册表"""
        return await self.send_command("config/entity_registry/list")

    async def call_service(
        self, 
        domain: str, 
        service: str, 
        service_data: dict = None, 
        target: dict = None
    ) -> Any:
        """调用服务"""
        payload = {
            "domain": domain,
            "service": service
        }
        if service_data:
            payload["service_data"] = service_data
        if target:
            payload["target"] = target
            
        return await self.send_command("call_service", **payload)

    async def render_template(self, template: str) -> str:
        """渲染 Jinja2 模板"""
        return await self.send_command("render_template", template=template)

    async def fire_event(self, event_type: str, event_data: dict = None) -> Any:
        """触发事件"""
        return await self.send_command(
            "fire_event", 
            event_type=event_type, 
            event_data=event_data or {}
        )
