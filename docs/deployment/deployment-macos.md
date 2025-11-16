# macOS Deployment Guide

[Back to Main Documentation](./environment-setup.md)

## Important Notes

⚠️ **This deployment guide is intended for developers with coding experience**

- macOS deployment supports **AI Engine backend only**
- Requires basic command-line and Python development skills
- Requires understanding of model configuration and system resource tuning
- **Does NOT support** `auto_opt_vram` automatic VRAM optimization

## System Requirements

- **Architecture**: Apple Silicon (M1/M2/M3 ARM-based chips)
- **Operating System**: macOS 12.0 or higher
- **Python**: 3.11 or higher
- **GPU Acceleration**: Metal Performance Shaders (MPS)
- **Development Tools**: Xcode Command Line Tools, CMake

## Deployment Steps

### 1. Download Models

Run the model download script:

```bash
bash scripts/download_models.sh
```

By default, the following models will be downloaded to the `models` directory:
- `xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/MiMo-VL-Miloco-7B_Q4_0.gguf`
- `xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/mmproj-MiMo-VL-Miloco-7B_BF16.gguf`
- `Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf`

Optional parameters:
- `--source huggingface` - Download from HuggingFace (default is ModelScope)
- `--target <dir>` - Specify download directory

### 2. Configure AI Engine

Edit [config/ai_engine_config.yaml](../../config/ai_engine_config.yaml):

#### Required Configuration Changes:

**1. Verify Model Paths**

Ensure paths match the downloaded model files:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    model_path: "models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/MiMo-VL-Miloco-7B_Q4_0.gguf"
    mmproj_path: "models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"

  Qwen3-8b:Q4_0:
    model_path: "models/Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf"
```

**2. Set Backend to MPS**

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    device: "mps"  # Must be set to mps

  Qwen3-8b:Q4_0:
    device: "mps"  # Must be set to mps
```

**3. Disable Automatic VRAM Optimization**

⚠️ **This feature is NOT supported on macOS deployment, manual configuration required**:

```yaml
auto_opt_vram: false  # Must remain false
```

#### Performance Tuning Configuration:

Manually adjust based on your Mac configuration:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    cache_seq_num: 5           # Dynamic cache sequence count
    parallel_seq_num: 12       # Parallel sequence count
    total_context_num: 16384   # Total context tokens, affects VRAM 
    context_per_seq: 4096      # Max context per sequence
    chunk_size: 256            # Sequence length
```

### 3. Build Metal Backend

Run the build script:

```bash
bash scripts/ai_engine_metal_build.sh
```

Build process:
- CMake configuration: `-DGGML_METAL=ON`
- Build target: `llama-mico`
- Output directory: `output/`

**Prerequisites**:
- Xcode Command Line Tools: `xcode-select --install`
- CMake: `brew install cmake`

### 4. Set Up Python Environment

#### 4.1 Install Conda

Visit [Miniconda official website](https://docs.conda.io/en/latest/miniconda.html) to download and install the macOS ARM64 version.


#### 4.2 Create and Activate Environment

```bash
# Create Python 3.11 environment
conda create -n miloco python=3.11 -y

# Activate environment
conda activate miloco
```

#### 4.3 Install Dependencies

```bash
cd miloco_ai_engine

# Install in development mode
pip install -e .
```

See dependencies in: [miloco_ai_engine/pyproject.toml](../../miloco_ai_engine/pyproject.toml)

### 5. Start Service

```bash
# Ensure you're in the conda environment
conda activate miloco

# Start from project root directory
python scripts/start_ai_engine.py
```

**Successful startup output**:

```
============================================================
LLaMA-MICO AI Engine HTTP Server
============================================================
Listening on: 0.0.0.0:8001
Available models: 2
============================================================
Model Configuration:
  - MiMo-VL-Miloco-7B:Q4_0: models/...
    Context size: 16384, Input length: 256, Device: mps
  - Qwen3-8b:Q4_0: models/...
    Context size: 12288, Input length: 1024, Device: mps
============================================================
```

## Service Access

- **API Endpoint**: `http://localhost:8001`
- **API Documentation**: `http://localhost:8001/docs`
- **Configure Port**: In [config/ai_engine_config.yaml](../../config/ai_engine_config.yaml) under `server.port`

## Troubleshooting

### Build Failure

Check if development tools are properly installed:
```bash
xcode-select -p  # Should return a path
cmake --version  # Should display version number
```

### Metal GPU Not Being Used

1. Verify configuration: `device: "mps"`
2. Check build logs for `-DGGML_METAL=ON`
3. Verify macOS version ≥ 12.0

### Model Loading Failure

Verify file integrity:
```bash
ls -lh models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/
ls -lh models/Qwen/Qwen3-8B-GGUF/
```

Check path configuration in [config/ai_engine_config.yaml](../../config/ai_engine_config.yaml).

### Memory Overflow

**`auto_opt_vram` is NOT supported**, manually reduce configuration parameters:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    parallel_seq_num: 6        # Reduce from 12 to 6
    total_context_num: 8192    # Reduce from 16384 to 8192
```
