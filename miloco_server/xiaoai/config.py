# Copyright (C) 2025 willianfu
# XiaoAI Speaker Integration Module for Miloco Server
#
# Configuration module for XiaoAI integration.

"""
Configuration module for XiaoAI Speaker Integration.

Provides configuration options for the XiaoAI service including:
- WebSocket server settings
- AI conversation settings
- Speaker control settings
- Session management settings
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# KV storage keys for XiaoAI configuration
class XiaoAIConfigKeys:
    """Keys for storing XiaoAI configuration in KV store."""
    XIAOAI_CONFIG = "XIAOAI_CONFIG"
    XIAOAI_ENABLED = "XIAOAI_ENABLED"


@dataclass
class SessionCommand:
    """Configuration for session-related voice commands."""
    # Commands to clear history and start new session
    clear_commands: List[str] = field(default_factory=lambda: ["清空对话", "重新开始", "忘记之前的"])
    # Commands to save current and start new session
    save_and_new_commands: List[str] = field(default_factory=lambda: ["新建对话", "开始新对话", "保存并新建"])
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "SessionCommand":
        return cls(
            clear_commands=data.get("clear_commands", ["清空对话", "重新开始", "忘记之前的"]),
            save_and_new_commands=data.get("save_and_new_commands", ["新建对话", "开始新对话", "保存并新建"])
        )


@dataclass
class ContextCompressionConfig:
    """Configuration for context auto-compression."""
    enabled: bool = True
    # Maximum number of messages before compression
    max_messages: int = 20
    # Maximum total tokens (approximate) before compression
    max_tokens: int = 8000
    # Strategy: "summary" | "truncate" | "sliding" | "auto"
    strategy: str = "auto"
    # Number of recent messages to keep after compression
    keep_recent: int = 5
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ContextCompressionConfig":
        return cls(
            enabled=data.get("enabled", True),
            max_messages=data.get("max_messages", 20),
            max_tokens=data.get("max_tokens", 8000),
            strategy=data.get("strategy", "auto"),
            keep_recent=data.get("keep_recent", 5)
        )


@dataclass
class TTSPlaybackConfig:
    """TTS播报内容控制配置
    
    控制语音播报时哪些内容需要播放：
    - 思考过程 (<reflect> 标签内容)
    - 工具调用 (简短的工具调用描述)
    - 最终回答 (<final_answer> 标签内容，始终播放)
    """
    play_thinking: bool = False   # 是否播报思考过程
    play_tool_calls: bool = False  # 是否播报工具调用（简短描述）
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "TTSPlaybackConfig":
        return cls(
            play_thinking=data.get("play_thinking", False),
            play_tool_calls=data.get("play_tool_calls", False),
        )


@dataclass
class TakeoverModeConfig:
    """全部接管模式配置
    
    全部接管模式开启后，可以通过语音指令进入/退出接管状态。
    接管状态下，所有用户语音都会由AI回复，小爱自身回复会被完全打断。
    
    关闭全部接管模式时，使用 call_ai_keywords 关键词匹配来决定是否接管单轮对话。
    """
    enabled: bool = False  # 是否启用全部接管模式
    enter_keywords: List[str] = field(default_factory=lambda: ["接管小爱", "AI接管"])  # 进入接管的触发词
    exit_keywords: List[str] = field(default_factory=lambda: ["退出接管", "恢复小爱"])   # 退出接管的触发词
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "TakeoverModeConfig":
        return cls(
            enabled=data.get("enabled", False),
            enter_keywords=data.get("enter_keywords") or ["接管小爱", "AI接管"],
            exit_keywords=data.get("exit_keywords") or ["退出接管", "恢复小爱"],
        )


@dataclass
class XiaoAIConfig:
    """
    小爱音箱集成配置
    
    Attributes:
        enabled: 是否启用服务
        host: WebSocket 服务器监听地址
        port: WebSocket 服务器端口
        mcp_list: AI对话使用的MCP客户端ID列表
        camera_ids: 对话时可访问的摄像头ID列表
        system_prompt: 自定义系统提示词
        history_max_length: 最大对话历史长度
        call_ai_keywords: 触发AI响应的关键词列表（空列表=完全不接管）
        tts_max_length: 单次TTS播报最大文本长度
        playback_timeout: 播放超时（秒）
        enable_interruption: 是否允许打断播放
        connection_announcement: 音箱连接时播报的文本
        session_commands: 会话管理语音指令
        context_compression: 上下文压缩配置
        share_session_with_web: 是否与网页共享会话
        tts_playback: TTS播报内容控制
        auto_save_session: 是否即时保存对话（每次问答后自动保存）
        takeover_mode: 全部接管模式配置
    """
    
    # 服务设置
    enabled: bool = False  # 默认禁用
    host: str = "0.0.0.0"
    port: int = 4399
    
    # AI对话设置
    mcp_list: List[str] = field(default_factory=list)
    camera_ids: List[str] = field(default_factory=list)
    system_prompt: Optional[str] = None
    history_max_length: int = 20
    call_ai_keywords: List[str] = field(default_factory=list)  # 空列表=完全不接管（需要配置关键词才触发）
    
    # 音箱控制设置
    tts_max_length: int = 120
    playback_timeout: int = 600  # 10分钟
    enable_interruption: bool = True
    
    # 连接设置
    connection_announcement: str = "已连接"
    
    # 会话管理
    session_commands: SessionCommand = field(default_factory=SessionCommand)
    context_compression: ContextCompressionConfig = field(default_factory=ContextCompressionConfig)
    share_session_with_web: bool = False  # 默认独立会话
    
    # TTS播报控制
    tts_playback: TTSPlaybackConfig = field(default_factory=TTSPlaybackConfig)
    
    # 即时保存
    auto_save_session: bool = False  # 默认不即时保存，等待语音指令保存
    
    # 全部接管模式配置
    takeover_mode: TakeoverModeConfig = field(default_factory=TakeoverModeConfig)
    
    def to_dict(self) -> dict:
        """转换为字典用于JSON序列化"""
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "mcp_list": self.mcp_list,
            "camera_ids": self.camera_ids,
            "system_prompt": self.system_prompt,
            "history_max_length": self.history_max_length,
            "call_ai_keywords": self.call_ai_keywords,
            "tts_max_length": self.tts_max_length,
            "playback_timeout": self.playback_timeout,
            "enable_interruption": self.enable_interruption,
            "connection_announcement": self.connection_announcement,
            "session_commands": self.session_commands.to_dict(),
            "context_compression": self.context_compression.to_dict(),
            "share_session_with_web": self.share_session_with_web,
            "tts_playback": self.tts_playback.to_dict(),
            "auto_save_session": self.auto_save_session,
            "takeover_mode": self.takeover_mode.to_dict(),
        }
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)
    
    @classmethod
    def from_dict(cls, data: dict) -> "XiaoAIConfig":
        """从字典创建配置"""
        session_commands = data.get("session_commands")
        if isinstance(session_commands, dict):
            session_commands = SessionCommand.from_dict(session_commands)
        else:
            session_commands = SessionCommand()
        
        context_compression = data.get("context_compression")
        if isinstance(context_compression, dict):
            context_compression = ContextCompressionConfig.from_dict(context_compression)
        else:
            context_compression = ContextCompressionConfig()
        
        tts_playback = data.get("tts_playback")
        if isinstance(tts_playback, dict):
            tts_playback = TTSPlaybackConfig.from_dict(tts_playback)
        else:
            tts_playback = TTSPlaybackConfig()
        
        takeover_mode = data.get("takeover_mode")
        if isinstance(takeover_mode, dict):
            takeover_mode = TakeoverModeConfig.from_dict(takeover_mode)
        else:
            takeover_mode = TakeoverModeConfig()
        
        return cls(
            enabled=data.get("enabled", False),
            host=data.get("host", "0.0.0.0"),
            port=data.get("port", 4399),
            mcp_list=data.get("mcp_list") or [],
            camera_ids=data.get("camera_ids") or [],
            system_prompt=data.get("system_prompt"),
            history_max_length=data.get("history_max_length", 20),
            call_ai_keywords=data.get("call_ai_keywords") or [],
            tts_max_length=data.get("tts_max_length", 120),
            playback_timeout=data.get("playback_timeout", 600),
            enable_interruption=data.get("enable_interruption", True),
            connection_announcement=data.get("connection_announcement", "已连接"),
            session_commands=session_commands,
            context_compression=context_compression,
            share_session_with_web=data.get("share_session_with_web", False),
            tts_playback=tts_playback,
            auto_save_session=data.get("auto_save_session", False),
            takeover_mode=takeover_mode,
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> "XiaoAIConfig":
        """Create from JSON string."""
        try:
            data = json.loads(json_str)
            return cls.from_dict(data)
        except json.JSONDecodeError:
            logger.warning("Failed to parse XiaoAI config JSON, using defaults")
            return cls()
    
    def should_call_ai(self, text: str) -> bool:
        """
        Check if the given text should trigger an AI response.
        
        在非全部接管模式下（或接管模式但未进入接管状态时），
        使用关键词匹配来决定是否接管该轮对话。
        
        注意：空的 call_ai_keywords 表示完全不接管（需要配置关键词才触发）
        
        Args:
            text: The user's spoken text
            
        Returns:
            True if AI should be called, False otherwise
        """
        # 空关键词列表 = 完全不接管（除非在全部接管模式的接管状态中）
        if not self.call_ai_keywords:
            return False
        
        text_lower = text.lower().strip()
        for keyword in self.call_ai_keywords:
            if not keyword:
                continue
            # 检查文本是否包含关键词（使用包含而非仅开头匹配）
            if keyword.lower() in text_lower:
                return True
        
        return False
    
    def is_takeover_enter_command(self, text: str) -> bool:
        """检查是否是进入接管状态的指令"""
        if not self.takeover_mode.enabled:
            return False
        text_stripped = text.strip()
        for cmd in self.takeover_mode.enter_keywords:
            if cmd and cmd in text_stripped:
                return True
        return False
    
    def is_takeover_exit_command(self, text: str) -> bool:
        """检查是否是退出接管状态的指令"""
        if not self.takeover_mode.enabled:
            return False
        text_stripped = text.strip()
        for cmd in self.takeover_mode.exit_keywords:
            if cmd and cmd in text_stripped:
                return True
        return False
    
    def is_clear_session_command(self, text: str) -> bool:
        """Check if text is a command to clear session and start new."""
        text_stripped = text.strip()
        for cmd in self.session_commands.clear_commands:
            if cmd and text_stripped == cmd:
                return True
        return False
    
    def is_save_and_new_command(self, text: str) -> bool:
        """Check if text is a command to save and start new session."""
        text_stripped = text.strip()
        for cmd in self.session_commands.save_and_new_commands:
            if cmd and text_stripped == cmd:
                return True
        return False


def load_xiaoai_config() -> XiaoAIConfig:
    """
    Load XiaoAI configuration from KV store.
    
    Returns:
        XiaoAIConfig instance
    """
    try:
        from miloco_server.service.manager import get_manager
        manager = get_manager()
        config_json = manager.kv_dao.get(XiaoAIConfigKeys.XIAOAI_CONFIG)
        if config_json:
            return XiaoAIConfig.from_json(config_json)
    except Exception as e:
        logger.warning("Failed to load XiaoAI config: %s", e)
    
    return XiaoAIConfig()


def save_xiaoai_config(config: XiaoAIConfig) -> bool:
    """
    Save XiaoAI configuration to KV store.
    
    Args:
        config: XiaoAIConfig instance
        
    Returns:
        True if saved successfully
    """
    try:
        from miloco_server.service.manager import get_manager
        manager = get_manager()
        return manager.kv_dao.set(XiaoAIConfigKeys.XIAOAI_CONFIG, config.to_json())
    except Exception as e:
        logger.error("Failed to save XiaoAI config: %s", e)
        return False


# Default configuration instance (will be loaded from KV store when available)
_config_instance: Optional[XiaoAIConfig] = None


def get_xiaoai_config() -> XiaoAIConfig:
    """Get the current XiaoAI configuration (singleton)."""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_xiaoai_config()
    return _config_instance


def update_xiaoai_config(config: XiaoAIConfig) -> bool:
    """Update and persist XiaoAI configuration."""
    global _config_instance
    if save_xiaoai_config(config):
        _config_instance = config
        return True
    return False


def reload_xiaoai_config() -> XiaoAIConfig:
    """Reload configuration from storage."""
    global _config_instance
    _config_instance = load_xiaoai_config()
    return _config_instance
