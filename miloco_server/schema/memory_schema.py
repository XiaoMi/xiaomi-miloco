# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory data models for the intelligent memory system.
记忆系统数据模型
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """记忆类型"""
    PREFERENCE = "preference"           # 用户偏好（如：喜欢26度空调）
    FACT = "fact"                       # 事实信息（如：猫叫咪咪）
    HABIT = "habit"                     # 习惯模式（如：每天7点起床）
    DEVICE_SETTING = "device_setting"   # 设备设置偏好
    SCHEDULE = "schedule"               # 时间安排
    RELATIONSHIP = "relationship"       # 关系信息（如：家庭成员）
    CUSTOM = "custom"                   # 自定义记忆


class MemoryAction(str, Enum):
    """记忆操作类型"""
    ADD = "add"         # 添加记忆
    UPDATE = "update"   # 更新记忆
    DELETE = "delete"   # 删除记忆
    QUERY = "query"     # 查询记忆
    NONE = "none"       # 无操作


class Memory(BaseModel):
    """记忆数据模型"""
    id: Optional[str] = Field(None, description="记忆唯一标识")
    user_id: str = Field(default="default", description="用户ID")
    content: str = Field(..., description="记忆内容")
    memory_type: MemoryType = Field(default=MemoryType.CUSTOM, description="记忆类型")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="元数据")
    created_at: Optional[datetime] = Field(default=None, description="创建时间")
    updated_at: Optional[datetime] = Field(default=None, description="更新时间")
    source: str = Field(default="auto", description="来源：auto/manual")
    confidence: float = Field(default=1.0, description="置信度 0-1")
    is_active: bool = Field(default=True, description="是否有效")
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class MemoryExtractionResult(BaseModel):
    """记忆提取结果"""
    should_save: bool = Field(..., description="是否应该保存")
    action: MemoryAction = Field(default=MemoryAction.NONE, description="建议的操作")
    memories: List[Memory] = Field(default_factory=list, description="提取的记忆列表")
    reasoning: str = Field(default="", description="判断理由")
    related_memory_ids: List[str] = Field(default_factory=list, description="相关的已有记忆ID（用于更新/删除）")


class MemorySearchResult(BaseModel):
    """记忆搜索结果"""
    memory: Memory
    score: float = Field(..., description="相关性分数")
    distance: Optional[float] = Field(None, description="向量距离")


class MemoryContext(BaseModel):
    """注入到Prompt的记忆上下文"""
    memories: List[MemorySearchResult] = Field(default_factory=list)
    context_text: str = Field(default="", description="格式化的上下文文本")
    
    def to_prompt_text(self) -> str:
        """转换为可注入Prompt的文本（简洁格式，节省token）"""
        if not self.memories:
            return ""
        
        # 简洁的提示格式
        lines = ["[用户记忆]"]
        for result in self.memories:
            mem = result.memory
            lines.append(f"- {mem.content}")
        
        return "\n".join(lines)


class ManualMemoryCommand(BaseModel):
    """手动记忆管理指令解析结果"""
    action: MemoryAction = Field(..., description="操作类型")
    content: str = Field(default="", description="记忆内容")
    memory_type: MemoryType = Field(default=MemoryType.CUSTOM, description="记忆类型")
    target_description: str = Field(default="", description="目标记忆描述（用于更新/删除）")
    confidence: float = Field(default=1.0, description="解析置信度")


class MemoryStats(BaseModel):
    """记忆统计信息"""
    total_count: int = Field(default=0, description="总记忆数")
    by_type: Dict[str, int] = Field(default_factory=dict, description="按类型统计")
    by_source: Dict[str, int] = Field(default_factory=dict, description="按来源统计")
    active_count: int = Field(default=0, description="有效记忆数")


# API 请求/响应模型
class MemoryAddRequest(BaseModel):
    """添加记忆请求"""
    content: str = Field(..., description="记忆内容")
    memory_type: Optional[MemoryType] = Field(None, description="记忆类型")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元数据")


class MemoryUpdateRequest(BaseModel):
    """更新记忆请求"""
    content: Optional[str] = Field(None, description="新内容")
    memory_type: Optional[MemoryType] = Field(None, description="新类型")
    metadata: Optional[Dict[str, Any]] = Field(None, description="新元数据")
    is_active: Optional[bool] = Field(None, description="是否有效")


class MemorySearchRequest(BaseModel):
    """搜索记忆请求"""
    query: str = Field(..., description="搜索查询")
    limit: int = Field(default=5, description="返回数量")
    memory_types: Optional[List[MemoryType]] = Field(None, description="过滤类型")
    include_inactive: bool = Field(default=False, description="是否包含无效记忆")


class MemoryNaturalLanguageRequest(BaseModel):
    """自然语言记忆管理请求"""
    command: str = Field(..., description="自然语言指令")


class MemoryListResponse(BaseModel):
    """记忆列表响应"""
    memories: List[Memory] = Field(default_factory=list)
    total: int = Field(default=0)
    page: int = Field(default=1)
    page_size: int = Field(default=20)
