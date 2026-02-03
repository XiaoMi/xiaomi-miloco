# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Data model definitions
Defines OpenAI-compatible API request and response data structures
"""

from typing import List, Optional, Dict, Any, Union
from enum import Enum
from pydantic import BaseModel, Field

class Role(str, Enum):
    """Chat role enumeration"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    FUNCTION = "function"
    TOOL = "tool"

class FinishReason(str, Enum):
    """Finish reason enumeration"""
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    CONTENT_FILTER = "content_filter"

class ContentType(str, Enum):
    """Content type enumeration"""
    TEXT = "text"
    IMAGE = "image"
    IMAGE_URL = "image_url"
    AUDIO = "audio"
    VIDEO = "video"


class FunctionCall(BaseModel):
    """Function call model"""
    name: str = Field(..., description="Function name")
    arguments: Optional[str] = Field(None, description="Function arguments")


class ToolCall(BaseModel):
    """Tool call model"""
    id: str = Field(..., description="Tool call ID")
    type: str = Field(default="function", description="Tool type")
    function: FunctionCall = Field(..., description="Function call")


class FunctionDesc(BaseModel):
    """Function description model"""
    name: str = Field(..., description="Function name")
    description: str = Field(..., description="Function description")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Function parameters")


class Tool(BaseModel):
    """Tool model"""
    type: str = Field(default="function", description="Tool type")  # Currently only supports function
    function: FunctionDesc = Field(..., description="Function description")

class URLContent(BaseModel):
    """URL content model"""
    url: str = Field(..., description="URL")

class Content(BaseModel):
    """Content model"""
    type: ContentType = Field(..., description="Content type, cannot be empty")
    text: Optional[str] = Field(None, description="Text content")
    image: Optional[str] = Field(None, description="Image content, base64 encoded")
    audio: Optional[str] = Field(None, description="Audio content, base64 encoded")
    video: Optional[List[str]] = Field(None, description="Video content, list of base64 encoded images")
    image_url: Optional[URLContent] = Field(None, description="Image URL content")


class ChatMessage(BaseModel):
    """Chat message model"""
    # Enhanced field validation
    role: Optional[Role] = Field(None, description="Message role, optional")
    content: Union[str, List[Content]] = Field(
        "",
        description="Message content, can be string or list of content objects"
    )
    name: Optional[str] = Field(None, description="Message name, optional")
    tool_calls: Optional[List[ToolCall]] = Field(
        None, description="Tool call information, optional")
    tool_call_id: Optional[str] = Field(None,
                                        description="Tool call ID, optional")


class ChatCompletionChoice(BaseModel):
    """Chat completion choice model"""
    index: int = Field(default=0, description="Choice index")
    message: Optional[ChatMessage] = Field(None, description="Message")
    delta: Optional[ChatMessage] = Field(None, description="Delta message")
    finish_reason: Optional[FinishReason] = Field(None, description="Finish reason")

class ChatCompletionResponse(BaseModel):
    """Chat completion response model"""
    id: str = Field(default="local-chatcmpl-0", description="ID")
    object: str = Field(default="chat.completion", description="Object type")
    created: int = Field(default=0, description="Creation timestamp")
    model: str = Field(default="default", description="Model name")
    choices: List[ChatCompletionChoice] = Field(..., description="Choices")
    usage: Optional[Dict[str, int]] = Field(None, description="Token usage")

class ChatCompletionRequest(BaseModel):
    """Chat completion request model"""
    model: str = Field(..., description="Model name")
    messages: List[ChatMessage] = Field(..., description="Messages")
    tools: Optional[List[Tool]] = Field(None, description="Tools")
    temperature: Optional[float] = Field(default=-1.0, le=2.0)
    top_p: Optional[float] = Field(default=1.0, le=1.0)
    top_k: Optional[int] = Field(default=40)
    max_tokens: Optional[int] = Field(default=2048, ge=1)
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    presence_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    seed: Optional[int] = None
    priority: Optional[int] = Field(default=0, ge=0, le=10, description="Task priority (0-10)")

class ModelInfo(BaseModel):
    """Model information model"""
    id: str = Field(default="local-model-0", description="Model ID")
    object: str = "model"
    created: int = Field(default=0, description="Creation timestamp")
    owned_by: str = "llama-mico"
    permission: Optional[List[Dict[str, Any]]] = Field(None, description="Permissions")
    root: Optional[str] = Field(None, description="Root model")
    parent: Optional[str] = Field(None, description="Parent model")


class ModelDescription(ModelInfo):
    """Model detailed information model"""
    loaded: bool = Field(default=False, description="Whether loaded")
    estimate_vram_usage: float = Field(default=-1.0, description="Estimated VRAM usage")


class ModelListResponse(BaseModel):
    """Model list response model"""
    object: str = "list"
    data: List[ModelInfo]


class ModelDescriptionListRespone(BaseModel):
    """Model detailed information list response model"""
    object: str = "list"
    data: List[ModelDescription]


class StreamErrorChunkMessage(BaseModel):
    """Stream error chunk information model"""
    message: str = Field(..., description="Error message")
    type: str = Field("error", description="Error type")
    function: Optional[str] = Field(None, description="Error function")


class StreamErrorChunk(BaseModel):
    """Stream error chunk model"""
    error: StreamErrorChunkMessage = Field(..., description="Error message")


class VramUsage(BaseModel):
    total: float = Field(..., description="Total VRAM (GB)")
    free: float = Field(..., description="Free VRAM (GB)")
