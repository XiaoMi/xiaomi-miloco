import asyncio
import json
import logging
import websockets
from typing import Dict, Any, Optional, Callable, Awaitable

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_client")

class HomeAssistantClient:
    def __init__(self):
        self.ws_url = "ws://10.126.126.1:8123/api/websocket"
        self.token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI1NmQ1OGE3MzMzNDI0MDQ3YmZiYjBiMDc3ZGUyNmM2YiIsImlhdCI6MTc2NjM4NDYyOCwiZXhwIjoyMDgxNzQ0NjI4fQ.3AzJBwqiR4OHkw0NJBV1rHwmp1-ZECcy4BgUJYxl68c"
        self.websocket = None
        self.reconnect_interval = 5
        self.timeout = 10
        self._message_id = 1
        self._pending_futures: Dict[int, asyncio.Future] = {}
        self._event_callbacks: Dict[int, Callable[[dict], Awaitable[None]]] = {}
        self._connected = False
        self._connection_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._ha_task = None

    # 确保 HA 客户端连接循环正在运行，并等待连接就绪
    async def ensure_connection(self):
        # 启动连接任务（如果尚未启动）
        if self._ha_task is None or self._ha_task.done():
            logger.info("正在启动 Home Assistant 连接循环...")
            _ha_task = asyncio.create_task(ha_client.connect())

        # 等待实际连接建立
        try:
            await ha_client.wait_until_connected(timeout=10.0)
        except Exception as e:
            logger.error(f"确保连接时出错: {e}")
            # 这里不抛出异常，让后续的 send_command 尝试再次处理或报错

    async def connect(self):
        """主连接循环，包含自动重连逻辑。"""
        while True:
            try:
                logger.info(f"正在连接到 Home Assistant: {self.ws_url}...")
                async with websockets.connect(self.ws_url) as ws:
                    self.websocket = ws
                    
                    # 进行认证
                    if await self._authenticate():
                        self._connected = True
                        self._connected_event.set()
                        logger.info("认证成功。已连接到 Home Assistant。")
                        
                        # 开始监听循环
                        await self._listen()
                    else:
                        logger.error("认证失败。5秒后重试...")
            
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                logger.warning(f"连接丢失: {e}。{self.reconnect_interval} 秒后重连...")
                self._connected = False
                self._connected_event.clear()
            except Exception as e:
                logger.error(f"发生意外错误: {e}。{self.reconnect_interval} 秒后重连...")
                self._connected = False
                self._connected_event.clear()
            
            # 确保清理状态
            self._connected = False
            self._connected_event.clear()
            self.websocket = None
            await asyncio.sleep(self.reconnect_interval)

    async def wait_until_connected(self, timeout: float = 10.0):
        """等待直到连接建立。"""
        if self._connected:
            return
        
        try:
            logger.info(f"等待 Home Assistant 连接 (超时: {timeout}s)...")
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"等待 Home Assistant 连接超时 ({timeout}s)")

    async def _authenticate(self) -> bool:
        """处理认证阶段。"""
        try:
            # 等待 auth_required 消息
            message = await self.websocket.recv()
            data = json.loads(message)
            
            if data.get("type") != "auth_required":
                logger.error(f"预期收到 auth_required，实际收到: {data}")
                return False

            # 发送 auth 消息
            await self.websocket.send(json.dumps({
                "type": "auth",
                "access_token": self.token
            }))

            # 等待 auth_ok 消息
            message = await self.websocket.recv()
            data = json.loads(message)
            
            if data.get("type") == "auth_ok":
                return True
            else:
                logger.error(f"认证失败: {data}")
                return False
        except Exception as e:
            logger.error(f"认证过程中出错: {e}")
            return False

    async def _listen(self):
        """监听传入的消息。"""
        try:
            async for message in self.websocket:
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
                    # 处理事件（尚未完全实现用于通用工具，
                    # 但已准备好用于订阅回调）
                    # 目前我们只是记录日志或处理已跟踪的特定订阅 ID
                    pass
                
                elif msg_type == "pong":
                    pass

                else:
                    logger.debug(f"收到未知消息类型: {data}")

        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket 连接已关闭。")
            self._connected = False
            self._connected_event.clear()
            pending = list(self._pending_futures.items())
            self._pending_futures.clear()
            for _, future in pending:
                if not future.done() and not future.cancelled():
                    future.set_exception(ConnectionError("Home Assistant WebSocket 连接已断开"))
            raise

    async def send_command(self, command_type: str, **kwargs) -> Any:
        """发送命令到 Home Assistant 并等待结果。"""
        logger.info(f"准备发送命令: {command_type}, 参数: {kwargs}")
        
        # 再次确认连接状态
        if not self._connected:
            logger.warning("发送命令时发现未连接，尝试等待连接...")
            try:
                await self.wait_until_connected(timeout=5.0)
            except ConnectionError as e:
                raise ConnectionError("未连接到 Home Assistant，且重连超时") from e

        if not self.websocket:
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

        logger.info(f"发送 WebSocket 消息 (ID: {msg_id}): {payload}")
        await self.websocket.send(json.dumps(payload))
        
        try:
            # 等待结果，带超时控制
            # 使用配置文件中的超时时间
            timeout = self.timeout
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.info(f"命令 {command_type} (ID: {msg_id}) 执行成功")
            return result
        except asyncio.TimeoutError:
            if msg_id in self._pending_futures:
                del self._pending_futures[msg_id]
            error_msg = f"调用 Home Assistant 超时 (超过 {timeout} 秒) - ID: {msg_id}"
            logger.error(error_msg)
            # 这里抛出异常，让上层工具捕获并返回给 AI
            raise TimeoutError(error_msg)

    # --- 常用 HA 命令的辅助封装 ---

    async def get_config(self) -> dict:
        """返回 HA 配置信息。"""
        return await self.send_command("get_config")

    async def get_states(self) -> list:
        """返回所有实体的状态列表。"""
        return await self.send_command("get_states")

    async def get_services(self) -> dict:
        """返回所有服务的列表。"""
        return await self.send_command("get_services")

    async def get_devices(self) -> dict:
        """返回 HA 设备列表。"""
        return await self.send_command("config/device_registry/list")

    async def get_device_entities(self, device_id: str) -> dict:
        """返回 HA 指定设备的实体列表。"""
        return await self.send_command("search/related", item_id=device_id, item_type="device")

    async def get_areas(self) -> dict:
        """返回 HA 区域位置列表"""
        return await self.send_command("config/area_registry/list")

    async def call_service(self, domain: str, service: str, service_data: dict = None, target: dict = None) -> Any:
        """调用服务。"""
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
        """渲染 Jinja2 模板。"""
        return await self.send_command("render_template", template=template)

    async def fire_event(self, event_type: str, event_data: dict = None) -> Any:
        """触发事件。"""
        return await self.send_command("fire_event", event_type=event_type, event_data=event_data or {})

# 全局客户端实例
ha_client = HomeAssistantClient()
