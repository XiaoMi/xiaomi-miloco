# Runtime and Development Environment Setup

[English](./environment-setup.md) | [简体中文](./environment-setup_zh-Hans.md)

Runtime Environment Requirements:
- System Requirements:
  - **Linux**: x64 architecture, Ubuntu 22.04 LTS or higher is recommended
  - **Windows**: x64 architecture, Windows 11 22H2 or higher is recommended; WSL2 support is required
  - **macOS**: ARM architecture, currently not supported
- GPU Requirements (The project's AI Engine requires GPU support)
  - **NVIDIA**: GeForce RTX 30 series or newer recommended, with at least 8 GB VRAM
  - **AMD**: Currently not supported
  - **Intel**: Currently not supported
  - **MThreads**: Currently not supported
- Software Requirements:
  - **Docker**: Version 20.10 or higher, with `docker compose` support required

## Environment Setup

> NOTICE:
>
> - If running via Docker, follow the steps below to install the environment. If the environment is already installed and passes verification, you may skip the setup steps; otherwise, the program may fail to run.
> - The camera only allows streaming within the local area network. On Windows, you need to set the WSL2 network mode to **Mirrored**.
> - After setting the WSL2 network to **Mirrored** mode, make sure to configure the Hyper-V firewall to allow inbound connections. Refresh the camera list; if it still shows as offline, you can try disabling the Windows firewall.

### Linux


### Windows


### macOS (M Series & Intel Series)


## Model Downloads
All steps below are to be performed in the `models` folder.
### Xiaomi MiMo-VL-Miloco-7B
Xiaomi’s self-developed multimodal model for local image inference.  
Download links:
- `huggingface`:
- - Quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B
- `modelscope`:
- - Quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B
In the `models` folder, create a new directory named `MiMo-VL-Miloco-7B`, then open the `modelspace` quantized model download link:
- Download `MiMo-VL-Miloco-7B_Q4_0.gguf` and place it into the `MiMo-VL-Miloco-7B` directory
- Download `mmproj-MiMo-VL-Miloco-7B_BF16.gguf` and place it into the `MiMo-VL-Miloco-7B` directory
### Qwen3-8B
If your GPU memory is sufficient, you can also download a local planning model, such as `Qwen-8B`. You can modify the config file to use other models as well.  
Download links:
- `huggingface`: https://huggingface.co/Qwen/Qwen3-8B
- `modelscope`: https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/files
Create a folder `Qwen3-8B` under `models`, open the above links, and download the Q4 quantized version:
- Download `Qwen3-8B-Q4_K_M.gguf` into the `Qwen3-8B` folder
## Run
Use `docker compose` to run the program. Copy `.env.example` to `.env`, modify ports as needed.  
Run the program:
```shell
# Pull images
docker compose pull
# Stop and remove containers
docker compose down
# Start
docker compose up -d
```
## Access Service
Access via `https://<your ip>:8000`.  
If accessing on localhost, use IP `127.0.0.1`.
> NOTICE:
>
> - Use **https** instead of **http**
> - Under WSL2, you can try directly accessing the WSL IP from Windows, e.g. `https://<wsl ip>:8000`
