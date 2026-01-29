# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Configuration optimizer module
Dynamically adjusts model configuration based on system resources in loading time
"""

from enum import Enum
from miloco_ai_engine.config.config_info import ModelConfig, ModelConfigUpdate, ModelDevice
from miloco_ai_engine.utils.cuda_info import get_cuda_memory_info
from miloco_ai_engine.config.config import AUTO_OPT_VRAM
import logging
logger = logging.getLogger(__name__)

LOW_MEMORY_THRESHOLD = 0.5  # Low VRAM threshold (GB)
SMALL_MEMORY_THRESHOLD = 8  # Small VRAM threshold (GB)
MEDIUM_MEMORY_THRESHOLD = 12  # Medium VRAM threshold (GB)
LARGE_MEMORY_THRESHOLD = 16  # Large VRAM threshold (GB)

PROCESS_TIMEOUT = 10
PROCESS_SUCESS_CODE = 0

DEFAULT_SUPPORT_MIMO_VL_NAME = "MiMo-VL-Miloco-7B:Q4_0"
DEFAULT_SUPPORT_QWEN3_NAME = "Qwen3-8b:Q4_0"

DEFAULT_NO_GPU_MIMO_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CPU,
    cache_seq_num=3,
    parallel_seq_num=8,
    total_context_num=8192,
    chunk_size=256
)
LOW_MODE_MIMO_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CUDA,
    cache_seq_num=3,
    parallel_seq_num=8,
    total_context_num=8192,
    chunk_size=256
)
SMALL_MODE_MIMO_CONFIG_UPDATE = LOW_MODE_MIMO_CONFIG_UPDATE
MEDIUM_MODE_MIMO_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CUDA,
    cache_seq_num=5,
    parallel_seq_num=12,
    total_context_num=16384,
    chunk_size=256
)
FULL_MODE_MIMO_CONFIG_UPDATE = MEDIUM_MODE_MIMO_CONFIG_UPDATE

DEFAULT_NO_GPU_QWEN3_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CPU,
    cache_seq_num=0,
    parallel_seq_num=2,
    total_context_num=12288,
    chunk_size=1024
)
LOW_MODE_QWEN3_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CUDA,
    cache_seq_num=0,
    parallel_seq_num=2,
    total_context_num=12288,
    chunk_size=1024
)
SMALL_MODE_QWEN3_CONFIG_UPDATE = LOW_MODE_QWEN3_CONFIG_UPDATE
MEDIUM_MODE_QWEN3_CONFIG_UPDATE = SMALL_MODE_QWEN3_CONFIG_UPDATE
FULL_MODE_QWEN3_CONFIG_UPDATE = ModelConfigUpdate(
    device=ModelDevice.CUDA,
    cache_seq_num=0,
    parallel_seq_num=3,
    total_context_num=24576,
    context_per_seq=8192,
    chunk_size=1024
)

class FreeMemoryLevel(Enum):
    """Memory mode enumeration"""
    LEVEL_0 = "Unavailable VRAM"  # <0.5G: No GPU or severely insufficient VRAM
    LEVEL_1 = "Insufficient"      # 0.5-8GB: Insufficient VRAM
    LEVEL_2 = "Tight"             # 8-12GB: Tight VRAM
    LEVEL_3 = "Moderate"          # 12-16GB: Moderate VRAM
    LEVEL_4 = "Sufficient"        # >16GB: Sufficient VRAM

    @classmethod
    def detect_memory_mode(cls, free_memory: float, cuda_available: bool) -> "FreeMemoryLevel":
        """
        Detect memory mode based on CUDA VRAM status
        """
        if not cuda_available:
            return cls.LEVEL_0
        elif free_memory < LOW_MEMORY_THRESHOLD:
            return cls.LEVEL_0
        elif free_memory < SMALL_MEMORY_THRESHOLD:
            return cls.LEVEL_1
        elif free_memory < MEDIUM_MEMORY_THRESHOLD:
            return cls.LEVEL_2
        elif free_memory < LARGE_MEMORY_THRESHOLD:
            return cls.LEVEL_3
        else:
            return cls.LEVEL_4

DEFUALT_MODEL_CONFIG_UPDATE_MAP = {
    DEFAULT_SUPPORT_MIMO_VL_NAME: {
        FreeMemoryLevel.LEVEL_0: DEFAULT_NO_GPU_MIMO_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_1: LOW_MODE_MIMO_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_2: SMALL_MODE_MIMO_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_3: MEDIUM_MODE_MIMO_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_4: FULL_MODE_MIMO_CONFIG_UPDATE
    },
    DEFAULT_SUPPORT_QWEN3_NAME: {
        FreeMemoryLevel.LEVEL_0: DEFAULT_NO_GPU_QWEN3_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_1: LOW_MODE_QWEN3_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_2: SMALL_MODE_QWEN3_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_3: MEDIUM_MODE_QWEN3_CONFIG_UPDATE,
        FreeMemoryLevel.LEVEL_4: FULL_MODE_QWEN3_CONFIG_UPDATE
    }
}

def apply_memory_optimization_to_default_model(config: ModelConfig, memory_mode: FreeMemoryLevel) -> ModelConfig:
    """
    Optimize default model configuration based on memory mode
    memory_mode: MemoryMode enum value
    Returns: optimized model config
    """
    if not config or not AUTO_OPT_VRAM:
        return config

    if memory_mode == FreeMemoryLevel.LEVEL_0:
        logger.warning("No GPU detected, model layers attemp load to CPU")
    if memory_mode == FreeMemoryLevel.LEVEL_1:
        logger.warning("Low VRAM mode detected, Some model layers maybe offload to CPU")

    update = DEFUALT_MODEL_CONFIG_UPDATE_MAP.get(config.model_name, {}).get(memory_mode)
    if not update:
        logger.error(
                "Unsupported optimization model name: %s, please disable AUTO_OPT_VRAM", config.model_name)
        return config

    config.update(update)
    logger.info("Model %s optimized to %s", config.model_name, str(update))
    return config


def adjust_config_by_memory(model_config: ModelConfig) -> ModelConfig:
    """
    Adjust default model configuration based on CUDA VRAM status
    """
    device = ModelDevice(model_config.model_dump().get("device", ModelDevice.CPU.value))
    if device == ModelDevice.MPS:
        logger.info("MPS device configured; skip CUDA-based VRAM auto optimization")
        return model_config

    _, free_memory, cuda_available = get_cuda_memory_info()

    memory_mode = FreeMemoryLevel.detect_memory_mode(free_memory, cuda_available)
    return apply_memory_optimization_to_default_model(model_config, memory_mode)
