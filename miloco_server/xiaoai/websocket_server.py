# Copyright (C) 2025 willianfu
# 小爱音箱集成模块 - Miloco Server
#
# WebSocket服务器，用于音箱连接（支持多音箱）

"""
小爱音箱WebSocket服务器

支持多个音箱并发连接，每个音箱通过设备序列号（SN）识别。
"""

import json
import asyncio
import logging
from typing import Optional, Dict, Callable, Any, Awaitable, Set
from dataclasses import dataclass, field

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed

from miloco_server.xiaoai.protocol import (
    AppMessage, MessageType, Request, Response, Event, Stream,
    EventType, PlayingStatus, RecognizeResult
)
from miloco_server.xiaoai.config import XiaoAIConfig

logger = logging.getLogger(__name__)


@dataclass
class SpeakerConnection:
    """Represents a connected speaker."""
    websocket: WebSocketServerProtocol
    speaker_id: str  # Usually device SN
    model: str = "unknown"
    status: PlayingStatus = PlayingStatus.IDLE
    connected_at: float = 0.0
    
    @property
    def remote_address(self) -> str:
        return str(self.websocket.remote_address)


@dataclass
class PendingRequest:
    """Pending RPC request waiting for response."""
    future: asyncio.Future
    request_id: str
    speaker_id: str


class XiaoAIWebSocketServer:
    """
    WebSocket server for multiple XiaoAI speaker connections.
    
    Features:
    - Supports multiple concurrent speaker connections
    - Each speaker identified by device SN
    - Independent message handling per speaker
    - RPC mechanism for calling speaker commands
    """
    
    def __init__(self, config: XiaoAIConfig):
        """
        初始化WebSocket服务器
        
        Args:
            config: 小爱音箱配置
        """
        self._config = config
        self._server: Optional[websockets.WebSocketServer] = None
        self._running = False
        
        # 音箱连接: speaker_id -> SpeakerConnection
        self._speakers: Dict[str, SpeakerConnection] = {}
        # 反向查找: websocket -> speaker_id
        self._ws_to_speaker: Dict[WebSocketServerProtocol, str] = {}
        # 正在关闭的旧连接（重连时），避免重复触发断开事件
        self._closing_websockets: Set[WebSocketServerProtocol] = set()
        
        # RPC管理
        self._pending_requests: Dict[str, PendingRequest] = {}
        self._rpc_handlers: Dict[str, Callable[[Request, str], Awaitable[Response]]] = {}
        
        # 事件处理器（第二个参数为speaker_id）
        self._event_handler: Optional[Callable[[Event, str], Awaitable[None]]] = None
        self._stream_handler: Optional[Callable[[Stream, str], Awaitable[None]]] = None
        self._connection_handler: Optional[Callable[[str, dict], Awaitable[None]]] = None
        self._disconnection_handler: Optional[Callable[[str], Awaitable[None]]] = None
        
        # 并发请求信号量
        self._semaphore = asyncio.Semaphore(32)
    
    @property
    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running
    
    @property
    def connected_speakers(self) -> Dict[str, SpeakerConnection]:
        """Get all connected speakers."""
        return self._speakers.copy()
    
    def get_speaker(self, speaker_id: str) -> Optional[SpeakerConnection]:
        """Get speaker connection by ID."""
        return self._speakers.get(speaker_id)
    
    def is_speaker_connected(self, speaker_id: str) -> bool:
        """Check if a specific speaker is connected."""
        return speaker_id in self._speakers
    
    def set_event_handler(self, handler: Callable[[Event, str], Awaitable[None]]):
        """Set handler for event messages. Handler receives (event, speaker_id)."""
        self._event_handler = handler
    
    def set_stream_handler(self, handler: Callable[[Stream, str], Awaitable[None]]):
        """Set handler for stream messages. Handler receives (stream, speaker_id)."""
        self._stream_handler = handler
    
    def set_connection_handler(self, handler: Callable[[str, dict], Awaitable[None]]):
        """Set handler for new connections. Handler receives (speaker_id, device_info)."""
        self._connection_handler = handler
    
    def set_disconnection_handler(self, handler: Callable[[str], Awaitable[None]]):
        """Set handler for disconnections. Handler receives (speaker_id)."""
        self._disconnection_handler = handler
    
    def add_rpc_command(
        self,
        command: str,
        handler: Callable[[Request, str], Awaitable[Response]]
    ):
        """Add RPC command handler. Handler receives (request, speaker_id)."""
        self._rpc_handlers[command] = handler
    
    async def start(self):
        """Start the WebSocket server."""
        if self._running:
            logger.warning("Server already running")
            return
        
        self._running = True
        logger.info("Starting XiaoAI WebSocket server on %s:%d",
                   self._config.host, self._config.port)
        
        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self._config.host,
                self._config.port
            )
            logger.info("XiaoAI WebSocket server started on %s:%d",
                       self._config.host, self._config.port)
        except Exception as e:
            logger.error("Failed to start WebSocket server: %s", e)
            self._running = False
            raise
    
    async def stop(self):
        """停止WebSocket服务器（带超时保护）"""
        self._running = False
        
        # 取消所有pending的RPC请求
        for rid, req in list(self._pending_requests.items()):
            try:
                req.future.cancel()
            except Exception:
                pass
        self._pending_requests.clear()
        
        # 关闭所有音箱连接（带超时）
        for speaker_id, conn in list(self._speakers.items()):
            self._closing_websockets.add(conn.websocket)
            try:
                await asyncio.wait_for(conn.websocket.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        
        self._speakers.clear()
        self._ws_to_speaker.clear()
        self._closing_websockets.clear()
        
        # 关闭服务器（带超时）
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("WebSocket服务器关闭超时，强制关闭")
            self._server = None
        
        logger.info("小爱音箱WebSocket服务器已停止")
    
    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        """处理新的WebSocket连接"""
        import time
        
        remote_addr = websocket.remote_address
        logger.info("新连接来自 %s", remote_addr)
        
        # 生成临时ID，直到获取到设备信息
        temp_id = f"temp_{id(websocket)}"
        speaker_id = temp_id
        is_reconnection = False
        
        try:
            # 请求设备信息以获取真实的speaker_id
            device_info = await self._get_device_info(websocket)
            
            if device_info and device_info.get("sn"):
                speaker_id = device_info["sn"]
                
                # 检查是否是重连（同一音箱已连接）
                if speaker_id in self._speakers:
                    old_conn = self._speakers[speaker_id]
                    is_reconnection = True
                    logger.info("[%s] 检测到重连，关闭旧连接", speaker_id)
                    
                    # 标记旧连接为正在关闭，避免触发断开事件
                    self._closing_websockets.add(old_conn.websocket)
                    
                    # 清理旧连接的映射
                    if old_conn.websocket in self._ws_to_speaker:
                        del self._ws_to_speaker[old_conn.websocket]
                    
                    # 关闭旧websocket
                    try:
                        await old_conn.websocket.close()
                    except Exception:
                        pass
            else:
                # 使用地址作为备用ID
                speaker_id = f"{remote_addr[0]}_{remote_addr[1]}"
                device_info = {"model": "unknown", "sn": speaker_id}
            
            # 注册新连接
            conn = SpeakerConnection(
                websocket=websocket,
                speaker_id=speaker_id,
                model=device_info.get("model", "unknown"),
                connected_at=time.time()
            )
            self._speakers[speaker_id] = conn
            self._ws_to_speaker[websocket] = speaker_id
            
            logger.info("[%s] 音箱已连接 (型号: %s, 地址: %s, 重连: %s)",
                       speaker_id, conn.model, remote_addr, is_reconnection)
            
            # 通知连接处理器
            if self._connection_handler:
                try:
                    await self._connection_handler(speaker_id, device_info)
                except Exception as e:
                    logger.error("[%s] 连接处理器错误: %s", speaker_id, e)
            
            # 处理消息
            await self._process_messages(websocket, speaker_id)
            
        except ConnectionClosed as e:
            logger.info("连接关闭 %s: %s", remote_addr, e)
        except Exception as e:
            logger.error("连接处理错误: %s", e, exc_info=True)
        finally:
            # 检查是否是被标记为关闭的旧连接
            is_old_closing = websocket in self._closing_websockets
            if is_old_closing:
                self._closing_websockets.discard(websocket)
                logger.info("旧连接已关闭（重连场景），跳过断开处理")
                return
            
            # 获取当前speaker_id（可能已被新连接覆盖）
            current_speaker_id = self._ws_to_speaker.get(websocket)
            
            # 只有当前连接仍然是活跃连接时才清理
            if current_speaker_id:
                current_conn = self._speakers.get(current_speaker_id)
                if current_conn and current_conn.websocket == websocket:
                    del self._speakers[current_speaker_id]
                    logger.info("[%s] 已从活跃连接列表移除", current_speaker_id)
            
            if websocket in self._ws_to_speaker:
                del self._ws_to_speaker[websocket]
            
            # 移除该音箱的pending请求
            to_remove = [
                rid for rid, req in self._pending_requests.items()
                if req.speaker_id == speaker_id
            ]
            for rid in to_remove:
                req = self._pending_requests.pop(rid)
                req.future.cancel()
            
            # 通知断开处理器（仅对真实连接，非临时ID）
            if self._disconnection_handler and speaker_id != temp_id and not is_old_closing:
                # 只有当没有新连接占用这个speaker_id时才通知断开
                if speaker_id not in self._speakers:
                    try:
                        await self._disconnection_handler(speaker_id)
                    except Exception as e:
                        logger.error("[%s] 断开处理器错误: %s", speaker_id, e)
            
            logger.info("[%s] 连接已清理", speaker_id)
    
    async def _get_device_info(self, websocket: WebSocketServerProtocol) -> Optional[dict]:
        """通过run_shell获取音箱设备信息
        
        open-xiaoai客户端注册的RPC命令为:
        get_version, run_shell, start_play, stop_play, start_recording, stop_recording
        没有 get_device 命令，所以我们通过 run_shell 执行命令获取设备信息。
        """
        try:
            import uuid
            request_id = str(uuid.uuid4())
            # 使用 run_shell 命令，payload 必须是纯字符串
            script = "echo $(micocfg_model) $(micocfg_sn)"
            request = Request(id=request_id, command="run_shell", payload=script)
            message = AppMessage(type=MessageType.REQUEST, content=request)
            
            await websocket.send(message.to_json())
            
            # 等待响应（可能先收到事件消息，需要跳过）
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    response_text = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=remaining
                    )
                    
                    app_msg = AppMessage.from_json(response_text)
                    if not app_msg:
                        continue
                    
                    # 跳过事件消息，只处理Response
                    if app_msg.type == MessageType.RESPONSE:
                        response = app_msg.content
                        if response.id == request_id and response.data:
                            # 解析 run_shell 返回的 CommandResult
                            data = response.data
                            if isinstance(data, str):
                                import json
                                data = json.loads(data)
                            
                            stdout = data.get("stdout", "") if isinstance(data, dict) else ""
                            info = stdout.strip().split()
                            model = info[0] if len(info) > 0 else "unknown"
                            sn = info[1] if len(info) > 1 else ""
                            
                            if sn:
                                logger.info("获取到设备信息: model=%s, sn=%s", model, sn)
                                return {"model": model, "sn": sn}
                            else:
                                logger.warning("未获取到设备SN: stdout=%s", stdout)
                        break
                    # 事件消息，继续等待
                    elif app_msg.type == MessageType.EVENT:
                        logger.debug("等待设备信息时收到事件: %s，继续等待", 
                                   app_msg.content.event if hasattr(app_msg.content, 'event') else 'unknown')
                        continue
                    
                except asyncio.TimeoutError:
                    logger.warning("获取设备信息超时")
                    break
            
        except Exception as e:
            logger.warning("获取设备信息失败: %s", e)
        
        return None
    
    async def _process_messages(self, websocket: WebSocketServerProtocol, speaker_id: str):
        """Process incoming WebSocket messages for a speaker."""
        async for message in websocket:
            try:
                if isinstance(message, bytes):
                    await self._handle_binary_message(message, speaker_id)
                else:
                    await self._handle_text_message(message, speaker_id)
            except Exception as e:
                logger.error("Error processing message from %s: %s", speaker_id, e)
    
    async def _handle_text_message(self, text: str, speaker_id: str):
        """Handle text WebSocket message."""
        app_message = AppMessage.from_json(text)
        if not app_message:
            return
        
        if app_message.type == MessageType.REQUEST:
            await self._handle_request(app_message.content, speaker_id)
        elif app_message.type == MessageType.RESPONSE:
            await self._handle_response(app_message.content)
        elif app_message.type == MessageType.EVENT:
            await self._handle_event(app_message.content, speaker_id)
    
    async def _handle_binary_message(self, data: bytes, speaker_id: str):
        """Handle binary WebSocket message."""
        try:
            stream = Stream.from_bytes(data)
            if self._stream_handler:
                await self._stream_handler(stream, speaker_id)
        except Exception as e:
            logger.error("Error handling binary message: %s", e)
    
    async def _handle_request(self, request: Request, speaker_id: str):
        """Handle incoming RPC request."""
        async with self._semaphore:
            handler = self._rpc_handlers.get(request.command)
            
            if handler:
                try:
                    response = await handler(request, speaker_id)
                    response.id = request.id
                except Exception as e:
                    logger.error("Error handling request %s: %s", request.command, e)
                    response = Response.error(request.id, str(e))
            else:
                response = Response.error(request.id, f"Command not found: {request.command}")
            
            await self._send_response(response, speaker_id)
    
    async def _handle_response(self, response: Response):
        """Handle incoming RPC response."""
        pending = self._pending_requests.pop(response.id, None)
        if pending:
            pending.future.set_result(response)
    
    async def _handle_event(self, event: Event, speaker_id: str):
        """处理接收到的事件
        
        关键：事件处理器必须作为后台任务执行，不能阻塞消息循环！
        
        原因：如果事件处理器内部调用了 RPC（如 run_shell），
        RPC 需要等待响应，而响应需要消息循环来接收。
        如果事件处理器阻塞了消息循环，就会形成死锁：
        event_handler -> run_shell -> call_rpc(等待响应) 
            ↑ 但响应需要 _process_messages 来处理 ↑ (被阻塞)
        """
        # 同步更新播放状态（快速操作，不阻塞）
        if event.event == EventType.PLAYING:
            speaker = self._speakers.get(speaker_id)
            if speaker:
                speaker.status = PlayingStatus.from_event_data(event.data)
        
        # 事件处理器作为后台任务执行，不阻塞消息循环
        if self._event_handler:
            asyncio.create_task(self._fire_event_handler(event, speaker_id))
    
    async def _fire_event_handler(self, event: Event, speaker_id: str):
        """安全地执行事件处理器（作为后台任务）"""
        try:
            await self._event_handler(event, speaker_id)
        except Exception as e:
            logger.error("[%s] 事件处理器异常: %s", speaker_id, e, exc_info=True)
    
    async def _send_response(self, response: Response, speaker_id: str):
        """Send RPC response to speaker."""
        speaker = self._speakers.get(speaker_id)
        if not speaker:
            return
        
        message = AppMessage(type=MessageType.RESPONSE, content=response)
        try:
            await speaker.websocket.send(message.to_json())
        except Exception as e:
            logger.error("Failed to send response to %s: %s", speaker_id, e)
    
    async def send_event(self, speaker_id: str, event_name: str, data: Any = None):
        """Send event to a specific speaker."""
        speaker = self._speakers.get(speaker_id)
        if not speaker:
            logger.warning("Cannot send event: speaker %s not connected", speaker_id)
            return
        
        event = Event.create(event_name, data)
        message = AppMessage(type=MessageType.EVENT, content=event)
        
        try:
            await speaker.websocket.send(message.to_json())
        except Exception as e:
            logger.error("Failed to send event to %s: %s", speaker_id, e)
    
    async def send_stream(self, speaker_id: str, tag: str, data: bytes, metadata: Any = None):
        """Send binary stream to a specific speaker."""
        speaker = self._speakers.get(speaker_id)
        if not speaker:
            return
        
        stream = Stream.create(tag, data, metadata)
        
        try:
            await speaker.websocket.send(stream.to_bytes())
        except Exception as e:
            logger.error("Failed to send stream to %s: %s", speaker_id, e)
    
    async def call_rpc(
        self,
        speaker_id: str,
        command: str,
        payload: Any = None,
        timeout_ms: int = 10000
    ) -> Optional[Response]:
        """
        Call RPC command on a specific speaker.
        
        Args:
            speaker_id: Target speaker ID
            command: Command name
            payload: Command payload
            timeout_ms: Timeout in milliseconds
            
        Returns:
            Response or None if failed/timeout
        """
        speaker = self._speakers.get(speaker_id)
        if not speaker:
            logger.warning("Cannot call RPC: speaker %s not connected", speaker_id)
            return None
        
        import uuid
        request_id = str(uuid.uuid4())
        request = Request(id=request_id, command=command, payload=payload)
        
        # Create pending request
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = PendingRequest(
            future=future,
            request_id=request_id,
            speaker_id=speaker_id
        )
        
        # Send request
        message = AppMessage(type=MessageType.REQUEST, content=request)
        try:
            await speaker.websocket.send(message.to_json())
        except Exception as e:
            logger.error("Failed to send RPC request: %s", e)
            self._pending_requests.pop(request_id, None)
            return None
        
        # Wait for response
        try:
            response = await asyncio.wait_for(
                future,
                timeout=timeout_ms / 1000
            )
            return response
        except asyncio.TimeoutError:
            logger.warning("RPC call timeout: %s to %s", command, speaker_id)
            self._pending_requests.pop(request_id, None)
            return None
        except Exception as e:
            logger.error("RPC call error: %s", e)
            self._pending_requests.pop(request_id, None)
            return None
    
    async def broadcast_event(self, event_name: str, data: Any = None):
        """Broadcast event to all connected speakers."""
        for speaker_id in list(self._speakers.keys()):
            await self.send_event(speaker_id, event_name, data)
