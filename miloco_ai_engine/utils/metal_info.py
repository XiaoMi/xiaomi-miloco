# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Metal (Apple GPU) memory information utility
On Apple Silicon, CPU and GPU share unified memory.
"""

import subprocess
import logging
import psutil

logger = logging.getLogger(__name__)

PROCESS_TIMEOUT = 10


def get_metal_memory_info():
    """
    Get Metal (Apple unified memory) information.
    On Apple Silicon, GPU and CPU share the same memory pool.

    return: (total_memory_gb, free_memory_gb, available) or (None, None, False)
    """
    try:
        # Get total physical memory via sysctl
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=PROCESS_TIMEOUT, check=True
        )
        if result.returncode != 0:
            logger.error("Failed to get total memory: %s", result.stderr)
            return None, None, False

        total_bytes = int(result.stdout.strip())
        total_gb = total_bytes / (1024 ** 3)

        # Get available memory via psutil
        mem = psutil.virtual_memory()
        # available = free + buffers + cached (memory that can be used by GPU)
        free_gb = mem.available / (1024 ** 3)

        return round(total_gb, 2), round(free_gb, 2), True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to get Metal memory info: %s", e)
        return None, None, False
