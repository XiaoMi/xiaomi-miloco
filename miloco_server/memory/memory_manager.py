# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory Manager - Core memory management using Mem0 and ChromaDB.
记忆管理器 - 基于 Mem0 和 ChromaDB 的核心记忆管理
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    chromadb = None
    Settings = None

try:
    from mem0 import Memory as Mem0Memory
    MEM0_AVAILABLE = True
except ImportError:
    MEM0_AVAILABLE = False
    Mem0Memory = None

from miloco_server.schema.memory_schema import (
    Memory as MemoryModel,
    MemoryType,
    MemorySearchResult,
    MemoryContext,
    MemoryStats,
)

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    记忆管理器
    
    使用 Mem0 进行智能记忆管理，ChromaDB 进行向量存储和检索。
    支持：
    - 自动记忆提取和存储
    - 语义检索
    - 记忆去重和更新
    - 记忆失效管理
    """
    
    _instance: Optional["MemoryManager"] = None
    _initialized: bool = False
    
    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(
        self,
        persist_directory: Optional[str] = None,
        collection_name: str = "miloco_memories",
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        """
        初始化记忆管理器
        
        Args:
            persist_directory: ChromaDB 持久化目录
            collection_name: 集合名称
            embedding_model: 嵌入模型名称
        """
        if self._initialized:
            return
            
        self._persist_directory = persist_directory or str(
            Path(__file__).parent.parent / ".temp" / "memory_db"
        )
        self._collection_name = collection_name
        self._embedding_model = embedding_model
        
        # 初始化 ChromaDB
        self._chroma_client: Optional[chromadb.PersistentClient] = None
        self._collection: Optional[chromadb.Collection] = None
        
        # 初始化 Mem0
        self._mem0: Optional[Memory] = None
        
        self._initialized = True
        logger.info("MemoryManager initialized with persist_directory: %s", self._persist_directory)
    
    async def initialize(self) -> bool:
        """
        异步初始化（连接数据库等）
        
        Returns:
            bool: 是否初始化成功
        """
        # 检查依赖
        if not CHROMADB_AVAILABLE:
            logger.error("ChromaDB not installed. Run: pip install chromadb")
            return False
        
        try:
            # 确保目录存在
            Path(self._persist_directory).mkdir(parents=True, exist_ok=True)
            logger.info("Memory persist directory: %s", self._persist_directory)
            
            # 初始化 ChromaDB
            self._chroma_client = chromadb.PersistentClient(
                path=self._persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                )
            )
            logger.info("ChromaDB client initialized")
            
            # 获取或创建集合
            self._collection = self._chroma_client.get_or_create_collection(
                name=self._collection_name,
                metadata={"description": "Miloco smart home memory storage"}
            )
            logger.info("ChromaDB collection '%s' ready", self._collection_name)
            
            # 初始化 Mem0（可选，用于增强功能）
            if MEM0_AVAILABLE:
                try:
                    mem0_config = {
                        "vector_store": {
                            "provider": "chroma",
                            "config": {
                                "collection_name": f"{self._collection_name}_mem0",
                                "path": self._persist_directory,
                            }
                        },
                        "version": "v1.1"
                    }
                    self._mem0 = Mem0Memory.from_config(mem0_config)
                    logger.info("Mem0 initialized successfully")
                except Exception as mem0_error:
                    logger.warning("Mem0 initialization failed (non-critical): %s", mem0_error)
                    self._mem0 = None
            else:
                logger.warning("Mem0 not installed. Some advanced features disabled. Run: pip install mem0ai")
                self._mem0 = None
            
            logger.info("MemoryManager initialized successfully")
            return True
            
        except Exception as e:
            logger.error("Failed to initialize MemoryManager: %s", e, exc_info=True)
            return False
    
    async def add_memory(
        self,
        content: str,
        user_id: str = "default",
        memory_type: MemoryType = MemoryType.CUSTOM,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "auto",
    ) -> Optional[MemoryModel]:
        """
        添加记忆
        
        Args:
            content: 记忆内容
            user_id: 用户ID
            memory_type: 记忆类型
            metadata: 元数据
            source: 来源（auto/manual）
            
        Returns:
            MemoryModel: 创建的记忆对象
        """
        try:
            memory_id = str(uuid.uuid4())
            now = datetime.now()
            
            # 准备元数据
            full_metadata = {
                "user_id": user_id,
                "memory_type": memory_type.value,
                "source": source,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "is_active": True,
                **(metadata or {})
            }
            
            # 存储到 ChromaDB
            self._collection.add(
                ids=[memory_id],
                documents=[content],
                metadatas=[full_metadata]
            )
            
            # 同时使用 Mem0 进行智能存储（用于后续的智能检索和管理）
            if self._mem0:
                try:
                    self._mem0.add(
                        messages=[{"role": "user", "content": content}],
                        user_id=user_id,
                        metadata={"memory_id": memory_id, "type": memory_type.value}
                    )
                except Exception as mem0_error:
                    logger.warning("Mem0 add failed (non-critical): %s", mem0_error)
            
            memory = MemoryModel(
                id=memory_id,
                user_id=user_id,
                content=content,
                memory_type=memory_type,
                metadata=metadata or {},
                created_at=now,
                updated_at=now,
                source=source,
            )
            
            logger.info("Memory added: id=%s, type=%s, content=%s", memory_id, memory_type, content[:50])
            return memory
            
        except Exception as e:
            logger.error("Failed to add memory: %s", e)
            return None
    
    async def search_memories(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 5,
        memory_types: Optional[List[MemoryType]] = None,
        include_inactive: bool = False,
    ) -> List[MemorySearchResult]:
        """
        搜索相关记忆
        
        Args:
            query: 搜索查询
            user_id: 用户ID
            limit: 返回数量
            memory_types: 过滤的记忆类型
            include_inactive: 是否包含无效记忆
            
        Returns:
            List[MemorySearchResult]: 搜索结果列表
        """
        try:
            # 构建过滤条件 (ChromaDB 需要使用 $and 组合多个条件)
            conditions = [{"user_id": {"$eq": user_id}}]
            if not include_inactive:
                conditions.append({"is_active": {"$eq": True}})
            
            # 如果指定了类型过滤
            if memory_types:
                type_values = [t.value for t in memory_types]
                if len(type_values) == 1:
                    conditions.append({"memory_type": {"$eq": type_values[0]}})
                else:
                    conditions.append({"memory_type": {"$in": type_values}})
            
            # 组合条件
            where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
            
            # 执行向量搜索
            results = self._collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )
            
            search_results = []
            if results and results["ids"] and results["ids"][0]:
                for i, memory_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                    distance = results["distances"][0][i] if results["distances"] else 0
                    
                    # 转换距离为相似度分数（距离越小越相似）
                    score = 1.0 / (1.0 + distance)
                    
                    memory = MemoryModel(
                        id=memory_id,
                        user_id=metadata.get("user_id", user_id),
                        content=results["documents"][0][i],
                        memory_type=MemoryType(metadata.get("memory_type", "custom")),
                        metadata={k: v for k, v in metadata.items() 
                                  if k not in ["user_id", "memory_type", "source", "created_at", "updated_at", "is_active"]},
                        source=metadata.get("source", "auto"),
                        is_active=metadata.get("is_active", True),
                    )
                    
                    search_results.append(MemorySearchResult(
                        memory=memory,
                        score=score,
                        distance=distance
                    ))
            
            logger.debug("Memory search: query='%s', found %d results", query[:30], len(search_results))
            return search_results
            
        except Exception as e:
            logger.error("Failed to search memories: %s", e)
            return []
    
    async def get_memory_context(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 5,
    ) -> MemoryContext:
        """
        获取记忆上下文（用于注入Prompt）
        
        Args:
            query: 当前查询/对话内容
            user_id: 用户ID
            limit: 最大记忆数量
            
        Returns:
            MemoryContext: 记忆上下文
        """
        search_results = await self.search_memories(
            query=query,
            user_id=user_id,
            limit=limit,
            include_inactive=False
        )
        
        # 过滤掉相关性太低的结果
        filtered_results = [r for r in search_results if r.score > 0.3]
        
        context = MemoryContext(memories=filtered_results)
        context.context_text = context.to_prompt_text()
        
        return context
    
    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        metadata: Optional[Dict[str, Any]] = None,
        is_active: Optional[bool] = None,
    ) -> bool:
        """
        更新记忆
        
        Args:
            memory_id: 记忆ID
            content: 新内容
            memory_type: 新类型
            metadata: 新元数据
            is_active: 是否有效
            
        Returns:
            bool: 是否更新成功
        """
        try:
            # 获取现有记忆
            existing = self._collection.get(ids=[memory_id], include=["documents", "metadatas"])
            if not existing or not existing["ids"]:
                logger.warning("Memory not found: %s", memory_id)
                return False
            
            # 准备更新数据
            current_metadata = existing["metadatas"][0] if existing["metadatas"] else {}
            current_content = existing["documents"][0] if existing["documents"] else ""
            
            new_content = content if content is not None else current_content
            new_metadata = {**current_metadata}
            
            if memory_type is not None:
                new_metadata["memory_type"] = memory_type.value
            if metadata is not None:
                new_metadata.update(metadata)
            if is_active is not None:
                new_metadata["is_active"] = is_active
            
            new_metadata["updated_at"] = datetime.now().isoformat()
            
            # 更新 ChromaDB
            self._collection.update(
                ids=[memory_id],
                documents=[new_content],
                metadatas=[new_metadata]
            )
            
            logger.info("Memory updated: id=%s", memory_id)
            return True
            
        except Exception as e:
            logger.error("Failed to update memory: %s", e)
            return False
    
    async def delete_memory(self, memory_id: str, soft_delete: bool = True) -> bool:
        """
        删除记忆
        
        Args:
            memory_id: 记忆ID
            soft_delete: 是否软删除（标记为无效）
            
        Returns:
            bool: 是否删除成功
        """
        try:
            if soft_delete:
                return await self.update_memory(memory_id, is_active=False)
            else:
                self._collection.delete(ids=[memory_id])
                logger.info("Memory hard deleted: id=%s", memory_id)
                return True
                
        except Exception as e:
            logger.error("Failed to delete memory: %s", e)
            return False
    
    async def get_all_memories(
        self,
        user_id: str = "default",
        include_inactive: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryModel]:
        """
        获取所有记忆
        
        Args:
            user_id: 用户ID
            include_inactive: 是否包含无效记忆
            limit: 返回数量
            offset: 偏移量
            
        Returns:
            List[MemoryModel]: 记忆列表
        """
        try:
            # 构建过滤条件 (ChromaDB 需要使用 $and 组合多个条件)
            conditions = [{"user_id": {"$eq": user_id}}]
            if not include_inactive:
                conditions.append({"is_active": {"$eq": True}})
            
            where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
            
            results = self._collection.get(
                where=where_filter,
                include=["documents", "metadatas"],
                limit=limit,
                offset=offset
            )
            
            memories = []
            if results and results["ids"]:
                for i, memory_id in enumerate(results["ids"]):
                    metadata = results["metadatas"][i] if results["metadatas"] else {}
                    
                    memory = MemoryModel(
                        id=memory_id,
                        user_id=metadata.get("user_id", user_id),
                        content=results["documents"][i],
                        memory_type=MemoryType(metadata.get("memory_type", "custom")),
                        metadata={k: v for k, v in metadata.items() 
                                  if k not in ["user_id", "memory_type", "source", "created_at", "updated_at", "is_active"]},
                        source=metadata.get("source", "auto"),
                        is_active=metadata.get("is_active", True),
                    )
                    memories.append(memory)
            
            return memories
            
        except Exception as e:
            logger.error("Failed to get all memories: %s", e)
            return []
    
    async def get_stats(self, user_id: str = "default") -> MemoryStats:
        """
        获取记忆统计信息
        
        Args:
            user_id: 用户ID
            
        Returns:
            MemoryStats: 统计信息
        """
        try:
            # 获取所有记忆（包括已删除的）用于总数统计
            all_memories = await self.get_all_memories(user_id=user_id, include_inactive=True, limit=10000)
            # 获取有效记忆用于详细统计
            active_memories = await self.get_all_memories(user_id=user_id, include_inactive=False, limit=10000)
            
            by_type: Dict[str, int] = {}
            by_source: Dict[str, int] = {}
            
            # 只统计有效记忆
            for memory in active_memories:
                # 按类型统计
                type_key = memory.memory_type.value
                by_type[type_key] = by_type.get(type_key, 0) + 1
                
                # 按来源统计
                source_key = memory.source
                by_source[source_key] = by_source.get(source_key, 0) + 1
            
            return MemoryStats(
                total_count=len(active_memories),  # 只统计有效记忆
                by_type=by_type,
                by_source=by_source,
                active_count=len(active_memories)
            )
            
        except Exception as e:
            logger.error("Failed to get memory stats: %s", e)
            return MemoryStats()
    
    async def find_similar_memories(
        self,
        content: str,
        user_id: str = "default",
        threshold: float = 0.8,
    ) -> List[MemorySearchResult]:
        """
        查找相似记忆（用于去重）
        
        Args:
            content: 要查找的内容
            user_id: 用户ID
            threshold: 相似度阈值
            
        Returns:
            List[MemorySearchResult]: 相似的记忆列表
        """
        results = await self.search_memories(
            query=content,
            user_id=user_id,
            limit=5,
            include_inactive=True
        )
        
        return [r for r in results if r.score >= threshold]
    
    async def cleanup(self):
        """清理资源"""
        try:
            if self._chroma_client:
                # ChromaDB PersistentClient 会自动持久化，不需要显式关闭
                pass
            logger.info("MemoryManager cleanup completed")
        except Exception as e:
            logger.error("Failed to cleanup MemoryManager: %s", e)


# 全局实例
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取全局记忆管理器实例"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager


async def initialize_memory_manager() -> bool:
    """初始化全局记忆管理器"""
    manager = get_memory_manager()
    return await manager.initialize()
