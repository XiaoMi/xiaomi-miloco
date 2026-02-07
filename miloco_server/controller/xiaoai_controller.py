# Copyright (C) 2025 willianfu
# 小爱音箱集成模块 - Miloco Server
#
# 小爱音箱服务管理 API 控制器

"""
小爱音箱控制器模块

提供小爱音箱集成服务的 REST API 端点：
- 服务状态和控制
- 配置管理
- 音箱管理
- 会话管理
"""

import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from miloco_server.xiaoai.service import (
    get_xiaoai_service,
    restart_xiaoai_service
)
from miloco_server.xiaoai.config import (
    XiaoAIConfig,
    get_xiaoai_config,
    update_xiaoai_config,
    SessionCommand,
    ContextCompressionConfig,
    TTSPlaybackConfig,
    TakeoverModeConfig,
)
from miloco_server.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xiaoai", tags=["XiaoAI Speaker"])


# ==================== 请求/响应模型 ====================

class SessionCommandModel(BaseModel):
    """会话指令配置模型"""
    clear_commands: List[str] = Field(default=["清空对话", "重新开始", "忘记之前的"])
    save_and_new_commands: List[str] = Field(default=["新建对话", "开始新对话", "保存并新建"])


class ContextCompressionModel(BaseModel):
    """上下文压缩配置模型"""
    enabled: bool = True
    max_messages: int = 20
    max_tokens: int = 8000
    strategy: str = "auto"
    keep_recent: int = 5


class TTSPlaybackModel(BaseModel):
    """TTS播报内容控制模型"""
    play_thinking: bool = False
    play_tool_calls: bool = False


class TakeoverModeModel(BaseModel):
    """全部接管模式配置模型"""
    enabled: bool = False
    enter_keywords: List[str] = Field(default=["接管小爱", "AI接管"])
    exit_keywords: List[str] = Field(default=["退出接管", "恢复小爱"])


class XiaoAIConfigModel(BaseModel):
    """小爱音箱配置模型"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 4399
    mcp_list: List[str] = Field(default_factory=list)
    camera_ids: List[str] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    history_max_length: int = 20
    call_ai_keywords: List[str] = Field(default_factory=list)
    tts_max_length: int = 120
    playback_timeout: int = 600
    enable_interruption: bool = True
    connection_announcement: str = "已连接"
    session_commands: SessionCommandModel = Field(default_factory=SessionCommandModel)
    context_compression: ContextCompressionModel = Field(default_factory=ContextCompressionModel)
    share_session_with_web: bool = False
    tts_playback: TTSPlaybackModel = Field(default_factory=TTSPlaybackModel)
    auto_save_session: bool = False
    takeover_mode: TakeoverModeModel = Field(default_factory=TakeoverModeModel)


class SpeakerInfo(BaseModel):
    """已连接音箱信息"""
    speaker_id: str
    model: str
    status: str
    connected_at: float
    remote_address: str


class StatusResponse(BaseModel):
    """服务状态响应"""
    running: bool
    enabled: bool
    host: str
    port: int
    connected_speakers: List[SpeakerInfo]


class SpeakRequest(BaseModel):
    """语音播报请求"""
    speaker_id: str
    text: str
    blocking: bool = True
    filter_for_tts: bool = False  # 是否根据TTS配置过滤内容


class BroadcastSpeakRequest(BaseModel):
    """广播播报请求"""
    text: str
    blocking: bool = True


class AskRequest(BaseModel):
    """AI问答请求"""
    speaker_id: str = ""
    text: str


class SessionInfo(BaseModel):
    """会话信息"""
    session_id: Optional[str]
    speaker_id: str
    message_count: int
    messages: List[dict]


# ==================== 状态与控制 ====================

@router.get("/status", response_model=NormalResponse)
async def get_status():
    """获取小爱音箱服务状态"""
    service = get_xiaoai_service()
    config = service.config
    
    speakers = []
    for info in service.connected_speakers:
        speakers.append(SpeakerInfo(**info).model_dump())
    
    return NormalResponse(
        code=0,
        message="success",
        data={
            "running": service.is_running,
            "enabled": config.enabled,
            "host": config.host,
            "port": config.port,
            "connected_speakers": speakers
        }
    )


@router.post("/start", response_model=NormalResponse)
async def start_service():
    """启动小爱音箱服务"""
    service = get_xiaoai_service()
    
    if service.is_running:
        return NormalResponse(
            code=0,
            message="服务已在运行中"
        )
    
    try:
        await service.start()
        return NormalResponse(
            code=0,
            message=f"服务已启动，监听 {service.config.host}:{service.config.port}"
        )
    except Exception as e:
        logger.error("启动小爱音箱服务失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop", response_model=NormalResponse)
async def stop_service():
    """停止小爱音箱服务"""
    service = get_xiaoai_service()
    
    if not service.is_running:
        return NormalResponse(
            code=0,
            message="服务未在运行"
        )
    
    try:
        await service.stop()
        return NormalResponse(
            code=0,
            message="服务已停止"
        )
    except Exception as e:
        logger.error("停止小爱音箱服务失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/restart", response_model=NormalResponse)
async def restart_service():
    """重启小爱音箱服务"""
    try:
        await restart_xiaoai_service()
        service = get_xiaoai_service()
        
        if service.is_running:
            return NormalResponse(
                code=0,
                message=f"服务已重启，监听 {service.config.host}:{service.config.port}"
            )
        else:
            return NormalResponse(
                code=0,
                message="服务已停止（配置为禁用状态）"
            )
    except Exception as e:
        logger.error("重启小爱音箱服务失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 配置管理 ====================

@router.get("/config", response_model=NormalResponse)
async def get_config():
    """获取小爱音箱配置"""
    config = get_xiaoai_config()
    
    config_data = XiaoAIConfigModel(
        enabled=config.enabled,
        host=config.host,
        port=config.port,
        mcp_list=config.mcp_list,
        camera_ids=config.camera_ids,
        system_prompt=config.system_prompt,
        history_max_length=config.history_max_length,
        call_ai_keywords=config.call_ai_keywords,
        tts_max_length=config.tts_max_length,
        playback_timeout=config.playback_timeout,
        enable_interruption=config.enable_interruption,
        connection_announcement=config.connection_announcement,
        session_commands=SessionCommandModel(
            clear_commands=config.session_commands.clear_commands,
            save_and_new_commands=config.session_commands.save_and_new_commands
        ),
        context_compression=ContextCompressionModel(
            enabled=config.context_compression.enabled,
            max_messages=config.context_compression.max_messages,
            max_tokens=config.context_compression.max_tokens,
            strategy=config.context_compression.strategy,
            keep_recent=config.context_compression.keep_recent
        ),
        share_session_with_web=config.share_session_with_web,
        tts_playback=TTSPlaybackModel(
            play_thinking=config.tts_playback.play_thinking,
            play_tool_calls=config.tts_playback.play_tool_calls,
        ),
        auto_save_session=config.auto_save_session,
        takeover_mode=TakeoverModeModel(
            enabled=config.takeover_mode.enabled,
            enter_keywords=config.takeover_mode.enter_keywords,
            exit_keywords=config.takeover_mode.exit_keywords,
        ),
    )
    
    return NormalResponse(
        code=0,
        message="success",
        data=config_data.model_dump()
    )


@router.put("/config", response_model=NormalResponse)
async def update_config(config_data: XiaoAIConfigModel):
    """更新小爱音箱配置"""
    try:
        # 转换为内部配置格式
        new_config = XiaoAIConfig(
            enabled=config_data.enabled,
            host=config_data.host,
            port=config_data.port,
            mcp_list=config_data.mcp_list,
            camera_ids=config_data.camera_ids,
            system_prompt=config_data.system_prompt,
            history_max_length=config_data.history_max_length,
            call_ai_keywords=config_data.call_ai_keywords,
            tts_max_length=config_data.tts_max_length,
            playback_timeout=config_data.playback_timeout,
            enable_interruption=config_data.enable_interruption,
            connection_announcement=config_data.connection_announcement,
            session_commands=SessionCommand(
                clear_commands=config_data.session_commands.clear_commands,
                save_and_new_commands=config_data.session_commands.save_and_new_commands
            ),
            context_compression=ContextCompressionConfig(
                enabled=config_data.context_compression.enabled,
                max_messages=config_data.context_compression.max_messages,
                max_tokens=config_data.context_compression.max_tokens,
                strategy=config_data.context_compression.strategy,
                keep_recent=config_data.context_compression.keep_recent
            ),
            share_session_with_web=config_data.share_session_with_web,
            tts_playback=TTSPlaybackConfig(
                play_thinking=config_data.tts_playback.play_thinking,
                play_tool_calls=config_data.tts_playback.play_tool_calls,
            ),
            auto_save_session=config_data.auto_save_session,
            takeover_mode=TakeoverModeConfig(
                enabled=config_data.takeover_mode.enabled,
                enter_keywords=config_data.takeover_mode.enter_keywords,
                exit_keywords=config_data.takeover_mode.exit_keywords,
            ),
        )
        
        service = get_xiaoai_service()
        needs_restart = service.update_config(new_config)
        
        return NormalResponse(
            code=0,
            message="配置已保存",
            data={"needs_restart": needs_restart}
        )
    except Exception as e:
        logger.error("更新小爱音箱配置失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 音箱控制 ====================

@router.get("/speakers", response_model=NormalResponse)
async def get_speakers():
    """获取已连接的音箱列表"""
    service = get_xiaoai_service()
    speakers = [SpeakerInfo(**info).model_dump() for info in service.connected_speakers]
    return NormalResponse(
        code=0,
        message="success",
        data=speakers
    )


@router.post("/speak", response_model=NormalResponse)
async def speak_to_speaker(request: SpeakRequest):
    """向指定音箱播报文本
    
    当 filter_for_tts=True 时，会根据小爱音箱TTS配置过滤内容，
    只播报配置中启用的部分（思考过程、工具调用、最终回答）。
    """
    service = get_xiaoai_service()
    
    if not service.is_running:
        raise HTTPException(status_code=400, detail="服务未运行")
    
    try:
        text = request.text
        
        # 如果需要TTS过滤，应用和音箱一样的播报规则
        if request.filter_for_tts and text:
            from miloco_server.xiaoai.ai_client import AIConversationClient, AIResponse, ResponsePart
            config = get_xiaoai_config()
            # 构造一个临时 AIResponse 用于解析
            parts = []
            AIConversationClient._parse_step_content(text, parts)
            if parts:
                temp_response = AIResponse(
                    text=text, success=True, 
                    full_response=text, response_parts=parts
                )
                text = AIConversationClient.build_tts_text(temp_response, config.tts_playback)
            else:
                # 没有解析到标签，直接清理标签播报
                text = AIConversationClient._clean_tags(text)
        
        success = await service.speak(
            request.speaker_id,
            text,
            blocking=request.blocking
        )
        
        if success:
            return NormalResponse(code=0, message="播报成功")
        else:
            return NormalResponse(code=1, message="播报失败")
    except Exception as e:
        logger.error("播报失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/broadcast", response_model=NormalResponse)
async def broadcast_speak(request: BroadcastSpeakRequest):
    """向所有已连接音箱广播播报"""
    service = get_xiaoai_service()
    
    if not service.is_running:
        raise HTTPException(status_code=400, detail="服务未运行")
    
    try:
        results = await service.broadcast_speak(request.text, blocking=request.blocking)
        
        success_count = sum(1 for v in results.values() if v)
        total = len(results)
        
        return NormalResponse(
            code=0 if success_count > 0 else 1,
            message=f"已向 {success_count}/{total} 个音箱播报"
        )
    except Exception as e:
        logger.error("广播播报失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/speaker/{speaker_id}/device", response_model=NormalResponse)
async def get_speaker_device(speaker_id: str):
    """获取音箱设备信息"""
    service = get_xiaoai_service()
    
    controller = service.get_speaker_controller(speaker_id)
    if not controller:
        raise HTTPException(status_code=404, detail="音箱未找到")
    
    try:
        device = await controller.get_device()
        return NormalResponse(
            code=0,
            message="success",
            data=device
        )
    except Exception as e:
        logger.error("获取设备信息失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ==================== AI 与会话管理 ====================

@router.post("/ask", response_model=NormalResponse)
async def ask_ai(request: AskRequest):
    """向AI提问"""
    service = get_xiaoai_service()
    
    try:
        response = await service.ask_ai(request.speaker_id, request.text)
        return NormalResponse(
            code=0,
            message="success",
            data={"response": response}
        )
    except Exception as e:
        logger.error("AI问答失败: %s", e)
        return NormalResponse(
            code=1,
            message=str(e)
        )


@router.get("/session/{speaker_id}", response_model=NormalResponse)
async def get_session(speaker_id: str):
    """获取指定音箱的会话信息"""
    service = get_xiaoai_service()
    
    info = service.get_session_info(speaker_id)
    if info:
        return NormalResponse(
            code=0,
            message="success",
            data=info
        )
    
    raise HTTPException(status_code=404, detail="会话未找到")


@router.get("/sessions", response_model=NormalResponse)
async def get_all_sessions():
    """获取所有音箱的会话信息"""
    service = get_xiaoai_service()
    return NormalResponse(
        code=0,
        message="success",
        data=service.get_all_sessions_info()
    )


@router.post("/session/{speaker_id}/clear", response_model=NormalResponse)
async def clear_session(speaker_id: str):
    """清空指定音箱的对话历史"""
    service = get_xiaoai_service()
    
    success = await service.clear_session(speaker_id)
    
    return NormalResponse(
        code=0 if success else 1,
        message="会话已清空" if success else "会话未找到"
    )


@router.post("/session/{speaker_id}/save", response_model=NormalResponse)
async def save_session(speaker_id: str):
    """保存当前会话并开始新会话"""
    service = get_xiaoai_service()
    
    old_session_id = await service.save_and_new_session(speaker_id)
    
    if old_session_id:
        return NormalResponse(
            code=0,
            message="会话已保存",
            data={"old_session_id": old_session_id}
        )
    else:
        return NormalResponse(
            code=0,
            message="新会话已开始（无需保存）"
        )
