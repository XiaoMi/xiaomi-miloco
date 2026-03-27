# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
CUDA memory information utility
"""
import subprocess
import logging
from miloco_ai_engine.utils.utils import validate_model_path, get_file_size
from typing import Optional

logger = logging.getLogger(__name__)

PROCESS_TIMEOUT = 10
PROCESS_SUCESS_CODE = 0


def get_cuda_memory_info():
    """
    get CUDA memory information
    return: (total_memory_gb, free_memory_gb, available, best_gpu_index)
            or (None, None, False, 0)
    """
    try:
        result = subprocess.run([
            'nvidia-smi', '--query-gpu=memory.total,memory.free',
            '--format=csv,noheader,nounits'
        ], capture_output=True, text=True, timeout=PROCESS_TIMEOUT, check=True)

        if result.returncode == PROCESS_SUCESS_CODE:
            lines = result.stdout.strip().split('\n')
            best_gpu_index = 0
            max_free_mb = -1.0
            best_total_mb = 0.0

            if lines:
                for idx, line in enumerate(lines):
                    if not line:
                        continue
                    try:
                        total_mb_str, free_mb_str = line.split(', ')
                        curr_total_mb = float(total_mb_str)
                        curr_free_mb = float(free_mb_str)
                        
                        if curr_free_mb > max_free_mb:
                            max_free_mb = curr_free_mb
                            best_total_mb = curr_total_mb
                            best_gpu_index = idx
                    except ValueError:
                        continue
                
                if max_free_mb >= 0:
                    total_gb = best_total_mb / 1024
                    free_gb = max_free_mb / 1024
                    return total_gb, free_gb, True, best_gpu_index
            
            return None, None, False, 0
        else:
            logger.error('get CUDA memory information failed: %s', result.stderr)
            return None, None, False, 0
    except Exception as e: # pylint: disable=broad-exception-caught
        logger.warning('Failed to get CUDA memory info: %s', e)
        return None, None, False, 0


def estimate_vram_usage(model_path: str, mmproj_path: Optional[str], n_ctx: int, n_input: int) -> float:
    try:
        if not validate_model_path(model_path):
            logger.warning('model path is not valid: %s', model_path)
            return -1.0
        model_bytes = get_file_size(model_path) or 0

        mmproj_bytes = 0
        if mmproj_path and validate_model_path(mmproj_path):
            mmproj_bytes = get_file_size(mmproj_path) or 0

        weight_vram_bytes = model_bytes + mmproj_bytes

        safe_ctx = int(max(1, n_ctx or 1))
        safe_input = int(max(1, n_input or 1))
        # 36layes * 1024dim * 2 (key + value) * 2 (float16)
        bytes_per_token_kv = 36 * 1024 * 2 * 2
        kv_cache_bytes = safe_ctx * bytes_per_token_kv

        runtime_overhead_bytes = (safe_input/256) * \
            (safe_ctx/1024) * 5 * (10 ** -6) * 1024 * 36
        total_bytes = weight_vram_bytes + kv_cache_bytes + runtime_overhead_bytes

        total_gb = total_bytes / (1024 ** 3)
        return round(total_gb, 2)
    except Exception as e: # pylint: disable=broad-exception-caught
        logger.warning('estimate vram usage failed for %s: %s', model_path, e)
        return -1.0
