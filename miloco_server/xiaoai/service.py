# Copyright (C) 2025 willianfu
# XiaoAI Speaker Integration Module for Miloco Server
#
# Main service entry point.

"""
Main XiaoAI service entry point.

Provides the main service class that orchestrates all XiaoAI components
for multiple speaker support.
"""

import asyncio
import logging
from typing import Optional, Callable, Awaitable, Dict, List

from miloco_server.xiaoai.config import (
    XiaoAIConfig, get_xiaoai_config, update_xiaoai_config, reload_xiaoai_config
)
from miloco_server.xiaoai.websocket_server import XiaoAIWebSocketServer
from miloco_server.xiaoai.speaker import SpeakerManager, SpeakerController
from miloco_server.xiaoai.ai_client import AIConversationClient
from miloco_server.xiaoai.message_handler import MessageHandler

logger = logging.getLogger(__name__)


class XiaoAIService:
    """
    Main XiaoAI service class.
    
    Supports multiple concurrent speaker connections with:
    - Independent AI conversation sessions per speaker
    - Context compression for long conversations
    - Session management via voice commands
    - Integration with Miloco's AI backend
    """
    
    def __init__(self, config: Optional[XiaoAIConfig] = None):
        """
        Initialize XiaoAI service.
        
        Args:
            config: XiaoAI configuration (uses stored config if not provided)
        """
        self._config = config or get_xiaoai_config()
        self._running = False
        self._server: Optional[XiaoAIWebSocketServer] = None
        self._speaker_manager: Optional[SpeakerManager] = None
        self._message_handler: Optional[MessageHandler] = None
    
    @property
    def config(self) -> XiaoAIConfig:
        """Get current configuration."""
        return self._config
    
    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._running
    
    @property
    def connected_speakers(self) -> List[dict]:
        """Get list of connected speakers."""
        if self._speaker_manager:
            return self._speaker_manager.get_connected_speakers_info()
        return []
    
    def get_speaker_controller(self, speaker_id: str) -> Optional[SpeakerController]:
        """Get controller for a specific speaker."""
        if self._speaker_manager:
            return self._speaker_manager.get_controller(speaker_id)
        return None
    
    def get_ai_client(self, speaker_id: str) -> Optional[AIConversationClient]:
        """Get AI client for a specific speaker."""
        if self._message_handler:
            return self._message_handler.get_ai_client(speaker_id)
        return None
    
    def set_message_handler(
        self,
        handler: Callable[[str, str], Awaitable[Optional[str]]]
    ):
        """
        Set custom message handler.
        
        Handler receives (text, speaker_id) and returns Optional[response].
        """
        if self._message_handler:
            self._message_handler.set_custom_message_handler(handler)
    
    async def start(self):
        """Start the XiaoAI service."""
        if self._running:
            logger.warning("XiaoAI service already running")
            return
        
        logger.info("Starting XiaoAI service...")
        
        # Initialize components
        self._server = XiaoAIWebSocketServer(self._config)
        self._speaker_manager = SpeakerManager(self._server, self._config)
        self._message_handler = MessageHandler(
            self._server,
            self._speaker_manager,
            self._config
        )
        
        # Start WebSocket server
        await self._server.start()
        self._running = True
        
        logger.info("XiaoAI service started on %s:%d",
                   self._config.host, self._config.port)
    
    async def stop(self):
        """Stop the XiaoAI service."""
        if not self._running:
            return
        
        logger.info("Stopping XiaoAI service...")
        
        if self._server:
            await self._server.stop()
        
        self._server = None
        self._speaker_manager = None
        self._message_handler = None
        self._running = False
        
        logger.info("XiaoAI service stopped")
    
    async def restart(self, new_config: Optional[XiaoAIConfig] = None):
        """
        Restart the service with optional new configuration.
        
        Args:
            new_config: New configuration to apply
        """
        logger.info("Restarting XiaoAI service...")
        
        await self.stop()
        
        if new_config:
            self._config = new_config
            update_xiaoai_config(new_config)
        else:
            self._config = reload_xiaoai_config()
        
        if self._config.enabled:
            await self.start()
        else:
            logger.info("XiaoAI service disabled in config, not starting")
    
    def update_config(self, config: XiaoAIConfig):
        """
        Update configuration without restart.
        
        Note: Some settings require restart to take effect.
        """
        old_config = self._config
        self._config = config
        update_xiaoai_config(config)
        
        # Check if restart is needed
        needs_restart = (
            old_config.host != config.host or
            old_config.port != config.port
        )
        
        return needs_restart
    
    async def ask_ai(self, speaker_id: str, text: str) -> str:
        """
        Ask AI a question for a specific speaker.
        
        Args:
            speaker_id: Speaker ID (or use "" for anonymous)
            text: Question text
            
        Returns:
            AI response text
        """
        if self._message_handler:
            ai_client = self._message_handler.get_ai_client(speaker_id or "anonymous")
            await ai_client.initialize()
            response = await ai_client.ask(text)
            return response.text
        return "服务未启动"
    
    async def speak(
        self,
        speaker_id: str,
        text: str,
        blocking: bool = True
    ) -> bool:
        """
        Speak text through a specific speaker.
        
        Args:
            speaker_id: Target speaker ID
            text: Text to speak
            blocking: Wait for playback to complete
            
        Returns:
            True if successful
        """
        controller = self.get_speaker_controller(speaker_id)
        if not controller:
            logger.warning("Speaker %s not connected", speaker_id)
            return False
        
        return await controller.play(text=text, blocking=blocking)
    
    async def broadcast_speak(self, text: str, blocking: bool = True) -> Dict[str, bool]:
        """
        Speak text through all connected speakers.
        
        Args:
            text: Text to speak
            blocking: Wait for playback to complete
            
        Returns:
            Dict of speaker_id -> success
        """
        results = {}
        for speaker_info in self.connected_speakers:
            speaker_id = speaker_info["speaker_id"]
            success = await self.speak(speaker_id, text, blocking)
            results[speaker_id] = success
        return results
    
    def get_session_info(self, speaker_id: str) -> Optional[dict]:
        """Get session info for a speaker."""
        if self._message_handler:
            return self._message_handler.get_speaker_session_info(speaker_id)
        return None
    
    def get_all_sessions_info(self) -> Dict[str, dict]:
        """Get session info for all speakers."""
        if self._message_handler:
            return self._message_handler.get_all_sessions_info()
        return {}
    
    async def clear_session(self, speaker_id: str) -> bool:
        """Clear conversation history for a speaker."""
        ai_client = self.get_ai_client(speaker_id)
        if ai_client:
            ai_client.clear_history()
            return True
        return False
    
    async def save_and_new_session(self, speaker_id: str) -> Optional[str]:
        """Save current session and start new one."""
        ai_client = self.get_ai_client(speaker_id)
        if ai_client:
            return await ai_client.save_and_new_session()
        return None


# Singleton instance
_service_instance: Optional[XiaoAIService] = None


def get_xiaoai_service() -> XiaoAIService:
    """Get the XiaoAI service singleton instance."""
    global _service_instance
    
    if _service_instance is None:
        config = get_xiaoai_config()
        _service_instance = XiaoAIService(config)
    
    return _service_instance


async def start_xiaoai_service_if_enabled() -> Optional[XiaoAIService]:
    """
    Start the XiaoAI service if enabled in configuration.
    
    Returns:
        XiaoAIService instance if started, None if disabled
    """
    service = get_xiaoai_service()
    
    if service.config.enabled:
        await service.start()
        return service
    else:
        logger.info("XiaoAI service disabled in configuration")
        return None


async def restart_xiaoai_service(new_config: Optional[XiaoAIConfig] = None) -> XiaoAIService:
    """
    Restart the XiaoAI service with optional new config.
    
    Args:
        new_config: New configuration to apply
        
    Returns:
        XiaoAIService instance
    """
    service = get_xiaoai_service()
    await service.restart(new_config)
    return service
