# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
LLaMA-MICO AI Engine for MICO Server Startup Script
"""
import sys
import os

def setup_env():
    """Setup environment variables"""
    from miloco_ai_engine.utils.utils import is_wsl, is_linux, is_macos
    if is_macos():
        print("🍎 macOS detected, using Metal GPU backend")
    elif is_linux():
        if not is_wsl():
            os.environ["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] = "1"
        else:
            print("WSL environment detected, \
                  Do not using UNIFIED_MEMORY to prevent automatically utilizing the WSL virtual memory pool.")
    else:
        print("⚠️ Unverified system, using default environment")

def main():
    """Main entry point for LLaMA-MICO AI Engine"""
    setup_env()
    # Print startup information
    from miloco_ai_engine.config import config
    print("=" * 60)
    print("LLaMA-MICO AI Engine HTTP Server")
    print("=" * 60)
    print(
        f"Listening on: {config.SERVER_CONFIG['host']}:{config.SERVER_CONFIG['port']}")
    print(f"Available models: {len(config.MODELS_CONFIG)}")
    print("=" * 60)

    # Print model information
    print("Model Configuration:")
    for model_name, model_config in config.MODELS_CONFIG.items():
        print(f"  - {model_name}: {model_config['model_path']}")
        print(f"    Context size: {model_config['total_context_num']}, "
              f"Input length: {model_config['chunk_size']}, "
              f"Device: {model_config['device']}")
    print("=" * 60)

    # Start server
    from miloco_ai_engine.main import start_server
    try:
        start_server()
    except KeyboardInterrupt:
        print("Server interrupted by user")
    except Exception as e:
        print(f"Failed to start server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
