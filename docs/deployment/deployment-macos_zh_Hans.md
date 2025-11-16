# macOS 环境部署指南

[返回主文档](./environment-setup_zh-Hans.md)

## 重要说明

⚠️ **本部署指南面向具有一定代码能力的开发者**

- macOS 端仅支持 **AI Engine 后端**部署
- 需要具备基础的命令行操作和 Python 开发经验
- 需要理解模型配置和系统资源调优
- **不支持** `auto_opt_vram` 自动显存优化功能

## 系统要求

- **架构**: Apple Silicon (M1/M2/M3等 ARM 架构)
- **操作系统**: macOS 12.0 或更高版本
- **Python**: 3.11 或更高版本
- **GPU 加速**: Metal Performance Shaders (MPS)
- **开发工具**: Xcode Command Line Tools, CMake

## 部署步骤

### 1. 下载模型

执行模型下载脚本:

```bash
bash scripts/download_models.sh
```

默认下载以下模型到 `models` 目录:
- `xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/MiMo-VL-Miloco-7B_Q4_0.gguf`
- `xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/mmproj-MiMo-VL-Miloco-7B_BF16.gguf`
- `Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf`

可选参数:
- `--source huggingface` - 从 HuggingFace 下载(默认 ModelScope)
- `--target <dir>` - 指定下载目录

### 2. 配置 AI Engine

编辑 [config/ai_engine_config.yaml](../config/ai_engine_config.yaml):

#### 必须修改的配置:

**1. 验证模型路径**

确保路径与下载的模型文件一致:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    model_path: "models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/MiMo-VL-Miloco-7B_Q4_0.gguf"
    mmproj_path: "models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"

  Qwen3-8b:Q4_0:
    model_path: "models/Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf"
```

**2. 设置 backend 为 mps**

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    device: "mps"  # 必须设置为 mps

  Qwen3-8b:Q4_0:
    device: "mps"  # 必须设置为 mps
```

**3. 禁用自动显存优化**

⚠️ **macOS 部署不支持此功能,必须手动配置**:

```yaml
auto_opt_vram: false  # 必须保持 false
```

#### 性能调优配置:

根据您的 Mac 配置手动调整:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    cache_seq_num: 5           # 动态缓存序列数
    parallel_seq_num: 12       # 并行序列数
    total_context_num: 16384   # 总上下文令牌数,影响显存
    context_per_seq: 4096      # 每序列最大上下文
    chunk_size: 256            # 序列长度
```

### 3. 编译 Metal 后端

执行编译脚本:

```bash
bash scripts/ai_engine_metal_build.sh
```

编译过程:
- CMake 配置: `-DGGML_METAL=ON`
- 构建目标: `llama-mico`
- 输出目录: `output/`

**前置要求**:
- Xcode Command Line Tools: `xcode-select --install`
- CMake: `brew install cmake`

### 4. 配置 Python 环境

#### 4.1 安装 Conda

请访问 [Miniconda 官网](https://docs.conda.io/en/latest/miniconda.html) 选择适合 macOS ARM64 的版本进行安装。


#### 4.2 创建并激活环境

```bash
# 创建 Python 3.11 环境
conda create -n miloco python=3.11 -y

# 激活环境
conda activate miloco
```

#### 4.3 安装依赖

```bash
cd miloco_ai_engine

# 开发模式安装
pip install -e .
```

依赖项参见: [miloco_ai_engine/pyproject.toml](../miloco_ai_engine/pyproject.toml)

### 5. 启动服务

```bash
# 确保在 conda 环境中
conda activate miloco

# 从项目根目录启动
python scripts/start_ai_engine.py
```

**启动成功输出**:

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

## 服务访问

- **API 地址**: `http://localhost:8001`
- **API 文档**: `http://localhost:8001/docs`
- **配置端口**: [config/ai_engine_config.yaml](../config/ai_engine_config.yaml) 中的 `server.port`

## 常见问题排查

### 编译失败

检查开发工具是否完整安装:
```bash
xcode-select -p  # 应返回路径
cmake --version  # 应显示版本号
```

### Metal GPU 未被使用

1. 确认配置: `device: "mps"`
2. 检查编译日志是否包含 `-DGGML_METAL=ON`
3. 验证 macOS 版本 ≥ 12.0

### 模型加载失败

验证文件完整性:
```bash
ls -lh models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/
ls -lh models/Qwen/Qwen3-8B-GGUF/
```

检查 [config/ai_engine_config.yaml](../config/ai_engine_config.yaml) 路径配置。

### 内存溢出

**不支持 `auto_opt_vram`**,需手动降低配置参数:

```yaml
models:
  MiMo-VL-Miloco-7B:Q4_0:
    parallel_seq_num: 6        # 从 12 降至 6
    total_context_num: 8192    # 从 16384 降至 8192
```

