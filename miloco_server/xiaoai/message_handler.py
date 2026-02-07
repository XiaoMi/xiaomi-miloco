# Copyright (C) 2025 willianfu
# 小爱音箱集成模块 - Miloco Server
#
# 消息处理模块，用于处理事件和路由

"""
小爱音箱消息处理模块

处理来自音箱客户端的事件，并路由到相应的处理器。
支持多音箱独立会话管理。
"""

import re
import asyncio
import logging
from typing import Optional, Dict, Callable, Awaitable

from miloco_server.xiaoai.protocol import (
    Event, Stream, EventType, PlayingStatus, RecognizeResult, Request, Response
)
from miloco_server.xiaoai.config import XiaoAIConfig
from miloco_server.xiaoai.websocket_server import XiaoAIWebSocketServer
from miloco_server.xiaoai.speaker import SpeakerManager, SpeakerController
from miloco_server.xiaoai.ai_client import AIConversationClient, AIResponse, ResponsePart

logger = logging.getLogger(__name__)


class MessageHandler:
    """
    Message handler for XiaoAI events.
    
    Manages conversation sessions for multiple speakers,
    processing events and routing to appropriate handlers.
    
    支持两种接管模式:
    1. 关键词匹配模式: 根据 call_ai_keywords 判断是否接管单轮对话
    2. 全部接管模式: 通过语音指令进入/退出接管状态，接管状态下所有对话都由AI回复
    """
    
    def __init__(
        self,
        server: XiaoAIWebSocketServer,
        speaker_manager: SpeakerManager,
        config: XiaoAIConfig
    ):
        """
        Initialize message handler.
        
        Args:
            server: WebSocket server instance
            speaker_manager: Speaker manager instance
            config: XiaoAI configuration
        """
        self._server = server
        self._speaker_manager = speaker_manager
        self._config = config
        
        # AI clients per speaker: speaker_id -> AIConversationClient
        self._ai_clients: Dict[str, AIConversationClient] = {}
        
        # Processing locks per speaker
        self._processing_locks: Dict[str, asyncio.Lock] = {}
        self._is_processing: Dict[str, bool] = {}
        
        # 全部接管模式的当前状态: speaker_id -> bool
        # True = 当前处于接管状态，所有对话由AI回复
        self._takeover_active: Dict[str, bool] = {}
        
        # Custom message handler
        self._custom_message_handler: Optional[
            Callable[[str, str], Awaitable[Optional[str]]]
        ] = None
        
        # Register handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Setup event and RPC handlers."""
        self._server.set_event_handler(self._on_event)
        self._server.set_stream_handler(self._on_stream)
        self._server.set_connection_handler(self._on_connection)
        self._server.set_disconnection_handler(self._on_disconnection)
        
        # Register RPC commands
        self._server.add_rpc_command("get_version", self._handle_get_version)
    
    def set_custom_message_handler(
        self,
        handler: Callable[[str, str], Awaitable[Optional[str]]]
    ):
        """
        Set custom message handler.
        
        Handler receives (text, speaker_id) and returns Optional[response].
        """
        self._custom_message_handler = handler
    
    def get_ai_client(self, speaker_id: str) -> AIConversationClient:
        """Get or create AI client for a speaker."""
        if speaker_id not in self._ai_clients:
            self._ai_clients[speaker_id] = AIConversationClient(
                speaker_id=speaker_id,
                config=self._config
            )
        return self._ai_clients[speaker_id]
    
    @staticmethod
    def _clean_ai_response(text: str) -> str:
        """清理AI回复中的标签和格式
        
        移除AI模型输出中可能包含的XML标签，如:
        <reflect>...</reflect>
        <final_answer>...</final_answer>
        <think>...</think> 等
        """
        if not text:
            return text
        
        # 移除常见的XML标签（保留标签内的内容）
        cleaned = re.sub(r'</?(?:reflect|final_answer|think|thinking|answer|response|result|output)>', '', text)
        
        # 清理多余的空行
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        
        return cleaned.strip()
    
    def get_processing_lock(self, speaker_id: str) -> asyncio.Lock:
        """Get processing lock for a speaker."""
        if speaker_id not in self._processing_locks:
            self._processing_locks[speaker_id] = asyncio.Lock()
        return self._processing_locks[speaker_id]
    
    async def _on_connection(self, speaker_id: str, device_info: dict):
        """处理新音箱连接
        
        注意：此方法在 _process_messages 之前被调用，
        因此不能在这里直接 await 任何需要RPC响应的操作（如TTS播放），
        否则会造成死锁。播报等操作需要作为后台任务执行。
        """
        logger.info("[%s] 音箱已连接, 设备信息: %s", speaker_id, device_info)
        
        # 为该音箱初始化AI客户端
        ai_client = self.get_ai_client(speaker_id)
        await ai_client.initialize()
        
        # 初始化处理状态
        self._is_processing[speaker_id] = False
        
        # 播报连接提示（作为后台任务，不阻塞消息处理循环）
        # 必须用 create_task 而非 await，否则会死锁:
        # _announce_connection -> play() -> run_shell() -> call_rpc() 需要等待响应,
        # 但响应需要 _process_messages 循环来处理，而该循环在 _on_connection 返回后才启动
        asyncio.create_task(self._announce_connection(speaker_id))
    
    async def _on_disconnection(self, speaker_id: str):
        """处理音箱断开连接"""
        logger.info("[%s] 音箱断开连接", speaker_id)
        
        # 保存会话（如果有内容）
        ai_client = self._ai_clients.get(speaker_id)
        if ai_client:
            try:
                await ai_client.save_and_new_session()
            except Exception as e:
                logger.error("[%s] 保存会话失败: %s", speaker_id, e)
        
        # 清理资源
        self._speaker_manager.remove_controller(speaker_id)
        self._ai_clients.pop(speaker_id, None)  # 重要：清理AI客户端
        self._is_processing.pop(speaker_id, None)
        self._processing_locks.pop(speaker_id, None)
        self._takeover_active.pop(speaker_id, None)  # 清理接管状态
        logger.info("[%s] 资源已清理", speaker_id)
    
    async def _announce_connection(self, speaker_id: str):
        """播报连接提示语（作为后台任务运行）"""
        try:
            announcement = self._config.connection_announcement
            if not announcement:
                logger.info("[%s] 未配置连接提示语，跳过播报", speaker_id)
                return
            
            # 等待消息处理循环启动和连接稳定
            await asyncio.sleep(2)
            
            controller = self._speaker_manager.get_controller(speaker_id)
            if controller:
                logger.info("[%s] 播报连接提示: %s", speaker_id, announcement)
                success = await controller.play(text=announcement)
                if not success:
                    logger.warning("[%s] 连接提示播报失败", speaker_id)
                else:
                    logger.info("[%s] 连接提示播报成功", speaker_id)
            else:
                logger.warning("[%s] 无法获取控制器，无法播报连接提示", speaker_id)
        except Exception as e:
            logger.error("[%s] 播报连接提示异常: %s", speaker_id, e, exc_info=True)
    
    async def _on_event(self, event: Event, speaker_id: str):
        """处理接收到的事件"""
        logger.info("[%s] 收到事件: type=%s, data=%s", speaker_id, event.event, 
                   str(event.data)[:200] if event.data else None)
        
        try:
            if event.event == EventType.PLAYING:
                await self._handle_playing_event(event, speaker_id)
            elif event.event == EventType.INSTRUCTION:
                await self._handle_instruction_event(event, speaker_id)
            elif event.event == EventType.KWS:
                await self._handle_kws_event(event, speaker_id)
            else:
                logger.info("[%s] 未知事件类型: %s", speaker_id, event.event)
        except Exception as e:
            logger.error("[%s] 处理事件 %s 失败: %s",
                        speaker_id, event.event, e, exc_info=True)
    
    async def _on_stream(self, stream: Stream, speaker_id: str):
        """Handle incoming stream data."""
        logger.debug("[%s] Received stream: tag=%s, size=%d",
                    speaker_id, stream.tag, len(stream.bytes))
    
    async def _handle_playing_event(self, event: Event, speaker_id: str):
        """处理播放状态变化事件"""
        # 状态已在server中自动更新
        logger.debug("[%s] 播放状态变化: %s", speaker_id, event.data)
    
    async def _handle_instruction_event(self, event: Event, speaker_id: str):
        """处理语音指令事件（语音识别结果）"""
        logger.info("[%s] 处理instruction事件, data类型=%s", speaker_id, type(event.data).__name__)
        
        if not isinstance(event.data, dict):
            logger.warning("[%s] instruction事件data不是dict: %s", speaker_id, event.data)
            return
        
        result = RecognizeResult.from_instruction_data(event.data)
        if not result:
            logger.debug("[%s] 未解析到语音识别结果（可能是中间结果或其他类型）", speaker_id)
            return
        
        text = result.text.strip()
        if not text:
            logger.warning("[%s] 语音识别结果为空", speaker_id)
            return
        
        logger.info("[%s] 🎤 用户说: %s", speaker_id, text)
        await self._process_user_message(text, speaker_id)
    
    async def _handle_kws_event(self, event: Event, speaker_id: str):
        """处理唤醒词检测事件"""
        keyword = event.data
        logger.info("[%s] 检测到唤醒词: %s", speaker_id, keyword)
    
    async def _handle_get_version(self, request: Request, speaker_id: str) -> Response:
        """处理get_version RPC命令"""
        from miloco_server.xiaoai import __version__
        return Response.from_data(__version__)
    
    async def _process_user_message(self, text: str, speaker_id: str):
        """处理用户消息
        
        处理逻辑:
        1. 会话命令（清空/保存新建）优先级最高
        2. 全部接管模式的进入/退出指令
        3. 判断是否需要接管该轮对话:
           - 全部接管模式+已进入接管状态 → 接管
           - 关键词匹配模式+匹配成功 → 接管
           - 否则 → 不接管
        """
        logger.info("[%s] 处理用户消息: %s", speaker_id, text)
        
        try:
            # 首先检查会话命令
            if self._config.is_clear_session_command(text):
                logger.info("[%s] 检测到清空会话命令", speaker_id)
                await self._handle_clear_session(speaker_id)
                return
            
            if self._config.is_save_and_new_command(text):
                logger.info("[%s] 检测到保存并新建会话命令", speaker_id)
                await self._handle_save_and_new_session(speaker_id)
                return
            
            # 检查全部接管模式的指令
            if self._config.takeover_mode.enabled:
                # 检查进入接管指令
                if self._config.is_takeover_enter_command(text):
                    logger.info("[%s] 检测到进入接管状态指令", speaker_id)
                    await self._handle_enter_takeover(speaker_id)
                    return
                
                # 检查退出接管指令
                if self._config.is_takeover_exit_command(text):
                    logger.info("[%s] 检测到退出接管状态指令", speaker_id)
                    await self._handle_exit_takeover(speaker_id)
                    return
            
            # 判断是否需要接管该轮对话
            should_call = self._should_takeover_this_turn(text, speaker_id)
            
            is_takeover = self._takeover_active.get(speaker_id, False)
            logger.info("[%s] 接管判定: should_call=%s, takeover_mode_enabled=%s, takeover_active=%s, keywords=%s", 
                       speaker_id, should_call, self._config.takeover_mode.enabled, 
                       is_takeover, self._config.call_ai_keywords)
            
            if not should_call:
                logger.info("[%s] 不接管该轮对话，跳过处理", speaker_id)
                return
            
            # 确认接管后，立即打断小爱自身的回复
            await self._interrupt_xiaoai_immediately(speaker_id)
            
            # 防止同一音箱并发处理
            if self._is_processing.get(speaker_id, False):
                logger.warning("[%s] 正在处理中，跳过: %s", speaker_id, text)
                return
            
            lock = self.get_processing_lock(speaker_id)
            async with lock:
                self._is_processing[speaker_id] = True
                try:
                    logger.info("[%s] ✅ 开始AI对话处理", speaker_id)
                    await self._process_message_internal(text, speaker_id)
                except Exception as e:
                    logger.error("[%s] AI对话处理异常: %s", speaker_id, e, exc_info=True)
                finally:
                    self._is_processing[speaker_id] = False
        except Exception as e:
            logger.error("[%s] 处理用户消息异常: %s", speaker_id, e, exc_info=True)
    
    def _should_takeover_this_turn(self, text: str, speaker_id: str) -> bool:
        """判断是否应该接管该轮对话
        
        Returns:
            True = 应该接管，由AI回复
            False = 不接管，让小爱自己回复
        """
        # 如果全部接管模式启用且当前处于接管状态，接管所有对话
        if self._config.takeover_mode.enabled:
            if self._takeover_active.get(speaker_id, False):
                logger.info("[%s] 全部接管模式：已进入接管状态，接管该轮对话", speaker_id)
                return True
        
        # 否则使用关键词匹配
        return self._config.should_call_ai(text)
    
    async def _interrupt_xiaoai_immediately(self, speaker_id: str):
        """确认接管后立即打断小爱自身的回复
        
        在确认需要接管后立即调用，不等待AI处理完成。
        这样可以尽快打断小爱自身的回复，避免用户听到小爱的回答后又听到AI的回答。
        """
        controller = self._speaker_manager.get_controller(speaker_id)
        if not controller:
            logger.warning("[%s] 无法获取控制器，无法打断", speaker_id)
            return
        
        try:
            # 不检查状态，直接发送停止播放命令，确保尽快打断
            logger.info("[%s] 🔇 立即打断小爱自身回复", speaker_id)
            await controller.set_playing(False)
        except Exception as e:
            logger.warning("[%s] 打断小爱失败: %s", speaker_id, e)
    
    async def _handle_enter_takeover(self, speaker_id: str):
        """处理进入接管状态"""
        # 先立即打断
        await self._interrupt_xiaoai_immediately(speaker_id)
        
        self._takeover_active[speaker_id] = True
        
        controller = self._speaker_manager.get_controller(speaker_id)
        if controller:
            await controller.play(text="好的，我来接管小爱，有什么可以帮你的？", blocking=True)
        
        logger.info("[%s] 已进入全部接管状态", speaker_id)
    
    async def _handle_exit_takeover(self, speaker_id: str):
        """处理退出接管状态"""
        # 先立即打断
        await self._interrupt_xiaoai_immediately(speaker_id)
        
        self._takeover_active[speaker_id] = False
        
        controller = self._speaker_manager.get_controller(speaker_id)
        if controller:
            await controller.play(text="好的，已退出接管，小爱恢复正常", blocking=True)
        
        logger.info("[%s] 已退出全部接管状态", speaker_id)
    
    async def _handle_clear_session(self, speaker_id: str):
        """处理清空会话命令"""
        ai_client = self.get_ai_client(speaker_id)
        ai_client.clear_history()
        
        controller = self._speaker_manager.get_controller(speaker_id)
        if controller:
            await controller.play(text="好的，已清空对话记录，我们重新开始吧", blocking=True)
        
        logger.info("[%s] 会话已通过语音命令清空", speaker_id)
    
    async def _handle_save_and_new_session(self, speaker_id: str):
        """处理保存并新建会话命令"""
        ai_client = self.get_ai_client(speaker_id)
        old_session_id = await ai_client.save_and_new_session()
        
        controller = self._speaker_manager.get_controller(speaker_id)
        if controller:
            if old_session_id:
                await controller.play(text="好的，已保存当前对话，开始新的对话", blocking=True)
            else:
                await controller.play(text="好的，开始新的对话", blocking=True)
        
        logger.info("[%s] 会话已通过语音命令保存并新建", speaker_id)
    
    async def _process_message_internal(self, text: str, speaker_id: str):
        """内部消息处理
        
        流程（参考open-xiaoai的onMessage回调）:
        1. 调用AI获取回复（打断已在_process_user_message中处理）
        2. 根据TTS配置构建播报文本
        3. 通过TTS播放AI回复
        4. 如果配置了即时保存，保存到会话记录
        
        注意：打断小爱已经在确认接管时立即执行（_interrupt_xiaoai_immediately），
        这里不再重复打断，避免不必要的延迟。
        """
        controller = self._speaker_manager.get_controller(speaker_id)
        if not controller:
            logger.error("[%s] 无法获取控制器", speaker_id)
            return
        
        logger.info("[%s] 🤖 开始处理: %s", speaker_id, text)
        
        tts_text: Optional[str] = None
        ai_response: Optional[AIResponse] = None
        
        # 步骤2: 获取AI回复
        # 首先尝试自定义处理器
        if self._custom_message_handler:
            try:
                custom_response = await self._custom_message_handler(text, speaker_id)
                if custom_response:
                    tts_text = custom_response
            except Exception as e:
                logger.error("[%s] 自定义处理器错误: %s", speaker_id, e)
        
        # 回退到AI处理
        if tts_text is None:
            logger.info("[%s] 📡 调用AI获取响应...", speaker_id)
            ai_client = self.get_ai_client(speaker_id)
            try:
                ai_response = await ai_client.ask(text)
                if ai_response.success:
                    # 根据TTS配置构建播报文本
                    tts_text = AIConversationClient.build_tts_text(
                        ai_response, self._config.tts_playback
                    )
                    logger.info("[%s] ✅ AI响应成功，最终回答长度=%d, TTS文本长度=%d, 工具调用=%d", 
                               speaker_id, len(ai_response.text), len(tts_text), ai_response.tool_calls_made)
                else:
                    logger.error("[%s] ❌ AI响应失败: %s", speaker_id, ai_response.error_message)
                    tts_text = "抱歉，我暂时无法回答这个问题"
            except Exception as e:
                logger.error("[%s] ❌ AI调用异常: %s", speaker_id, e, exc_info=True)
                tts_text = "抱歉，处理出现了错误"
        
        # 步骤3: 播报回复前再次检查并打断
        try:
            status = await controller.get_playing(sync=True)
            if status == PlayingStatus.PLAYING:
                logger.info("[%s] 🔇 播报前再次打断小爱", speaker_id)
                await controller.set_playing(False)
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("[%s] 播报前打断失败: %s", speaker_id, e)
        
        # 步骤4: 播报AI回复
        if tts_text:
            logger.info("[%s] 📢 TTS播报: %s", speaker_id, tts_text[:100])
            success = await controller.play(text=tts_text, blocking=True)
            if not success:
                logger.error("[%s] TTS播放失败", speaker_id)
            else:
                logger.info("[%s] ✅ TTS播放完成", speaker_id)
        
        # 步骤5: 即时保存对话
        if self._config.auto_save_session and ai_response and ai_response.success:
            try:
                ai_client = self.get_ai_client(speaker_id)
                await ai_client.incremental_save()
                logger.info("[%s] 📝 对话已即时保存", speaker_id)
            except Exception as e:
                logger.error("[%s] 即时保存失败: %s", speaker_id, e)
    
    def get_speaker_session_info(self, speaker_id: str) -> Optional[dict]:
        """获取指定音箱的会话信息"""
        ai_client = self._ai_clients.get(speaker_id)
        if ai_client:
            return ai_client.get_history_summary()
        return None
    
    def get_all_sessions_info(self) -> Dict[str, dict]:
        """获取所有音箱的会话信息"""
        result = {}
        for speaker_id, ai_client in self._ai_clients.items():
            result[speaker_id] = ai_client.get_history_summary()
        return result
