# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
LLaMA-MICO Library Manager
Manages library loading, releasing and memory management
"""

import ctypes
import ctypes.util
import os
import threading
import logging
from typing import Optional
from miloco_ai_engine.middleware.exceptions import CoreNormalException, InvalidArgException
logger = logging.getLogger(__name__)

LLAMA_MICO_LIB_NAME = "llama-mico"  # Library name

class LibraryManager:
    """Library manager - Singleton pattern"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):  # Singleton pattern, avoid duplicate initialization
            return

        self._initialized = True
        self._library = None
        self._function_loaded = False

    def _get_library_path(self):
        """Get library path"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        lib_dir = os.path.join(current_dir, "..", "..", "output", "lib")
        if not os.path.exists(lib_dir):
            logger.error("Library directory not found: %s", lib_dir)
            raise InvalidArgException(f"Library directory not found: {lib_dir}")
        return lib_dir

    def _add_search_path(self):
        """Add necessary search paths to load llama-mico dependencies.

        On Windows, the CUDA_PATH would not search by default, we need to add it
        explicitly.
        """
        if os.name != "nt":
            return
        cuda_path = os.environ.get("CUDA_PATH")
        if not cuda_path or not os.path.exists(cuda_path):
            return
        os.add_dll_directory(os.path.join(cuda_path, "bin/x64"))
        os.add_dll_directory(os.path.join(cuda_path, "bin"))

    def _load_library(self) -> Optional[ctypes.CDLL]:
        """Load library"""
        if self._library is not None:
            return self._library

        # Get library path
        self._add_search_path()
        lib_dir = self._get_library_path()
        # Library name list
        library_names = [
            f"lib{LLAMA_MICO_LIB_NAME}.so",  # Linux
            f"lib{LLAMA_MICO_LIB_NAME}.dylib",  # macOS
            f"{LLAMA_MICO_LIB_NAME}.dll",  # Windows
            LLAMA_MICO_LIB_NAME,  # Generic name
        ]

        # Try to load library from specific directory first
        for lib_name in library_names:
            lib_path = os.path.join(lib_dir, lib_name)
            if not os.path.exists(lib_path):
                logger.warning("Library file not found: %s", lib_path)
                continue
            logger.info("Attempting to load library from: %s", lib_path)
            try:
                # Load library using RTLD_GLOBAL mode with full path
                self._library = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
                logger.info("LLaMA-MICO library loaded successfully from: %s", lib_path)
                return self._library
            except Exception as e: # pylint: disable=broad-exception-caught
                logger.warning("Cannot load library from %s: %s", lib_path, e)
                continue

        # Try to load from system path
        for lib_name in library_names:
            logger.info("Attempting to load library from system path: %s", lib_name)
            try:
                self._library = ctypes.CDLL(ctypes.util.find_library(lib_name))
                logger.info("LLaMA-MICO library loaded from system path")
                return self._library
            except Exception as e: # pylint: disable=broad-exception-caught
                logger.warning("Cannot load library from system path %s: %s", lib_name, e)
                continue

        logger.error("Cannot find LLaMA-MICO dynamic library")
        raise InvalidArgException("Cannot find LLaMA-MICO dynamic library")

    def _setup_function_signatures(self):
        """Setup function signatures"""
        if self._library is None:
            logger.error("Library not loaded")
            raise InvalidArgException("Library not loaded")

        try:
            # Initialize function
            self._library.llama_mico_init.restype = ctypes.c_int32
            self._library.llama_mico_init.argtypes = [
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_void_p)
            ]

            # Free function
            self._library.llama_mico_free.restype = ctypes.c_int32
            self._library.llama_mico_free.argtypes = [ctypes.c_void_p]

            # Prompt request function
            self._library.llama_mico_request_prompt.restype = ctypes.c_int32
            self._library.llama_mico_request_prompt.argtypes = [
                ctypes.c_void_p,  # handle
                ctypes.c_char_p,  # request_json_str
                ctypes.POINTER(ctypes.c_int32),  # is_finished
                ctypes.POINTER(ctypes.c_char_p)  # content
            ]

            # Generate request function
            self._library.llama_mico_request_generate.restype = ctypes.c_int32
            self._library.llama_mico_request_generate.argtypes = [
                ctypes.c_void_p,  # handle
                ctypes.c_char_p,  # request_json_str
                ctypes.POINTER(ctypes.c_int32),  # is_finished
                ctypes.POINTER(ctypes.c_char_p)  # content
            ]

            logger.info("Function signatures setup successfully")
            self._function_loaded = True
            return True

        except Exception as e:
            logger.error("Failed to setup function signatures: %s", e)
            raise CoreNormalException(f"Failed to setup function signatures: {e}") from e

    def get_library(self) -> Optional[ctypes.CDLL]:
        """Get library instance"""
        if self._library is None:
            self._load_library()
        if self._library and not self._function_loaded:
            self._setup_function_signatures()
        return self._library

# Global library manager instance
lib_manager = LibraryManager()


def get_library() -> Optional[ctypes.CDLL]:
    """Convenience function to get library instance"""
    lib = lib_manager.get_library()
    if not lib:
        logger.error("LLaMA-MICO library not loaded")
        raise CoreNormalException("LLaMA-MICO library not loaded")
    return lib
