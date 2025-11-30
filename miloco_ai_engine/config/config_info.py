# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Model configuration"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any
from enum import Enum

MAX_CUDA_LAYERS = 50

class ModelDevice(Enum):
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"
    UNKNOWN = "unknown"

    @classmethod
    def __missing__(cls, value: Any) -> "ModelDevice":
        return cls.UNKNOWN

class ModelConfigUpdate(BaseModel):
    """Model configuration update"""
    model_config = ConfigDict(protected_namespaces=(), extra="allow")

    device: ModelDevice = Field(description="Device")
    cache_seq_num: int = Field(description="Cache sequence count")
    parallel_seq_num: int = Field(description="Parallel sequence count")
    total_context_num: int = Field(description="Context window size")
    context_per_seq: int = Field(default=-1, description="Maximum available context")
    chunk_size: int = Field(description="Batch size")

class ModelConfig(BaseModel):
    """Single model configuration"""
    model_config = ConfigDict(protected_namespaces=(), extra="allow")

    model_name: str = Field(default="default_name", description="Model name")
    model_path: str = Field(description="Model file path")
    mmproj_path: Optional[str] = Field(default=None, description="MMProject path")

    # Cache settings
    cache_seq_num: int = Field(default=0, description="Cache sequence count")

    # Model parameters
    n_seq_max: int = Field(default=1, description="Maximum sequence count")
    total_context_num: int = Field(default=4096, description="Context window size")
    chunk_size: int = Field(default=1024, description="Batch size")
    n_gpu_layers: int = Field(default=MAX_CUDA_LAYERS, description="GPU layers, 0 for CPU only")

    # Inference parameter defaults
    context_per_seq: int = Field(
        default=4096, description="Maximum available context")
    temperature: float = Field(default=-1, description="Temperature parameter")

    # Business hardcoded configuration
    task_classification: Dict[str, int] = Field(default={}, description="Task classification")


    def __init__(self, model_name: str, **data: Any):
        super().__init__(**data)
        self.model_name = model_name

        self.n_seq_max = data.get("cache_seq_num", 0) + data.get("parallel_seq_num", 6)
        device = data.get("device", "cpu")
        model_device = ModelDevice(device)
        # MPS backend in ggml uses the same n_gpu_layers semantics as CUDA.
        self.n_gpu_layers = MAX_CUDA_LAYERS if model_device in (ModelDevice.CUDA, ModelDevice.MPS) else 0

        business = data.get("business", {})
        task_labels = business.get("task_labels", [])
        task_priorities = business.get("task_priorities", [])
        self.task_classification = {
            label: priority
            for label, priority in zip(task_labels, task_priorities)
        }

    def update(self, config_update: ModelConfigUpdate) -> None:
        """
        Update model configuration
        """
        self.n_gpu_layers = MAX_CUDA_LAYERS if config_update.device in (ModelDevice.CUDA, ModelDevice.MPS) else 0
        self.cache_seq_num = config_update.cache_seq_num
        self.n_seq_max = self.cache_seq_num + config_update.parallel_seq_num

        self.total_context_num = config_update.total_context_num
        self.context_per_seq = config_update.context_per_seq \
            if config_update.context_per_seq > 0 else self.context_per_seq
        self.chunk_size = config_update.chunk_size

    def to_dict(self) -> dict:
        """
        Convert to dictionary for C++ library initialization input
        """
        r = self.model_dump()
        r.pop("task_classification")
        # Remove keys with None values from config dictionary
        dels = []
        for key, value in r.items():
            if value is None:
                dels.append(key)
        for key in dels:
            del r[key]
        return r
